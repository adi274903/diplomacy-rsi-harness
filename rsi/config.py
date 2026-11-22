"""Typed run configuration loaded from YAML (see configs/)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    backend: Literal["http", "mock"] = "http"
    # OpenAI-compatible base URL INCLUDING /v1. Normally rsi-gateway (which fronts the provider API and
    # adds caching, budget, retries); a provider URL also works directly.
    base_url: str = "http://127.0.0.1:9000/v1"
    model: str = "inkling"
    api_key_env: str = "RSI_API_KEY"
    timeout_s: float = 900.0
    max_connections: int = 256
    # How reasoning effort is passed: top-level `reasoning_effort`, `chat_template_kwargs`, both, or none.
    # Providers differ; check yours and set this so unknown fields are not rejected.
    effort_mode: Literal["top", "kwargs", "both", "none"] = "both"
    # json_schema = provider-side constrained decoding; json_object / none = parse + validate (+1 repair) only.
    json_mode: Literal["json_schema", "json_object", "none"] = "json_schema"
    run_name: str = "default"
    max_tokens: dict[str, int] = Field(default_factory=lambda: {
        "send": 1536, "judge": 1536, "orders": 1536, "review": 6144, "item_judge": 1024, "text": 512,
    })


class GameConfig(BaseModel):
    press_rounds: int = 2
    max_messages_per_round: int = 2
    end_year: int = 1906
    history_window: int = 12          # messages per dialog shown to a player
    memory_window: int = 6            # own memory notes shown to a player
    max_order_options: int = 40       # per location, keeps prompts bounded mid-game
    strict_isolation: bool = True     # raise on any detected leak instead of logging it
    render_maps: bool = True          # save an SVG board (with orders) per phase for the viewer


class LoopConfig(BaseModel):
    generations: int = 4
    games_per_generation: int = 12
    parallel_games: int = 12
    gate_games: int = 6
    candidates: int = 3
    max_diffs_per_candidate: int = 3
    patience: int = 2
    bootstrap_n: int = 1000
    p_gain_min: float = 0.80
    ece_tolerance: float = 0.02
    human_val_tolerance: float = 0.01
    human_val_truth_ratio: int = 4     # truths sampled per lie in the human val gate set
    sim_gate_truth_ratio: int = 4
    frozen_share: float = 0.5          # share of seats given to frozen league members
    label_mode: Literal["intent", "promise", "either"] = "intent"
    seed: int = 0


class Paths(BaseModel):
    runs: str = "runs"
    data: str = "data"


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    reviewer_llm: LLMConfig | None = None    # defaults to `llm` with a separate prompt/seed
    reviewer_effort: str = "high"
    game: GameConfig = Field(default_factory=GameConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    paths: Paths = Field(default_factory=Paths)

    @property
    def reviewer(self) -> LLMConfig:
        return self.reviewer_llm or self.llm


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Config.model_validate(data)
