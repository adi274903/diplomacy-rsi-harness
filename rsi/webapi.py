"""Extension endpoints for the web apps, served by `rsi.viewer serve` at /api/x/<name>.

`handle(name, query, run_dir)` returns (status, content_type, body_bytes). With `--dev` the viewer
re-imports this module on every request, so endpoints can be added without restarting the server.

RSI dashboard endpoints (all read-only, all confined to `run_dir`):

    run                    config summary, live status, baseline, per-generation pipeline + metrics, incumbent
    gates                  every gate decision with its generation, candidate kind and bootstrap CIs
    lineage                genome tree (by content hash) with per-node playbook diff vs parent, and the
                           judge's proposals per generation (votes, games, lint result, used by which candidates)
    games                  every game: generation, status, seats (genome + model), deception counts, final SCs
    deception              per game x power rows (as sender and as receiver) for client-side grouping
    review?game=ID         the judge's grades joined with the messages they grade, its diffs and summary
    graph?game=ID&phase=P  one phase's decision graph (rsi.causal.phase_graph) + deceptive-message chains
    graph?game=ID          the whole game on one canvas (rsi.causal.game_trace)
    f1?scope=&rule=        gate F1 per genome / generation (gate_log, baseline, else evals re-scored) and live F1
                           per receiver country / model from the game records (rule=decision|p_lie, scope=all|gate|N)
    changelog              per-generation playbook history: proposals (votes, evidence, lint), candidates, verdict
    standings              winners per game, leaderboards by model / country (+ deception and F1 per model), and
                           a model x country matrix of games and wins (rsi.standings + generation tags)

Files are parsed once per (mtime, size) and the parsed form is cached across the --dev re-imports.
Game ids are looked up in a scanned index, never joined into a path, so a query cannot escape run_dir.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

# survives importlib.reload (module globals are kept and only re-assigned if the code says so)
_CACHE: dict = globals().get("_CACHE", {})
_CACHE_MAX = 400


def _json(obj, status: int = 200):
    return status, "application/json", json.dumps(_clean(obj), separators=(",", ":")).encode()


def _clean(x):
    """NaN/inf -> None (JSON.parse rejects NaN; an AUPRC with no lies in the gate set is NaN)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _stamp(p: Path):
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _cached(key, paths: list[Path], build):
    """build() memoised on the (mtime, size) of every path in `paths`."""
    sig = tuple(_stamp(p) for p in paths)
    hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    val = build()
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (sig, val)
    return val


def _read_json(p: Path):
    def build():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None
    return _cached(("json", str(p)), [p], build)


def _read_rows(p: Path) -> list[dict]:
    def build():
        out = []
        try:
            for line in p.read_text().splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass   # a row being appended right now
        except OSError:
            pass
        return out
    return _cached(("rows", str(p)), [p], build)


def _q(query: dict, k: str, default: str = "") -> str:
    v = query.get(k, [default])
    return v[0] if v else default


# ---- run-dir scanning -------------------------------------------------------------------------------

_GAME_RX = re.compile(r"^game_([A-Za-z0-9_\-]+)\.json$")
_ID_RX = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _game_files(run_dir: Path) -> dict[str, tuple[str | int, Path]]:
    """game id -> (generation or "gate", path). Only gate_games/ and gen_<N>/games/ are scanned."""
    out: dict[str, tuple[str | int, Path]] = {}
    dirs: list[tuple[str | int, Path]] = [("gate", run_dir / "gate_games")]
    for d in sorted(run_dir.glob("gen_*")):
        m = re.fullmatch(r"gen_(\d+)", d.name)
        if m and d.is_dir():
            dirs.append((int(m.group(1)), d / "games"))
    for gen, d in dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            m = _GAME_RX.match(f.name)
            if m:
                out[m.group(1)] = (gen, f)
    return out


def _genomes(run_dir: Path) -> dict[str, dict]:
    """hash -> {"genome": <first file's json>, "names": [...], "files": [...]}."""
    from rsi.genome import Genome

    d = run_dir / "genomes"
    files = sorted(d.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name)) if d.is_dir() else []

    def build():
        out: dict[str, dict] = {}
        for f in files:
            raw = _read_json(f)
            if not raw:
                continue
            try:
                h = Genome.model_validate(raw).hash
            except Exception:  # noqa: BLE001 - a half-written file
                continue
            e = out.setdefault(h, {"genome": raw, "names": [], "files": []})
            if raw.get("name") not in e["names"]:
                e["names"].append(raw.get("name"))
            e["files"].append(f.name)
        return out
    return _cached(("genomes", str(d)), [d, *files], build)


def _status(run_dir: Path) -> dict:
    return _read_json(run_dir / "rsi_status.json") or {}


def _config(run_dir: Path) -> dict:
    return _read_json(run_dir / "config.json") or {}


def _kind(name: str) -> str:
    if "-exemplars" in name:
        return "exemplars"
    if "-effort-" in name:
        return "effort"
    return "playbook" if re.match(r"g\d+c\d+", name) else "root" if name == "v0" else "other"


def _decisions(run_dir: Path) -> list[dict]:
    """Gate decisions in log order, each tagged with its generation and candidate name.

    gate_log.jsonl has no generation field: generation g owns the next `candidates` rows (from
    generations.jsonl); rows past the last finished generation belong to the one in progress."""
    gl, gp = run_dir / "gate_log.jsonl", run_dir / "generations.jsonl"
    genomes = _genomes(run_dir)
    st = _status(run_dir)

    def build():
        rows = _read_rows(gl)
        quota = [(r["gen"], int(r.get("candidates", 0))) for r in _read_rows(gp) if r.get("gen", 0) >= 1]
        out, i = [], 0
        for gen, n in quota:
            for _ in range(n):
                if i < len(rows):
                    out.append((gen, rows[i]))
                    i += 1
        cur = st.get("gen") or ((quota[-1][0] + 1) if quota else 1)
        out += [(cur, r) for r in rows[i:]]
        res = []
        for k, (gen, d) in enumerate(out):
            names = genomes.get(d["candidate"], {}).get("names", [])
            name = next((n for n in names if n and n.startswith(f"g{gen}")), names[0] if names else d["candidate"])
            inc_names = genomes.get(d["incumbent"], {}).get("names", [])
            res.append({**d, "idx": k, "gen": gen, "name": name, "kind": _kind(name),
                        "incumbent_name": inc_names[0] if inc_names else d["incumbent"]})
        return res
    return _cached(("decisions", str(run_dir)), [gl, gp, run_dir / "genomes", run_dir / "rsi_status.json"], build)


