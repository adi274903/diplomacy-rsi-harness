// Decision-graph explorer: a swimlane layout (rows = countries, columns = the order decisions are
// made in a phase: plan → messages → judgments → plan → messages → judgments → orders → SC), with
// phases laid side by side for the whole-game view. Pan with drag / arrows, zoom with wheel / pinch /
// + − 0, click or Enter on a node to select it; its same-phase ancestors and descendants light up.
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const NODE_W = 132, NODE_H = 30, PITCH = 36, COLW = 156, PHASE_GAP = 56, LANE_PAD = 10, TOP = 58, LEFT = 112;
  const KIND = { plan: 'Plan', message: 'Message', judgment: 'Judgment', orders: 'Orders', sc: 'Supply centres' };
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const short = (p) => (p || '').slice(0, 3);

  function create(host, opts = {}) {
    host.innerHTML = '';
    const svg = el('svg', { role: 'group', 'aria-label': 'Decision graph' }, host);
    const root = el('g', {}, svg);
    const lanesHtml = document.createElement('div');
    lanesHtml.className = 'lanes';
    lanesHtml.setAttribute('aria-hidden', 'true');
    host.appendChild(lanesHtml);
    let view = { k: 1, tx: 0, ty: 0 }, data = null, layout = null, selected = null, chainsOn = false, chainFocus = null, key = '';
    let nodeEls = new Map(), edgeEls = [], bounds = { w: 0, h: 0 };

    const apply = () => {
      root.setAttribute('transform', `translate(${view.tx},${view.ty}) scale(${view.k})`);
      if (!layout) return;
      for (const lab of lanesHtml.children) {
        const y = +lab.dataset.y, h = +lab.dataset.h;
        lab.style.transform = `translateY(${view.ty + y * view.k}px)`;
        lab.style.height = Math.max(18, h * view.k) + 'px';
      }
      lanesHtml.style.opacity = view.tx > -LEFT * view.k + 20 ? 0 : 1;
    };

    function doLayout(d, powers) {
      const nodes = d.nodes.filter((n) => powers.includes(n.power));
      const phases = d.mode === 'game' ? d.phases : [d.phase];
      const pi = (n) => (d.mode === 'game' ? n.pi : 0);
      // columns used per phase
      const used = phases.map(() => new Set());
      for (const n of nodes) used[pi(n)].add(n.col);
      const colX = [], phaseX = [];
      let x = LEFT;
      phases.forEach((ph, i) => {
        const cols = [...used[i]].sort((a, b) => a - b);
        phaseX[i] = { x, cols, phase: ph };
        colX[i] = {};
        cols.forEach((c, j) => { colX[i][c] = x + j * COLW; });
        x += Math.max(1, cols.length) * COLW + PHASE_GAP;
      });
      // stack nodes per cell; lane height = tallest cell
      const cells = new Map();
      for (const n of nodes) {
        const k = `${n.power}|${pi(n)}|${n.col}`;
        if (!cells.has(k)) cells.set(k, []);
        cells.get(k).push(n);
      }
      const num = (n) => { const m = /(\d+)$/.exec(n.id); return m ? +m[1] : 0; };
      for (const list of cells.values()) list.sort((a, b) => num(a) - num(b));
      const laneN = {};
      for (const p of powers) laneN[p] = 1;
      for (const [k, list] of cells) { const p = k.split('|')[0]; laneN[p] = Math.max(laneN[p], list.length); }
      const laneY = {};
      let y = TOP;
      for (const p of powers) { laneY[p] = y; y += laneN[p] * PITCH + LANE_PAD * 2; }
      const pos = new Map();
      for (const [k, list] of cells) {
        const [p, i, c] = k.split('|');
        list.forEach((n, j) => pos.set(n.id, { x: colX[+i][+c], y: laneY[p] + LANE_PAD + j * PITCH }));
      }
      return { nodes, pos, phases, phaseX, laneY, laneN, width: x - PHASE_GAP + 24, height: y + 12 };
    }

    function label(n) {
      if (n.kind === 'message') return n.label;
      if (n.kind === 'judgment') { const m = /p_lie=([\d.]+) (\w+)/.exec(n.detail); return m ? `p ${(+m[1]).toFixed(2)} · ${m[2]}` : n.label; }
      if (n.kind === 'plan') return 'plan, round ' + (/plan(\d)/.exec(n.id)?.[1] || '');
      if (n.kind === 'sc') return 'SC ' + n.label;
      if (n.kind === 'orders') return n.label.replace(/^1 orders$/, '1 order');
      return n.label;
    }

    function edgePath(a, b) {
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
      if (x2 > x1 + 4) { const dx = Math.max(24, (x2 - x1) / 2); return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; }
      // same column (e.g. nothing in between): loop below
      const xa = a.x + NODE_W / 2, xb = b.x + NODE_W / 2;
      return `M${xa},${a.y + NODE_H} C${xa},${a.y + NODE_H + 30} ${xb},${b.y - 30} ${xb},${b.y}`;
    }

    function render(d, o = {}) {
      const newKey = `${d.game}|${d.mode}|${d.phase || ''}|${o.powers.join(',')}`;
      const keepView = newKey === key && o.keepView !== false;
      key = newKey; data = d; chainsOn = !!o.chains;
      layout = doLayout(d, o.powers);
      root.innerHTML = ''; nodeEls = new Map(); edgeEls = [];
      lanesHtml.innerHTML = '';
      const L = layout;
      bounds = { w: L.width, h: L.height };
      // lanes
      for (const p of o.powers) {
        const h = L.laneN[p] * PITCH + LANE_PAD * 2 - 4;
        el('rect', { x: 0, y: L.laneY[p], width: L.width, height: h, class: 'g-lane', rx: 3 }, root);
        const t = el('text', { x: 10, y: L.laneY[p] + 20, class: 'g-lanelabel' }, root); t.textContent = p;
        const model = (d.models || {})[p];
        if (model) { const s = el('text', { x: 10, y: L.laneY[p] + 35, class: 'g-lanesub' }, root); s.textContent = model.split('/').pop().slice(0, 16); }
        const lab = document.createElement('div');
        lab.className = 'lane'; lab.dataset.y = L.laneY[p]; lab.dataset.h = h;
        lab.innerHTML = `<b>${short(p)}</b>`;
        lanesHtml.appendChild(lab);
      }
      // phase and column headers
      L.phaseX.forEach((px, i) => {
        if (i > 0) el('line', { x1: px.x - PHASE_GAP / 2, x2: px.x - PHASE_GAP / 2, y1: 8, y2: L.height, class: 'g-phasesep' }, root);
        const t = el('text', { x: px.x, y: 22, class: 'g-phasehead' }, root); t.textContent = px.phase;
        px.cols.forEach((c, j) => { const h = el('text', { x: px.x + j * COLW, y: 42, class: 'g-colhead' }, root); h.textContent = d.cols[c]; });
      });
      // edges
      const visible = new Set(L.nodes.map((n) => n.id));
      const eg = el('g', {}, root), lg = el('g', {}, root);
      const allEdges = d.edges.filter((e) => visible.has(e.from) && visible.has(e.to));
      // chain links with no traced edge (receiver acted without citing the sender) are drawn when lit
      const have = new Set(allEdges.map((e) => e.from + '>' + e.to));
      for (const c of d.chains || []) {
        for (let i = 1; i < c.path.length; i++) {
          const a = c.path[i - 1], b = c.path[i];
          if (visible.has(a) && visible.has(b) && !have.has(a + '>' + b)) { allEdges.push({ from: a, to: b, kind: 'acted', label: 'same phase' }); have.add(a + '>' + b); }
        }
      }
      for (const e of allEdges) {
        const a = L.pos.get(e.from), b = L.pos.get(e.to);
        const p = el('path', { d: edgePath(a, b), class: `ge k-${e.kind}` }, eg);
        if (e.kind === 'acted') p.style.display = 'none';
        const t = el('text', { x: (a.x + NODE_W + b.x) / 2, y: (a.y + b.y) / 2 + NODE_H / 2 - 4, class: 'ge-label', 'text-anchor': 'middle' }, lg);
        t.textContent = e.label || '';
        t.style.opacity = 0;
        edgeEls.push({ e, p, t });
      }
      // nodes
      const ng = el('g', {}, root);
      for (const n of L.nodes) {
        const at = L.pos.get(n.id);
        const g = el('g', { class: `gn k-${n.kind}${n.tone ? ' t-' + n.tone : ''}`, transform: `translate(${at.x},${at.y})`, tabindex: 0, role: 'button',
          'aria-label': `${n.power} ${KIND[n.kind] || n.kind}${n.phase ? ' ' + n.phase : ''}: ${label(n)}` }, ng);
        el('rect', { width: NODE_W, height: NODE_H, rx: 4 }, g);
        if (n.tone) el('rect', { class: 'tone', width: 4, height: NODE_H - 8, x: 4, y: 4, rx: 2 }, g);
        const t = el('text', { x: n.tone ? 14 : 9, y: 19 }, g);
        const s = label(n); t.textContent = s.length > 20 ? s.slice(0, 19) + '…' : s;
        g.addEventListener('click', () => select(n.id, true));
        g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); select(n.id, true); } });
        g.addEventListener('focus', () => ensureVisible(n.id));
        nodeEls.set(n.id, g);
      }
      if (!keepView) initialView(d);
      apply();
      if (selected && !visible.has(selected)) selected = null;
      highlight();
    }

    function initialView(d) {
      const cw = host.clientWidth || 800, ch = host.clientHeight || 500;
      if (d.mode === 'game') {
        view.k = Math.min(1, Math.max(0.35, (ch - 16) / bounds.h));
        const last = layout.phaseX[layout.phaseX.length - 1];
        view.tx = d.status === 'running' && last ? Math.min(0, cw - (last.x + last.cols.length * COLW + 24) * view.k) : 0;
        view.ty = 8;
      } else fit();
    }

    function fit() {
      const cw = host.clientWidth || 800, ch = host.clientHeight || 500;
      view.k = Math.min(1.25, (cw - 24) / bounds.w, (ch - 24) / bounds.h);
      view.tx = (cw - bounds.w * view.k) / 2; view.ty = Math.max(8, (ch - bounds.h * view.k) / 2);
      apply();
    }

    function zoomAt(f, px, py) {
      const k2 = Math.min(3, Math.max(0.08, view.k * f));
      if (px == null) { px = host.clientWidth / 2; py = host.clientHeight / 2; }
      view.tx = px - (px - view.tx) * (k2 / view.k); view.ty = py - (py - view.ty) * (k2 / view.k); view.k = k2;
      apply();
    }

    function ensureVisible(id) {
      const at = layout?.pos.get(id); if (!at) return;
      const cw = host.clientWidth, ch = host.clientHeight;
      const sx = view.tx + at.x * view.k, sy = view.ty + at.y * view.k;
      if (sx < 40 || sx + NODE_W * view.k > cw - 20) view.tx = cw / 2 - (at.x + NODE_W / 2) * view.k;
      if (sy < 20 || sy + NODE_H * view.k > ch - 20) view.ty = ch / 2 - (at.y + NODE_H / 2) * view.k;
      apply();
    }

    // same-phase closure (carry edges excluded, so one selection does not light the whole game)
    function closure(id) {
      const on = new Set([id]), edges = new Set();
      const walk = (dir) => {
        const q = [id];
        while (q.length) {
          const cur = q.shift();
          for (const x of edgeEls) {
            if (x.e.kind === 'carry' || x.e.kind === 'acted') continue;
            const [a, b] = dir ? [x.e.from, x.e.to] : [x.e.to, x.e.from];
            if (a === cur) { edges.add(x); if (!on.has(b)) { on.add(b); q.push(b); } }
          }
        }
      };
      walk(true); walk(false);
      for (const x of edgeEls) if (x.e.kind === 'carry' && (x.e.from === id || x.e.to === id)) { edges.add(x); on.add(x.e.from); on.add(x.e.to); }
      return { on, edges };
    }

    function chainSets(list) {
      const on = new Set(), edges = new Set();
      for (const c of list) {
        c.path.forEach((p) => on.add(p));
        for (let i = 1; i < c.path.length; i++) for (const x of edgeEls) if (x.e.from === c.path[i - 1] && x.e.to === c.path[i]) edges.add(x);
      }
      return { on, edges };
    }

    function highlight() {
      let sets = null, chain = false;
      if (chainFocus) { sets = chainSets([chainFocus]); chain = true; }
      else if (selected) sets = closure(selected);
      else if (chainsOn) { sets = chainSets(data.chains || []); chain = true; }
      svg.classList.toggle('dim', !!sets);
      for (const [id, g] of nodeEls) { g.classList.toggle('on', !!sets && sets.on.has(id)); g.classList.toggle('sel', id === selected); }
      for (const x of edgeEls) {
        const lit = !!sets && sets.edges.has(x);
        x.p.classList.toggle('on', lit); x.p.classList.toggle('chain', lit && chain);
        x.t.classList.toggle('on', lit); x.t.style.opacity = lit ? 1 : 0;
        if (x.e.kind === 'acted') x.p.style.display = lit ? '' : 'none';
      }
    }

    function select(id, fromUser) {
      chainFocus = null;
      selected = id && nodeEls.has(id) ? id : null;
      highlight();
      if (selected && !fromUser) ensureVisible(selected);
      if (opts.onSelect) {
        const n = selected ? data.nodes.find((x) => x.id === selected) : null;
        const inc = n ? data.edges.filter((e) => e.to === n.id) : [], out = n ? data.edges.filter((e) => e.from === n.id) : [];
        opts.onSelect(n, inc, out);
      }
    }

    function showChain(c) {
      selected = null; chainFocus = c; highlight();
      const mid = c.path.find((p) => p.includes('msg:'));
      if (mid) ensureVisible(mid);
    }

    // ---- input ----------------------------------------------------------------------------------------
    const pointers = new Map();
    let drag = null, pinch = null;
    host.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.gn')) return;
      host.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 1) { drag = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false }; host.classList.add('dragging'); }
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: view.k };
        drag = null;
      }
    });
    host.addEventListener('pointermove', (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && pointers.size === 2) {
        const [a, b] = [...pointers.values()], r = host.getBoundingClientRect();
        const f = (Math.hypot(a.x - b.x, a.y - b.y) / pinch.d) * pinch.k / view.k;
        zoomAt(f, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
      } else if (drag) {
        view.tx = drag.tx + e.clientX - drag.x; view.ty = drag.ty + e.clientY - drag.y;
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 4) drag.moved = true;
        apply();
      }
    });
    const end = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!pointers.size) {
        host.classList.remove('dragging');
        if (drag && !drag.moved && e.type === 'pointerup' && (selected || chainFocus)) select(null, true);   // click on empty canvas clears
        drag = null;
      }
    };
    host.addEventListener('pointerup', end);
    host.addEventListener('pointercancel', end);
    host.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = host.getBoundingClientRect();
      if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX)) zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
      else { view.tx -= e.deltaX; apply(); }
    }, { passive: false });
    host.addEventListener('keydown', (e) => {
      const step = 60;
      if (e.key === 'ArrowLeft') view.tx += step; else if (e.key === 'ArrowRight') view.tx -= step;
      else if (e.key === 'ArrowUp') view.ty += step; else if (e.key === 'ArrowDown') view.ty -= step;
      else if (e.key === '+' || e.key === '=') return zoomAt(1.2);
      else if (e.key === '-' || e.key === '_') return zoomAt(1 / 1.2);
      else if (e.key === '0') return fit();
      else if (e.key === 'Escape') return select(null, true);
      else return;
      e.preventDefault(); apply();
    });
    new ResizeObserver(() => apply()).observe(host);

    function revealChains() {
      const c = (data?.chains || []).find((x) => x.path.some((p) => nodeEls.has(p)));
      const mid = c && c.path.find((p) => p.includes('msg:') && nodeEls.has(p));
      if (mid) ensureVisible(mid);
    }

    return { render, select, showChain, revealChains, fit, zoomIn: () => zoomAt(1.25), zoomOut: () => zoomAt(0.8), get selected() { return selected; } };
  }

  window.DecisionGraph = { create, KIND };
})();
