"""Structured outputs. Each model's JSON schema is sent as `response_format` so the engine's grammar
backend constrains decoding; responses are validated again here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["truthful", "deceptive"]
Decision = Literal["trust", "distrust"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- player turns ----------------------------------------------------------------------------

class OutMessage(_Strict):
    to: str = Field(description="Recipient power, e.g. FRANCE")
    text: str = Field(description="The message the recipient will read")
    intent: Intent = Field(description="PRIVATE honest label: is this message meant to deceive the recipient?")


class SendTurn(_Strict):
    analysis: str = Field(description="PRIVATE: brief situation analysis (never shown to others)")
    plan: list[str] = Field(description="PRIVATE: the orders you currently intend to submit this phase")
    messages: list[OutMessage] = Field(max_length=4)
    memory: str = Field(description="PRIVATE: one note to remember for later phases")


class Judgment(_Strict):
    msg_id: int
    reasoning: str = Field(description="Why you trust or distrust this message, citing evidence")
    cues: list[str] = Field(description="Short labels for the deception/honesty cues you relied on")
    p_lie: float = Field(ge=0.0, le=1.0, description="Probability the sender intends to deceive you")
    decision: Decision


class JudgeTurn(_Strict):
    judgments: list[Judgment]


class OrdersTurn(_Strict):
    reasoning: str
    orders: list[str] = Field(description="Orders copied exactly from the legal-order lists")
    memory: str


# ---- single-item judging (gate replays, human data) -----------------------------------------

class ItemJudgment(_Strict):
    reasoning: str
    cues: list[str]
    p_lie: float = Field(ge=0.0, le=1.0)
    decision: Decision


# ---- reviewer --------------------------------------------------------------------------------

class ReceiverGrade(_Strict):
    msg_id: int
    promise: Literal["kept", "broken", "no_promise", "unclear"] = Field(
        description="Did the sender's actual orders keep the commitments made in this message?")
    reasoning_quality: int = Field(ge=1, le=5, description="Receiver's reasoning: evidence use, no invented facts, calibration")
    leak_flag: bool = Field(description="Receiver's reasoning cites information it could not have seen")
    note: str


class PlaybookDiff(_Strict):
    op: Literal["add", "edit", "delete"]
    heuristic_id: str | None = Field(default=None, description="Target id for edit/delete, e.g. H3")
    text: str | None = Field(default=None, description="New heuristic text for add/edit; general, no game specifics")
    rationale: str
    evidence_msg_ids: list[int]


class GameReview(_Strict):
    grades: list[ReceiverGrade]
    diffs: list[PlaybookDiff] = Field(max_length=3)
    summary: str
