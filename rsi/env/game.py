"""Thin wrapper around the MILA `diplomacy` adjudicator (standard map).

Only public information is exposed here (board, legal orders, revealed orders). Private state lives
in `rsi.env.vault`.
"""

from __future__ import annotations

import re

POWERS = ["AUSTRIA", "ENGLAND", "FRANCE", "GERMANY", "ITALY", "RUSSIA", "TURKEY"]
SEASONS = {"S": "Spring", "F": "Fall", "W": "Winter"}
_PHASE = re.compile(r"^([SFW])(\d{4})([MRA])$")


def map_powers(map_name: str = "standard") -> list[str]:
    from diplomacy import Game

    return sorted(Game(map_name=map_name).powers)


def game_powers(cfg) -> list[str]:
    """Powers played by agents under a GameConfig."""
    allp = POWERS if cfg.map_name == "standard" else map_powers(cfg.map_name)
    if cfg.active_powers:
        bad = set(cfg.active_powers) - set(allp)
        assert not bad, f"unknown powers for map {cfg.map_name}: {bad}"
        return sorted(cfg.active_powers)
    return list(allp)


_LOC_PREFIX = re.compile(r"^[A-Z]{3}(?:/[A-Z]{2})?\s*:\s*")


def _norm_order(o: str) -> str:
    o = " ".join(o.strip().upper().split())
    return _LOC_PREFIX.sub("", o)    # some models write "PAR: A PAR H"


class DiplomacyEnv:
    def __init__(self, end_year: int = 1906, max_order_options: int = 40, map_name: str = "standard"):
        from diplomacy import Game  # imported lazily so the rest of the harness imports without it

        self.game = Game(map_name=map_name)
        self.powers: list[str] = sorted(self.game.powers)
        self.end_year = end_year
        self.max_order_options = max_order_options
        self.last_orders: dict[str, list[str]] = {}
        self.last_phase: str | None = None

    # -- phase ----------------------------------------------------------------------------------
    @property
    def phase(self) -> str:
        return self.game.get_current_phase()

    def phase_parts(self) -> tuple[str, int, str]:
        """('Spring'|'Fall'|'Winter', year, 'M'|'R'|'A')"""
        m = _PHASE.match(self.phase)
        if not m:
            return "", 0, ""
        return SEASONS[m.group(1)], int(m.group(2)), m.group(3)

    def done(self) -> bool:
        if self.game.is_game_done:
            return True
        _, year, _ = self.phase_parts()
        return year > self.end_year or year == 0

    # -- public state ---------------------------------------------------------------------------
    def units(self) -> dict[str, list[str]]:
        return {p: list(self.game.get_units(p)) for p in self.powers}

    def centers(self) -> dict[str, list[str]]:
        return {p: list(self.game.get_centers(p)) for p in self.powers}

    def sc_counts(self) -> dict[str, int]:
        return {p: len(c) for p, c in self.centers().items()}

    def alive(self) -> list[str]:
        return [p for p in self.powers if self.game.get_units(p) or self.game.get_centers(p)]

    # -- orders ---------------------------------------------------------------------------------
    def possible_orders(self, power: str) -> dict[str, list[str]]:
        """Legal orders for `power`, keyed by location. Hold/no-move options are kept first when the
        list is capped so the cap never removes the safe default."""
        locs = self.game.get_orderable_locations(power) or []
        if not locs:
            return {}
        allp = self.game.get_all_possible_orders()
        out = {}
        for loc in locs:
            opts = list(allp.get(loc, []))
            if len(opts) > self.max_order_options:
                safe = [o for o in opts if o.endswith(" H") or o == "WAIVE" or o.endswith(" D")]
                rest = [o for o in opts if o not in safe]
                opts = safe + rest[: max(0, self.max_order_options - len(safe))]
            if opts:
                out[loc] = opts
        return out

    def validate(self, power: str, orders: list[str],
                 possible: dict[str, list[str]] | None = None) -> tuple[list[str], list[str]]:
        """Split proposed orders into (valid, invalid); at most one order per location."""
        possible = possible if possible is not None else self.possible_orders(power)
        lookup = {_norm_order(o): (loc, o) for loc, opts in possible.items() for o in opts}
        valid, invalid, used = [], [], set()
        for o in orders:
            hit = lookup.get(_norm_order(o))
            if hit is None or hit[0] in used:
                invalid.append(o)
                continue
            used.add(hit[0])
            valid.append(hit[1])
        return valid, invalid

    def submit(self, power: str, orders: list[str]) -> None:
        if orders:
            self.game.set_orders(power, orders)

    def render_svg(self) -> str | None:
        """Current board as SVG, with the orders set for this phase drawn on it (None if the map
        ships without board art)."""
        from diplomacy.engine.renderer import Renderer

        try:
            return Renderer(self.game).render(incl_orders=True, incl_abbrev=True, output_format="svg")
        except Exception:  # noqa: BLE001 - variant maps without an SVG
            return None

    def process(self, submitted: dict[str, list[str]]) -> None:
        """Adjudicate. Orders become public afterwards, as in the real game."""
        self.last_phase = self.phase
        self.last_orders = {p: list(o) for p, o in submitted.items()}
        self.game.process()
