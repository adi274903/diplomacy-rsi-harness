// Small SVG charts for the RSI monitor, drawn to the dataviz rules: thin marks, 2px lines, >= 8px
// markers with a 2px surface ring, 4px rounded data ends, hairline grids, one hue per chart, text in
// text tokens only, a legend for two or more series, and a hover tooltip on every mark.
(() => {
  const NS = 'http://www.w3.org/2000/svg', MINUS = '−';
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const txt = (parent, s, attrs) => { const t = el('text', attrs, parent); t.textContent = s; return t; };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fin = (v) => typeof v === 'number' && Number.isFinite(v);
  const fmt = {
    dec: (d) => (v) => (fin(v) ? v.toFixed(d) : '—'),
    signed: (d) => (v) => (!fin(v) ? '—' : (v > 0 ? '+' : v < 0 ? MINUS : '±') + Math.abs(v).toFixed(d)),
    pct: (v) => (fin(v) ? Math.round(v * 100) + '%' : '—'),
  };

  function ticks(min, max, count = 4) {
    if (!(max > min)) { max = min + 1; }
    const raw = (max - min) / count, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
    const lo = Math.floor(min / step + 1e-9) * step, hi = Math.ceil(max / step - 1e-9) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  // ---- tooltip -------------------------------------------------------------------------------------
  const tip = () => document.getElementById('tip');
  function showTip(html, evt) {
    const t = tip(); if (!t) return;
    t.innerHTML = html; t.hidden = false;
    const r = t.getBoundingClientRect();
    let x, y;
    if (evt && 'clientX' in evt && evt.clientX) { x = evt.clientX + 14; y = evt.clientY + 14; }
    else { const b = evt.target.getBoundingClientRect(); x = b.right + 8; y = b.top; }
    if (x + r.width > innerWidth - 8) x = Math.max(8, x - r.width - 28);
    if (y + r.height > innerHeight - 8) y = Math.max(8, innerHeight - r.height - 8);
    t.style.left = x + 'px'; t.style.top = y + 'px';
  }
  const hideTip = () => { const t = tip(); if (t) t.hidden = true; };
  function hover(node, html) {
    node.addEventListener('pointermove', (e) => showTip(typeof html === 'function' ? html() : html, e));
    node.addEventListener('pointerleave', hideTip);
    node.addEventListener('focus', (e) => showTip(typeof html === 'function' ? html() : html, e));
    node.addEventListener('blur', hideTip);
  }

  // ---- legend: each swatch is its mark scaled down ----------------------------------------------------
  function legend(target, items) {
    target.innerHTML = '';
    for (const it of items) {
      const span = document.createElement('span');
      const s = el('svg', { viewBox: '0 0 16 12', 'aria-hidden': 'true' }, span);
      if (it.mark === 'line') el('line', { x1: 1, x2: 15, y1: 6, y2: 6, stroke: it.color, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-dasharray': it.dash || null }, s);
      else if (it.mark === 'ring') el('circle', { cx: 8, cy: 6, r: 4, fill: 'var(--surface)', stroke: it.color, 'stroke-width': 1.5 }, s);
      else if (it.mark === 'dot') el('circle', { cx: 8, cy: 6, r: 4, fill: it.color }, s);
      else if (it.mark === 'ci') { el('line', { x1: 1, x2: 15, y1: 6, y2: 6, stroke: it.color, 'stroke-width': 2, 'stroke-linecap': 'round' }, s); el('circle', { cx: 8, cy: 6, r: 3.5, fill: it.color, stroke: 'var(--surface)', 'stroke-width': 1.5 }, s); }
      else if (it.mark === 'tick') el('line', { x1: 8, x2: 8, y1: 0, y2: 12, stroke: it.color, 'stroke-width': 1.5 }, s);
      else if (it.mark === 'box') el('rect', { x: 1, y: 1, width: 14, height: 10, rx: 2, fill: 'var(--surface)', stroke: it.color, 'stroke-width': it.width || 1.5, 'stroke-dasharray': it.dash || null }, s);
      else el('rect', { x: 1, y: 2, width: 14, height: 8, rx: 2, fill: it.color }, s);
      span.append(it.label);
      target.appendChild(span);
    }
  }

  // ---- generation chart: incumbent as a step line, candidates as rings / dots ---------------------------
  // opts: { gens: [0..N], line: [{x, y}], dots: [{x, y, accepted, label}], format, empty }
  function genChart(target, opts) {
    target.innerHTML = '';
    const pts = [...opts.line, ...opts.dots].filter((p) => fin(p.y));
    if (!pts.length) { target.innerHTML = `<p class="empty">${opts.empty || 'No values yet.'}</p>`; return; }
    const W = Math.max(280, target.clientWidth || 480), H = 200, m = { l: 44, r: 16, t: 10, b: 28 };
    const ys = pts.map((p) => p.y);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    const pad = Math.max((hi - lo) * 0.15, 0.01); lo = Math.max(0, lo - pad); hi = hi + pad;
    const yt = ticks(lo, hi, 4), y0 = yt[0], y1 = yt[yt.length - 1];
    const gmax = Math.max(1, ...opts.gens);
    const X = (g) => m.l + (g / gmax) * (W - m.l - m.r), Y = (v) => m.t + (1 - (v - y0) / (y1 - y0)) * (H - m.t - m.b);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': opts.aria || '' }, target);
    for (const t of yt) {
      el('line', { x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), class: t === y0 ? 'axisline' : 'gridline' }, svg);
      txt(svg, opts.format(t), { x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end', class: 'tick' });
    }
    const step = Math.max(1, Math.ceil(gmax / 10));
    for (let g = 0; g <= gmax; g += step) txt(svg, g === 0 ? 'Base' : 'G' + g, { x: X(g), y: H - 8, 'text-anchor': 'middle', class: 'tick' });
    // incumbent step line
    const ln = opts.line.filter((p) => fin(p.y));
    if (ln.length) {
      let d = `M${X(ln[0].x)},${Y(ln[0].y)}`;
      for (let i = 1; i < ln.length; i++) d += ` H${X(ln[i].x)} V${Y(ln[i].y)}`;
      el('path', { d, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
      const last = ln[ln.length - 1];
      el('circle', { cx: X(last.x), cy: Y(last.y), r: 4, fill: 'var(--accent)', stroke: 'var(--surface)', 'stroke-width': 2 }, svg);
      txt(svg, opts.format(last.y), { x: Math.min(X(last.x) + 8, W - 4), y: Y(last.y) - 12, 'text-anchor': X(last.x) + 40 > W ? 'end' : 'start', style: 'fill:var(--text)' });
      for (const p of ln) {
        const h = el('circle', { cx: X(p.x), cy: Y(p.y), r: 10, class: 'hit', tabindex: 0, 'aria-label': `${p.label}: ${opts.format(p.y)}` }, svg);
        hover(h, `<b>${esc(p.label)}</b><br>${esc(opts.name)} ${opts.format(p.y)}`);
      }
    }
    // candidates, jittered within their generation so equal values stay visible
    const byGen = {};
    for (const p of opts.dots.filter((p) => fin(p.y))) (byGen[p.x] ||= []).push(p);
    for (const [g, list] of Object.entries(byGen)) {
      list.forEach((p, i) => {
        const cx = X(+g) + (i - (list.length - 1) / 2) * 7, cy = Y(p.y);
        el('circle', p.accepted ? { cx, cy, r: 4, fill: 'var(--pos-mark)', stroke: 'var(--surface)', 'stroke-width': 2 }
          : { cx, cy, r: 4, fill: 'var(--surface)', stroke: 'var(--ink-mark)', 'stroke-width': 1.5, opacity: 0.75 }, svg);
        const h = el('circle', { cx, cy, r: 9, class: 'hit', tabindex: 0, 'aria-label': `${p.label}: ${opts.format(p.y)}` }, svg);
        hover(h, `<b>${esc(p.label)}</b> <span class="t-muted">G${g}</span><br>${esc(opts.name)} ${opts.format(p.y)} · ${p.accepted ? 'accepted' : 'rejected'}`);
      });
    }
  }

  // ---- forest plot of gate decisions: HTML rows, a shared x-scale ------------------------------------
  // decisions: [{gen, name, kind, accepted, reasons, sim: {auprc: {delta, lo, hi, p_gain}, ece: {...}}}]
  function forest(target, decisions, opts) {
    target.innerHTML = '';
    if (!decisions.length) { target.innerHTML = `<p class="empty">${opts.empty}</p>`; return; }
    const vals = decisions.flatMap((d) => [d.sim.auprc?.lo, d.sim.auprc?.hi, d.sim.auprc?.delta]).filter(fin);
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    if (hi - lo < 0.02) { lo -= 0.02; hi += 0.02; }
    const xt = ticks(lo, hi, 4); lo = xt[0]; hi = xt[xt.length - 1];
    const wrap = document.createElement('div'); wrap.className = 'forest';
    const plotW = 1000, rowH = 30;
    const X = (v) => ((v - lo) / (hi - lo)) * plotW;
    const pmin = opts.pGainMin;

    const head = document.createElement('div'); head.className = 'frow fhead';
    head.innerHTML = '<div class="fl">Candidate</div><div class="fc"></div><div class="fp">p_gain (bar ' + pmin.toFixed(2) + ')</div><div class="fe hide-sm">ΔECE</div><div class="fv">Verdict</div>';
    // tick labels are HTML (an SVG stretched with preserveAspectRatio=none would stretch its text)
    const axis = head.querySelector('.fc');
    axis.setAttribute('aria-hidden', 'true');
    for (const t of xt) {
      const a = document.createElement('span'); a.className = 'ftick';
      a.style.left = `${((X(t) + 8) / (plotW + 16)) * 100}%`;
      a.textContent = t === 0 ? '0' : fmt.signed(2)(t);
      if (t === xt[0]) a.style.transform = 'none';
      if (t === xt[xt.length - 1]) a.style.transform = 'translateX(-100%)';
      axis.appendChild(a);
    }
    wrap.appendChild(head);
    let gen = null;
    for (const d of decisions) {
      if (d.gen !== gen) {
        gen = d.gen;
        const g = document.createElement('div'); g.className = 'fgen';
        g.textContent = `Generation ${gen} · against ${d.incumbent_name}`;
        wrap.appendChild(g);
      }
      const a = d.sim.auprc || {}, e = d.sim.ece || {};
      const row = document.createElement('div');
      row.className = 'frow' + (d.accepted ? ' acc' : ''); row.tabIndex = 0; row.setAttribute('role', 'button');
      row.setAttribute('aria-label', `${d.name}, generation ${d.gen}: delta AUPRC ${fmt.signed(3)(a.delta)}, interval ${fmt.signed(3)(a.lo)} to ${fmt.signed(3)(a.hi)}, p_gain ${fmt.dec(2)(a.p_gain)}, ${d.accepted ? 'accepted' : 'rejected'}`);
      const kind = { playbook: 'playbook edits', exemplars: 'new exemplars', effort: 'judge effort' }[d.kind] || d.kind;
      const pg = fin(a.p_gain) ? a.p_gain : 0;
      row.innerHTML = `<div class="fl"><b>${esc(d.name)}</b><span>${esc(kind)}</span></div><div class="fc"></div>`
        + `<div class="fp"><span class="pg">${fmt.dec(2)(a.p_gain)}</span><span class="pmeter" aria-hidden="true"><i style="width:${pg * 100}%" class="${pg >= pmin ? 'ok' : ''}"></i><b style="left:${pmin * 100}%"></b></span></div>`
        + `<div class="fe hide-sm">${fmt.signed(3)(e.delta)}</div>`
        + `<div class="fv">${d.accepted ? '<span class="pill pos"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.5l2.5 2.5 4.5-6"/></svg>Accepted</span>' : '<span class="pill">Rejected</span>'}`
        + `${d.reasons?.length ? `<small>${esc(d.reasons.join('; '))}</small>` : ''}</div>`;
      const s = el('svg', { viewBox: `-8 0 ${plotW + 16} ${rowH}`, preserveAspectRatio: 'none', height: rowH, 'aria-hidden': 'true' }, row.querySelector('.fc'));
      for (const t of xt) el('line', { x1: X(t), x2: X(t), y1: 0, y2: rowH, class: t === 0 ? 'axisline' : 'gridline', 'vector-effect': 'non-scaling-stroke' }, s);
      if (fin(a.lo) && fin(a.hi)) el('line', { x1: X(a.lo), x2: X(a.hi), y1: rowH / 2, y2: rowH / 2, stroke: d.accepted ? 'var(--pos-mark)' : 'var(--ink-mark)', 'stroke-width': 2, 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' }, s);
      // the point estimate as an HTML dot so it stays round under the stretched viewBox
      if (fin(a.delta)) {
        const dot = document.createElement('i'); dot.className = 'fdot' + (d.accepted ? ' ok' : '');
        dot.style.left = `calc(${((X(a.delta) + 8) / (plotW + 16)) * 100}% - 5px)`;
        row.querySelector('.fc').appendChild(dot);
      } else {
        const na = document.createElement('span'); na.className = 'fna';
        na.textContent = 'undefined: no lies in the gate set';
        row.querySelector('.fc').appendChild(na);
      }
      hover(row, () => `<b>${esc(d.name)}</b> <span class="t-muted">G${d.gen} · ${esc(kind)}</span><br>ΔAUPRC ${fmt.signed(3)(a.delta)} [${fmt.signed(3)(a.lo)}, ${fmt.signed(3)(a.hi)}]<br>p_gain ${fmt.dec(2)(a.p_gain)} (bar ${pmin.toFixed(2)}) · ΔECE ${fmt.signed(3)(e.delta)}`
        + (d.human?.auprc ? `<br>human-val ΔAUPRC ${fmt.signed(3)(d.human.auprc.delta)}` : '')
        + `<br>AUPRC ${fmt.dec(3)(d.sim_metrics?.incumbent?.auprc)} → ${fmt.dec(3)(d.sim_metrics?.candidate?.auprc)}`);
      if (opts.onSelect) {
        row.addEventListener('click', () => opts.onSelect(d));
        row.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts.onSelect(d); } });
      }
      wrap.appendChild(row);
    }
    target.appendChild(wrap);
  }

  // ---- horizontal two-part bars (caught | fooled) -------------------------------------------------------
  // rows: [{label, sub, a, b}] ; opts: {aLabel, bLabel, aColor, bColor, of}
  function splitBars(target, rows, opts) {
    target.innerHTML = '';
    if (!rows.length || rows.every((r) => !(r.a + r.b))) { target.innerHTML = `<p class="empty">${opts.empty}</p>`; return; }
    const max = Math.max(...rows.map((r) => r.a + r.b), 1);
    const list = document.createElement('div'); list.className = 'sbars';
    for (const r of rows) {
      const tot = r.a + r.b;
      const row = document.createElement('div'); row.className = 'sbar'; row.tabIndex = 0;
      const share = tot ? Math.round((r.a / tot) * 100) : 0;
      row.setAttribute('aria-label', `${r.label}: ${r.a} ${opts.aLabel}, ${r.b} ${opts.bLabel}, of ${tot}`);
      row.innerHTML = `<div class="sl" title="${esc(r.full || r.label)}">${esc(r.label)}</div><div class="st"><span class="seg-a" style="width:${(r.a / max) * 100}%"></span><span class="seg-b" style="width:${(r.b / max) * 100}%"></span></div>`
        + `<div class="sv">${tot ? `${r.a} of ${tot} <span class="muted">${share}%</span>` : '<span class="muted">0</span>'}</div>`;
      row.querySelector('.seg-a').style.background = opts.aColor;
      row.querySelector('.seg-b').style.background = opts.bColor;
      if (!r.a) row.querySelector('.seg-a').style.display = 'none';
      if (!r.b) row.querySelector('.seg-b').style.display = 'none';
      hover(row, `<b>${esc(r.full || r.label)}</b><br>${r.a} ${esc(opts.aLabel)} · ${r.b} ${esc(opts.bLabel)} · ${tot} total`);
      list.appendChild(row);
    }
    target.appendChild(list);
  }


  // ---- several series over ordered labels, fixed 0..1 scale; null = gap, unstable = hollow point -------
  // opts: { labels: [..], sub: [..] (small second x line, e.g. lie counts), series: [{name, color, values: [{y, unstable, tip}]}], format, empty, aria }
  function seriesChart(target, opts) {
    target.innerHTML = '';
    const any = opts.series.some((s) => s.values.some((v) => fin(v?.y)));
    if (!opts.labels.length || !any) { target.innerHTML = `<p class="empty">${opts.empty}</p>`; return; }
    const W = Math.max(280, target.clientWidth || 480), H = 210, m = { l: 40, r: 14, t: 10, b: opts.sub ? 42 : 28 };
    const n = opts.labels.length, yt = [0, 0.25, 0.5, 0.75, 1];
    const X = (i) => m.l + (n === 1 ? (W - m.l - m.r) / 2 : (i / (n - 1)) * (W - m.l - m.r));
    const Y = (v) => m.t + (1 - v) * (H - m.t - m.b);
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': opts.aria || '' }, target);
    for (const t of yt) {
      el('line', { x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), class: t === 0 ? 'axisline' : 'gridline' }, svg);
      txt(svg, t.toFixed(2), { x: m.l - 8, y: Y(t) + 4, 'text-anchor': 'end', class: 'tick' });
    }
    const every = Math.max(1, Math.ceil(n / Math.floor((W - m.l - m.r) / 64)));
    opts.labels.forEach((l, i) => {
      if (i % every && i !== n - 1) return;
      txt(svg, l, { x: X(i), y: H - (opts.sub ? 24 : 8), 'text-anchor': 'middle', class: 'tick' });
      if (opts.sub) txt(svg, opts.sub[i], { x: X(i), y: H - 8, 'text-anchor': 'middle', class: 'tick', style: 'font-size:11px' });
    });
    for (const s of opts.series) {
      let d = '', pen = false;
      s.values.forEach((v, i) => { if (fin(v?.y)) { d += `${pen ? 'L' : 'M'}${X(i)},${Y(v.y)} `; pen = true; } else pen = false; });
      if (d) el('path', { d, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
      s.values.forEach((v, i) => {
        if (!fin(v?.y)) return;
        const c = { cx: X(i), cy: Y(v.y), r: 4 };
        el('circle', v.unstable ? { ...c, fill: 'var(--surface)', stroke: s.color, 'stroke-width': 1.5 } : { ...c, fill: s.color, stroke: 'var(--surface)', 'stroke-width': 2 }, svg);
        const h = el('circle', { cx: c.cx, cy: c.cy, r: 9, class: 'hit', tabindex: 0, 'aria-label': `${s.name} ${opts.labels[i]}: ${opts.format(v.y)}` }, svg);
        hover(h, `<b>${esc(s.name)}</b> <span class="t-muted">${esc(opts.labels[i])}</span><br>${opts.format(v.y)}${v.tip ? '<br>' + v.tip : ''}${v.unstable ? '<br><span class="t-muted">unstable: fewer than 5 lies</span>' : ''}`);
      });
      const last = [...s.values].reverse().find((v) => fin(v?.y));
      if (last && opts.endLabels) {
        const i = s.values.lastIndexOf(last);
        txt(svg, opts.format(last.y), { x: X(i) - 6, y: Y(last.y) - 9, 'text-anchor': 'end', style: 'fill:var(--text)' });
      }
    }
  }

  window.Viz = { el, txt, esc, fmt, fin, ticks, legend, genChart, seriesChart, forest, splitBars, hover, showTip, hideTip };
})();
