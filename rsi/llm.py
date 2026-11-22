"""Async LLM client.

`HttpLLM` speaks the OpenAI chat-completions protocol to rsi-gateway (which fronts vLLM, SGLang or
llama.cpp). `MockLLM` returns schema-valid, seed-deterministic outputs so the whole harness runs
on a laptop with no model at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from rsi.config import LLMConfig

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class CallMeta:
    reasoning: str = ""          # native thinking trace (reasoning_content), if the engine returns it
    raw: str = ""
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    cached: bool = False
    repaired: bool = False


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> Any:
    text = _FENCE.sub("", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Tolerate prose around a JSON object (json_mode=none backends).
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class HttpLLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        headers = {"x-rsi-run": cfg.run_name}
        key = os.environ.get(cfg.api_key_env)
        if key:
            headers["authorization"] = f"Bearer {key}"
        self.client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(cfg.timeout_s, connect=10.0),
            limits=httpx.Limits(max_connections=cfg.max_connections,
                                max_keepalive_connections=cfg.max_connections),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _body(self, messages, role, seed, effort, temperature, schema: type[BaseModel] | None):
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": self.cfg.max_tokens.get(role, 1024),
            "temperature": temperature,
            "seed": seed,
        }
        if effort and self.cfg.effort_mode in ("top", "both"):
            body["reasoning_effort"] = effort
        if effort and self.cfg.effort_mode in ("kwargs", "both"):
            body["chat_template_kwargs"] = {"reasoning_effort": effort}
        if schema is not None and self.cfg.json_mode == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema(), "strict": True}}
        elif schema is not None and self.cfg.json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, body, priority) -> tuple[dict, CallMeta]:
        t0 = time.perf_counter()
        r = await self.client.post("/chat/completions", json=body, headers={"x-rsi-priority": priority})
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        meta = CallMeta(
            reasoning=msg.get("reasoning_content") or msg.get("reasoning") or "",
            raw=msg.get("content") or "",
            usage=data.get("usage") or {},
            latency_s=time.perf_counter() - t0,
            cached=r.headers.get("x-rsi-cache") == "hit",
        )
        if data["choices"][0].get("finish_reason") == "length":
            meta.raw += "\n<<truncated: max_tokens reached>>"
        return data, meta

    async def complete_json(self, *, messages: list[dict], schema: type[T], role: str, seed: int,
                            effort: str = "low", priority: str = "normal", temperature: float = 1.0,
                            mock_ctx: dict | None = None) -> tuple[T, CallMeta]:
        body = self._body(messages, role, seed, effort, temperature, schema)
        _, meta = await self._post(body, priority)
        try:
            return schema.model_validate(_extract_json(meta.raw)), meta
        except (ValueError, ValidationError) as err:
            # One repair attempt: show the model its invalid output and the validation error.
            repair = messages + [
                {"role": "assistant", "content": meta.raw[:4000]},
                {"role": "user", "content": f"That reply was invalid ({str(err)[:400]}). "
                                            f"Reply again with only a JSON object matching the schema."},
            ]
            body = self._body(repair, role, seed + 7919, effort, temperature, schema)
            _, meta2 = await self._post(body, priority)
            meta2.repaired = True
            try:
                return schema.model_validate(_extract_json(meta2.raw)), meta2
            except (ValueError, ValidationError) as err2:
                raise LLMError(f"{schema.__name__}: invalid output after repair: {err2}") from err2

    async def complete_text(self, *, messages: list[dict], role: str = "text", seed: int = 0,
                            effort: str = "minimal", priority: str = "low",
                            temperature: float = 0.0) -> tuple[str, CallMeta]:
        body = self._body(messages, role, seed, effort, temperature, None)
        _, meta = await self._post(body, priority)
        return meta.raw, meta


# ---- mock backend ------------------------------------------------------------------------------

MockPolicy = Callable[[random.Random, dict], dict]
MOCK_POLICIES: dict[str, MockPolicy] = {}


def register_mock(schema: type[BaseModel]):
    """Agents register a mock policy per output schema; `mock_ctx` carries what a fake player
    needs (legal orders, recipients, message ids) without the mock having to parse prompts."""

    def deco(fn: MockPolicy) -> MockPolicy:
        MOCK_POLICIES[schema.__name__] = fn
        return fn

    return deco


class MockLLM:
    def __init__(self, cfg: LLMConfig | None = None, latency_s: float = 0.0):
        self.cfg = cfg
        self.latency_s = latency_s
        self.calls = 0
        self.prompts: list[list[dict]] = []   # kept for isolation tests

    async def aclose(self) -> None:
        return None

    async def complete_json(self, *, messages, schema: type[T], role, seed, effort="low", priority="normal",
                            temperature=1.0, mock_ctx=None) -> tuple[T, CallMeta]:
        self.calls += 1
        self.prompts.append(messages)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        policy = MOCK_POLICIES.get(schema.__name__)
        if policy is None:
            raise LLMError(f"no mock policy registered for {schema.__name__}")
        rng = random.Random(f"{seed}:{schema.__name__}")
        out = schema.model_validate(policy(rng, mock_ctx or {}))
        return out, CallMeta(reasoning=f"[mock reasoning seed={seed}]", raw=out.model_dump_json())

    async def complete_text(self, *, messages, role="text", seed=0, effort="minimal", priority="low",
                            temperature=0.0) -> tuple[str, CallMeta]:
        self.calls += 1
        text = "mock continuation " + str(seed)
        return text, CallMeta(raw=text)


def make_llm(cfg: LLMConfig):
    return MockLLM(cfg) if cfg.backend == "mock" else HttpLLM(cfg)