# ---- per-game summaries -----------------------------------------------------------------------------

def _outcome(intent: str | None, j: dict | None) -> str:
    if not j or not j.get("ok", True):
        return "unjudged"
    lie, distrust = intent == "deceptive", j.get("decision") == "distrust"
    return "caught" if lie and distrust else "fooled" if lie else "false_alarm" if distrust else "trusted"


def _power_rows(rec: dict) -> dict[str, dict]:
    """Per power: as sender (sent, lies, lies_caught, lies_fooled) and as receiver (received,
    lies_received, caught, fooled, false_alarms, trusted_truths), plus p_lie sums for separation."""
    sends = {int(k): v for k, v in rec.get("sends", {}).items()}
    js = {int(k): v for k, v in rec.get("judgments", {}).items()}
    powers = rec.get("active_powers") or sorted(rec.get("seating", {}))
    rows = {p: {"sent": 0, "lies": 0, "lies_caught": 0, "lies_fooled": 0, "received": 0, "lies_received": 0,
                "caught": 0, "fooled": 0, "false_alarms": 0, "trusted_truths": 0, "judged": 0,
                "p_lie_on_lies": 0.0, "p_lie_on_truths": 0.0, "truths_judged": 0} for p in powers}
    for m in rec.get("messages", []):
        s, r = rows.get(m["sender"]), rows.get(m["recipient"])
        intent = sends.get(m["id"], {}).get("intent")
        j = js.get(m["id"])
        oc = _outcome(intent, j)
        lie = intent == "deceptive"
        if s:
            s["sent"] += 1
            s["lies"] += lie
            s["lies_caught"] += oc == "caught"
            s["lies_fooled"] += oc == "fooled"
        if r:
            r["received"] += 1
            r["lies_received"] += lie
            r["caught"] += oc == "caught"
            r["fooled"] += oc == "fooled"
            r["false_alarms"] += oc == "false_alarm"
            r["trusted_truths"] += oc == "trusted"
            if oc != "unjudged":
                r["judged"] += 1
                if lie:
                    r["p_lie_on_lies"] += j["p_lie"]
                else:
                    r["p_lie_on_truths"] += j["p_lie"]
                    r["truths_judged"] += 1
    return rows


def _game_summary(gid: str, gen, path: Path, run_dir: Path) -> dict | None:
    def build():
        rec = _read_json(path)
        if not rec or "messages" not in rec:
            return None
        rows = _power_rows(rec)
        models = rec.get("models", {})
        names = rec.get("genome_names", {})
        seats = {p: {"genome": h, "genome_name": names.get(p, ""), "model": models.get(p, "")}
                 for p, h in rec.get("seating", {}).items()}
        tot = {k: sum(r[k] for r in rows.values()) for k in ("sent", "lies", "caught", "fooled", "false_alarms")}
        review = path.with_name(f"review_{gid}.json")
        return {"id": gid, "gen": gen, "status": rec.get("status", "finished"), "error": rec.get("error"),
                "final_phase": rec.get("final_phase"), "phases": rec.get("phases") or [],
                "active_powers": rec.get("active_powers") or sorted(seats), "seats": seats,
                "final_sc": rec.get("final_sc", {}), "sc_history": rec.get("sc_history", []),
                "messages": tot["sent"], "lies": tot["lies"], "caught": tot["caught"], "fooled": tot["fooled"],
                "false_alarms": tot["false_alarms"], "judged": sum(r["judged"] for r in rows.values()),
                "powers": rows, "has_review": review.exists(), "activity": rec.get("activity", {}),
                "wall_s": rec.get("stats", {}).get("wall_s")}
    out = _cached(("summary", str(path)), [path, path.with_name(f"review_{gid}.json")], build)
    if out is not None:
        out = {**out, "version": _stamp(path)[0] if _stamp(path) else 0}
    return out


def _all_games(run_dir: Path) -> list[dict]:
    files = _game_files(run_dir)
    out = [s for gid, (gen, p) in files.items() if (s := _game_summary(gid, gen, p, run_dir))]
    order = lambda g: (-1 if g["gen"] == "gate" else g["gen"], g["id"])  # noqa: E731
    return sorted(out, key=order)


# ---- proposals (judge diffs) ------------------------------------------------------------------------

def _norm(t: str | None) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def _proposals(run_dir: Path) -> dict[int, dict]:
    """gen -> {"parent": hash, "ranked": [...], "rejected": [...]}, re-derived from gen_<N>/reviews.jsonl
    with the loop's own aggregation (votes = games proposing the same diff). The lint is re-run
    without the held-out human-data contamination check, which needs the human corpus."""
    from rsi.evolve import aggregate_diffs
    from rsi.genome import Genome
    from rsi.schemas import GameReview

    gens = {r["gen"]: r for r in _read_rows(run_dir / "generations.jsonl") if r.get("gen", 0) >= 1}
    st = _status(run_dir)
    genomes = _genomes(run_dir)
    out = {}
    for d in sorted(run_dir.glob("gen_*")):
        m = re.fullmatch(r"gen_(\d+)", d.name)
        if not m:
            continue
        gen, rp = int(m.group(1)), d / "reviews.jsonl"
        if not rp.exists():
            continue
        parent = gens.get(gen, {}).get("parent") or (st.get("parent") or {}).get("hash")
        pg = genomes.get(parent, {}).get("genome")

        def build(rp=rp, pg=pg, parent=parent, gen=gen):
            reviews = {}
            for row in _read_rows(rp):
                row = dict(row)   # the parsed rows are cached: never mutate them
                gid = row.pop("game_id", None)
                try:
                    reviews[gid] = GameReview.model_validate(row)
                except Exception:  # noqa: BLE001
                    continue
            if pg is None:
                return {"gen": gen, "parent": parent, "ranked": [], "rejected": []}
            ranked, rejected = aggregate_diffs(reviews, Genome.model_validate(pg))
            return {"gen": gen, "parent": parent,
                    "ranked": [{"op": s.diff.op, "heuristic_id": s.diff.heuristic_id, "text": s.diff.text,
                                "rationale": s.diff.rationale, "votes": s.votes, "games": s.games,
                                "evidence": s.diff.evidence_msg_ids, "key": s.key} for s in ranked],
                    "rejected": [{**r["diff"], "game": r["game"], "errors": r["errors"]} for r in rejected]}
        out[gen] = _cached(("proposals", str(rp), parent), [rp, run_dir / "genomes"], build)
    return out


