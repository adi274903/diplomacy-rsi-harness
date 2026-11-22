"""League of genomes. Seats mix the current genome with frozen past genomes so players do not just
learn to read their own clones (self-play collapse). Ratings are a simple supply-centre share,
tracked for reporting only (never a gate criterion: we optimise detection, not winning)."""

from __future__ import annotations

import random
from collections import defaultdict

from rsi.env.game import POWERS
from rsi.genome import Genome


class League:
    def __init__(self, v0: Genome):
        self.members: dict[str, Genome] = {v0.hash: v0}
        self.frozen: list[str] = [v0.hash]
        self.sc_share: dict[str, list[float]] = defaultdict(list)

    def freeze(self, g: Genome) -> None:
        self.members[g.hash] = g
        if g.hash not in self.frozen:
            self.frozen.append(g.hash)

    def seat(self, current: Genome, rng: random.Random, frozen_share: float,
             powers: list[str] | None = None) -> dict[str, Genome]:
        powers = powers or POWERS
        self.members[current.hash] = current
        seating = {}
        for p in powers:
            if rng.random() < frozen_share:
                seating[p] = self.members[rng.choice(self.frozen)]
            else:
                seating[p] = current
        if all(g.hash != current.hash for g in seating.values()):   # current always plays
            seating[rng.choice(powers)] = current
        return seating

    def record(self, game: dict) -> None:
        total = sum(game["final_sc"].values()) or 1
        for p, h in game["seating"].items():
            self.sc_share[h].append(game["final_sc"][p] / total)

    def table(self) -> list[dict]:
        rows = []
        for h, shares in self.sc_share.items():
            g = self.members.get(h)
            rows.append({"genome": h, "name": g.name if g else "?", "seats": len(shares),
                         "mean_sc_share": sum(shares) / len(shares)})
        return sorted(rows, key=lambda r: -r["mean_sc_share"])
