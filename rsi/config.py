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
    effort_mode: Literal["top", "kwargs", "both", "openrouter", "none"] = "both"   # openrouter: reasoning.effort
    # json_schema = provider-side constrained decoding; json_object / none = parse + validate (+1 repair) only.
    json_mode: Literal["json_schema", "json_object", "none"] = "json_schema"
    # Some providers (e.g. Tinker's OpenAI-compatible API) only honour max_completion_tokens.
    max_tokens_field: Literal["max_tokens", "max_completion_tokens", "both"] = "max_tokens"
    extra_headers: dict[str, str] = Field(default_factory=dict)
    run_name: str = "default"
    max_tokens: dict[str, int] = Field(default_factory=lambda: {
        "send": 1536, "judge": 1536, "orders": 1536, "review": 6144, "item_judge": 1024, "text": 512,
    })


class GameConfig(BaseModel):
    map_name: str = "standard"
    # Powers played by agents; the rest are uncontrolled (units hold, no press). None = all powers.
    active_powers: list[str] | None = None
    # Optional per-power model override (e.g. a different lab's model per country). Unset = llm.model.
    power_models: dict[str, str] = Field(default_factory=dict)
    # Rotate these models across the active powers every game (game k: power i plays
    # model_pool[(i + k) % len]), so no model is tied to one country. Overrides power_models.
    model_pool: list[str] = Field(default_factory=list)
    press_rounds: int = 2
    max_messages_per_round: int = 2
    end_year: int = 1906
    history_window: int = 12          # messages per dialog shown to a player
    memory_window: int = 6            # own memory notes shown to a player
    max_order_options: int = 40       # per location, keeps prompts bounded mid-game
    strict_isolation: bool = True     # raise on any detected leak instead of logging it
    render_maps: bool = True          # save an SVG board (with orders) per phase for the viewer
    # Live mode: deliver each agent's messages the moment it finishes writing (instead of after the
    # whole round) and checkpoint on every event. Message ids then depend on API timing, so replays
    # from the response cache are not byte-identical; keep False for experiments.
    stream_events: bool = False


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
    # Which item set the acceptance gate's AUPRC/ECE test runs on (the human-val transfer check is unchanged):
    #   sim         -> receiver-seat replays of frozen self-play gate games (original behaviour)
    #   human_train -> human TRAIN games `human_gate_games` (all lies + human_gate_truth_ratio truths per lie)
    #   auto        -> sim, unless the sim gate set has fewer than `min_gate_lies` lies
    gate_source: Literal["sim", "human_train", "auto"] = "sim"
    human_gate_games: list[int] = Field(default_factory=lambda: [7, 8, 9, 10])
    human_gate_truth_ratio: int = 5
    min_gate_lies: int = 20
    frozen_share: float = 0.5          # share of seats given to frozen league members
    label_mode: Literal["intent", "promise", "either"] = "intent"
    # Start from a saved genome (e.g. playbooks/human_v1.json) instead of the built-in v0.
    initial_genome: str | None = None
    # Human-validation transfer check in the gate. Turn off when the starting playbook was learned
    # from the validation split (the check would be optimistic).
    human_val_gate: bool = True
    # Receivers answer with a decision graph (claims, cited evidence, adjustments); see rsi/decision_graph.py.
    # Sets Genome.structured_judgments on v0, so every genome descended from it inherits it.
    structured_judgments: bool = False
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