def _genome_diff(child: dict, parent: dict | None) -> dict:
    if parent is None:
        return {"added": [], "edited": [], "removed": [], "exemplars": None, "effort": [], "other": []}
    ph = {h["id"]: h for h in parent.get("playbook", [])}
    ch = {h["id"]: h for h in child.get("playbook", [])}
    added = [h for i, h in ch.items() if i not in ph]
    removed = [h for i, h in ph.items() if i not in ch]
    edited = [{**h, "before": ph[i]["text"]} for i, h in ch.items() if i in ph and ph[i]["text"] != h["text"]]
    pe, ce = parent.get("exemplars", []), child.get("exemplars", [])
    ex = None if pe == ce else {"before": len(pe), "after": len(ce), "items": ce}
    eff = [{"key": k, "before": parent.get("effort", {}).get(k), "after": v}
           for k, v in child.get("effort", {}).items() if parent.get("effort", {}).get(k) != v]
    other = [k for k in ("system_prompt", "temperature", "threshold") if child.get(k) != parent.get(k)]
    return {"added": added, "edited": edited, "removed": removed, "exemplars": ex, "effort": eff, "other": other}


# ---- endpoints --------------------------------------------------------------------------------------

def ep_run(run_dir: Path, query) -> dict:
    cfg, st = _config(run_dir), _status(run_dir)
    lc, gc = cfg.get("loop", {}), cfg.get("game", {})
    gens = _read_rows(run_dir / "generations.jsonl")
    decs = _decisions(run_dir)
    genomes = _genomes(run_dir)
    games = _all_games(run_dir)
    baseline = next((r.get("sim_gate") for r in gens if r.get("gen") == 0), None) or st.get("baseline")

    def gname(h):
        return (genomes.get(h, {}).get("names") or [h])[0] if h else None

    # metrics trajectory: the incumbent's gate metrics after each generation
    cur = {"auprc": (baseline or {}).get("auprc"), "ece": (baseline or {}).get("ece"),
           "brier": (baseline or {}).get("brier"), "human_auprc": None}
    props = _proposals(run_dir)
    rows = []
    max_gen = max([r["gen"] for r in gens] + [st.get("gen") or 0] + [d["gen"] for d in decs] + [0])
    finished = {r["gen"]: r for r in gens}
    for g in range(1, max_gen + 1):
        r = finished.get(g, {})
        gd = [d for d in decs if d["gen"] == g]
        acc = [d for d in gd if d["accepted"]]
        if acc and r:
            best = next((d for d in acc if d["candidate"] == r.get("incumbent")),
                        max(acc, key=lambda d: d["sim"]["auprc"]["delta"] or -1))
            cm = best["sim_metrics"].get("candidate", {})
            cur = {"auprc": cm.get("auprc"), "ece": cm.get("ece"), "brier": cm.get("brier"),
                   "human_auprc": (best.get("human_metrics") or {}).get("candidate", {}).get("auprc")}
        gg = [x for x in games if x["gen"] == g]
        rdir = run_dir / f"gen_{g}" / "games"
        n_reviews = len(list(rdir.glob("review_*.json"))) if rdir.is_dir() else 0
        live = st.get("gen") == g and not r
        rows.append({
            "gen": g, "done": bool(r), "live": live, "stage": st.get("stage") if live else ("done" if r else ""),
            "parent": r.get("parent") or ((st.get("parent") or {}).get("hash") if live else None),
            "parent_name": gname(r.get("parent") or ((st.get("parent") or {}).get("hash") if live else None)),
            "incumbent": r.get("incumbent"), "incumbent_name": gname(r.get("incumbent")),
            "games_planned": lc.get("games_per_generation"), "games": len(gg),
            "games_finished": sum(1 for x in gg if x["status"] != "running"),
            "games_running": sum(1 for x in gg if x["status"] == "running"),
            "reviews": n_reviews,
            "diffs_kept": r.get("diffs_kept", len(props[g]["ranked"]) if g in props else None),
            "diffs_rejected": r.get("diffs_rejected", len(props[g]["rejected"]) if g in props else None),
            "candidates": r.get("candidates", len(gd) if gd else None), "gated": len(gd),
            "accepted": [{"hash": h, "name": gname(h)} for h in r.get("accepted", [])] if r else
                        [{"hash": d["candidate"], "name": d["name"]} for d in acc],
            "best_p_gain": max((d["sim"]["auprc"]["p_gain"] for d in gd if d["sim"]["auprc"]["p_gain"] is not None),
                               default=None),
            "leak_flags": r.get("leak_flags"), "wall_s": r.get("wall_s"),
            "lies": sum(x["lies"] for x in gg), "caught": sum(x["caught"] for x in gg),
            "messages": sum(x["messages"] for x in gg),
            "incumbent_metrics": dict(cur) if r else None,
            "league": r.get("league"),
        })

    inc_hash = (st.get("incumbent") or {}).get("hash") or (gens[-1]["incumbent"] if gens else None)
    inc = genomes.get(inc_hash, {}).get("genome")
    gate_games = [x for x in games if x["gen"] == "gate"]
    return {
        "run": run_dir.name,
        "config": {
            "generations": lc.get("generations"), "games_per_generation": lc.get("games_per_generation"),
            "gate_games": lc.get("gate_games"), "candidates": lc.get("candidates"),
            "max_diffs_per_candidate": lc.get("max_diffs_per_candidate"), "patience": lc.get("patience"),
            "p_gain_min": lc.get("p_gain_min", 0.8), "ece_tolerance": lc.get("ece_tolerance", 0.02),
            "human_val_tolerance": lc.get("human_val_tolerance", 0.01), "bootstrap_n": lc.get("bootstrap_n"),
            "end_year": gc.get("end_year"), "press_rounds": gc.get("press_rounds"),
            "active_powers": gc.get("active_powers"), "power_models": gc.get("power_models") or {},
            "player_model": cfg.get("llm", {}).get("model"), "backend": cfg.get("llm", {}).get("backend"),
            "judge_model": (cfg.get("reviewer_llm") or {}).get("model") or cfg.get("llm", {}).get("model"),
        },
        "status": {"stage": st.get("stage"), "gen": st.get("gen"), "updated": st.get("updated"),
                   "events": st.get("events", [])[:20]},
        "baseline": baseline,
        "gate_set": {"games": len(gate_games), "finished": sum(1 for x in gate_games if x["status"] != "running"),
                     "n": (baseline or {}).get("n"), "n_lie": (baseline or {}).get("n_lie")},
        "generations": rows,
        "incumbent": {"hash": inc_hash, "name": gname(inc_hash), "genome": inc,
                      "generation_adopted": next((r["gen"] for r in reversed(gens)
                                                  if r.get("gen", 0) >= 1 and inc_hash in r.get("accepted", [])), 0)},
        "totals": {"games": len(games), "games_running": sum(1 for x in games if x["status"] == "running"),
                   "messages": sum(x["messages"] for x in games), "lies": sum(x["lies"] for x in games),
                   "caught": sum(x["caught"] for x in games), "fooled": sum(x["fooled"] for x in games),
                   "false_alarms": sum(x["false_alarms"] for x in games),
                   "candidates": len(decs), "accepted": sum(1 for d in decs if d["accepted"]),
                   "genomes": len(genomes)},
    }


