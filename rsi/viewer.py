"""Run + game viewer.

    python -m rsi.viewer build runs/<run>                     # writes runs/<run>/index.html + game pages
    python -m rsi.viewer serve runs/<run> --port 8765 \\
        --gateway player=http://127.0.0.1:9000 --gateway reviewer=http://127.0.0.1:9001

`build` produces static, self-contained pages (open them directly, or zip/share the run folder).
`serve` also rebuilds on every index load and proxies live gateway telemetry (`/stats`) into the
dashboard, refreshed every few seconds.

The game pages show the PRIVILEGED view (sender intent labels, receiver p_lie): they are for
researchers, never for players.
"""

from __future__ import annotations

import argparse
import html
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from rsi.causal import game_graphs
from rsi.replay import is_replay

CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--accent:#2f5bd3;
--good:#1f8a4c;--bad:#c2372f;--warn:#b7791f;--chip:#efefec}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121214;--panel:#1b1b1e;--ink:#ececef;
--muted:#9a9aa2;--line:#2c2c31;--accent:#7f9cf5;--good:#4cc38a;--bad:#f2706a;--warn:#e0b04f;--chip:#26262b}}
:root[data-theme=dark]{--bg:#121214;--panel:#1b1b1e;--ink:#ececef;--muted:#9a9aa2;--line:#2c2c31;--accent:#7f9cf5;
--good:#4cc38a;--bad:#f2706a;--warn:#e0b04f;--chip:#26262b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}a{color:var(--accent)}
.muted{color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.scroll{overflow-x:auto}.grid{display:grid;gap:16px}
@media(min-width:960px){.two{grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)}}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px;margin-right:4px}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi .v{font-size:20px;font-weight:650}.kpi .l{color:var(--muted);font-size:12px}
.msg{border-bottom:1px solid var(--line);padding:8px 0}.msg:last-child{border:0}
.msg .h{font-size:12px;color:var(--muted)}.msg .t{margin:3px 0}
.map img{width:100%;height:auto;border-radius:8px;background:#fff}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
button,select{font:inherit;padding:5px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink)}
details summary{cursor:pointer;color:var(--muted)}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--bad);margin-right:6px;
animation:pulse 1.4s infinite}@keyframes pulse{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.live{animation:none}}
.graphwrap{overflow-x:auto}.graph text{font:11px system-ui,sans-serif;fill:var(--ink)}
.graph .hdr{fill:var(--muted);font-weight:600}.graph .lane{stroke:var(--line)}
.graph .n rect{fill:var(--chip);stroke:var(--line);stroke-width:1.2;cursor:pointer}
.graph .n.good rect{stroke:var(--good);stroke-width:2}.graph .n.bad rect{stroke:var(--bad);stroke-width:2}
.graph .n.warn rect{stroke:var(--warn);stroke-width:2}.graph .n.sel rect{fill:var(--accent);fill-opacity:.18}
.graph path{fill:none;stroke-width:1.4;opacity:.8}.graph .e-wrote{stroke:var(--muted)}
.graph .e-judged{stroke:var(--accent)}.graph .e-cited{stroke:var(--warn);stroke-dasharray:5 4}
.graph .e-kept,.graph .e-result{stroke:var(--muted);stroke-dasharray:2 3}
#gdetail{white-space:pre-wrap;max-height:320px;overflow:auto;font-size:13px}
.legend span{margin-right:14px;font-size:12px;color:var(--muted)}
.act{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:var(--chip);
margin:0 6px 6px 0;font-size:12px}.rsi h3{margin:0 0 6px;font-size:14px}.rsi ol{margin:6px 0 0 18px;padding:0}
.rsi .ev{font-size:12px;color:var(--muted);margin:2px 0}.rsi .grid{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
"""


def _e(x) -> str:
    return html.escape(str(x))


def _f(x, nd=3) -> str:
    return "-" if x is None else f"{x:.{nd}f}" if isinstance(x, float) else _e(x)


def _rows(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line] if p.exists() else []


def _page(title: str, body: str, script: str = "") -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
            f"<style>{CSS}</style></head><body><main>{body}</main><script>{script}</script></body></html>")


def _outcome(intent: str | None, j: dict | None) -> tuple[str, str]:
    if not j or not j.get("ok", True):
        return "no judgment", "muted"
    lie, distrust = intent == "deceptive", j["decision"] == "distrust"
    if lie and distrust:
        return "lie caught", "good"
    if lie:
        return "fooled", "bad"
    if distrust:
        return "false alarm", "warn"
    return "trusted truth", "muted"


# ---- game page ------------------------------------------------------------------------------------

GAME_JS = r"""
let D = JSON.parse(document.getElementById('data').textContent);
const sel = document.getElementById('phase'), img = document.getElementById('map');
const msgs = document.getElementById('msgs'), ords = document.getElementById('orders'), sc = document.getElementById('sc');
function fillPhases(){ sel.innerHTML=''; D.phases.forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=p;sel.appendChild(o);}); }
fillPhases();
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function show(i){
  i = Math.max(0, Math.min(D.phases.length-1, i)); sel.value = i; const ph = D.phases[i]; remember(i);
  drawGraph(D.graphs[ph]);
  if (D.maps) { img.src = D.maps + '/' + ph + '.svg'; img.alt = 'Board at ' + ph; }
  const ms = D.messages.filter(m => m.phase === ph);
  msgs.innerHTML = ms.length ? ms.map(m => {
    const s = D.sends[m.id] || {}, j = D.judgments[m.id], o = D.outcomes[m.id];
    const jl = j ? `p_lie ${j.p_lie.toFixed(2)} · ${esc(j.decision)}` : 'no judgment';
    return `<div class=msg><div class=h>#${m.id} · round ${m.round} · <b>${esc(m.sender)}</b> → <b>${esc(m.recipient)}</b>
      · <span class="chip ${s.intent==='deceptive'?'bad':''}">${esc(s.intent||'?')}</span>
      <span class="chip">${jl}</span> <span class="${o[1]}">${esc(o[0])}</span></div>
      <div class=t>${esc(m.text)}</div>
      ${j && j.reasoning ? `<details><summary>receiver reasoning</summary><div class=muted>${esc(j.reasoning)}</div></details>` : ''}
      ${s.plan ? `<details><summary>sender's plan when sending (private)</summary><div class=muted>${esc(s.plan.join('; ')||'-')}</div></details>` : ''}
    </div>`; }).join('') : '<p class=muted>No messages this phase.</p>';
  const od = D.orders[ph] || {};
  ords.innerHTML = Object.keys(od).length ? '<table><tr><th>Power</th><th>Orders</th></tr>' +
    Object.entries(od).sort().map(([p,o]) => `<tr><td>${esc(p)}</td><td>${esc(o.join('; ')||'(none)')}</td></tr>`).join('') + '</table>'
    : '<p class=muted>No orders recorded (final position).</p>';
  const h = D.sc_history.find(x => x.phase === ph) || {sc: D.final_sc};
  sc.innerHTML = '<table><tr><th>Power</th><th>Model · playbook</th><th>SC</th></tr>' + Object.entries(h.sc).sort((a,b)=>b[1]-a[1])
    .map(([p,n]) => `<tr><td>${esc(p)}</td><td class=muted>${esc(D.seats[p]||'')}</td><td>${n}</td></tr>`).join('') + '</table>';
}
const live = D.status === 'running';
function startIndex(){
  const h = decodeURIComponent(location.hash);
  if (h.startsWith('#p=')) { const i = D.phases.indexOf(h.slice(3)); if (i >= 0) return i; }
  return live || h === '#live' ? D.phases.length - 1 : 0;
}
function remember(i){ history.replaceState(null, '', (live && i === D.phases.length - 1) ? '#live' : '#p=' + D.phases[i]); }
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent){ const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function drawGraph(g){
  const box = document.getElementById('graph'), det = document.getElementById('gdetail');
  box.innerHTML = ''; det.textContent = 'Click a node to read the agent\'s plan, message, judgment or orders reasoning.';
  if (!g || !g.nodes.length) { box.innerHTML = '<p class=muted>No decisions recorded for this phase yet.</p>'; return; }
  const colW = 128, nodeW = 108, nodeH = 24, gap = 4, padL = 84, padT = 30;
  const stack = {}; g.nodes.forEach(n => { const k = n.col + '|' + n.power; n.k = stack[k] = (stack[k] || 0) + 1; });
  const laneH = g.powers.map(p => Math.max(56, Math.max(0, ...g.nodes.filter(n => n.power === p).map(n => n.k)) * (nodeH + gap) + 18));
  const laneY = []; laneH.reduce((y, h, i) => (laneY[i] = y, y + h), padT);
  const W = padL + g.cols.length * colW, H = laneY[laneY.length - 1] + laneH[laneH.length - 1] + 6;
  const svg = el('svg', {class: 'graph', width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img',
                        'aria-label': 'Decision-trace graph for ' + g.phase}, box);
  const defs = el('defs', {}, svg), mk = el('marker', {id: 'arr', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse'}, defs);
  el('path', {d: 'M0,0 L8,4 L0,8 z', style: 'fill:var(--muted);stroke:none'}, mk);
  g.cols.forEach((c, i) => { const t = el('text', {x: padL + i * colW + 4, y: 16, class: 'hdr'}, svg); t.textContent = c; });
  g.powers.forEach((p, i) => { el('line', {x1: 0, x2: W, y1: laneY[i] - 6, y2: laneY[i] - 6, class: 'lane'}, svg);
    const t = el('text', {x: 4, y: laneY[i] + 14, class: 'hdr'}, svg); t.textContent = p; });
  const pos = {}; g.nodes.forEach(n => { pos[n.id] = {x: padL + n.col * colW, y: laneY[g.powers.indexOf(n.power)] + (n.k - 1) * (nodeH + gap)}; });
  const incoming = {};
  g.edges.forEach(e => { const a = pos[e.from], b = pos[e.to]; if (!a || !b) return;
    (incoming[e.to] = incoming[e.to] || []).push(e);
    const x1 = a.x + nodeW, y1 = a.y + nodeH / 2, x2 = b.x - 2, y2 = b.y + nodeH / 2, dx = Math.max(30, (x2 - x1) / 2);
    const path = el('path', {d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`, class: 'e-' + e.kind, 'marker-end': 'url(#arr)'}, svg);
    const tt = el('title', {}, path); tt.textContent = e.kind + (e.label ? ': ' + e.label : '') + (e.evidence ? '\n' + e.evidence.join('\n') : ''); });
  g.nodes.forEach(n => { const p = pos[n.id], gEl = el('g', {class: 'n ' + (n.tone || ''), transform: `translate(${p.x},${p.y})`, tabindex: 0}, svg);
    el('rect', {width: nodeW, height: nodeH, rx: 6}, gEl);
    const t = el('text', {x: 7, y: 16}, gEl); t.textContent = n.kind === 'plan' ? 'plan' : n.label;
    const pick = () => { svg.querySelectorAll('.n.sel').forEach(x => x.classList.remove('sel')); gEl.classList.add('sel');
      const ev = (incoming[n.id] || []).filter(e => e.evidence).map(e => `← ${e.label} (from ${e.from}):\n  "${e.evidence.join('"\n  "')}"`);
      det.textContent = n.detail + (ev.length ? '\n\nCITED EARLIER JUDGMENTS:\n' + ev.join('\n') : ''); };
    gEl.addEventListener('click', pick); gEl.addEventListener('keydown', e => { if (e.key === 'Enter') pick(); }); });
}
sel.onchange = () => show(+sel.value);
document.getElementById('prev').onclick = () => show(+sel.value - 1);
document.getElementById('next').onclick = () => show(+sel.value + 1);
document.addEventListener('keydown', e => { if (e.key==='ArrowLeft') show(+sel.value-1); if (e.key==='ArrowRight') show(+sel.value+1); });
function renderFeed(){
  const f = document.getElementById('feed'); if (!f) return;
  f.innerHTML = D.feed.length ? D.feed.map(m => `<div class=msg><div class=h>${esc(m.phase)} · <b>${esc(m.sender)}</b>
    <span class=muted>(${esc(D.models[m.sender]||'')})</span> → <b>${esc(m.recipient)}</b>
    · <span class="chip ${m.intent==='deceptive'?'bad':''}">${esc(m.intent||'?')}</span>
    ${m.p_lie===null ? '<span class=muted>awaiting judgment…</span>' : `<span class=chip>p_lie ${m.p_lie.toFixed(2)}</span> <span class="${m.outcome[1]}">${esc(m.outcome[0])}</span>`}</div>
    <div class=t>${esc(m.text)}</div></div>`).join('') : '<p class=muted>Waiting for the first messages…</p>';
}
const esc2 = s => esc(s == null ? '' : s);
function renderActivity(){
  const a = document.getElementById('activity'); if (!a) return;
  const e = Object.entries(D.activity || {});
  a.innerHTML = e.length ? e.map(([p, w]) => `<span class=act><span class=live></span><b>${esc(p)}</b>
    <span class=muted>${esc2((D.models[p]||'').split('/').pop())}</span> ${esc(w)}…</span>`).join('')
    : (D.status === 'running' ? '<span class="act muted">adjudicating…</span>' : '');
}
function renderRSI(r){
  const box = document.getElementById('rsi'); if (!r || !r.stage) return; box.hidden = false;
  const pb = r.incumbent || {}, diffs = r.diffs || [], ev = r.events || [];
  box.innerHTML = `<div class=grid>
   <div><h3>RSI · ${esc(r.stage)}${r.gen ? ' · gen ' + r.gen : ''}</h3>
     <div class=muted style="font-size:12px">current playbook <b>${esc2(pb.name)}</b> (${esc2(pb.hash)}) · ${(pb.heuristics||[]).length} heuristics · ${esc2(pb.exemplars)} exemplars · judge effort ${esc2(pb.judge_effort)}</div>
     ${ev.slice(0, 6).map(e => `<div class=ev>${esc(e.t)} · ${esc(e.text)}</div>`).join('')}</div>
   <div><h3>Playbook in use</h3><ol>${(pb.heuristics||[]).map(h => `<li title="${esc(h.id)}" style="font-size:12px">${esc(h.text)}</li>`).join('')}</ol></div>
   ${diffs.length ? `<div><h3>Judge's proposed edits (latest)</h3>${diffs.map(d => `<div class=ev><span class=chip>${esc(d.op)}${d.id ? ' ' + esc(d.id) : ''} ×${d.votes}</span>${esc2(d.text)}</div>`).join('')}</div>` : ''}
  </div>`;
}
async function rsi(){ try { const r = await fetch('/api/rsi'); if (r.ok) renderRSI(await r.json()); } catch (e) {} }
const followRun = new URLSearchParams(location.search).has('follow');
async function waitNext(){
  try { const c = await (await fetch('/api/current')).json(); await rsi();
    if (c.path && c.path !== location.pathname) { location.href = c.path + '?follow=1#live'; return; } } catch (e) {}
  setTimeout(waitNext, 3000);
}
renderFeed(); renderActivity(); rsi();
show(startIndex());
// Live: poll the server for the latest checkpoint and re-render in place (no page reload).
async function refresh(){
  try {
    const r = await fetch('/api/game?f=' + encodeURIComponent(location.pathname));
    if (!r.ok) throw 0;
    const nd = await r.json();
    const follow = +sel.value === D.phases.length - 1, cur = D.phases[+sel.value];
    D = nd; fillPhases(); renderFeed(); renderActivity(); rsi();
    show(follow ? D.phases.length - 1 : Math.max(0, D.phases.indexOf(cur)));
    if (D.status === 'running') setTimeout(refresh, 2000);
    else if (followRun) waitNext();     // follow the run into its next game
    else location.reload();
  } catch (e) { setTimeout(() => location.reload(), 5000); }
}
if (live) setTimeout(refresh, 2000); else if (followRun) waitNext();
"""


def game_summary(rec: dict) -> dict:
    sends = {int(k): v for k, v in rec["sends"].items()}
    js = {int(k): v for k, v in rec["judgments"].items()}
    lies = [m for m in sends if sends[m]["intent"] == "deceptive"]
    caught = [m for m in lies if js.get(m, {}).get("decision") == "distrust"]
    return {"messages": len(rec["messages"]), "lies": len(lies), "caught": len(caught),
            "false_alarms": sum(1 for m, j in js.items() if j["decision"] == "distrust"
                                and sends.get(m, {}).get("intent") == "truthful"),
            "final_phase": rec["final_phase"], "error": rec.get("error"), "leaks": len(rec.get("leak_hits", [])),
            "status": rec.get("status", "finished")}


def game_data(rec: dict) -> dict:
    """Everything the game page renders; also served live by `/api/game`."""
    sends = {int(k): v for k, v in rec["sends"].items()}
    js = {int(k): v for k, v in rec["judgments"].items()}
    outcomes = {m["id"]: _outcome(sends.get(m["id"], {}).get("intent"), js.get(m["id"])) for m in rec["messages"]}
    phases = rec.get("phases") or [h["phase"] for h in rec["sc_history"]] or [rec["final_phase"]]
    models = rec.get("models", {})
    feed = [{"id": m["id"], "phase": m["phase"], "sender": m["sender"], "recipient": m["recipient"], "text": m["text"],
             "intent": sends.get(m["id"], {}).get("intent"), "outcome": outcomes[m["id"]],
             "p_lie": js[m["id"]]["p_lie"] if m["id"] in js else None}
            for m in rec["messages"][-14:]][::-1]
    return {
        "status": rec.get("status", "finished"), "graphs": game_graphs(rec), "feed": feed, "models": models,
        "activity": rec.get("activity", {}), "game_id": rec["game_id"],
        "phases": phases, "maps": rec.get("maps_dir"), "messages": rec["messages"], "sends": sends, "judgments": js,
        "outcomes": outcomes, "orders": rec["orders"], "sc_history": rec["sc_history"], "final_sc": rec["final_sc"],
        "seats": {p: (models.get(p) or "") + " · " + rec["genome_names"].get(p, "") for p in rec["seating"]},
        "summary": game_summary(rec),
    }


def build_game_page(game_json: Path) -> Path:
    rec = json.loads(game_json.read_text())
    data = game_data(rec)
    s = game_summary(rec)
    st = rec.get("stats", {})
    invalid = f" · <b class=bad>INVALID: {_e(rec['error'])}</b>" if rec.get("error") else ""
    map_html = ('<img id=map alt="">' if rec.get("maps_dir")
                else '<p class=muted id=map>Maps were not rendered for this game.</p>')
    data_json = json.dumps(data).replace("</", "<\\/")
    body = f"""
<p><a href="{_e(_rel_index(game_json))}">← run overview</a></p>
<h1>{'<span class=live title="live"></span>' if rec.get('status') == 'running' else ''}Game {_e(rec['game_id'])}</h1>
<p class=muted>Seed {_e(rec['seed'])} · {_e(rec.get('map', 'standard'))} map · agents: {_e(', '.join(rec.get('active_powers') or sorted(rec['seating'])))} · {'playing ' if rec.get('status') == 'running' else 'reached '}{_e(rec['final_phase'])} · privileged view (intent labels and
receiver judgments are hidden from players during play){invalid}</p>
<div class=kpis>
 <div class="panel kpi"><div class=v>{s['messages']}</div><div class=l>messages</div></div>
 <div class="panel kpi"><div class=v>{s['lies']}</div><div class=l>self-labelled lies</div></div>
 <div class="panel kpi"><div class="v good">{s['caught']}</div><div class=l>lies caught</div></div>
 <div class="panel kpi"><div class="v warn">{s['false_alarms']}</div><div class=l>false alarms</div></div>
 <div class="panel kpi"><div class=v>{_e(st.get('calls', '-'))}</div><div class=l>model calls ({_e(st.get('failed_calls', 0))} failed)</div></div>
 <div class="panel kpi"><div class=v>{_e(s['leaks'])}</div><div class=l>isolation leaks</div></div>
</div>
<div class=controls style="margin-top:16px"><button id=prev aria-label="Previous phase">←</button>
<select id=phase aria-label="Phase"></select><button id=next aria-label="Next phase">→</button>
<span class=muted>arrow keys step through phases</span></div>
<div class="panel rsi" id=rsi hidden style="margin-top:12px"></div>
{'<h2>Live feed</h2><div id=activity></div><div class=panel id=feed></div>' if rec.get('status') == 'running' else ''}
<div class="grid two">
 <div><div class="panel map">{map_html}</div>
  <h2>Supply centres</h2><div class="panel scroll" id=sc></div></div>
 <div><h2 style="margin-top:0">Messages this phase</h2><div class=panel id=msgs></div>
  <h2>Orders</h2><div class="panel scroll" id=orders></div></div>
</div>
<h2>Decision-trace graph</h2>
<div class=panel>
 <p class="muted legend"><span>── wrote / kept / result</span><span style="color:var(--accent)">── judged</span>
 <span style="color:var(--warn)">- - cited (receiver's later reasoning names the sender)</span>
 <span style="color:var(--good)">▢ lie caught</span><span style="color:var(--bad)">▢ deceptive / fooled</span><span style="color:var(--warn)">▢ false alarm</span></p>
 <div class=graphwrap id=graph></div>
 <p class=muted style="font-size:12px">Links are attributed from the agents' own reasoning traces, not causally identified.</p>
 <div class=panel id=gdetail style="margin-top:10px"></div>
</div>
<script type=application/json id=data>{data_json}</script>"""
    out = game_json.with_suffix(".html")
    out.write_text(_page(f"Game {rec['game_id']}", body, GAME_JS))
    return out


_RUN_ROOT: Path | None = None


def _rel_index(game_json: Path) -> str:
    if _RUN_ROOT is None:
        return "index.html"
    depth = len(game_json.parent.relative_to(_RUN_ROOT).parts)
    return "../" * depth + "index.html"


# ---- run index ------------------------------------------------------------------------------------

INDEX_JS = r"""
async function poll(){
  const box = document.getElementById('live'); if (!box) return;
  try {
    const r = await fetch('api/stats'); if (!r.ok) throw 0; const all = await r.json();
    if (!Object.keys(all).length) return;   // served without --gateway: nothing live to show
    box.innerHTML = Object.entries(all).map(([name, s]) => s.error ? `<p><b>${name}</b>: <span class=bad>${s.error}</span></p>` :
      `<h3 style="margin:6px 0">${name}</h3><div class=kpis>
       <div class="panel kpi"><div class=v>${s.requests}</div><div class=l>requests (${s.cache_hits} cache hits)</div></div>
       <div class="panel kpi"><div class=v>$${(s.est_cost_usd||0).toFixed(2)}</div><div class=l>est. cost</div></div>
       <div class="panel kpi"><div class=v>${(s.prompt_tokens/1e6).toFixed(2)}M / ${(s.completion_tokens/1e6).toFixed(2)}M</div><div class=l>prompt / completion tokens</div></div>
       <div class="panel kpi"><div class=v>${s.latency_ms.high.p50} / ${s.latency_ms.high.p99} ms</div><div class=l>game-turn latency p50 / p99</div></div>
       <div class="panel kpi"><div class=v>${s.queued.high + s.queued.normal + s.queued.low}</div><div class=l>queued (${s.free_slots} free slots)</div></div>
       <div class="panel kpi"><div class=v>${s.retries} / ${s.upstream_errors}</div><div class=l>retries / upstream errors</div></div></div>`).join('');
    document.getElementById('live-wrap').hidden = false;
  } catch (e) { /* static mode: no live telemetry */ }
}
poll(); setInterval(poll, 5000);
if (document.querySelector('td .live')) setTimeout(() => location.reload(), 8000);   // games still running
"""


def build_index(run_dir: Path) -> Path:
    global _RUN_ROOT
    _RUN_ROOT = run_dir
    games = sorted(g for g in run_dir.rglob("game_*.json") if not is_replay(g, run_dir))
    rows = []
    for g in games:
        page = build_game_page(g)
        rec = json.loads(g.read_text())
        s = game_summary(rec)
        group = g.parent.relative_to(run_dir).as_posix()
        rows.append((group, rec["game_id"], page.relative_to(run_dir).as_posix(), s))

    gens = _rows(run_dir / "generations.jsonl")
    gates = _rows(run_dir / "gate_log.jsonl")
    final = json.loads((run_dir / "final_test.json").read_text()) if (run_dir / "final_test.json").exists() else None

    parts = [f"<h1>Run {_e(run_dir.name)}</h1><p class=muted>Self-play RSI · playbook evolution only · "
             f"{len(games)} games</p>",
             "<div id=live-wrap hidden><h2>Live telemetry (gateways)</h2><div id=live></div></div>"]
    if gens:
        g0 = gens[0].get("sim_gate", {})
        parts.append(f"<h2>Baseline v0 on the self-play gate</h2><div class=kpis>"
                     f"<div class='panel kpi'><div class=v>{_f(g0.get('auprc'))}</div><div class=l>AUPRC</div></div>"
                     f"<div class='panel kpi'><div class=v>{_f(g0.get('ece'))}</div><div class=l>ECE</div></div>"
                     f"<div class='panel kpi'><div class=v>{_e(g0.get('n_lie', '-'))}/{_e(g0.get('n', '-'))}</div>"
                     f"<div class=l>lies / items</div></div></div>")
        parts.append("<h2>Generations</h2><div class='panel scroll'><table><tr><th>Gen</th><th>Parent</th>"
                     "<th>Games</th><th>Diffs kept / rejected</th><th>Candidates</th><th>Accepted</th>"
                     "<th>Leak flags</th><th>Wall</th></tr>" + "".join(
                         f"<tr><td>{g['gen']}</td><td>{_e(g['parent'])}</td><td>{g['games']}</td>"
                         f"<td>{g['diffs_kept']} / {g['diffs_rejected']}</td><td>{g['candidates']}</td>"
                         f"<td>{_e(', '.join(g['accepted']) or '-')}</td><td>{g['leak_flags']}</td>"
                         f"<td>{_f(g['wall_s'], 0)}s</td></tr>" for g in gens[1:]) + "</table></div>")
    if gates:
        parts.append("<h2>Gate decisions</h2><div class='panel scroll'><table><tr><th>Candidate</th><th>Result</th>"
                     "<th>Δ sim AUPRC [95% CI]</th><th>p_gain</th><th>Δ ECE</th><th>Δ human-val AUPRC</th>"
                     "<th>Reasons</th></tr>" + "".join(
                         f"<tr><td>{_e(d['candidate'])}</td><td class={'good' if d['accepted'] else 'muted'}>"
                         f"{'accepted' if d['accepted'] else 'rejected'}</td>"
                         f"<td>{_f(d['sim']['auprc']['delta'])} [{_f(d['sim']['auprc']['lo'])}, {_f(d['sim']['auprc']['hi'])}]</td>"
                         f"<td>{_f(d['sim']['auprc']['p_gain'], 2)}</td><td>{_f(d['sim']['ece']['delta'])}</td>"
                         f"<td>{_f(d.get('human', {}).get('auprc', {}).get('delta'))}</td>"
                         f"<td class=muted>{_e('; '.join(d['reasons']))}</td></tr>" for d in gates) + "</table></div>")
    if final:
        parts.append("<h2>Human test (run once)</h2><div class='panel scroll'><table><tr><th>Genome</th><th>Split</th>"
                     "<th>AUPRC</th><th>Lie-F1</th><th>ECE → calibrated</th></tr>" + "".join(
                         f"<tr><td>{_e(n)}</td><td>{sp}</td><td>{_f(r[sp].get('auprc'))}</td><td>{_f(r[sp].get('lie_f1'))}</td>"
                         f"<td>{_f(r[sp].get('ece'))} → {_f(r[sp + '_calibrated'].get('ece'))}</td></tr>"
                         for n, r in final["results"].items() for sp in ("test", "test_paraphrased")) + "</table></div>")
    parts.append("<h2>Games</h2><div class='panel scroll'><table><tr><th>Group</th><th>Game</th><th>Reached</th>"
                 "<th>Messages</th><th>Lies</th><th>Caught</th><th>False alarms</th><th>Status</th></tr>" + "".join(
                     f"<tr><td class=muted>{_e(gr)}</td><td><a href='{_e(href)}'>{_e(gid)}</a></td><td>{_e(s['final_phase'])}</td>"
                     f"<td>{s['messages']}</td><td>{s['lies']}</td><td class=good>{s['caught']}</td>"
                     f"<td class=warn>{s['false_alarms']}</td>"
                     f"<td>{'<span class=bad>invalid</span>' if s['error'] else '<span class=live></span>playing' if s['status'] == 'running' else 'finished'}</td></tr>"
                     for gr, gid, href, s in rows) + "</table></div>")
    out = run_dir / "index.html"
    out.write_text(_page(f"Run {run_dir.name}", "".join(parts), INDEX_JS))
    return out


# ---- live server ------------------------------------------------------------------------------------

def _current_game(run_dir: Path) -> str | None:
    """URL path of the game being played now (else the most recent one), for /live follow mode."""
    games = sorted(run_dir.rglob("game_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    # a featured game (game_g00 in cadence runs) keeps /live on ONE game while many run in parallel
    games.sort(key=lambda p: not p.stem.endswith("g00"))
    for g in games:
        try:
            if json.loads(g.read_text()).get("status") == "running":
                if not g.with_suffix(".html").exists():
                    build_game_page(g)
                return "/" + g.with_suffix(".html").relative_to(run_dir).as_posix()
        except (ValueError, OSError):
            continue
    if games:
        if not games[0].with_suffix(".html").exists():
            build_game_page(games[0])
        return "/" + games[0].with_suffix(".html").relative_to(run_dir).as_posix()
    return None


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def serve(run_dir: Path, port: int, gateways: dict[str, str], dev: bool = False, host: str = "127.0.0.1") -> None:
    build_index(run_dir)
    lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(run_dir), **kw)

        def log_message(self, *a):  # quiet
            pass

        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            # /ui/<app>/...: front-end apps in <repo>/web (served from disk, so edits show on reload)
            if path == "/ui" or path.startswith("/ui/"):
                rel = path[len("/ui/"):] or ""
                target = (WEB_ROOT / rel).resolve()
                if target.is_dir():
                    target = target / "index.html"
                if not target.is_relative_to(WEB_ROOT.resolve()) or not target.exists():
                    return self._send(404, "text/plain", b"not found")
                ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                         ".svg": "image/svg+xml", ".json": "application/json"}.get(target.suffix, "application/octet-stream")
                return self._send(200, ctype, target.read_bytes())
            # /api/x/<name>: extension endpoints in rsi/webapi.py (re-imported per request in --dev mode)
            if path.startswith("/api/x/"):
                import importlib

                from rsi import webapi
                if dev:
                    importlib.reload(webapi)
                from urllib.parse import parse_qs, urlparse
                try:
                    status, ctype, body = webapi.handle(path[len("/api/x/"):], parse_qs(urlparse(self.path).query),
                                                        run_dir)
                except Exception as e:  # noqa: BLE001 - surface backend errors to the UI developer
                    status, ctype, body = 500, "application/json", json.dumps({"error": repr(e)}).encode()
                return self._send(status, ctype, body)
            if path == "/api/standings":
                from rsi.standings import standings
                return self._send(200, "application/json", json.dumps(standings(run_dir)).encode())
            if self.path.startswith("/api/rsi") or self.path.startswith("/api/current") or \
                    self.path.split("?")[0] == "/live":
                cur = _current_game(run_dir)
                if self.path.split("?")[0] == "/live":
                    self.send_response(302)
                    self.send_header("location", (cur + "?follow=1#live") if cur else "/index.html")
                    self.end_headers()
                    return
                if self.path.startswith("/api/rsi"):
                    p = run_dir / "rsi_status.json"
                    body = p.read_bytes() if p.exists() else b"{}"
                else:
                    body = json.dumps({"path": cur}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/game"):
                from urllib.parse import parse_qs, urlparse

                f = parse_qs(urlparse(self.path).query).get("f", [""])[0]
                src = (run_dir / f.lstrip("/")).with_suffix(".json")
                try:
                    if not (src.resolve().is_relative_to(run_dir.resolve()) and src.name.startswith("game_")):
                        raise FileNotFoundError
                    d = game_data(json.loads(src.read_text()))
                    d["dir"] = "/" + src.parent.relative_to(run_dir.resolve() if src.is_absolute() else run_dir).as_posix()
                    body = json.dumps(d).encode()
                    self.send_response(200)
                except (OSError, ValueError, KeyError):
                    body = b"{}"
                    self.send_response(404)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/stats"):
                out = {}
                for name, url in gateways.items():
                    try:
                        out[name] = httpx.get(url.rstrip("/") + "/stats", timeout=2).json()
                    except Exception as e:  # noqa: BLE001
                        out[name] = {"error": str(e)[:200]}
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            path = self.path.split("?")[0].split("#")[0]
            if path in ("/", "/index.html"):
                with lock:
                    build_index(run_dir)   # pick up new / progressing games
            elif path.endswith(".html"):
                src = (run_dir / path.lstrip("/")).with_suffix(".json")
                if src.name.startswith("game_") and src.exists():
                    with lock:
                        try:
                            build_game_page(src)   # live: re-render from the latest checkpoint
                        except (ValueError, KeyError):
                            pass
            super().do_GET()

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"viewer: http://127.0.0.1:{port}/  ·  live game: http://127.0.0.1:{port}/live  "
          f"(run {run_dir}; gateways: {gateways or 'none'})")
    srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.viewer")
    ap.add_argument("command", choices=["build", "serve"])
    ap.add_argument("run_dir")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gateway", action="append", default=[], help="name=http://host:port (repeatable)")
    ap.add_argument("--dev", action="store_true", help="re-import rsi/webapi.py on every /api/x request")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 inside a container)")
    a = ap.parse_args()
    run_dir = Path(a.run_dir)
    if a.command == "build":
        print(build_index(run_dir))
    else:
        serve(run_dir, a.port, dict(g.split("=", 1) for g in a.gateway), dev=a.dev, host=a.host)


if __name__ == "__main__":
    main()
