"""Play Diplomacy yourself against the model agents.

    python -m rsi.play --config configs/play.yaml --power FRANCE --run-name play1
    # then open http://127.0.0.1:8770/ui/play/

You take one country; the other active countries are model agents (random seats from game.model_pool)
using the current playbook. Your seat sees exactly what a model agent sees: the public board, your own
conversations and notes. Nothing private about the other players is ever sent to your browser while the
game runs. After the game the privileged reveal (who lied to you, how the agents judged you) unlocks, and
optionally the judge model reviews the game.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from rsi.agents.human import HumanSeat
from rsi.agents.reviewer import Reviewer
from rsi.config import load_config
from rsi.env.game import game_powers
from rsi.genome import Genome, default_genome
from rsi.llm import make_llm
from rsi.loop import _progress
from rsi.runner import run_game
from rsi.standings import game_result, short_model

WEB = Path(__file__).resolve().parents[1] / "web" / "play"


class PlayState:
    def __init__(self, seat: HumanSeat, run_dir: Path, game_id: str, models: dict[str, str]):
        self.seat, self.run_dir, self.game_id, self.models = seat, run_dir, game_id, models
        self.status = "starting"
        self.review: dict | None = None
        self.lock = threading.Lock()

    def _record(self) -> dict | None:
        p = self.run_dir / "games" / f"game_{self.game_id}.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def public_state(self) -> dict:
        """Only what the human's seat may see: public board + their own dialogs, no other secrets."""
        rec = self._record() or {}
        me = self.seat.power
        mine = [m for m in rec.get("messages", []) if me in (m["sender"], m["recipient"])]
        phases = rec.get("phases", [])
        maps = rec.get("maps_dir")
        mdir = self.run_dir / "games" / maps if maps else None
        latest_map = None
        if mdir and mdir.exists():
            svgs = sorted(mdir.glob("*.svg"), key=lambda p: p.stat().st_mtime)
            latest_map = f"/maps/{svgs[-1].name}" if svgs else None
        sc = rec.get("sc_history", [])[-1]["sc"] if rec.get("sc_history") else rec.get("final_sc", {})
        my_judged = {int(k): v for k, v in rec.get("judgments", {}).items()
                     if v.get("receiver") == me}
        return {
            "status": self.status, "game_status": rec.get("status"), "power": me, "game_id": self.game_id,
            "phase": rec.get("final_phase"), "phases": phases, "models": self.models,
            "active": rec.get("active_powers", list(self.models)), "sc": sc, "map": latest_map,
            "messages": [{**{k: m[k] for k in ("id", "phase", "round", "sender", "recipient", "text")},
                          "my_p_lie": my_judged.get(m["id"], {}).get("p_lie")} for m in mine],
            "orders_history": rec.get("orders", {}),          # orders are public once resolved
            "activity": {p: a for p, a in rec.get("activity", {}).items() if p != me},
            "seat": self.seat.state(), "max_months": rec.get("max_months"),
        }

    def reveal(self) -> dict:
        """Privileged view of the human's own conversations, only once the game is over."""
        if self.status != "finished":
            return {"error": "the reveal unlocks when the game is over"}
        rec = self._record() or {}
        me = self.seat.power
        sends = {int(k): v for k, v in rec.get("sends", {}).items()}
        judg = {int(k): v for k, v in rec.get("judgments", {}).items()}
        rows = []
        for m in rec.get("messages", []):
            if me not in (m["sender"], m["recipient"]):
                continue
            j = judg.get(m["id"], {})
            rows.append({"id": m["id"], "phase": m["phase"], "sender": m["sender"], "recipient": m["recipient"],
                         "text": m["text"], "intent": sends.get(m["id"], {}).get("intent"),
                         "p_lie": j.get("p_lie"), "decision": j.get("decision"),
                         "reasoning": j.get("reasoning") if m["sender"] == me else None})
        res = game_result(rec) if rec else {}
        return {"messages": rows, "result": res, "review": self.review}