def ep_gates(run_dir: Path, query) -> dict:
    cfg = _config(run_dir).get("loop", {})
    keep = ("auprc", "ece", "brier")
    out = []
    for d in _decisions(run_dir):
        out.append({"idx": d["idx"], "gen": d["gen"], "name": d["name"], "kind": d["kind"],
                    "candidate": d["candidate"], "incumbent": d["incumbent"], "incumbent_name": d["incumbent_name"],
                    "accepted": d["accepted"], "reasons": d["reasons"],
                    "sim": {k: d["sim"].get(k) for k in keep if k in d["sim"]},
                    "human": d.get("human") or {},
                    "sim_metrics": d.get("sim_metrics") or {}, "human_metrics": d.get("human_metrics") or {}})
    return {"p_gain_min": cfg.get("p_gain_min", 0.8), "ece_tolerance": cfg.get("ece_tolerance", 0.02),
            "human_val_tolerance": cfg.get("human_val_tolerance", 0.01), "bootstrap_n": cfg.get("bootstrap_n"),
            "decisions": out}


def ep_lineage(run_dir: Path, query) -> dict:
    genomes = _genomes(run_dir)
    decs = _decisions(run_dir)
    gens = _read_rows(run_dir / "generations.jsonl")
    st = _status(run_dir)
    props = _proposals(run_dir)
    inc = (st.get("incumbent") or {}).get("hash") or (gens[-1]["incumbent"] if gens else None)
    past_inc = {r["incumbent"] for r in gens if r.get("incumbent")}

    # which proposals did each candidate take? match its added/edited/removed heuristics to diff keys
    def used(diff: dict, gen_props: dict | None) -> list[dict]:
        if not gen_props:
            return []
        out = []
        for p in gen_props["ranked"]:
            if p["op"] == "add" and any(_norm(h["text"]) == _norm(p["text"]) for h in diff["added"]):
                out.append(p)
            elif p["op"] == "edit" and any(h["id"] == p["heuristic_id"] and _norm(h["text"]) == _norm(p["text"])
                                           for h in diff["edited"]):
                out.append(p)
            elif p["op"] == "delete" and any(h["id"] == p["heuristic_id"] for h in diff["removed"]):
                out.append(p)
        return out

    nodes = []
    for h, e in genomes.items():
        g = e["genome"]
        parent = g.get("parent")
        nd = [d for d in decs if d["candidate"] == h]
        name = e["names"][0]
        diff = _genome_diff(g, genomes.get(parent, {}).get("genome") if parent else None)
        loop_gens = sorted({d["gen"] for d in nd})
        taken = []
        for lg in loop_gens or [g.get("generation")]:
            taken += [{"gen": lg, **p} for p in used(diff, props.get(lg))]
        status = ("incumbent" if h == inc else "accepted" if any(d["accepted"] for d in nd) else
                  "root" if not parent else "rejected" if nd else "pending")
        nodes.append({"hash": h, "name": name, "names": e["names"], "parent": parent,
                      "generation": g.get("generation", 0), "loop_gens": loop_gens, "kind": _kind(name),
                      "status": status, "was_incumbent": h in past_inc or h == inc,
                      "decisions": [{"gen": d["gen"], "idx": d["idx"], "accepted": d["accepted"],
                                     "reasons": d["reasons"], "d_auprc": d["sim"]["auprc"]["delta"],
                                     "lo": d["sim"]["auprc"]["lo"], "hi": d["sim"]["auprc"]["hi"],
                                     "p_gain": d["sim"]["auprc"]["p_gain"],
                                     "auprc": d["sim_metrics"].get("candidate", {}).get("auprc")} for d in nd],
                      "playbook": g.get("playbook", []), "exemplars": len(g.get("exemplars", [])),
                      "effort": g.get("effort", {}), "threshold": g.get("threshold"),
                      "diff": diff, "proposals_used": taken})
    nodes.sort(key=lambda n: (n["generation"], min(n["loop_gens"] or [0]), n["name"]))
    return {"incumbent": inc, "nodes": nodes, "proposals": [props[k] for k in sorted(props)],
            "lint_note": "lint re-run here without the held-out human-data contamination check"}


def ep_games(run_dir: Path, query) -> dict:
    games = _all_games(run_dir)
    for g in games:   # the list view does not need per-power rows or the SC trajectory
        g.pop("powers", None)
        g.pop("sc_history", None)
    return {"games": games}


def ep_deception(run_dir: Path, query) -> dict:
    rows = []
    for g in _all_games(run_dir):
        for p, r in g["powers"].items():
            seat = g["seats"].get(p, {})
            rows.append({"game": g["id"], "gen": g["gen"], "status": g["status"], "power": p,
                         "model": seat.get("model", ""), "genome": seat.get("genome", ""),
                         "genome_name": seat.get("genome_name", ""), **r})
    return {"rows": rows}


