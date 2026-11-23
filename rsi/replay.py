"""Replay recorded games as if they were being played live, with no model calls.

The live game view follows whichever `game_*.json` has `status: "running"`, and the runner rewrites that
file after every message and phase. This module does the same from a finished record: it reveals the
game message by message (with each receiver's judgment), then the season's orders and board, so the
viewer shows a game in progress. No gateway, API key or credit is involved, so a public demo costs $0.

Replay files live in `<run_dir>/replay/`. Standings, the game index and history seeding skip that
directory, so replays are never counted as extra games.

    python -m rsi.replay runs/rsi_bg                    # cycle through the run's finished games forever
    python -m rsi.replay runs/rsi_bg --games g00_17 g03_4 --once
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path
from typing import Iterator

REPLAY_DIR = "replay"


def is_replay(path: Path, run_dir: Path) -> bool:
    """True for files under `<run_dir>/replay/` (replayed copies, not real games)."""
    try:
        return REPLAY_DIR in path.relative_to(run_dir).parts
    except ValueError:
        return REPLAY_DIR in path.parts


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def snapshots(rec: dict) -> Iterator[tuple[str, dict]]:
    """Yield (kind, record) steps that rebuild `rec` as a live game would have written it.

    kind is "message", "judgment" or "orders"; the last step is the finished record itself.
    """
    phases = rec.get("phases") or [h["phase"] for h in rec["sc_history"]] + [rec["final_phase"]]
    sc_by_phase = {h["phase"]: h for h in rec["sc_history"]}
    sends = {str(k): v for k, v in rec["sends"].items()}
    judgments = {str(k): v for k, v in rec["judgments"].items()}
    msgs_by_phase: dict[str, list[dict]] = {}
    for m in sorted(rec["messages"], key=lambda m: m["id"]):
        msgs_by_phase.setdefault(m["phase"], []).append(m)
    initial_sc = rec["sc_history"][0]["sc"] if rec["sc_history"] else rec["final_sc"]
    seated = list(rec.get("seating", {}))

    shown_msgs: list[dict] = []
    shown_sends: dict = {}
    shown_judgments: dict = {}
    shown_orders: dict = {}
    shown_boards: dict = {}
    shown_reasoning: dict = {}
    sc_history: list[dict] = []

    def state(phase_i: int, activity: dict) -> dict:
        out = dict(rec)
        out.update({
            "status": "running", "final_phase": phases[phase_i],
            "final_sc": sc_history[-1]["sc"] if sc_history else initial_sc,
            "sc_history": list(sc_history), "phases": phases[:phase_i + 1],
            "messages": list(shown_msgs), "sends": dict(shown_sends), "judgments": dict(shown_judgments),
            "orders": dict(shown_orders), "boards": dict(shown_boards), "reasoning": dict(shown_reasoning),
            "activity": activity,
        })
        return out

    for i, phase in enumerate(phases):
        for m in msgs_by_phase.get(phase, []):
            key = str(m["id"])
            shown_msgs.append(m)
            if key in sends:
                shown_sends[key] = sends[key]
            yield "message", state(i, {m["sender"]: f"writing to {m['recipient']}"})
            if key in judgments:
                shown_judgments[key] = judgments[key]
                yield "judgment", state(i, {m["recipient"]: f"judging {m['sender']}'s message"})
        if phase in sc_by_phase:
            for src, dst in ((rec.get("orders", {}), shown_orders), (rec.get("boards", {}), shown_boards),
                             (rec.get("reasoning", {}), shown_reasoning)):
                if phase in src:
                    dst[phase] = src[phase]
            sc_history.append(sc_by_phase[phase])
            yield "orders", state(i, {p: "writing orders" for p in seated})
    yield "finished", dict(rec, status="finished")


def pick_games(source: Path, run_dir: Path, names: list[str] | None = None, min_messages: int = 10) -> list[Path]:
    """Finished, error-free recorded games under `source` (replays excluded), newest first."""
    out = []
    for f in sorted(source.rglob("game_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if is_replay(f, run_dir):
            continue
        if names and f.stem.removeprefix("game_") not in names:
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("status", "finished") != "finished" or rec.get("error"):
            continue
        if len(rec.get("messages", [])) < min_messages:
            continue
        out.append(f)
    return out


def replay_game(src: Path, run_dir: Path, pace: dict[str, float], sleep=time.sleep) -> Path:
    """Replay one recorded game into `<run_dir>/replay/`; returns the replay file."""
    rec = json.loads(src.read_text())
    out_dir = run_dir / REPLAY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    gid = f"r{rec['game_id']}"
    rec["game_id"] = gid
    rec["replay_of"] = str(src.relative_to(run_dir)) if src.is_relative_to(run_dir) else str(src)
    if rec.get("maps_dir"):
        maps_src = (src.parent / rec["maps_dir"]).resolve()
        link = out_dir / f"maps_{gid}"
        if maps_src.is_dir() and not link.exists():
            link.symlink_to(maps_src, target_is_directory=True)
        rec["maps_dir"] = link.name if link.exists() else None
    dest = out_dir / f"game_{gid}.json"
    dest.with_suffix(".html").unlink(missing_ok=True)   # the viewer rebuilds the page shell for this game
    for kind, snap in snapshots(rec):
        _write_json(dest, snap)
        sleep(pace.get(kind, 0.0))
    return dest


def _clean_old(run_dir: Path, keep: Path) -> None:
    for f in (run_dir / REPLAY_DIR).glob("game_*"):
        if f.stem != keep.stem:
            f.unlink(missing_ok=True)
    for link in (run_dir / REPLAY_DIR).glob("maps_*"):
        if link.is_symlink() and link.name != f"maps_{keep.stem.removeprefix('game_')}":
            link.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.replay", description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", help="run directory the viewer serves")
    ap.add_argument("--source", help="where to find recorded games (default: run_dir)")
    ap.add_argument("--games", nargs="*", help="game ids to replay (default: all finished games)")
    ap.add_argument("--min-messages", type=int, default=10)
    ap.add_argument("--message-s", type=float, default=3.0, help="pause after each message")
    ap.add_argument("--judgment-s", type=float, default=1.5, help="pause after each judgment")
    ap.add_argument("--orders-s", type=float, default=4.0, help="pause after each season's orders")
    ap.add_argument("--end-s", type=float, default=20.0, help="pause on the finished game")
    ap.add_argument("--once", action="store_true", help="replay each game once, then stop")
    ap.add_argument("--seed", type=int, default=None, help="shuffle order (default: newest first)")
    a = ap.parse_args()
    run_dir = Path(a.run_dir)
    games = pick_games(Path(a.source) if a.source else run_dir, run_dir, a.games, a.min_messages)
    if not games:
        raise SystemExit(f"no finished games with >= {a.min_messages} messages under {a.source or run_dir}")
    pace = {"message": a.message_s, "judgment": a.judgment_s, "orders": a.orders_s, "finished": a.end_s}
    print(f"replaying {len(games)} recorded games into {run_dir / REPLAY_DIR} (no model calls)", flush=True)
    rng = random.Random(a.seed)
    while True:
        order = games[:] if a.seed is None else rng.sample(games, len(games))
        for g in order:
            dest = replay_game(g, run_dir, pace)
            _clean_old(run_dir, dest)
            print(f"replayed {g.name}", flush=True)
        if a.once:
            return


if __name__ == "__main__":
    main()
