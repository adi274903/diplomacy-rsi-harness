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
const D = JSON.parse(document.getElementById('data').textContent);
const sel = document.getElementById('phase'), img = document.getElementById('map');
const msgs = document.getElementById('msgs'), ords = document.getElementById('orders'), sc = document.getElementById('sc');
D.phases.forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=p;sel.appendChild(o);});
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function show(i){
  i = Math.max(0, Math.min(D.phases.length-1, i)); sel.value = i; const ph = D.phases[i];
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
  sc.innerHTML = '<table><tr><th>Power</th><th>Seat</th><th>SC</th></tr>' + Object.entries(h.sc).sort((a,b)=>b[1]-a[1])
    .map(([p,n]) => `<tr><td>${esc(p)}</td><td class=muted>${esc(D.seats[p]||'')}</td><td>${n}</td></tr>`).join('') + '</table>';
}
sel.onchange = () => show(+sel.value);
document.getElementById('prev').onclick = () => show(+sel.value - 1);
document.getElementById('next').onclick = () => show(+sel.value + 1);
document.addEventListener('keydown', e => { if (e.key==='ArrowLeft') show(+sel.value-1); if (e.key==='ArrowRight') show(+sel.value+1); });
show(0);
"""


def game_summary(rec: dict) -> dict:
    sends = {int(k): v for k, v in rec["sends"].items()}
    js = {int(k): v for k, v in rec["judgments"].items()}
    lies = [m for m in sends if sends[m]["intent"] == "deceptive"]
    caught = [m for m in lies if js.get(m, {}).get("decision") == "distrust"]
    return {"messages": len(rec["messages"]), "lies": len(lies), "caught": len(caught),
            "false_alarms": sum(1 for m, j in js.items() if j["decision"] == "distrust"
                                and sends.get(m, {}).get("intent") == "truthful"),
            "final_phase": rec["final_phase"], "error": rec.get("error"), "leaks": len(rec.get("leak_hits", []))}


def build_game_page(game_json: Path) -> Path:
    rec = json.loads(game_json.read_text())
    sends = {int(k): v for k, v in rec["sends"].items()}
    js = {int(k): v for k, v in rec["judgments"].items()}
    outcomes = {m["id"]: _outcome(sends.get(m["id"], {}).get("intent"), js.get(m["id"])) for m in rec["messages"]}
    phases = rec.get("phases") or [h["phase"] for h in rec["sc_history"]] or [rec["final_phase"]]
    data = {
        "phases": phases, "maps": rec.get("maps_dir"), "messages": rec["messages"], "sends": sends, "judgments": js,
        "outcomes": outcomes, "orders": rec["orders"], "sc_history": rec["sc_history"], "final_sc": rec["final_sc"],
        "seats": {p: f"{rec['genome_names'].get(p, '')} {h}" for p, h in rec["seating"].items()},
    }
    s = game_summary(rec)
    st = rec.get("stats", {})
    invalid = f" · <b class=bad>INVALID: {_e(rec['error'])}</b>" if rec.get("error") else ""
    map_html = ('<img id=map alt="">' if rec.get("maps_dir")
                else '<p class=muted id=map>Maps were not rendered for this game.</p>')
    data_json = json.dumps(data).replace("</", "<\\/")
    body = f"""
<p><a href="{_e(_rel_index(game_json))}">← run overview</a></p>
<h1>Game {_e(rec['game_id'])}</h1>
<p class=muted>Seed {_e(rec['seed'])} · reached {_e(rec['final_phase'])} · privileged view (intent labels and
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
<div class="grid two">
 <div><div class="panel map">{map_html}</div>
  <h2>Supply centres</h2><div class="panel scroll" id=sc></div></div>
 <div><h2 style="margin-top:0">Messages this phase</h2><div class=panel id=msgs></div>
  <h2>Orders</h2><div class="panel scroll" id=orders></div></div>
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
"""


def build_index(run_dir: Path) -> Path:
    global _RUN_ROOT
    _RUN_ROOT = run_dir
    games = sorted(run_dir.rglob("game_*.json"))
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
                     f"<td>{'<span class=bad>invalid</span>' if s['error'] else 'ok'}</td></tr>"
                     for gr, gid, href, s in rows) + "</table></div>")
    out = run_dir / "index.html"
    out.write_text(_page(f"Run {run_dir.name}", "".join(parts), INDEX_JS))
    return out


# ---- live server ------------------------------------------------------------------------------------

def serve(run_dir: Path, port: int, gateways: dict[str, str]) -> None:
    build_index(run_dir)
    lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(run_dir), **kw)

        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
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
            if self.path in ("/", "/index.html"):
                with lock:
                    build_index(run_dir)   # pick up games finished since the last load
            super().do_GET()

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"viewer: http://127.0.0.1:{port}/  (run {run_dir}; gateways: {gateways or 'none'})")
    srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsi.viewer")
    ap.add_argument("command", choices=["build", "serve"])
    ap.add_argument("run_dir")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gateway", action="append", default=[], help="name=http://host:port (repeatable)")
    a = ap.parse_args()
    run_dir = Path(a.run_dir)
    if a.command == "build":
        print(build_index(run_dir))
    else:
        serve(run_dir, a.port, dict(g.split("=", 1) for g in a.gateway))


if __name__ == "__main__":
    main()