def _game(run_dir: Path, gid: str):
    if not _ID_RX.match(gid or ""):
        return None, None
    hit = _game_files(run_dir).get(gid)
    if not hit:
        return None, None
    return hit[0], hit[1]


def ep_review(run_dir: Path, query):
    gid = _q(query, "game")
    gen, path = _game(run_dir, gid)
    if path is None:
        return _json({"error": "unknown game"}, 404)
    rec = _read_json(path) or {}
    rev = _read_json(path.with_name(f"review_{gid}.json"))
    if rev is None:
        return _json({"game": gid, "gen": gen, "review": None})
    msgs = {m["id"]: m for m in rec.get("messages", [])}
    sends = {int(k): v for k, v in rec.get("sends", {}).items()}
    js = {int(k): v for k, v in rec.get("judgments", {}).items()}
    grades = []
    for gr in rev.get("grades", []):
        m = msgs.get(gr.get("msg_id"), {})
        j = js.get(gr.get("msg_id"))
        intent = sends.get(gr.get("msg_id"), {}).get("intent")
        grades.append({**gr, "phase": m.get("phase"), "sender": m.get("sender"), "recipient": m.get("recipient"),
                       "text": m.get("text"), "intent": intent, "p_lie": j.get("p_lie") if j else None,
                       "decision": j.get("decision") if j else None, "judge_reasoning": j.get("reasoning") if j else None,
                       "outcome": _outcome(intent, j)})
    gp = _proposals(run_dir).get(gen) if isinstance(gen, int) else None
    diffs = []
    for d in rev.get("diffs", []):
        key = f"{d.get('op')}|{d.get('heuristic_id') or ''}|{_norm(d.get('text'))}"
        hit = next((p for p in (gp or {}).get("ranked", []) if p["key"] == key), None)
        rej = next((p for p in (gp or {}).get("rejected", []) if p.get("game") == gid
                    and _norm(p.get("text")) == _norm(d.get("text")) and p.get("op") == d.get("op")), None)
        diffs.append({**d, "votes": hit["votes"] if hit else None, "games": hit["games"] if hit else [],
                      "lint_errors": rej["errors"] if rej else []})
    return _json({"game": gid, "gen": gen, "summary": rev.get("summary", ""), "grades": grades, "diffs": diffs})


def ep_graph(run_dir: Path, query):
    import importlib

    from rsi import causal
    if not hasattr(causal, "game_trace"):   # a long-running viewer imported causal before these were added
        importlib.reload(causal)
    deception_chains, game_trace, phase_graph = causal.deception_chains, causal.game_trace, causal.phase_graph

    gid, phase = _q(query, "game"), _q(query, "phase")
    _, path = _game(run_dir, gid)
    if path is None:
        return _json({"error": "unknown game"}, 404)
    rec = _read_json(path)
    if not rec:
        return _json({"error": "game record unreadable"}, 503)
    phases = rec.get("phases") or [h["phase"] for h in rec.get("sc_history", [])]

    def build():
        if phase:
            if phase not in phases and phase not in rec.get("reasoning", {}):
                return None
            g = phase_graph(rec, phase)
            return {**g, "game": gid, "phases": phases, "mode": "phase",
                    "chains": deception_chains(rec, phase, g), "models": rec.get("models", {}),
                    "status": rec.get("status", "finished")}
        t = game_trace(rec)
        return {**t, "game": gid, "mode": "game", "models": rec.get("models", {}),
                "status": rec.get("status", "finished")}
    out = _cached(("graph", str(path), phase), [path], build)
    if out is None:
        return _json({"error": "unknown phase"}, 404)
    return _json({**out, "version": (_stamp(path) or (0,))[0]})

# ---- F1 telemetry -----------------------------------------------------------------------------------

F1_KEYS = ("n", "n_lie", "lie_f1", "macro_f1", "best_lie_f1", "best_threshold", "auprc", "auroc", "ece", "brier",
           "threshold", "base_rate")
UNSTABLE_N_LIE = 5


def _pick(m: dict | None) -> dict | None:
    if not m:
        return None
    out = {k: m.get(k) for k in F1_KEYS}
    if not m.get("n_lie"):   # the scorer returns lie-F1 0.0 with no positives; it is undefined, not zero
        for k in ("lie_f1", "macro_f1", "best_lie_f1", "best_threshold", "auprc", "auroc"):
            if k in out:
                out[k] = None
    out["unstable"] = (m.get("n_lie") or 0) < UNSTABLE_N_LIE
    return out


def _sim_items(run_dir: Path):
    """The sim-gate items exactly as rsi.loop built them (gate games -> receiver items -> balance)."""
    from rsi.judge import balance, items_from_game

    cfg = _config(run_dir)
    gc, lc = cfg.get("game", {}), cfg.get("loop", {})
    files = sorted(((gid, p) for gid, (gen, p) in _game_files(run_dir).items() if gen == "gate"),
                   key=lambda x: int(re.sub(r"\D", "", x[0]) or 0))

    mode = lc.get("label_mode", "intent")
    reviews = [p.with_name(f"review_{gid}.json") for gid, p in files]

    def build():
        recs = [_read_json(p) for _, p in files]
        if not recs or any(not r or r.get("status") == "running" for r in recs):
            return None
        items = []
        for (gid, _), r, rv in zip(files, recs, reviews):
            if r.get("error"):
                continue
            promises = _promises(rv) if mode in ("promise", "either") else None
            if mode in ("promise", "either") and promises is None:
                return None   # the loop labels gate items with the judge's review; not written yet
            items += items_from_game(r, gc.get("history_window", 12), mode, promises)
        return balance(items, lc.get("sim_gate_truth_ratio", 4), lc.get("seed", 0))
    return _cached(("sim_items", str(run_dir)), [p for _, p in files] + reviews + [run_dir / "config.json"], build)


def _promises(review_path: Path) -> dict[int, str] | None:
    rv = _read_json(review_path)
    return None if rv is None else {g["msg_id"]: g["promise"] for g in rv.get("grades", [])}


