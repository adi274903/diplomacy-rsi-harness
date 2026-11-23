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


# ---- structured judgments: the receiver's decision graph (opt-in via Genome.structured_judgments) ----
# See rsi/decision_graph.py for the checks and replay that run on these.

ClaimType = Literal[
    "promise_of_support", "non_aggression", "statement_of_intent", "claim_about_own_past_action",
    "claim_about_third_party", "information_or_intelligence", "request_or_proposal", "apology_or_excuse",
    "relationship_or_flattery", "no_substantive_claim",
]


class Claim(_Strict):
    id: str = Field(description="c1, c2, ...")
    text: str = Field(description="Short paraphrase of one claim or commitment in the message")
    type: ClaimType
    checkable: bool = Field(description="Could it later be checked against orders or other messages?")


class Evidence(_Strict):
    id: str = Field(description="e1, e2, ...")
    source: str = Field(description="Where it comes from, exactly as shown: #<id> for an earlier message, "
                                    "h<n> for a numbered history line, 'board', 'message' or 'scores'")
    claim: str = Field(description="The claim id this evidence bears on")
    relation: Literal["supports", "contradicts", "background"]
    note: str


class Adjustment(_Strict):
    basis: str = Field(description="An evidence id, a claim id, or 'base_rate_context'")
    direction: Literal["toward_lie", "toward_truth"]
    strength: Literal["weak", "moderate", "strong"]
    note: str


class _GraphFields(_Strict):
    claims: list[Claim]
    evidence: list[Evidence]
    adjustments: list[Adjustment]


class GraphJudgment(_GraphFields):
    msg_id: int
    reasoning: str = Field(description="One or two sentences summarising the decision")
    cues: list[str] = Field(description="Short labels for the deception/honesty cues you relied on")
    p_lie: float = Field(ge=0.0, le=1.0, description="Probability the sender intends to deceive you")
    decision: Decision


class GraphJudgeTurn(_Strict):
    judgments: list[GraphJudgment]


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


class GraphItemJudgment(_GraphFields):
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
