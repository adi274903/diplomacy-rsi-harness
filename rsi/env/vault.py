"""Private vault + information-isolation guard.

The vault holds everything a player must never see about another player: intent labels, plans
committed at send time, analysis, memory notes and judging traces. Only the reviewer reads it.

Isolation is enforced twice for every player prompt:
1. structurally: every message shown to power P must have P as sender or recipient;
2. by content: the prompt, with P's own legitimately-shown messages removed, is scanned (Aho-Corasick,
   Rust) for any secret registered by another power and for bilateral messages between other pairs.
Message texts are removed before scanning because quoting a received message to a third party is
legitimate gossip, not a leak.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rsi.core import LeakGuard
from rsi.env.game import POWERS
from rsi.prompts import PlayerView


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


class IsolationError(RuntimeError):
    pass


@dataclass
class SendRecord:
    msg_id: int
    intent: str
    plan: list[str]
    analysis: str


@dataclass
class JudgeRecord:
    msg_id: int
    receiver: str
    p_lie: float
    decision: str
    reasoning: str
    cues: list[str]
    native_reasoning: str = ""
    ok: bool = True


@dataclass
class Vault:
    sends: dict[int, SendRecord] = field(default_factory=dict)
    judgments: dict[int, JudgeRecord] = field(default_factory=dict)
    orders: dict[str, dict[str, list[str]]] = field(default_factory=dict)        # phase -> power -> orders
    memory: dict[str, list[str]] = field(default_factory=lambda: {p: [] for p in POWERS})
    traces: list[dict] = field(default_factory=list)                              # every private text, tagged
    leak_hits: list[dict] = field(default_factory=list)
    min_secret_len: int = 24
    powers: list[str] = field(default_factory=lambda: list(POWERS))

    def __post_init__(self) -> None:
        self._guards = {p: LeakGuard(self.min_secret_len) for p in self.powers}
        for p in self.powers:
            self.memory.setdefault(p, [])

    # -- registration -----------------------------------------------------------------------------
    def secret(self, owner: str, text: str, tag: str) -> None:
        """Register private text of `owner`; every other power's guard learns it, sentence by
        sentence, so a partial leak of a long trace is still caught."""
        if not text:
            return
        self.traces.append({"owner": owner, "tag": tag, "text": text})
        pieces = [s.strip() for s in _SENTENCE.split(text)]
        pieces = [s for s in pieces if len(s) >= self.min_secret_len] or [text]
        for p in self.powers:
            if p != owner:
                for s in pieces:
                    self._guards[p].add(s, f"{owner}:{tag}")

    def bilateral(self, sender: str, recipient: str, text: str, msg_id: int) -> None:
        """A message is secret from everyone except its two parties."""
        for p in self.powers:
            if p not in (sender, recipient):
                self._guards[p].add(text, f"msg:{msg_id}:{sender}->{recipient}")

    # -- checks -------------------------------------------------------------------------------------
    def check(self, power: str, view: PlayerView, prompt_text: str, strict: bool, where: str) -> None:
        """`prompt_text` should be the private part of the prompt (private block + task). The public
        board is excluded by construction: everything in it is public, so it cannot leak."""
        for m in view.shown_messages():
            if power not in (m.sender, m.recipient):
                self._fail(strict, {"power": power, "where": where, "kind": "structural", "msg": m.id})
        scrubbed = prompt_text
        for m in view.shown_messages():
            scrubbed = scrubbed.replace(m.text, " ")
        for tag in self._guards[power].scan(scrubbed):
            self._fail(strict, {"power": power, "where": where, "kind": "content", "secret": tag})

    def _fail(self, strict: bool, hit: dict) -> None:
        self.leak_hits.append(hit)
        if strict:
            raise IsolationError(f"information leak: {hit}")