def _metrics_from_evals(run_dir: Path, h: str, threshold: float) -> dict | None:
    """Score evals/sim_gate__<hash>.jsonl against the reconstructed gate items (only if the ids match)."""
    from rsi.judge import ItemResult
    from rsi.score import evaluate

    ep = run_dir / "evals" / f"sim_gate__{h}.jsonl"
    if not ep.exists():
        return None

    def build():
        items = _sim_items(run_dir)
        rows = _read_rows(ep)
        if not items or [r.get("item_id") for r in rows] != [it.item_id for it in items]:
            return None
        res = [ItemResult(**{k: r[k] for k in ("item_id", "p_lie", "decision", "reasoning", "cues", "ok")})
               for r in rows]
        return evaluate(items, res, threshold)
    return _cached(("evalmetrics", str(ep), threshold), [ep, run_dir / "gate_games"], build)


def _genome_metrics(run_dir: Path) -> dict[str, dict]:
    """hash -> {"sim": metrics, "sim_source": ..., "human": metrics}, from gate_log first, then evals."""
    genomes = _genomes(run_dir)
    out: dict[str, dict] = {}
    gens = _read_rows(run_dir / "generations.jsonl")
    for r in gens:
        if r.get("gen") == 0 and r.get("sim_gate"):
            out.setdefault(r["incumbent"], {})["sim"] = r["sim_gate"]
            out[r["incumbent"]]["sim_source"] = "baseline"
    for d in _decisions(run_dir):
        for role, h in (("incumbent", d["incumbent"]), ("candidate", d["candidate"])):
            e = out.setdefault(h, {})
            if (d.get("sim_metrics") or {}).get(role) and "sim" not in e:
                e["sim"], e["sim_source"] = d["sim_metrics"][role], "gate_log"
            if (d.get("human_metrics") or {}).get(role) and "human" not in e:
                e["human"] = d["human_metrics"][role]
    for h, g in genomes.items():
        e = out.setdefault(h, {})
        if "sim" not in e:
            m = _metrics_from_evals(run_dir, h, g["genome"].get("threshold", 0.5))
            if m:
                e["sim"], e["sim_source"] = m, "evals"
    return out


def _conf(tp=0, fp=0, fn=0, tn=0, unjudged=0) -> dict:
    """Lie = positive class. F1 is None (undefined) when its denominator is 0."""
    n, n_lie = tp + fp + fn + tn, tp + fn
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / n_lie if n_lie else None
    lie_f1 = 2 * tp / (2 * tp + fp + fn) if n_lie else None   # no lies -> undefined, never 0
    truth_f1 = 2 * tn / (2 * tn + fp + fn) if (tn + fp) else None
    macro = (lie_f1 + truth_f1) / 2 if lie_f1 is not None and truth_f1 is not None else None
    return {"n": n, "n_lie": n_lie, "caught": tp, "false_alarms": fp, "fooled": fn, "trusted_truths": tn,
            "unjudged": unjudged, "precision": prec, "recall": rec, "lie_f1": lie_f1, "truth_f1": truth_f1,
            "macro_f1": macro, "unstable": n_lie < UNSTABLE_N_LIE}


def _add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in ("tp", "fp", "fn", "tn", "unjudged")}


def _game_f1(gid: str, gen, path: Path, run_dir: Path, rule: str, label: str = "intent") -> dict | None:
    """Per receiver: confusion counts, per game and cumulative after each phase (for the rolling series).
    label "either": a message is deceptive if self-labelled deceptive OR the judge's review found the promise
    broken (reviews exist only once a generation is reviewed, so a running game counts intent only)."""
    genomes = _genomes(run_dir)
    rv_path = path.with_name(f"review_{gid}.json")

    def build():
        promises = (_promises(rv_path) or {}) if label in ("promise", "either") else {}
        rec = _read_json(path)
        if not rec or "messages" not in rec:
            return None
        sends = {int(k): v for k, v in rec.get("sends", {}).items()}
        js = {int(k): v for k, v in rec.get("judgments", {}).items()}
        powers = rec.get("active_powers") or sorted(rec.get("seating", {}))
        thr = {p: genomes.get(h, {}).get("genome", {}).get("threshold", 0.5) for p, h in rec.get("seating", {}).items()}
        phases = rec.get("phases") or []
        by_phase: dict[str, dict[str, dict]] = {}
        for m in rec["messages"]:
            r = m["recipient"]
            if r not in powers:
                continue
            c = by_phase.setdefault(m["phase"], {}).setdefault(r, {})
            j = js.get(m["id"])
            if not j or not j.get("ok", True) or m["id"] not in sends:
                c["unjudged"] = c.get("unjudged", 0) + 1
                continue
            intent_lie, broken = sends[m["id"]]["intent"] == "deceptive", promises.get(m["id"]) == "broken"
            lie = {"intent": intent_lie, "promise": broken}.get(label, intent_lie or broken)
            pred = j["decision"] == "distrust" if rule == "decision" else j["p_lie"] >= thr.get(r, 0.5)
            k = ("tp" if pred else "fn") if lie else ("fp" if pred else "tn")
            c[k] = c.get(k, 0) + 1
        total = {p: {} for p in powers}
        series = []
        for ph in phases + [p for p in by_phase if p not in phases]:
            if ph not in by_phase:
                continue
            for p, c in by_phase[ph].items():
                total[p] = _add(total.get(p, {}), c)
            all_ = {}
            for c in total.values():
                all_ = _add(all_, c)
            series.append({"phase": ph, "overall": _conf(**all_), "by_country": {p: _conf(**c) for p, c in total.items()}})
        return {"id": gid, "gen": gen, "status": rec.get("status", "finished"), "models": rec.get("models", {}),
                "genome_names": rec.get("genome_names", {}), "counts": total, "series": series,
                "reviewed": bool(promises)}
    return _cached(("f1game", str(path), rule, label), [path, rv_path, run_dir / "genomes"], build)