def make_handler(state: PlayState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, "application/json", json.dumps(obj).encode())

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/play/state":
                return self._json(state.public_state())
            if path == "/api/play/reveal":
                return self._json(state.reveal())
            if path.startswith("/maps/"):
                rec = state._record() or {}
                f = (state.run_dir / "games" / (rec.get("maps_dir") or "_") / Path(path).name)
                if f.exists():
                    return self._send(200, "image/svg+xml", f.read_bytes())
                return self._send(404, "text/plain", b"no map")
            if path in ("/", "/ui/play", "/ui/play/"):
                path = "/ui/play/index.html"
            if path.startswith("/ui/play/"):
                f = (WEB / path[len("/ui/play/"):]).resolve()
                if f.is_relative_to(WEB.resolve()) and f.is_file():
                    ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                             ".css": "text/css"}.get(f.suffix, "application/octet-stream")
                    return self._send(200, ctype, f.read_bytes())
            self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if urlparse(self.path).path != "/api/play/submit":
                return self._send(404, "text/plain", b"not found")
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            ok, msg = state.seat.submit(data)
            self._json({"ok": ok, "error": None if ok else msg}, 200 if ok else 409)

    return H


async def main_async(a) -> None:
    cfg = load_config(a.config)
    cfg.llm.run_name = a.run_name
    if a.months:
        cfg.game.end_year = max(cfg.game.end_year, 1900 + (a.months + 11) // 12 + 1)
    run_dir = Path(cfg.paths.runs) / a.run_name
    (run_dir / "games").mkdir(parents=True, exist_ok=True)
    powers = game_powers(cfg.game)
    me = a.power.upper()
    assert me in powers, f"--power must be one of {powers}"
    genome = Genome.load(a.playbook) if a.playbook else default_genome()
    pool = list(cfg.game.model_pool) or [cfg.llm.model]
    rng = random.Random(a.seed)
    rng.shuffle(pool)
    others = [p for p in powers if p != me]
    models = {p: pool[i % len(pool)] for i, p in enumerate(others)}
    seat = HumanSeat(me, genome.model_copy(update={"name": "human"}), timeout_s=a.turn_seconds)
    state = PlayState(seat, run_dir, "play", {me: "you", **models})

    srv = ThreadingHTTPServer((a.host, a.port), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _progress(f"[play] you are {me}; opponents: " + ", ".join(f"{p}={short_model(m)}" for p, m in models.items()))
    _progress(f"[play] open http://127.0.0.1:{a.port}/ui/play/  (each move times out after {a.turn_seconds:.0f}s)")

    llm = make_llm(cfg.llm)
    state.status = "playing"
    rec = await run_game("play", a.seed, {p: genome for p in powers}, llm, cfg.game, run_dir / "games",
                         progress=_progress, models=models, max_months=a.months or None,
                         player_overrides={me: seat})
    state.status = "finished"
    r = game_result(rec)
    _progress(f"[play] game over at {rec['final_phase']}; leader(s): {', '.join(r['winners'])}")
    if a.review:
        state.status = "reviewing"
        reviewer = Reviewer(make_llm(cfg.reviewer) if cfg.reviewer_llm else llm, effort=cfg.reviewer_effort)
        review = await reviewer.review_game(rec, genome)
        state.review = review.model_dump()
        (run_dir / "games" / "review_play.json").write_text(review.model_dump_json(indent=2))
        state.status = "finished"
    _progress("[play] reveal is unlocked in the page; press Ctrl+C to stop the server")
    await llm.aclose()
    while True:          # keep serving the reveal
        await asyncio.sleep(3600)


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.play")
    ap.add_argument("--config", default="configs/play.yaml")
    ap.add_argument("--power", default="FRANCE")
    ap.add_argument("--run-name", default="play1")
    ap.add_argument("--playbook", default="playbooks/human_v2.json", help="playbook the model agents use")
    ap.add_argument("--months", type=int, default=12, help="game length in months (3-month steps)")
    ap.add_argument("--turn-seconds", type=float, default=600, help="time you have for each move")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 inside a container)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--review", action=argparse.BooleanOptionalAction, default=True,
                    help="have the judge model review the game afterwards")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
