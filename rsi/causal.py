"""Decision-trace graph per phase, built from a privileged game record.

Columns (left -> right) follow the order decisions are made in a movement phase:

    plan r1 -> messages r1 -> judgments r1 -> plan r2 -> messages r2 -> judgments r2 -> orders -> SC change

Rows are the agent-controlled powers. Edges:
    plan      -> message    "wrote"       message composed under that plan (intent = truthful/deceptive)
    message   -> judgment   "judged"      receiver's p_lie / decision on that message
    judgment  -> plan/orders "cited"      the receiver's later reasoning names the sender (evidence quoted)
    plan      -> orders     "kept k/n"    how many planned orders were actually submitted
    orders    -> SC change  "result"

"cited" edges are attributions read off the reasoning traces (the agent says it considered that
sender), not causal identification. They are what a researcher would follow first when asking
"why did this agent act this way?".
"""

from __future__ import annotations

import re

COLS = ["plan 1", "messages 1", "judgments 1", "plan 2", "messages 2", "judgments 2", "orders", "SC change"]


def _sentences_mentioning(text: str, power: str, limit: int = 2) -> list[str]:
    if not text:
        return []
    rx = re.compile(rf"\b{re.escape(power)}\b", re.I)
    out = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if rx.search(s)]
    return [s[:240] for s in out[:limit]]


def phase_graph(rec: dict, phase: str) -> dict:
    active = rec.get("active_powers") or sorted(rec["seating"])
    msgs = [m for m in rec["messages"] if m["phase"] == phase]
    sends = {int(k): v for k, v in rec["sends"].items()}
    judg = {int(k): v for k, v in rec["judgments"].items()}
    rs = rec.get("reasoning", {}).get(phase, {})
    orders = rec.get("orders", {}).get(phase, {})
    nodes, edges = [], []

    def node(nid, col, power, kind, label, detail="", tone=""):
        nodes.append({"id": nid, "col": col, "power": power, "kind": kind, "label": label,
                      "detail": detail, "tone": tone})

    rounds = sorted({m["round"] for m in msgs}) or [1]
    for r in (1, 2):
        pc, mc, jc = (0, 1, 2) if r == 1 else (3, 4, 5)
        for p in active:
            info = rs.get(p, {})
            if info.get(f"send{r}_analysis") or info.get(f"send{r}_plan"):
                node(f"plan{r}:{p}", pc, p, "plan", "plan",
                     f"PLAN: {info.get(f'send{r}_plan', '-')}\n\nANALYSIS: {info.get(f'send{r}_analysis', '')}"
                     f"\n\nTHINKING: {info.get(f'send{r}_thinking', '')[:1500]}")
        for m in (x for x in msgs if x["round"] == r):
            intent = sends.get(m["id"], {}).get("intent", "?")
            node(f"msg:{m['id']}", mc, m["sender"], "message", f"#{m['id']} → {m['recipient'][:3]}",
                 f"{m['sender']} → {m['recipient']} ({intent}):\n{m['text']}", "bad" if intent == "deceptive" else "")
            if any(n["id"] == f"plan{r}:{m['sender']}" for n in nodes):
                edges.append({"from": f"plan{r}:{m['sender']}", "to": f"msg:{m['id']}", "kind": "wrote",
                              "label": intent})
            j = judg.get(m["id"])
            if j:
                lie, distrust = intent == "deceptive", j["decision"] == "distrust"
                tone = "good" if lie and distrust else "bad" if lie else "warn" if distrust else ""
                node(f"judge:{m['id']}", jc, m["recipient"], "judgment", f"p={j['p_lie']:.2f}",
                     f"{m['recipient']} judges #{m['id']} from {m['sender']}: p_lie={j['p_lie']:.2f} "
                     f"{j['decision']}\ncues: {', '.join(j.get('cues', []))}\n\n{j.get('reasoning', '')}", tone)
                edges.append({"from": f"msg:{m['id']}", "to": f"judge:{m['id']}", "kind": "judged",
                              "label": j["decision"]})

    for p in active:
        info = rs.get(p, {})
        if p in orders or info.get("orders_reasoning"):
            node(f"orders:{p}", 6, p, "orders", f"{len(orders.get(p, []))} orders",
                 f"ORDERS: {'; '.join(orders.get(p, [])) or '(none)'}\n\nREASONING: {info.get('orders_reasoning', '')}"
                 f"\n\nTHINKING: {info.get('orders_thinking', '')[:1500]}")
        last_plan = next((f"plan{r}:{p}" for r in (2, 1) if any(n["id"] == f"plan{r}:{p}" for n in nodes)), None)
        if last_plan and f"orders:{p}" in {n["id"] for n in nodes}:
            r = int(last_plan[4])
            planned = [o.strip() for o in info.get(f"send{r}_plan", "").split(";") if o.strip()]
            kept = sum(1 for o in planned if o in orders.get(p, []))
            edges.append({"from": last_plan, "to": f"orders:{p}", "kind": "kept",
                          "label": f"kept {kept}/{len(planned)}"})

    # "cited" edges: a judgment's receiver later reasons about that sender
    for m in msgs:
        j = judg.get(m["id"])
        if not j:
            continue
        recv, snd, r = m["recipient"], m["sender"], m["round"]
        info = rs.get(recv, {})
        targets = []
        if r == 1:
            targets.append((f"plan2:{recv}", info.get("send2_analysis", "") + "\n" + info.get("send2_thinking", "")))
        targets.append((f"orders:{recv}", info.get("orders_reasoning", "") + "\n" + info.get("orders_thinking", "")))
        ids = {n["id"] for n in nodes}
        for tgt, text in targets:
            ev = _sentences_mentioning(text, snd)
            if tgt in ids and ev:
                edges.append({"from": f"judge:{m['id']}", "to": tgt, "kind": "cited", "label": "cites " + snd[:3],
                              "evidence": ev})

    # SC change
    hist = {h["phase"]: h["sc"] for h in rec.get("sc_history", [])}
    phases = [h["phase"] for h in rec.get("sc_history", [])]
    if phase in hist:
        i = phases.index(phase)
        before = rec["sc_history"][i - 1]["sc"] if i > 0 else None
        for p in active:
            after = hist[phase].get(p, 0)
            delta = after - (before.get(p, 0) if before else after)
            node(f"sc:{p}", 7, p, "sc", f"{after} ({delta:+d})" if before else f"{after}",
                 f"{p} supply centres after {phase}: {after}", "good" if delta > 0 else "bad" if delta < 0 else "")
            if f"orders:{p}" in {n["id"] for n in nodes}:
                edges.append({"from": f"orders:{p}", "to": f"sc:{p}", "kind": "result", "label": ""})

    return {"phase": phase, "powers": active, "cols": COLS, "nodes": nodes, "edges": edges}


def game_graphs(rec: dict) -> dict[str, dict]:
    phases = rec.get("phases") or [h["phase"] for h in rec.get("sc_history", [])]
    return {ph: phase_graph(rec, ph) for ph in phases}