def ep_f1(run_dir: Path, query) -> dict:
    rule = "p_lie" if _q(query, "rule") == "p_lie" else "decision"
    scope = _q(query, "scope", "all")
    run_label = _config(run_dir).get("loop", {}).get("label_mode", "intent")
    label = _q(query, "label") or run_label
    label = label if label in ("intent", "promise", "either") else "intent"
    genomes = _genomes(run_dir)
    gm = _genome_metrics(run_dir)
    decs = _decisions(run_dir)
    gens = _read_rows(run_dir / "generations.jsonl")
    st = _status(run_dir)

    def gname(h):
        return (genomes.get(h, {}).get("names") or [h])[0] if h else None

    # gate F1 per genome
    rows = []
    for h, e in genomes.items():
        g = e["genome"]
        nd = [d for d in decs if d["candidate"] == h]
        m = gm.get(h, {})
        rows.append({"hash": h, "name": e["names"][0], "parent": g.get("parent"), "kind": _kind(e["names"][0]),
                     "loop_gens": sorted({d["gen"] for d in nd}), "accepted": any(d["accepted"] for d in nd),
                     "threshold": g.get("threshold", 0.5), "sim": _pick(m.get("sim")),
                     "sim_source": m.get("sim_source"), "human": _pick(m.get("human"))})
    rows.sort(key=lambda r: (min(r["loop_gens"] or [0]), r["name"]))

    # incumbent trajectory: gen 0 baseline, then the incumbent after each finished generation
    traj = []
    for r in gens:
        h = r.get("incumbent")
        traj.append({"gen": r["gen"], "hash": h, "name": gname(h), "sim": _pick(gm.get(h, {}).get("sim")),
                     "human": _pick(gm.get(h, {}).get("human"))})

    # each candidate against the incumbent it was gated against
    cands = []
    for d in decs:
        si, sc = d.get("sim_metrics", {}).get("incumbent") or {}, d.get("sim_metrics", {}).get("candidate") or {}
        hi, hc = (d.get("human_metrics") or {}).get("incumbent") or {}, (d.get("human_metrics") or {}).get("candidate") or {}

        def delta(a, b, k):
            x, y = a.get(k), b.get(k)
            return y - x if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None
        cands.append({"gen": d["gen"], "idx": d["idx"], "name": d["name"], "hash": d["candidate"], "kind": d["kind"],
                      "accepted": d["accepted"], "incumbent": d["incumbent"], "incumbent_name": d["incumbent_name"],
                      "sim": _pick(sc), "human": _pick(hc) if hc else None,
                      "delta": {k: delta(si, sc, k) for k in ("lie_f1", "macro_f1", "best_lie_f1", "auprc", "ece")},
                      "human_delta": {k: delta(hi, hc, k) for k in ("lie_f1", "macro_f1", "auprc")} if hc else None,
                      "ci": {k: d["sim"].get(k) for k in ("auprc", "ece") if k in d["sim"]}})

    # live F1 from the game records
    games = []
    for gid, (gen, p) in _game_files(run_dir).items():
        if scope != "all" and str(gen) != scope:
            continue
        g = _game_f1(gid, gen, p, run_dir, rule, label)
        if g:
            games.append(g)
    games.sort(key=lambda g: (-1 if g["gen"] == "gate" else g["gen"], g["id"]))
    by_country: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_genome: dict[str, dict] = {}
    overall: dict = {}
    for g in games:
        for p, c in g["counts"].items():
            by_country[p] = _add(by_country.get(p, {}), c)
            mk = g["models"].get(p) or "?"
            by_model[mk] = _add(by_model.get(mk, {}), c)
            gk = g["genome_names"].get(p) or "?"
            by_genome[gk] = _add(by_genome.get(gk, {}), c)
            overall = _add(overall, c)
    run_series, acc = [], {}
    for g in games:   # cumulative across the run, one point per game
        for c in g["counts"].values():
            acc = _add(acc, c)
        run_series.append({"game": g["id"], "gen": g["gen"], **_conf(**acc)})
    live = {
        "rule": rule, "scope": scope, "label": label, "run_label": run_label, "unstable_below": UNSTABLE_N_LIE,
        "unreviewed_games": [g["id"] for g in games if label != "intent" and not g["reviewed"]],
        "overall": _conf(**overall),
        "by_country": {k: _conf(**v) for k, v in sorted(by_country.items())},
        "by_model": {k: _conf(**v) for k, v in sorted(by_model.items())},
        "by_genome": {k: _conf(**v) for k, v in sorted(by_genome.items())},
        "games": [{"id": g["id"], "gen": g["gen"], "status": g["status"], "models": g["models"],
                   "overall": g["series"][-1]["overall"] if g["series"] else _conf(),
                   "by_country": {p: _conf(**c) for p, c in g["counts"].items()},
                   "series": g["series"]} for g in games],
        "run_series": run_series,
        "scopes": ["all"] + sorted({str(gen) for gen, _ in _game_files(run_dir).values()},
                                   key=lambda s: (s != "gate", int(s) if s.isdigit() else -1)),
    }
    return {"threshold_default": 0.5, "genomes": rows, "trajectory": traj, "candidates": cands, "live": live,
            "gen_live": st.get("gen")}


# ---- playbook changelog -----------------------------------------------------------------------------

def ep_changelog(run_dir: Path, query) -> dict:
    genomes = _genomes(run_dir)
    decs = _decisions(run_dir)
    gens = {r["gen"]: r for r in _read_rows(run_dir / "generations.jsonl")}
    props = _proposals(run_dir)
    st = _status(run_dir)
    gm = _genome_metrics(run_dir)
    lin = ep_lineage(run_dir, {})
    nodes = {n["hash"]: n for n in lin["nodes"]}

    def gname(h):
        return (genomes.get(h, {}).get("names") or [h])[0] if h else None

    # per-game evidence ids for each diff key, read from the judge's raw reviews
    def evidence(gen: int) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in _read_rows(run_dir / f"gen_{gen}" / "reviews.jsonl"):
            for d in row.get("diffs", []):
                key = f"{d.get('op')}|{d.get('heuristic_id') or ''}|{_norm(d.get('text'))}"
                out.setdefault(key, []).append({"game": row.get("game_id"), "msg_ids": d.get("evidence_msg_ids", [])})
        return out

    all_gens = sorted({g for g in gens if g >= 1} | set(props) | {d["gen"] for d in decs}
                      | ({st["gen"]} if st.get("gen") else set()))
    entries = []
    for g in all_gens:
        r = gens.get(g, {})
        gd = [d for d in decs if d["gen"] == g]
        p = props.get(g)
        ev = evidence(g) if p else {}
        used = {}
        for d in gd:
            for u in nodes.get(d["candidate"], {}).get("proposals_used", []):
                if u["gen"] == g:
                    used.setdefault(u["key"], []).append(d["name"])
        proposals = []
        if p:
            for x in p["ranked"]:
                proposals.append({**{k: x[k] for k in ("op", "heuristic_id", "text", "rationale", "votes", "games")},
                                  "evidence": ev.get(x["key"], []), "used_by": used.get(x["key"], []),
                                  "lint": "passed"})
            for x in p["rejected"]:
                proposals.append({"op": x.get("op"), "heuristic_id": x.get("heuristic_id"), "text": x.get("text"),
                                  "rationale": x.get("rationale"), "votes": None, "games": [x.get("game")],
                                  "evidence": [{"game": x.get("game"), "msg_ids": x.get("evidence_msg_ids", [])}],
                                  "used_by": [], "lint": "rejected", "lint_errors": x.get("errors", [])})
        elif st.get("gen") == g and st.get("diffs"):   # reviews not on disk yet: the loop's live summary
            proposals = [{"op": x.get("op"), "heuristic_id": x.get("id"), "text": x.get("text"), "rationale": x.get("why"),
                          "votes": x.get("votes"), "games": [], "evidence": [], "used_by": [], "lint": "passed"}
                         for x in st["diffs"]]

        def cand(d):
            si = d.get("sim_metrics", {}).get("incumbent") or {}
            sc = d.get("sim_metrics", {}).get("candidate") or {}
            dl = {k: (sc[k] - si[k]) if isinstance(si.get(k), (int, float)) and isinstance(sc.get(k), (int, float))
                  else None for k in ("lie_f1", "macro_f1", "auprc", "ece")}
            n = nodes.get(d["candidate"], {})
            return {"name": d["name"], "hash": d["candidate"], "kind": d["kind"], "accepted": d["accepted"],
                    "reasons": d["reasons"], "delta": dl, "ci": {k: d["sim"].get(k) for k in ("auprc", "ece") if k in d["sim"]},
                    "human": (d.get("human") or {}).get("auprc"), "sim": _pick(sc),
                    "changes": n.get("diff")}
        accepted = [cand(d) for d in gd if d["accepted"]]
        adopted = r.get("incumbent") if r and r.get("accepted") else None
        entries.append({
            "gen": g, "done": bool(r), "live": st.get("gen") == g and not r,
            "stage": st.get("stage") if st.get("gen") == g and not r else None,
            "parent": {"hash": r.get("parent"), "name": gname(r.get("parent"))} if r.get("parent") else None,
            "incumbent_before": (gd[0]["incumbent"] if gd else r.get("parent")),
            "incumbent_before_name": gname(gd[0]["incumbent"] if gd else r.get("parent")),
            "incumbent_after": r.get("incumbent"), "incumbent_after_name": gname(r.get("incumbent")),
            "adopted": {"hash": adopted, "name": gname(adopted),
                        "sim": _pick(gm.get(adopted, {}).get("sim"))} if adopted else None,
            "accepted": accepted, "candidates": [cand(d) for d in gd],
            "proposals": proposals, "lint_rejected": sum(1 for x in proposals if x["lint"] == "rejected"),
            "diffs_kept": r.get("diffs_kept"), "diffs_rejected": r.get("diffs_rejected"),
        })
    entries.reverse()   # newest first
    return {"entries": entries, "lint_note": lin["lint_note"]}


# ---- standings: winners, leaderboards, model x country rotation matrix ---------------------------------

def ep_standings(run_dir: Path, query) -> dict:
    """rsi.standings.standings() plus: generation per game, a model x country matrix (finished games), and
    F1 on each model row from its own receiver counts (deceptive = self-labelled lie OR broken promise)."""
    from rsi.standings import standings

    files = _game_files(run_dir)
    deps = [p for _, p in files.values()] + [p.with_name(f"review_{gid}.json") for gid, (_, p) in files.items()]

    def build():
        s = standings(run_dir)
        gen_of = {gid: gen for gid, (gen, _) in files.items()}
        games = [{**g, "gen": gen_of.get(g["game_id"])} for g in s["games"] if g["game_id"] in gen_of]
        games.sort(key=lambda g: (-1 if g["gen"] == "gate" else (g["gen"] or 0), g["game_id"]))
        models = sorted({m for g in games for m in g["models"].values() if m})
        countries = sorted({p for g in games for p in g["models"]})
        cells = {m: {c: {"games": 0, "wins": 0.0, "sc": 0} for c in countries} for m in models}
        for g in games:
            if g["status"] != "finished":
                continue
            for p, m in g["models"].items():
                if not m:
                    continue
                c = cells[m][p]
                c["games"] += 1
                c["sc"] += g["final_sc"].get(p, 0)
                if p in g["winners"]:
                    c["wins"] += 1 / len(g["winners"])
        for row in cells.values():
            for c in row.values():
                c["wins"] = round(c["wins"], 2)
                c["win_rate"] = c["wins"] / c["games"] if c["games"] else None
                c["mean_sc"] = c["sc"] / c["games"] if c["games"] else None
        by_model = []
        for r in s["by_model"]:
            judged = r.get("judged", 0)
            f1 = _conf(tp=r.get("caught", 0), fp=r.get("false_alarms", 0), fn=r.get("fooled", 0),
                       tn=max(0, judged - r.get("caught", 0) - r.get("fooled", 0) - r.get("false_alarms", 0)))
            by_model.append({**r, "f1": {k: f1[k] for k in ("n", "n_lie", "precision", "recall", "lie_f1", "macro_f1",
                                                            "unstable")}})
        rotated = any(len(r.get("countries", {})) > 1 for r in s["by_model"])
        return {"games": games, "by_model": by_model, "by_country": s["by_country"],
                "matrix": {"models": models, "countries": countries, "cells": cells}, "rotated": rotated,
                "finished": sum(1 for g in games if g["status"] == "finished"),
                "deception_label": "self-labelled lie or broken promise (judge review)"}
    return _cached(("standings", str(run_dir)), deps + [run_dir / "gate_games"], build)


ENDPOINTS = {"run": ep_run, "gates": ep_gates, "lineage": ep_lineage, "games": ep_games,
             "deception": ep_deception, "review": ep_review, "graph": ep_graph, "f1": ep_f1,
             "changelog": ep_changelog, "standings": ep_standings}


def handle(name: str, query: dict[str, list[str]], run_dir: Path):
    if name == "ping":
        return _json({"ok": True, "run": run_dir.name})
    fn = ENDPOINTS.get(name)
    if fn is None:
        return _json({"error": f"unknown endpoint {name}"}, 404)
    out = fn(run_dir, query)
    return out if isinstance(out, tuple) else _json(out)
