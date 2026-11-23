// RSI monitor: polls /api/x/* every few seconds and re-renders only the blocks whose data changed.
(() => {
  const { esc, fmt, fin } = Viz;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 4000;
  const S = {
    view: 'overview', raw: {}, data: {},
    lineageSel: null, propGen: null, gameSel: null, gradeFilter: 'all', gradesAll: false,
    decGroup: 'model', decScope: 'all',
    lf: { scope: 'all', rule: 'decision', group: 'by_country', game: null },
    g: { game: null, phase: '', powers: null, chains: false, version: null, status: null },
    lastOk: 0, failed: false,
  };
  const d3 = fmt.dec(3), s3 = fmt.signed(3), d2 = fmt.dec(2);
  const shortModel = (m) => (m || '—').split('/').pop();
  const kindName = { playbook: 'playbook edits', exemplars: 'new exemplars', effort: 'judge effort', root: 'seed', other: 'other' };
  const pct = (a, b) => (b ? Math.round((a / b) * 100) + '%' : '—');
  const ICON_OK = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.5l2.5 2.5 4.5-6"/></svg>';
  const ICON_X = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6"/></svg>';
  const ICON_WARN = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2v5M6 9.5v.1"/></svg>';
  const outcomePill = (o) => ({
    caught: `<span class="pill pos">${ICON_OK}Lie caught</span>`, fooled: `<span class="pill neg">${ICON_X}Fooled</span>`,
    false_alarm: `<span class="pill warn">${ICON_WARN}False alarm</span>`, trusted: '<span class="pill">Trusted, truthful</span>',
    unjudged: '<span class="pill">Not judged</span>' }[o] || esc(o));
  const gamePage = (g) => (g.gen === 'gate' ? `/gate_games/game_${g.id}.html` : `/gen_${g.gen}/games/game_${g.id}.html`);
  const genLabel = (gen) => (gen === 'gate' ? 'Gate' : 'G' + gen);

  // ---- fetching ----------------------------------------------------------------------------------------
  async function get(name, qs = '') {
    const r = await fetch(`/api/x/${name}${qs ? '?' + qs : ''}`, { cache: 'no-store' });
    const t = await r.text();
    if (!r.ok) throw new Error(`${name}: ${r.status} ${t.slice(0, 120)}`);
    return t;
  }
  // returns true when the payload changed since the last fetch
  async function pull(name, qs = '', key = name) {
    const t = await get(name, qs);
    if (S.raw[key] === t) return false;
    S.raw[key] = t; S.data[key] = JSON.parse(t);
    return true;
  }

  const NEEDS = { overview: ['run', 'gates', 'lineage'], playbook: ['run', 'lineage', 'changelog'], games: ['run', 'games'],
    deception: ['run', 'deception', 'standings'], f1: ['run', 'f1'], graph: ['run', 'games'] };
  const QS = { f1: () => `scope=${encodeURIComponent(S.lf.scope)}&rule=${S.lf.rule}` };

  let busy = false;
  async function tick(force = false) {
    if (busy || (document.hidden && !force)) return;
    busy = true;
    try {
      const names = NEEDS[S.view];
      const changed = await Promise.all(names.map((n) => pull(n, QS[n] ? QS[n]() : '')));
      const ch = Object.fromEntries(names.map((n, i) => [n, changed[i] || force]));
      if (S.runName && S.data.run?.run !== S.runName) { location.reload(); return; }   // the viewer now serves another run
      S.runName = S.data.run?.run;
      if (ch.run) renderHeader();
      if (S.view === 'overview' && (ch.run || ch.gates || ch.lineage)) renderOverview();
      if (S.view === 'playbook' && (ch.run || ch.lineage || ch.changelog)) { renderPlaybook(); renderChangelog(); }
      if (S.view === 'f1' && (ch.run || ch.f1)) renderF1();
      if (S.view === 'games' && (ch.games || force)) renderGames();
      if (S.view === 'games' && S.gameSel) await loadReview(force);
      if (S.view === 'deception' && (ch.deception || force)) renderDeception();
      if (S.view === 'deception' && (ch.standings || force)) renderStandings();
      if (S.view === 'graph') await loadGraph(ch.games || force);
      S.lastOk = Date.now(); S.failed = false;
    } catch (e) {
      S.failed = true; console.warn(e);
    } finally {
      busy = false; renderFresh();
    }
  }

  // ---- header ------------------------------------------------------------------------------------------
  function stageText(run) {
    const st = run.status || {};
    if (!st.stage) return 'waiting for the loop to start';
    return st.stage;
  }
  function renderHeader() {
    const run = S.data.run;
    $('title').textContent = `Run ${run.run}`;
    document.title = `${run.run} · RSI monitor`;
    const c = run.config, st = run.status || {};
    const models = Object.values(c.power_models || {});
    const who = models.length ? `${models.length} countries on ${new Set(models).size} models` : `player ${shortModel(c.player_model)}`;
    const gen = st.gen ? `generation ${st.gen} of ${c.generations ?? '—'}` : `${c.generations ?? '—'} generations planned`;
    $('statusline').textContent = `${stageText(run)} · ${gen} · ${who} · judge ${shortModel(c.judge_model)}`;
  }
  function renderFresh() {
    const run = S.data.run, f = $('fresh');
    const finished = run?.status?.stage === 'finished';
    f.className = 'fresh ' + (S.failed ? 'stale' : finished ? '' : 'live');
    const upd = run?.status?.updated ? `loop updated ${run.status.updated}` : 'no loop status yet';
    $('freshtext').textContent = S.failed ? 'Viewer unreachable — retrying' : finished ? `Run finished · ${upd}` : `Live · ${upd}`;
  }

  // ---- overview ----------------------------------------------------------------------------------------
  function finding(run, gates) {
    const p = run.config.p_gain_min ?? 0.8;
    const done = run.generations.filter((g) => g.done);
    const live = run.generations.find((g) => g.live);
    const parts = [];
    if (run.baseline && run.baseline.n != null && !run.baseline.n_lie) {
      parts.push(`The frozen gate set has <b>0 self-labelled lies</b> in ${run.baseline.n} items, so gate AUPRC is undefined and no candidate can show a gain until lies appear.`);
    }
    const last = done[done.length - 1];
    if (last) {
      const gd = gates.decisions.filter((d) => d.gen === last.gen);
      if (last.accepted.length) {
        const a = gd.find((d) => d.accepted && d.candidate === last.incumbent) || gd.find((d) => d.accepted);
        parts.push(`Generation ${last.gen} adopted <b>${esc(last.incumbent_name)}</b>: gate AUPRC ${d3(a?.sim_metrics?.incumbent?.auprc)} → ${d3(a?.sim_metrics?.candidate?.auprc)} (p_gain ${d2(a?.sim?.auprc?.p_gain)}).`);
      } else {
        parts.push(`Generation ${last.gen}: ${gd.length} candidate${gd.length === 1 ? '' : 's'} gated, none passed — best p_gain ${d2(last.best_p_gain)} against the ${p.toFixed(2)} bar. Playbook <b>${esc(last.incumbent_name)}</b> kept.`);
      }
    }
    if (live) parts.push(`Now: generation ${live.gen}, ${esc(live.stage.replace(/^generation \d+: /, ''))}.`);
    else if (!done.length && run.status.stage) parts.push(`Now: ${esc(run.status.stage)} with the seed playbook.`);
    return parts.join(' ');
  }

  function kpi(label, value, sub) {
    return `<div class="kpi" role="listitem"><p class="label">${label}</p><p class="value">${value}</p>${sub ? `<p class="delta">${sub}</p>` : ''}</div>`;
  }

  function renderOverview() {
    const run = S.data.run, gates = S.data.gates, lin = S.data.lineage;
    if (!run || !gates) return;
    $('finding').innerHTML = finding(run, gates);
    const t = run.totals, base = run.baseline || {};
    const lastDone = [...run.generations].reverse().find((g) => g.done);
    const im = lastDone?.incumbent_metrics || { auprc: base.auprc, ece: base.ece };
    const dA = fin(im.auprc) && fin(base.auprc) ? im.auprc - base.auprc : null;
    const dE = fin(im.ece) && fin(base.ece) ? im.ece - base.ece : null;
    const inc = run.incumbent;
    $('kpis').innerHTML =
      kpi('Incumbent playbook', esc(inc.name || '—'), inc.generation_adopted ? `adopted in generation ${inc.generation_adopted}` : 'seed playbook, not yet replaced')
      + kpi('Gate AUPRC', d3(im.auprc), fin(dA) ? `${s3(dA)} vs baseline ${d3(base.auprc)}` : base.n != null ? `${base.n_lie ?? 0} lies in ${base.n} gate items` : 'baseline pending')
      + kpi('Gate ECE', d3(im.ece), fin(dE) ? `${s3(dE)} vs baseline ${d3(base.ece)}` : 'lower is better')
      + kpi('Candidates accepted', `${t.accepted} <small>of ${t.candidates}</small>`, `${run.generations.filter((g) => g.done).length} generations finished`)
      + kpi('Lies caught in play', `${t.caught} <small>of ${t.lies}</small>`, `${t.fooled} fooled · ${t.false_alarms} false alarms · ${t.games} games`);
    renderPipeline(run);
    const p = gates.p_gain_min ?? 0.8;
    $('gate-note').textContent = `Bars: 95% paired cluster-bootstrap interval of ΔAUPRC (candidate − incumbent) on frozen gate replays${gates.bootstrap_n ? `, ${gates.bootstrap_n} resamples` : ''}. A candidate passes with p_gain ≥ ${p.toFixed(2)}, ECE no worse than +${(gates.ece_tolerance ?? 0.02).toFixed(2)}, and human-val AUPRC within −${(gates.human_val_tolerance ?? 0.01).toFixed(2)}. Select a row for its playbook diff.`;
    Viz.legend($('gate-legend'), [
      { mark: 'ci', color: 'var(--ink-mark)', label: 'Rejected: ΔAUPRC and 95% interval' },
      { mark: 'ci', color: 'var(--pos-mark)', label: 'Accepted' },
      { mark: 'tick', color: 'var(--text)', label: `p_gain bar ${p.toFixed(2)}` },
    ]);
    Viz.forest($('forest'), [...gates.decisions].reverse().sort((a, b) => b.gen - a.gen || a.idx - b.idx), {
      pGainMin: p, empty: run.generations.length ? 'No candidate has reached the gate yet.' : 'The gate opens after the first generation: games, judge review, then candidate playbooks.',
      onSelect: (d) => { S.lineageSel = d.candidate; location.hash = '#playbook'; },
    });
    renderTrend(run, gates);
    renderIncumbent(run, lin);
    $('events').innerHTML = (run.status.events || []).map((e) =>
      `<li><time>${esc(e.t)}</time><span class="${/ACCEPTED|playbook updated/.test(e.text) ? 'acc' : ''}">${esc(e.text)}</span></li>`).join('')
      || '<li><span class="muted">No events yet.</span></li>';
  }

  function stageIndex(stage) {
    if (/playing/.test(stage)) return 1;
    if (/review/.test(stage)) return 2;
    if (/gating/.test(stage)) return 4;
    return 0;
  }

  function renderPipeline(run) {
    const c = run.config, gs = run.gate_set, base = run.baseline;
    const cell = (n, s, cls = '') => `<div class="stage ${cls}"><span class="n">${n}</span><span class="s">${s}</span></div>`;
    const gsNow = gs.finished < (c.gate_games ?? 1) ? 'now' : '';
    let rows = `<tr><td class="gen">Gate set</td>
      <td>${cell(`${gs.finished}/${c.gate_games ?? gs.games}`, 'frozen games, seed playbook', gsNow)}</td>
      <td colspan="3" class="hide-sm">${cell(base?.n ?? '—', base ? `receiver-seat replay items, ${base.n_lie ?? 0} self-labelled lies · every candidate is scored on these` : 'replay items are cut once the gate games finish', base ? '' : 'wait')}</td>
      <td>${cell(base ? d3(base.auprc) : '—', base ? `v0 baseline AUPRC · ECE ${d3(base.ece)}` : 'baseline pending', base ? '' : 'wait')}</td></tr>`;
    for (const g of run.generations) {
      const now = g.live ? stageIndex(g.stage) : -1;
      const games = cell(`${g.games_finished}/${g.games_planned ?? g.games}`, g.games ? `${g.messages} messages · ${g.lies} ${g.lies === 1 ? 'lie' : 'lies'}` : 'not started', (now === 1 ? 'now' : '') + (g.games ? '' : ' wait'));
      const rev = cell(g.reviews || (now > 2 || g.done ? 0 : '—'), g.leak_flags ? `${g.leak_flags} leak flags` : 'judge reviews', (now === 2 ? 'now ' : '') + (g.reviews ? '' : 'wait'));
      const prop = cell(g.diffs_kept ?? '—', g.diffs_kept != null ? `passed lint · ${g.diffs_rejected ?? 0} rejected` : 'proposed edits', g.diffs_kept == null ? 'wait' : '');
      const cand = cell(g.candidates ?? '—', g.candidates != null ? `${g.gated} gated` : 'candidate playbooks', (now === 4 ? 'now ' : '') + (g.candidates == null ? 'wait' : ''));
      let verdict;
      if (g.accepted.length) verdict = cell(`<span class="verdict acc">${esc(g.accepted.map((a) => a.name).join(', '))}</span>`, `adopted · best p_gain ${d2(g.best_p_gain)}`);
      else if (g.done) verdict = cell(`<span class="verdict rej">Kept ${esc(g.incumbent_name)}</span>`, g.gated ? `best p_gain ${d2(g.best_p_gain)}` : 'no candidates');
      else verdict = cell('—', g.gated ? `${g.gated} decided so far` : 'pending', 'wait');
      rows += `<tr class="${g.live ? 'row-live' : ''}"><td class="gen">G${g.gen}${g.parent_name ? `<span class="s muted" style="display:block;font-weight:400;font-size:12px">from ${esc(g.parent_name)}</span>` : ''}</td>
        <td>${games}</td><td class="hide-sm">${rev}</td><td class="hide-sm">${prop}</td><td class="hide-sm">${cand}</td><td>${verdict}</td></tr>`;
    }
    $('pipeline').innerHTML = `<div class="tablewrap"><table class="pipe"><thead><tr><th>Generation</th><th>Games</th><th class="hide-sm">Judge reviews</th><th class="hide-sm">Proposed edits</th><th class="hide-sm">Candidates</th><th>Gate verdict</th></tr></thead><tbody>${rows}</tbody></table></div>`
      + (run.generations.length ? '' : `<p class="note" style="margin-top:10px">${c.generations ?? 0} generations of ${c.games_per_generation ?? '—'} game${c.games_per_generation === 1 ? '' : 's'} each are planned; each one starts when the gate baseline is in.</p>`);
  }

  function renderTrend(run, gates) {
    const base = run.baseline || {};
    const gens = [0, ...run.generations.map((g) => g.gen)];
    for (const [metric, target, name] of [['auprc', 'chart-auprc', 'AUPRC'], ['ece', 'chart-ece', 'ECE']]) {
      const line = [{ x: 0, y: base[metric], label: 'Baseline v0' },
        ...run.generations.filter((g) => g.done && g.incumbent_metrics).map((g) => ({ x: g.gen, y: g.incumbent_metrics[metric], label: `After G${g.gen}: ${g.incumbent_name}` }))];
      const dots = gates.decisions.map((d) => ({ x: d.gen, y: d.sim_metrics?.candidate?.[metric], accepted: d.accepted, label: d.name }));
      const why = base.n != null && !base.n_lie ? `Undefined: the gate set has 0 lies in ${base.n} items.` : 'Appears once the gate baseline is scored.';
      Viz.genChart($(target), { gens, line, dots, name, format: d3, empty: why,
        aria: `Gate ${name} of the incumbent by generation, with candidate values` });
    }
    Viz.legend($('auprc-legend'), [
      { mark: 'line', color: 'var(--accent)', label: 'Incumbent' },
      { mark: 'ring', color: 'var(--ink-mark)', label: 'Rejected candidate' },
      { mark: 'dot', color: 'var(--pos-mark)', label: 'Accepted' }]);
  }

  function renderIncumbent(run, lin) {
    const inc = run.incumbent;
    $('inc-title').textContent = `Incumbent playbook · ${inc.name || '—'}`;
    const g = inc.genome;
    if (!g) { $('incumbent').innerHTML = '<li><span></span><span class="muted">No playbook saved yet.</span></li>'; return; }
    const node = lin?.nodes?.find((n) => n.hash === inc.hash);
    const added = new Set((node?.diff?.added || []).map((h) => h.id)), edited = new Set((node?.diff?.edited || []).map((h) => h.id));
    $('incumbent').innerHTML = g.playbook.map((h) => `<li><span class="hid">${esc(h.id)}</span><span>${esc(h.text)}`
      + (added.has(h.id) ? ' <span class="pill pos new">new</span>' : edited.has(h.id) ? ' <span class="pill warn new">edited</span>' : '')
      + (h.rationale && h.rationale !== 'seed heuristic' ? `<span class="why">Why: ${esc(h.rationale)}</span>` : '') + '</span></li>').join('')
      + `<li><span class="hid">+</span><span class="muted">${g.exemplars.length} worked exemplar${g.exemplars.length === 1 ? '' : 's'} · judge effort ${esc(g.effort?.judge || '—')} · decision threshold ${g.threshold}</span></li>`;
  }

  // ---- playbook lineage -------------------------------------------------------------------------------
  function renderPlaybook() {
    const lin = S.data.lineage; if (!lin) return;
    const nodes = lin.nodes;
    if (!S.lineageSel || !nodes.some((n) => n.hash === S.lineageSel)) {
      const inc = nodes.find((n) => n.status === 'incumbent' && n.parent);
      S.lineageSel = (inc || nodes[nodes.length - 1] || {}).hash;
    }
    renderTree(nodes);
    renderDiff(nodes.find((n) => n.hash === S.lineageSel), lin);
    Viz.legend($('tree-legend'), [
      { mark: 'box', color: 'var(--accent)', width: 2, label: 'Incumbent' },
      { mark: 'box', color: 'var(--pos-mark)', label: 'Accepted' },
      { mark: 'box', color: 'var(--control)', dash: '3 3', label: 'Rejected' }]);
  }

  function renderTree(nodes) {
    const host = $('tree'); host.innerHTML = '';
    const col = (n) => (n.parent ? (n.loop_gens[0] ?? n.generation) : 0);
    const cols = {};
    for (const n of nodes) (cols[col(n)] ||= []).push(n);
    const order = { incumbent: 0, accepted: 1, pending: 2, rejected: 3, root: 0 };
    for (const list of Object.values(cols)) list.sort((a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name));
    const BW = 168, BH = 44, CW = 216, PITCH = 54, TOP = 26;
    const keys = Object.keys(cols).map(Number).sort((a, b) => a - b);
    const maxN = Math.max(...keys.map((k) => cols[k].length), 1);
    const W = keys.length * CW, H = TOP + maxN * PITCH;
    const pos = {};
    keys.forEach((k, i) => cols[k].forEach((n, j) => { pos[n.hash] = { x: i * CW + 4, y: TOP + j * PITCH + (maxN - cols[k].length) * PITCH / 2 * (k === 0 ? 1 : 0) }; }));
    const svg = Viz.el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, style: `min-width:${W}px`, role: 'group', 'aria-label': 'Playbook lineage tree' }, host);
    keys.forEach((k, i) => { const t = Viz.txt(svg, k === 0 ? 'Seed' : `Generation ${k}`, { x: i * CW + 4, y: 14 }); t.setAttribute('class', 'tick'); });
    for (const n of nodes) {
      if (!n.parent || !pos[n.parent]) continue;
      const a = pos[n.parent], b = pos[n.hash];
      const x1 = a.x + BW, y1 = a.y + BH / 2, x2 = b.x, y2 = b.y + BH / 2, dx = (x2 - x1) / 2;
      Viz.el('path', { d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`, class: 'tedge' }, svg);
    }
    for (const n of nodes) {
      const p = pos[n.hash];
      const last = n.decisions[n.decisions.length - 1];
      const meta = n.status === 'root' ? 'seed playbook' : last ? `ΔAUPRC ${s3(last.d_auprc)} · p ${d2(last.p_gain)}` : 'awaiting gate';
      const g = Viz.el('g', { class: `tnode st-${n.status}${n.hash === S.lineageSel ? ' sel' : ''}`, transform: `translate(${p.x},${p.y})`, tabindex: 0, role: 'button',
        'aria-pressed': n.hash === S.lineageSel ? 'true' : 'false', 'aria-label': `${n.name}, ${n.status}, ${meta}` }, svg);
      Viz.el('rect', { class: 'box', width: BW, height: BH, rx: 5 }, g);
      Viz.txt(g, n.name.length > 22 ? n.name.slice(0, 21) + '…' : n.name, { x: 10, y: 18, class: 'name' });
      Viz.txt(g, meta, { x: 10, y: 34, class: 'meta' });
      const pick = () => { S.lineageSel = n.hash; renderPlaybook(); };
      g.addEventListener('click', pick);
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); host.querySelector(`[aria-pressed="true"]`)?.focus(); } });
    }
  }

  function voteText(p, gamesPerGen) {
    if (!p) return '';
    const of = gamesPerGen ? ` of ${gamesPerGen}` : '';
    return `<span class="votes"><span class="votebar" style="width:${Math.max(4, p.votes * 14)}px"></span>${p.votes}${of} game${p.votes === 1 ? '' : 's'} proposed it</span>`;
  }

  function renderDiff(n, lin) {
    const box = $('diff');
    if (!n) { box.innerHTML = '<p class="empty">No playbooks saved yet.</p>'; return; }
    const st = { incumbent: '<span class="pill pos">Incumbent</span>', accepted: `<span class="pill pos">${ICON_OK}Accepted</span>`,
      rejected: '<span class="pill">Rejected</span>', pending: '<span class="pill warn">Awaiting gate</span>', root: '<span class="pill">Seed</span>' }[n.status];
    $('diff-title').innerHTML = `${esc(n.name)} ${st}`;
    const parent = lin.nodes.find((x) => x.hash === n.parent);
    const gpg = S.data.run?.config?.games_per_generation;
    const find = (op, h) => n.proposals_used.find((p) => p.op === op && (op === 'add' ? norm(p.text) === norm(h.text) : p.heuristic_id === h.id));
    let html = `<dl class="facts"><dt>Hash</dt><dd>${esc(n.hash)}</dd>`
      + (parent ? `<dt>Parent</dt><dd><button class="btn" type="button" data-pick="${esc(parent.hash)}">${esc(parent.name)}</button></dd>` : '')
      + `<dt>Kind</dt><dd>${esc(kindName[n.kind] || n.kind)}</dd>`
      + n.decisions.map((d) => `<dt>Gate G${d.gen}</dt><dd>ΔAUPRC ${s3(d.d_auprc)} [${s3(d.lo)}, ${s3(d.hi)}] · p_gain ${d2(d.p_gain)} · ${d.accepted ? '<b>accepted</b>' : 'rejected'}${d.reasons.length ? `<br><span class="muted">${esc(d.reasons.join('; '))}</span>` : ''}</dd>`).join('')
      + '</dl>';
    const D = n.diff, items = [];
    for (const h of D.added) { const p = find('add', h); items.push(`<li><span class="op add">Added</span> <b>${esc(h.id)}</b><p class="txt">${esc(h.text)}</p><p class="why">Judge's reason: ${esc(h.rationale || p?.rationale || '—')}</p>${voteText(p, gpg)}</li>`); }
    for (const h of D.edited) { const p = find('edit', h); items.push(`<li><span class="op edit">Edited</span> <b>${esc(h.id)}</b><p class="txt"><del>${esc(h.before)}</del></p><p class="txt">${esc(h.text)}</p><p class="why">Judge's reason: ${esc(h.rationale || p?.rationale || '—')}</p>${voteText(p, gpg)}</li>`); }
    for (const h of D.removed) { const p = find('delete', h); items.push(`<li><span class="op del">Removed</span> <b>${esc(h.id)}</b><p class="txt"><del>${esc(h.text)}</del></p><p class="why">Judge's reason: ${esc(p?.rationale || '—')}</p>${voteText(p, gpg)}</li>`); }
    if (D.exemplars) items.push(`<li><span class="op edit">Exemplars</span> ${D.exemplars.before} → ${D.exemplars.after} worked examples, mined from this generation's correct, confident judgments
      <details><summary class="note" style="cursor:pointer;margin-top:6px">Read them</summary>${D.exemplars.items.map((x) => `<p class="txt" style="white-space:pre-wrap">${esc(x)}</p>`).join('')}</details></li>`);
    for (const e of D.effort) items.push(`<li><span class="op edit">Effort</span> ${esc(e.key)}: ${esc(e.before)} → <b>${esc(e.after)}</b></li>`);
    for (const k of D.other) items.push(`<li><span class="op edit">Changed</span> ${esc(k)}</li>`);
    html += `<h4>What changed from ${esc(parent?.name || 'nothing')}</h4>` + (items.length ? `<ul class="diffs">${items.join('')}</ul>` : `<p class="empty">${n.parent ? 'Behaviourally identical to its parent.' : 'The seed playbook: six hand-written heuristics, no exemplars.'}</p>`);
    html += `<h4>Playbook (${n.playbook.length} heuristics)</h4><ol class="playbook">${n.playbook.map((h) => `<li><span class="hid">${esc(h.id)}</span><span>${esc(h.text)}</span></li>`).join('')}</ol>`;
    box.innerHTML = html;
    box.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => { S.lineageSel = b.dataset.pick; renderPlaybook(); }));
  }
  const norm = (t) => (t || '').toLowerCase().replace(/\W+/g, ' ').trim();

  function renderProposals(lin) {
    const props = lin.proposals;
    $('prop-note').textContent = props.length ? `Votes = games whose review proposed the same edit. ${lin.lint_note}.` : '';
    if (!props.length) { $('prop-tabs').innerHTML = ''; $('proposals').innerHTML = '<p class="empty">The judge has not reviewed a generation yet.</p>'; return; }
    if (!props.some((p) => p.gen === S.propGen)) S.propGen = props[props.length - 1].gen;
    $('prop-tabs').innerHTML = props.map((p) => `<button type="button" role="tab" aria-selected="${p.gen === S.propGen}" data-gen="${p.gen}">Generation ${p.gen}</button>`).join('');
    $('prop-tabs').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { S.propGen = +b.dataset.gen; renderProposals(lin); }));
    const p = props.find((x) => x.gen === S.propGen);
    const usedBy = (key) => lin.nodes.filter((n) => n.proposals_used.some((u) => u.key === key && u.gen === p.gen)).map((n) => n.name);
    const gpg = S.data.run?.config?.games_per_generation;
    const rows = p.ranked.map((d) => `<tr><td><span class="op ${d.op === 'add' ? 'add' : d.op === 'delete' ? 'del' : 'edit'}" style="font-weight:600">${esc(d.op)}${d.heuristic_id ? ' ' + esc(d.heuristic_id) : ''}</span></td>
        <td class="msg">${esc(d.text || '—')}<small>${esc(d.rationale)}</small></td><td>${voteText(d, gpg)}</td>
        <td><span class="pill pos">${ICON_OK}Passed</span></td><td class="muted">${esc(usedBy(d.key).join(', ') || 'none')}</td></tr>`)
      .concat(p.rejected.map((d) => `<tr><td><span class="op" style="font-weight:600">${esc(d.op)}${d.heuristic_id ? ' ' + esc(d.heuristic_id) : ''}</span></td>
        <td class="msg">${esc(d.text || '—')}<small>${esc(d.rationale || '')}</small></td><td class="muted">${esc(d.game)}</td>
        <td><span class="pill neg">${ICON_X}Rejected</span><small class="muted" style="display:block">${esc(d.errors.join('; '))}</small></td><td class="muted">—</td></tr>`));
    $('proposals').innerHTML = `<div class="tablewrap"><table class="table"><thead><tr><th>Edit</th><th>Heuristic and the judge's reason</th><th>Votes</th><th>Lint</th><th>Used by</th></tr></thead><tbody>${rows.join('') || '<tr><td colspan="5" class="muted">No edits proposed.</td></tr>'}</tbody></table></div>`;
  }

  // ---- games + review ------------------------------------------------------------------------------------
  function seatsHtml(g) {
    const models = new Set(Object.values(g.seats).map((s) => s.model));
    const genomes = new Set(Object.values(g.seats).map((s) => s.genome_name));
    return `<div class="seats">${g.active_powers.map((p) => { const s = g.seats[p] || {};
      return `<span><b>${esc(p.slice(0, 3))}</b> ${models.size > 1 ? esc(shortModel(s.model)) : ''}${genomes.size > 1 ? ' · ' + esc(s.genome_name) : ''}</span>`; }).join('')}`
      + `${models.size === 1 ? `<span>${esc(shortModel([...models][0]))}</span>` : ''}${genomes.size === 1 ? `<span>playbook ${esc([...genomes][0])}</span>` : ''}</div>`;
  }
  function leader(g) {
    const e = Object.entries(g.final_sc || {}).filter(([p]) => g.active_powers.includes(p)).sort((a, b) => b[1] - a[1]);
    return e.length ? `${esc(e[0][0].slice(0, 3))} ${e[0][1]}<small class="muted" style="display:block">${esc(shortModel(g.seats[e[0][0]]?.model))}</small>` : '—';
  }

  function renderGames() {
    const games = S.data.games?.games || [];
    const running = games.filter((g) => g.status === 'running').length;
    $('games-note').textContent = `${games.length} games${running ? ` · ${running} running` : ''} · select one for the judge's review`;
    if (!S.gameSel || !games.some((g) => g.id === S.gameSel)) S.gameSel = ([...games].reverse().find((g) => g.has_review) || games[games.length - 1] || {}).id || null;
    const rows = [...games].reverse().map((g) => `<tr class="clickable" tabindex="0" data-id="${esc(g.id)}" aria-selected="${g.id === S.gameSel}">
      <td class="strong">${esc(g.id)}</td><td>${genLabel(g.gen)}</td>
      <td>${g.status === 'running' ? '<span class="pill warn">Running</span>' : g.error ? '<span class="pill neg">Error</span>' : 'Finished'}<small class="muted" style="display:block">${esc(g.final_phase || '')}</small></td>
      <td class="hide-sm">${seatsHtml(g)}</td>
      <td class="num">${g.messages}</td><td class="num">${g.lies}</td><td class="num">${g.caught}</td><td class="num hide-sm">${g.fooled}</td><td class="num hide-sm">${g.false_alarms}</td>
      <td class="hide-sm">${leader(g)}</td><td class="hide-sm">${g.has_review ? 'Reviewed' : '<span class="muted">—</span>'}</td></tr>`).join('');
    $('games-table').innerHTML = `<thead><tr><th>Game</th><th>Gen</th><th>Status</th><th class="hide-sm">Seats</th><th class="num">Messages</th><th class="num">Lies</th><th class="num">Caught</th><th class="num hide-sm">Fooled</th><th class="num hide-sm">False alarms</th><th class="hide-sm">Leader (SC)</th><th class="hide-sm">Review</th></tr></thead><tbody>${rows || '<tr><td colspan="11" class="muted">No games yet.</td></tr>'}</tbody>`;
    $('games-table').querySelectorAll('tr[data-id]').forEach((tr) => {
      const pick = () => { S.gameSel = tr.dataset.id; S.gradesAll = false; renderGames(); loadReview(true); };
      tr.addEventListener('click', pick);
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    });
  }

  async function loadReview(force) {
    const g = (S.data.games?.games || []).find((x) => x.id === S.gameSel);
    if (!g) { $('review').innerHTML = '<p class="empty">No game selected.</p>'; return; }
    const changed = await pull('review', 'game=' + encodeURIComponent(g.id), 'review:' + g.id);
    if (changed || force) renderReview(g, S.data['review:' + g.id]);
  }

  function renderReview(g, r) {
    $('review-title').textContent = `Judge review · ${g.id}`;
    $('review-actions').innerHTML = `<a class="btn" href="#graph/${encodeURIComponent(g.id)}">Decision graph</a><a class="btn" href="${gamePage(g)}" target="_blank" rel="noopener">Game page</a>`;
    if (!r || !r.grades) {
      $('review').innerHTML = `<p class="empty">${g.gen === 'gate' ? '<b>Gate games are not reviewed.</b> They are the frozen evaluation set every candidate is replayed on.' : g.status === 'running' ? '<b>Still playing.</b> The judge reviews each game once its generation has finished.' : 'No review for this game yet.'}</p>`;
      return;
    }
    const counts = { all: r.grades.length, lies: r.grades.filter((x) => x.intent === 'deceptive').length,
      broken: r.grades.filter((x) => x.promise === 'broken').length, leaks: r.grades.filter((x) => x.leak_flag).length };
    const filt = { all: () => true, lies: (x) => x.intent === 'deceptive', broken: (x) => x.promise === 'broken', leaks: (x) => x.leak_flag }[S.gradeFilter];
    const shown = r.grades.filter(filt), cap = S.gradesAll ? shown.length : 10;
    const gpg = S.data.run?.config?.games_per_generation;
    const diffs = r.diffs.map((d) => `<li><span class="op ${d.op === 'add' ? 'add' : d.op === 'delete' ? 'del' : 'edit'}">${esc(d.op)}${d.heuristic_id ? ' ' + esc(d.heuristic_id) : ''}</span> ${esc(d.text || '')}
        <p class="why">${esc(d.rationale)}${d.evidence_msg_ids?.length ? ` · evidence ${d.evidence_msg_ids.map((i) => '#' + i).join(' ')}` : ''}</p>
        ${d.lint_errors.length ? `<span class="pill neg">${ICON_X}Lint: ${esc(d.lint_errors.join('; '))}</span>` : d.votes ? voteText(d, gpg) : ''}</li>`).join('');
    const qual = (n) => `${n}/5<span class="meter" aria-hidden="true"><i style="width:${n * 20}%"></i></span>`;
    const rows = shown.slice(0, cap).map((x) => `<tr><td class="num">#${x.msg_id}</td>
      <td class="msg"><b>${esc(x.sender?.slice(0, 3))} → ${esc(x.recipient?.slice(0, 3))}</b> <span class="muted">${esc(x.phase || '')} · sender says ${esc(x.intent || '?')}</span><br>${esc(x.text || '')}<small>Judge note: ${esc(x.note)}</small></td>
      <td class="num">${x.p_lie != null ? d2(x.p_lie) : '—'}<small class="muted" style="display:block">${esc(x.decision || '')}</small></td>
      <td>${outcomePill(x.outcome)}</td><td>${esc(x.promise.replace('_', ' '))}</td><td class="hide-sm">${qual(x.reasoning_quality)}</td>
      <td class="hide-sm">${x.leak_flag ? `<span class="pill neg">${ICON_WARN}Leak</span>` : '<span class="muted">—</span>'}</td></tr>`).join('');
    $('review').innerHTML = `<p class="finding" style="font-size:14px;margin-bottom:14px">${esc(r.summary || '')}</p>
      <h4>Proposed playbook edits</h4>${diffs ? `<ul class="diffs">${diffs}</ul>` : '<p class="empty">None.</p>'}
      <h4>Grades per message</h4>
      <div class="seg" role="radiogroup" aria-label="Filter grades" id="grade-seg">${[['all', 'All'], ['lies', 'Lies'], ['broken', 'Broken promises'], ['leaks', 'Leak flags']].map(([k, l]) =>
        `<button type="button" role="radio" aria-checked="${S.gradeFilter === k}" data-k="${k}">${l} ${counts[k]}</button>`).join('')}</div>
      <div class="tablewrap"><table class="table"><thead><tr><th class="num">Msg</th><th>Message</th><th class="num">p_lie</th><th>Outcome</th><th>Promise</th><th class="hide-sm">Receiver reasoning</th><th class="hide-sm">Leak</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="muted">No messages match.</td></tr>'}</tbody></table></div>
      ${shown.length > cap ? `<p style="margin:12px 0 0"><button class="btn" type="button" id="grades-all">Show all ${shown.length}</button></p>` : ''}`;
    $('grade-seg').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { S.gradeFilter = b.dataset.k; S.gradesAll = false; renderReview(g, r); }));
    $('grades-all')?.addEventListener('click', () => { S.gradesAll = true; renderReview(g, r); });
  }

  // ---- deception ------------------------------------------------------------------------------------------
  function renderDeception() {
    const rows = S.data.deception?.rows || [];
    const gens = [...new Set(rows.map((r) => r.gen))];
    const scopes = [['all', 'All games'], ...gens.map((g) => [String(g), g === 'gate' ? 'Gate games' : `Generation ${g}`])];
    if (!scopes.some(([k]) => k === S.decScope)) S.decScope = 'all';
    $('dec-scope').innerHTML = scopes.map(([k, l]) => `<option value="${k}" ${k === S.decScope ? 'selected' : ''}>${l}</option>`).join('');
    const groups = [['model', 'Model'], ['power', 'Country'], ['genome_name', 'Playbook']];
    $('dec-group').innerHTML = groups.map(([k, l]) => `<button type="button" role="radio" aria-checked="${S.decGroup === k}" data-k="${k}">${l}</button>`).join('');
    $('dec-group').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => { S.decGroup = b.dataset.k; renderDeception(); }));
    $('dec-by').textContent = { model: 'model', power: 'country', genome_name: 'playbook' }[S.decGroup];
    const sel = rows.filter((r) => S.decScope === 'all' || String(r.gen) === S.decScope);
    const agg = {};
    const keys = ['sent', 'lies', 'lies_caught', 'lies_fooled', 'received', 'lies_received', 'caught', 'fooled', 'false_alarms', 'trusted_truths', 'judged', 'p_lie_on_lies', 'p_lie_on_truths', 'truths_judged'];
    for (const r of sel) {
      const k = r[S.decGroup] || '—';
      const a = (agg[k] ||= { key: k, seats: 0, games: new Set(), ...Object.fromEntries(keys.map((x) => [x, 0])) });
      a.seats++; a.games.add(r.game);
      for (const x of keys) a[x] += r[x] || 0;
    }
    const list = Object.values(agg).sort((a, b) => b.lies_received + b.lies - (a.lies_received + a.lies) || a.key.localeCompare(b.key));
    const lab = (k) => (S.decGroup === 'model' ? shortModel(k) : k);
    Viz.splitBars($('chart-recv'), list.map((a) => ({ label: lab(a.key), full: a.key, a: a.caught, b: a.fooled })),
      { aLabel: 'caught', bLabel: 'fooled', aColor: 'var(--caught-mark)', bColor: 'var(--fooled-mark)', empty: 'No lies received in these games.' });
    Viz.splitBars($('chart-send'), list.map((a) => ({ label: lab(a.key), full: a.key, a: a.lies_caught, b: a.lies_fooled })),
      { aLabel: 'caught by the receiver', bLabel: 'got through', aColor: 'var(--caught-mark)', bColor: 'var(--fooled-mark)', empty: 'No lies told in these games.' });
    const lg = [{ mark: 'bar', color: 'var(--caught-mark)', label: 'Caught (distrusted)' }, { mark: 'bar', color: 'var(--fooled-mark)', label: 'Fooled (trusted)' }];
    Viz.legend($('recv-legend'), lg);
    Viz.legend($('send-legend'), [{ mark: 'bar', color: 'var(--caught-mark)', label: 'Caught' }, { mark: 'bar', color: 'var(--fooled-mark)', label: 'Got through' }]);
    const mean = (s, n) => (n ? s / n : null);
    const trs = list.map((a) => {
      const ml = mean(a.p_lie_on_lies, a.caught + a.fooled), mt = mean(a.p_lie_on_truths, a.truths_judged);
      const sep = fin(ml) && fin(mt) ? ml - mt : null;
      return `<tr><td class="strong" title="${esc(a.key)}">${esc(lab(a.key))}</td><td class="num hide-sm">${a.games.size}</td>
        <td class="num">${a.sent}</td><td class="num">${a.lies}</td><td class="num hide-sm">${pct(a.lies, a.sent)}</td><td class="num hide-sm">${a.lies_caught}</td>
        <td class="num hide-sm">${a.received}</td><td class="num">${a.lies_received}</td><td class="num">${a.caught}</td><td class="num">${a.fooled}</td><td class="num">${a.false_alarms}</td>
        <td class="num hide-sm">${fin(ml) ? d2(ml) : '—'}</td><td class="num hide-sm">${fin(mt) ? d2(mt) : '—'}</td><td class="num">${fin(sep) ? fmt.signed(2)(sep) : '—'}</td></tr>`;
    }).join('');
    $('dec-table').innerHTML = `<thead><tr><th>${esc($('dec-by').textContent.replace(/^./, (c) => c.toUpperCase()))}</th><th class="num hide-sm">Games</th>
      <th class="num">Sent</th><th class="num">Lies told</th><th class="num hide-sm">Lie rate</th><th class="num hide-sm">Told, caught</th>
      <th class="num hide-sm">Received</th><th class="num">Lies received</th><th class="num">Caught</th><th class="num">Fooled</th><th class="num">False alarms</th>
      <th class="num hide-sm">p_lie on lies</th><th class="num hide-sm">p_lie on truths</th><th class="num">Separation</th></tr></thead><tbody>${trs || '<tr><td colspan="14" class="muted">No games in this scope.</td></tr>'}</tbody>`;
  }


  // ---- F1 telemetry ----------------------------------------------------------------------------------------
  // F1 cells: undefined -> a dash with the reason; fewer than 5 lies -> the value marked unstable
  function f1cell(m, k) {
    const v = m?.[k];
    if (!fin(v)) return `<span class="undef" title="${m && !m.n_lie ? 'no lies in this set' : 'undefined'}">—</span>`;
    return m.unstable ? `${d3(v)} <span class="unst" title="fewer than 5 lies: noisy">unstable</span>` : d3(v);
  }
  const nLie = (m) => (m ? `${m.n} <span class="muted">· ${m.n_lie} ${m.n_lie === 1 ? 'lie' : 'lies'}</span>` : '—');

  function renderF1() {
    const f = S.data.f1; if (!f) return;
    const L = f.live;
    // controls
    $('lf-scope').innerHTML = L.scopes.map((k) => `<option value="${k}" ${k === S.lf.scope ? 'selected' : ''}>${k === 'all' ? 'All games' : k === 'gate' ? 'Gate games' : 'Generation ' + k}</option>`).join('');
    $('lf-rule').innerHTML = [['decision', 'Distrust decision'], ['p_lie', 'p_lie ≥ threshold']].map(([k, l]) =>
      `<button type="button" role="radio" aria-checked="${S.lf.rule === k}" data-k="${k}">${l}</button>`).join('');
    $('lf-group').innerHTML = [['by_country', 'Country'], ['by_model', 'Model'], ['by_genome', 'Playbook']].map(([k, l]) =>
      `<button type="button" role="radio" aria-checked="${S.lf.group === k}" data-k="${k}">${l}</button>`).join('');
    $('lf-rule').querySelectorAll('button').forEach((b) => b.onclick = () => { S.lf.rule = b.dataset.k; tick(true); });
    $('lf-group').querySelectorAll('button').forEach((b) => b.onclick = () => { S.lf.group = b.dataset.k; renderF1(); });
    const o = L.overall;
    const truth = L.label === 'intent' ? "the sender's private intent label" : L.label === 'promise' ? "the judge's broken-promise verdict" : "the sender's intent label or a broken promise found by the judge";
    $('lf-note').textContent = `Receiver's judgment vs ${truth}, ${L.rule === 'decision' ? 'a distrust decision' : 'p_lie at or above the playbook threshold'} counted as predicting deception. F1 is undefined with no deceptive messages and marked unstable below ${L.unstable_below}.`
      + (L.unreviewed_games.length ? ` Not yet reviewed (intent labels only): ${L.unreviewed_games.join(', ')}.` : '');
    $('lf-kpis').innerHTML = kpi('Lie-F1', f1cell(o, 'lie_f1'), `n ${nLie(o)}`) + kpi('Macro-F1', f1cell(o, 'macro_f1'), `truth-F1 ${fin(o.truth_f1) ? d3(o.truth_f1) : '—'}`)
      + kpi('Precision', fin(o.precision) ? d3(o.precision) : '—', `${o.caught} of ${o.caught + o.false_alarms} distrusted were lies`)
      + kpi('Recall', fin(o.recall) ? d3(o.recall) : '—', `${o.caught} of ${o.n_lie} lies caught${o.unjudged ? ` · ${o.unjudged} unjudged` : ''}`);
    $('lf-conf').innerHTML = `<div class="h"></div><div class="h">Judged lie</div><div class="h">Judged truthful</div>
      <div class="h">Lie</div><div class="c tp"><b>${o.caught}</b><span>caught</span></div><div class="c fn"><b>${o.fooled}</b><span>fooled</span></div>
      <div class="h">Truthful</div><div class="c fp"><b>${o.false_alarms}</b><span>false alarms</span></div><div class="c"><b>${o.trusted_truths}</b><span>trusted</span></div>`;
    const grp = L[S.lf.group];
    const lab = (k) => (S.lf.group === 'by_model' ? shortModel(k) : k);
    $('lf-table').innerHTML = `<thead><tr><th>${{ by_country: 'Receiver country', by_model: 'Receiver model', by_genome: 'Receiver playbook' }[S.lf.group]}</th>
      <th class="num">Judged</th><th class="num">Lies</th><th class="num hide-sm">Precision</th><th class="num hide-sm">Recall</th><th class="num">Lie-F1</th><th class="num">Macro-F1</th>
      <th class="num hide-sm">Caught</th><th class="num hide-sm">Fooled</th><th class="num hide-sm">False alarms</th><th class="num hide-sm">Trusted</th></tr></thead><tbody>`
      + (Object.entries(grp).map(([k, m]) => `<tr><td class="strong" title="${esc(k)}">${esc(lab(k))}</td><td class="num">${m.n}</td><td class="num">${m.n_lie}</td>
        <td class="num hide-sm">${fin(m.precision) ? d3(m.precision) : '—'}</td><td class="num hide-sm">${fin(m.recall) ? d3(m.recall) : '—'}</td>
        <td class="num">${f1cell(m, 'lie_f1')}</td><td class="num">${f1cell(m, 'macro_f1')}</td>
        <td class="num hide-sm">${m.caught}</td><td class="num hide-sm">${m.fooled}</td><td class="num hide-sm">${m.false_alarms}</td><td class="num hide-sm">${m.trusted_truths}</td></tr>`).join('')
        || '<tr><td colspan="11" class="muted">No judged messages in this scope.</td></tr>') + '</tbody>';
    // rolling series
    const games = L.games;
    if (!S.lf.game || !games.some((g) => g.id === S.lf.game)) S.lf.game = (games.find((g) => g.status === 'running') || games[games.length - 1] || {}).id;
    $('lf-game').innerHTML = [...games].reverse().map((g) => `<option value="${esc(g.id)}" ${g.id === S.lf.game ? 'selected' : ''}>${esc(g.id)} · ${genLabel(g.gen)}${g.status === 'running' ? ' · live' : ''}</option>`).join('');
    $('lf-game').onchange = (e) => { S.lf.game = e.target.value; renderF1(); };
    const g = games.find((x) => x.id === S.lf.game);
    const two = [{ mark: 'line', color: 'var(--series-1)', label: 'Lie-F1' }, { mark: 'line', color: 'var(--series-2)', label: 'Macro-F1' }, { mark: 'ring', color: 'var(--muted)', label: 'Unstable (< 5 lies)' }];
    Viz.legend($('lf-series-legend'), two); Viz.legend($('lf-run-legend'), two);
    const pts = (list, k) => list.map((x) => ({ y: x[k], unstable: x.unstable, tip: `n ${x.n} · ${x.n_lie} lies · ${x.caught} caught · ${x.false_alarms} false alarms` }));
    const ser = g ? g.series.map((x) => x.overall) : [];
    Viz.seriesChart($('chart-lf-phase'), { labels: g ? g.series.map((x) => x.phase) : [], sub: ser.map((x) => `${x.n_lie} ${x.n_lie === 1 ? "lie" : "lies"}`), format: d3, endLabels: true,
      series: [{ name: 'Lie-F1', color: 'var(--series-1)', values: pts(ser, 'lie_f1') }, { name: 'Macro-F1', color: 'var(--series-2)', values: pts(ser, 'macro_f1') }],
      empty: g ? 'Undefined so far: no lies have been told to a judged receiver in this game.' : 'No games in this scope.', aria: 'Cumulative F1 by phase' });
    const rs = L.run_series;
    Viz.seriesChart($('chart-lf-run'), { labels: rs.map((x) => x.game), sub: rs.map((x) => `${x.n_lie} ${x.n_lie === 1 ? "lie" : "lies"}`), format: d3, endLabels: true,
      series: [{ name: 'Lie-F1', color: 'var(--series-1)', values: pts(rs, 'lie_f1') }, { name: 'Macro-F1', color: 'var(--series-2)', values: pts(rs, 'macro_f1') }],
      empty: 'Undefined so far: no lies in the games in scope.', aria: 'Cumulative F1 across games' });
    // gate F1: incumbent trajectory + candidates, sim vs human
    const gens = [0, ...new Set([...f.trajectory.map((t) => t.gen), ...f.candidates.map((c) => c.gen)])];
    const anyHuman = f.candidates.some((c) => c.human);
    const base = f.trajectory[0]?.sim;
    for (const [set, metric, target] of [['sim', 'lie_f1', 'chart-gf-sim-lie'], ['human', 'lie_f1', 'chart-gf-hum-lie'], ['sim', 'macro_f1', 'chart-gf-sim-mac'], ['human', 'macro_f1', 'chart-gf-hum-mac']]) {
      const why = set === 'human' ? (anyHuman ? 'No defined values yet.' : 'Human-val is scored only for candidates that pass the sim gate; none has yet.')
        : base && !base.n_lie ? `Undefined: the gate set has 0 lies in ${base.n} items.` : 'Appears once the gate baseline is scored.';
      Viz.genChart($(target), { gens: [...new Set(gens)], name: metric === 'lie_f1' ? 'Lie-F1' : 'Macro-F1', format: d3, empty: why,
        line: f.trajectory.map((t) => ({ x: t.gen, y: t[set]?.[metric], label: t.gen ? `After G${t.gen}: ${t.name}` : `Baseline ${t.name}` })),
        dots: f.candidates.map((c) => ({ x: c.gen, y: c[set]?.[metric], accepted: c.accepted, label: c.name })), aria: `${set} ${metric} by generation` });
    }
    Viz.legend($('gf-legend'), [{ mark: 'line', color: 'var(--accent)', label: 'Incumbent' }, { mark: 'ring', color: 'var(--ink-mark)', label: 'Rejected candidate' }, { mark: 'dot', color: 'var(--pos-mark)', label: 'Accepted' }]);
    $('gf-note').textContent = base ? `Sim gate: ${base.n} receiver-seat replay items, ${base.n_lie} lies. Human val: the held-out human Diplomacy validation split.` : '';
    const src = { baseline: 'baseline', gate_log: 'gate log', evals: 're-scored from evals' };
    $('gf-table').innerHTML = `<thead><tr><th>Playbook</th><th class="hide-sm">Tested</th><th>Verdict</th><th class="num">Sim n</th><th class="num">Lie-F1</th><th class="num">Macro-F1</th>
      <th class="num hide-sm">Best lie-F1</th><th class="num hide-sm">AUPRC</th><th class="num hide-sm">ECE</th><th class="num hide-sm">Human lie-F1</th><th class="num hide-sm">Human macro-F1</th><th class="hide-sm">Source</th></tr></thead><tbody>`
      + f.genomes.map((r) => `<tr><td class="strong">${esc(r.name)}</td><td class="hide-sm">${r.loop_gens.map((x) => 'G' + x).join(', ') || 'seed'}</td>
        <td>${r.accepted ? `<span class="pill pos">${ICON_OK}Accepted</span>` : r.loop_gens.length ? '<span class="pill">Rejected</span>' : '<span class="muted">—</span>'}</td>
        <td class="num">${r.sim ? nLie(r.sim) : '—'}</td><td class="num">${f1cell(r.sim, 'lie_f1')}</td><td class="num">${f1cell(r.sim, 'macro_f1')}</td>
        <td class="num hide-sm">${fin(r.sim?.best_lie_f1) ? `${d3(r.sim.best_lie_f1)} <span class="muted">@${d2(r.sim.best_threshold)}</span>` : '—'}</td>
        <td class="num hide-sm">${fin(r.sim?.auprc) ? d3(r.sim.auprc) : '—'}</td><td class="num hide-sm">${fin(r.sim?.ece) ? d3(r.sim.ece) : '—'}</td>
        <td class="num hide-sm">${f1cell(r.human, 'lie_f1')}</td><td class="num hide-sm">${f1cell(r.human, 'macro_f1')}</td><td class="hide-sm muted">${esc(src[r.sim_source] || 'not scored')}</td></tr>`).join('') + '</tbody>';
  }
  $('lf-scope').addEventListener('change', (e) => { S.lf.scope = e.target.value; tick(true); });

  // ---- changelog -----------------------------------------------------------------------------------------
  const opCls = (op) => (op === 'add' ? 'add' : op === 'delete' ? 'del' : 'edit');
  const evText = (ev) => ev.filter((e) => e.msg_ids?.length).map((e) => `${esc(e.game)} ${e.msg_ids.map((i) => '#' + i).join(' ')}`).join(' · ');
  function deltaLine(c) {
    const a = c.ci?.auprc || {};
    return `<span class="deltas"><span>ΔAUPRC <b>${s3(a.delta)}</b> [${s3(a.lo)}, ${s3(a.hi)}]</span><span>p_gain <b>${d2(a.p_gain)}</b></span>`
      + `<span>Δlie-F1 <b>${s3(c.delta?.lie_f1)}</b></span><span>Δmacro-F1 <b>${s3(c.delta?.macro_f1)}</b></span><span>ΔECE <b>${s3(c.delta?.ece)}</b></span>`
      + (c.human ? `<span>human ΔAUPRC <b>${s3(c.human.delta)}</b></span>` : '') + '</span>';
  }
  function renderChangelog() {
    const cl = S.data.changelog; if (!cl) return;
    const gpg = S.data.run?.config?.games_per_generation;
    $('cl-note').textContent = cl.entries.length ? `Newest first. Votes = games whose review proposed the edit; evidence = the message ids the judge cited. ${cl.lint_note}.` : '';
    S.clOpen ||= new Set();
    if (!cl.entries.length) { $('changelog').innerHTML = '<p class="empty">The first generation has not reached the judge yet. Every playbook change will be listed here with its reason, votes and gate result.</p>'; return; }
    $('changelog').innerHTML = cl.entries.map((e, i) => {
      const open = S.clOpen.has(e.gen) || (!S.clTouched && i === 0);
      const head = e.live ? `<span class="pill warn">In progress</span><span class="muted">${esc((e.stage || '').replace(/^generation \d+: /, ''))}</span>`
        : e.adopted ? `<span class="pill pos">${ICON_OK}Adopted ${esc(e.adopted.name)}</span>` : `<span class="pill">Kept ${esc(e.incumbent_after_name || e.incumbent_before_name || '—')}</span>`;
      const props = e.proposals.map((x) => `<li><span class="op ${opCls(x.op)}">${esc(x.op)}${x.heuristic_id ? ' ' + esc(x.heuristic_id) : ''}</span> ${esc(x.text || '')}
          <p class="why">Judge's reason: ${esc(x.rationale || '—')}</p>
          <p class="why">${x.lint === 'rejected' ? `<span class="pill neg">${ICON_X}Lint rejected</span> ${esc((x.lint_errors || []).join('; '))}` : `${x.votes != null ? voteText(x, gpg) : ''}${x.used_by.length ? ` · used by ${esc(x.used_by.join(', '))}` : ' · in no candidate'}`}</p>
          ${evText(x.evidence) ? `<p class="ev">Evidence: ${evText(x.evidence)}</p>` : ''}</li>`).join('');
      const cands = e.candidates.map((c) => `<tr><td class="strong">${esc(c.name)}<small class="muted" style="display:block">${esc(kindName[c.kind] || c.kind)}</small></td>
          <td>${deltaLine(c)}</td><td>${c.accepted ? `<span class="pill pos">${ICON_OK}Accepted</span>` : '<span class="pill">Rejected</span>'}<small class="muted" style="display:block">${esc(c.reasons.join('; '))}</small></td></tr>`).join('');
      const acc = e.accepted[0];
      const applied = acc?.changes ? [...acc.changes.added.map((h) => `<li><span class="op add">Added</span> <b>${esc(h.id)}</b> ${esc(h.text)}</li>`),
        ...acc.changes.edited.map((h) => `<li><span class="op edit">Edited</span> <b>${esc(h.id)}</b> <del>${esc(h.before)}</del> → ${esc(h.text)}</li>`),
        ...acc.changes.removed.map((h) => `<li><span class="op del">Removed</span> <b>${esc(h.id)}</b> <del>${esc(h.text)}</del></li>`),
        ...(acc.changes.exemplars ? [`<li><span class="op edit">Exemplars</span> ${acc.changes.exemplars.before} → ${acc.changes.exemplars.after}</li>`] : []),
        ...acc.changes.effort.map((x) => `<li><span class="op edit">Effort</span> ${esc(x.key)}: ${esc(x.before)} → ${esc(x.after)}</li>`)].join('') : '';
      const verdict = acc ? `<p class="verdict-line"><b>${esc(acc.name)}</b> replaced ${esc(e.incumbent_before_name)}. ${deltaLine(acc)}</p>`
        : e.done ? `<p class="verdict-line">No candidate passed the gate; ${esc(e.incumbent_after_name)} stays.</p>` : '';
      return `<details class="cl" data-gen="${e.gen}" ${open ? 'open' : ''}><summary><b>Generation ${e.gen}</b>${head}
          <span class="muted">${e.proposals.length} proposed · ${e.lint_rejected} lint-rejected · ${e.candidates.length} candidates gated</span></summary>
        <div class="body">${verdict}${applied ? `<h4>Applied to the playbook</h4><ul class="diffs">${applied}</ul>` : ''}
          <h4>Judge proposals</h4>${props ? `<ul class="diffs">${props}</ul>` : '<p class="empty">None yet.</p>'}
          ${cands ? `<h4>Candidates on the gate</h4><div class="tablewrap"><table class="table"><tbody>${cands}</tbody></table></div>` : ''}</div></details>`;
    }).join('');
    $('changelog').querySelectorAll('details').forEach((d) => d.addEventListener('toggle', () => {
      S.clTouched = true; const g = +d.dataset.gen; if (d.open) S.clOpen.add(g); else S.clOpen.delete(g);
    }));
  }


  // ---- standings ---------------------------------------------------------------------------------------
  const pctv = (v) => (fin(v) ? Math.round(v * 100) + '%' : '—');
  const winShade = (r) => (fin(r) ? `background: color-mix(in srgb, var(--series-1) ${Math.round(8 + r * 40)}%, var(--raised))` : '');
  function renderStandings() {
    const st = S.data.standings; if (!st) return;
    const models = st.by_model, countries = st.by_country;
    // finding
    const lead = models[0];
    let find = st.finished ? '' : 'No finished games yet: winners and leaderboards fill in as games end.';
    if (lead && st.finished) {
      const cs = Object.keys(lead.countries || {});
      find = `<b>${esc(lead.short)}</b> leads with ${lead.wins} win${lead.wins === 1 ? '' : 's'} in ${lead.games} game${lead.games === 1 ? '' : 's'} (${pctv(lead.win_rate)}), mean ${d2(lead.mean_sc)} supply centres`
        + (cs.length === 1 ? `, but it has only played <b>${esc(cs[0])}</b>, so its result cannot be told apart from that country's starting position.` : `, across ${cs.length} countries.`);
      if (!st.rotated && models.length > 1) find += ' Every model has played a single country so far; the matrix below shows when rotation starts separating model from position.';
    }
    $('st-finding').innerHTML = find;
    $('st-model-note').textContent = `Finished games only. Deception counts each model as sender (lies, broken promises) and as receiver (caught, fooled, false alarms); deceptive = ${st.deception_label}.`;
    const nModels = st.matrix.models.length;
    $('st-models').innerHTML = `<thead><tr><th>Model</th><th class="num">Games</th><th class="num">Wins</th><th class="num">Win rate</th><th class="num">Mean SC</th><th class="num hide-sm">SC share</th><th class="hide-sm">Countries played</th>
      <th class="num hide-sm">Lies told</th><th class="num hide-sm">Promises broken</th><th class="num">Caught</th><th class="num">Fooled</th><th class="num hide-sm">False alarms</th><th class="num">Catch rate</th><th class="num">Lie-F1</th></tr></thead><tbody>`
      + (models.map((r) => `<tr><td class="strong" title="${esc(r.name)}">${esc(r.short)}</td><td class="num">${r.games}</td><td class="num wins"><b>${r.wins}</b></td>
        <td class="num">${pctv(r.win_rate)}<span class="meter" aria-hidden="true"><i style="width:${(r.win_rate || 0) * 100}%"></i></span></td>
        <td class="num">${d2(r.mean_sc)}</td><td class="num hide-sm">${pctv(r.mean_sc_share)}</td>
        <td class="hide-sm muted">${Object.entries(r.countries || {}).map(([c, n]) => `${esc(c.slice(0, 3))} ${n}`).join(' · ')}</td>
        <td class="num hide-sm">${r.lies_told ?? 0}</td><td class="num hide-sm">${r.promises_broken ?? 0}</td><td class="num">${r.caught ?? 0}</td><td class="num">${r.fooled ?? 0}</td>
        <td class="num hide-sm">${r.false_alarms ?? 0}</td><td class="num">${pctv(r.catch_rate)}</td>
        <td class="num">${f1cell(r.f1, 'lie_f1')}<small class="muted" style="display:block">${r.f1.n_lie} deceptive</small></td></tr>`).join('')
        || `<tr><td colspan="14" class="muted">No finished games yet.</td></tr>`) + '</tbody>';
    $('st-countries').innerHTML = `<thead><tr><th>Country</th><th class="num">Games</th><th class="num">Wins</th><th class="num">Win rate</th><th class="num">Mean SC</th><th class="num hide-sm">SC share</th></tr></thead><tbody>`
      + (countries.map((r) => `<tr><td class="strong">${esc(r.name)}</td><td class="num">${r.games}</td><td class="num wins"><b>${r.wins}</b></td>
        <td class="num">${pctv(r.win_rate)}<span class="meter" aria-hidden="true"><i style="width:${(r.win_rate || 0) * 100}%"></i></span></td><td class="num">${d2(r.mean_sc)}</td><td class="num hide-sm">${pctv(r.mean_sc_share)}</td></tr>`).join('')
        || '<tr><td colspan="6" class="muted">No finished games yet.</td></tr>') + '</tbody>';
    // matrix
    const M = st.matrix;
    const colTot = Object.fromEntries(M.countries.map((c) => [c, M.models.reduce((a, m) => a + M.cells[m][c].games, 0)]));
    $('st-matrix').innerHTML = M.models.length ? `<thead><tr><th></th>${M.countries.map((c) => `<th scope="col" title="${esc(c)}">${esc(c.slice(0, 3))}</th>`).join('')}</tr></thead><tbody>`
      + M.models.map((m) => `<tr><th scope="row" title="${esc(m)}">${esc(shortModel(m))}</th>${M.countries.map((c) => {
        const x = M.cells[m][c];
        return x.games ? `<td style="${winShade(x.win_rate)}" title="${esc(shortModel(m))} as ${esc(c)}: ${x.wins} win(s) in ${x.games} game(s), mean SC ${d2(x.mean_sc)}"><b>${x.wins}/${x.games}</b><span>${d2(x.mean_sc)} SC</span></td>`
          : '<td class="none" aria-label="not played">—</td>'; }).join('')}</tr>`).join('')
      + `</tbody><tfoot><tr><th scope="row">games</th>${M.countries.map((c) => `<td>${colTot[c]}</td>`).join('')}</tr></tfoot>` : '<tbody><tr><td class="none">No games yet.</td></tr></tbody>';
    const played = M.models.map((m) => M.countries.filter((c) => M.cells[m][c].games).length);
    $('st-matrix-note').textContent = !st.finished ? 'Each cell will read wins / games for that model playing that country.'
      : played.every((n) => n <= 1) && M.models.length > 1 ? 'Each model has played one country only, so a strong row is indistinguishable from a strong starting position. Rotation fills the other cells.'
      : `Each cell: wins / games and mean supply centres. ${played.filter((n) => n === M.countries.length).length} of ${M.models.length} models have played every country.`;
    $('st-matrix-legend').innerHTML = [0, 0.5, 1].map((r) => `<span><i class="swatch" style="${winShade(r)}"></i>${pctv(r)} wins</span>`).join('');
    // winners per game
    $('st-games').innerHTML = `<thead><tr><th>Game</th><th>Gen</th><th>Winner</th><th>Model</th><th class="num">SC</th><th class="hide-sm">Final supply centres</th><th class="hide-sm">Status</th></tr></thead><tbody>`
      + ([...st.games].reverse().map((g) => {
        const live = g.status !== 'finished';
        const sc = Object.entries(g.final_sc).sort((a, b) => b[1] - a[1]).map(([p, n]) => `${esc(p.slice(0, 3))} ${n}`).join(' · ');
        return `<tr><td class="strong">${esc(g.game_id)}</td><td>${genLabel(g.gen)}</td>
          <td>${g.winners.map((p) => esc(p)).join(', ')}${g.solo ? ' <span class="pill pos">Solo</span>' : ''}${live ? ' <span class="pill warn">Leading</span>' : g.winners.length > 1 ? ' <span class="pill">Shared</span>' : ''}</td>
          <td>${g.winner_models.map((m) => esc(shortModel(m))).join(', ')}</td><td class="num">${g.winner_sc}</td>
          <td class="hide-sm muted">${sc}</td><td class="hide-sm">${live ? `Playing · ${esc(g.final_phase)}` : `Finished · ${esc(g.final_phase)}`}</td></tr>`; }).join('')
        || '<tr><td colspan="7" class="muted">No games yet.</td></tr>') + '</tbody>';
  }

  // ---- decision graph ------------------------------------------------------------------------------------
  let graph = null;
  function ensureGraph() {
    if (graph) return graph;
    graph = DecisionGraph.create($('canvas'), { onSelect: inspect });
    $('z-in').addEventListener('click', () => graph.zoomIn());
    $('z-out').addEventListener('click', () => graph.zoomOut());
    $('z-fit').addEventListener('click', () => graph.fit());
    $('g-game').addEventListener('change', (e) => { S.g.game = e.target.value; S.g.phase = ''; S.g.powers = null; S.g.version = null; setGraphHash(); });
    $('g-phase').addEventListener('change', (e) => { S.g.phase = e.target.value; S.g.version = null; setGraphHash(); });
    $('g-chains').addEventListener('click', () => { S.g.chains = !S.g.chains; $('g-chains').setAttribute('aria-pressed', S.g.chains); drawGraph(true); if (S.g.chains) graph.revealChains(); });
    Viz.legend($('g-legend'), [
      { mark: 'box', color: 'var(--fooled-mark)', label: 'Deceptive message / receiver fooled' },
      { mark: 'box', color: 'var(--caught-mark)', label: 'Lie caught' },
      { mark: 'box', color: 'var(--warn-mark)', label: 'False alarm' },
      { mark: 'line', color: 'var(--axis)', label: 'wrote · judged · kept · result' },
      { mark: 'line', color: 'var(--accent)', dash: '4 3', label: 'cited: later reasoning names the sender' },
      { mark: 'line', color: 'var(--axis)', dash: '2 4', label: 'memory carried to next phase' }]);
    return graph;
  }
  function setGraphHash() {
    const h = `#graph/${encodeURIComponent(S.g.game || '')}${S.g.phase ? '/' + encodeURIComponent(S.g.phase) : ''}`;
    if (location.hash !== h) location.hash = h; else loadGraph(true);
  }

  async function loadGraph(gamesChanged) {
    ensureGraph();
    const games = S.data.games?.games || [];
    if (!games.length) { $('g-note').textContent = 'No games yet.'; return; }
    if (!S.g.game || !games.some((g) => g.id === S.g.game)) S.g.game = (games.find((g) => g.status === 'running') || games[games.length - 1]).id;
    const g = games.find((x) => x.id === S.g.game);
    if (gamesChanged || $('g-game').options.length !== games.length) {
      $('g-game').innerHTML = [...games].reverse().map((x) => `<option value="${esc(x.id)}" ${x.id === S.g.game ? 'selected' : ''}>${esc(x.id)} · ${genLabel(x.gen)}${x.status === 'running' ? ' · live' : ''}</option>`).join('');
    }
    $('g-game').value = S.g.game;
    const phases = g.phases || [];
    const want = ['', ...phases].join('|');
    if ($('g-phase').dataset.list !== want) {
      $('g-phase').innerHTML = `<option value="">Whole game</option>` + phases.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
      $('g-phase').dataset.list = want;
    }
    if (S.g.phase && !phases.includes(S.g.phase)) S.g.phase = '';
    $('g-phase').value = S.g.phase;
    if (S.g.version === g.version && S.g.loaded === `${g.id}|${S.g.phase}`) return;
    const qs = `game=${encodeURIComponent(g.id)}${S.g.phase ? '&phase=' + encodeURIComponent(S.g.phase) : ''}`;
    const key = `graph:${g.id}|${S.g.phase}`;
    await pull('graph', qs, key);
    S.g.version = g.version; S.g.loaded = `${g.id}|${S.g.phase}`; S.g.key = key;
    drawGraph(false);
  }

  function drawGraph(keep = true) {
    const d = S.data[S.g.key]; if (!d) return;
    const powers = d.powers || [];
    if (!S.g.powers || !S.g.powers.every((p) => powers.includes(p))) S.g.powers = [...powers];
    $('g-powers').innerHTML = powers.map((p) => `<button type="button" aria-pressed="${S.g.powers.includes(p)}" data-p="${esc(p)}" title="${esc(shortModel((d.models || {})[p]))}">${esc(p.slice(0, 3))}</button>`).join('');
    $('g-powers').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      const p = b.dataset.p, on = S.g.powers.includes(p);
      if (on && S.g.powers.length === 1) return;
      S.g.powers = on ? S.g.powers.filter((x) => x !== p) : powers.filter((x) => x === p || S.g.powers.includes(x));
      drawGraph(true);
    }));
    $('g-chains').setAttribute('aria-pressed', S.g.chains);
    const lies = d.chains || [];
    $('g-note').textContent = `${d.nodes.length} decisions · ${d.edges.length} links · ${lies.length} deceptive message${lies.length === 1 ? '' : 's'}${d.status === 'running' ? ' · live' : ''}`;
    graph.render(d, { powers: S.g.powers, chains: S.g.chains, keepView: keep });
    renderChains(d);
  }

  function renderChains(d) {
    const lies = d.chains || [];
    const caught = lies.filter((c) => c.outcome === 'caught').length;
    $('chains-note').textContent = lies.length ? `${caught} of ${lies.length} caught. Select one to light its path: sender's plan → message → receiver's judgment → receiver's plan and orders → supply centres.` : '';
    $('chains-table').innerHTML = lies.length ? `<thead><tr><th class="num">Msg</th><th>Phase</th><th>From → to</th><th>Outcome</th><th class="num">p_lie</th><th class="hide-sm">Receiver cites sender later</th></tr></thead><tbody>`
      + lies.map((c, i) => `<tr class="clickable" tabindex="0" data-i="${i}"><td class="num">#${c.msg_id}</td><td>${esc(c.phase)}</td><td>${esc(c.sender.slice(0, 3))} → ${esc(c.recipient.slice(0, 3))}</td>
        <td>${outcomePill(c.outcome)}</td><td class="num">${c.p_lie != null ? d2(c.p_lie) : '—'}</td><td class="hide-sm">${c.cited ? 'Yes' : '<span class="muted">No</span>'}</td></tr>`).join('') + '</tbody>'
      : '<tbody><tr><td class="muted">No deceptive messages in this view.</td></tr></tbody>';
    $('chains-table').querySelectorAll('tr[data-i]').forEach((tr) => {
      const go = () => {
        const c = lies[+tr.dataset.i];
        if (!c.path.every((p) => S.g.powers.includes(d.nodes.find((n) => n.id === p)?.power))) { S.g.powers = [...d.powers]; drawGraph(true); }
        graph.showChain(c);
        const msg = d.nodes.find((n) => n.id === c.path.find((p) => p.includes('msg:')));
        if (msg) inspect(msg, d.edges.filter((e) => e.to === msg.id), d.edges.filter((e) => e.from === msg.id), true);
        $('canvas').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      };
      tr.addEventListener('click', go);
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
  }

  function inspect(n, inc, out, viaChain) {
    const d = S.data[S.g.key];
    if (!n) { $('i-title').textContent = 'Select a node'; $('i-body').innerHTML = '<p class="note">Click a plan, message, judgment or orders node to read its reasoning. Its upstream and downstream decisions in the same phase light up.</p>'; return; }
    const byId = (id) => d.nodes.find((x) => x.id === id);
    const model = (d.models || {})[n.power];
    $('i-title').textContent = `${n.power} · ${DecisionGraph.KIND[n.kind] || n.kind}${n.phase || d.phase ? ' · ' + (n.phase || d.phase) : ''}`;
    const link = (e, other) => { const o = byId(other); if (!o) return '';
      return `<li><button type="button" data-go="${esc(o.id)}"><span class="lab">${esc(e.kind)}${e.label ? ' · ' + esc(e.label) : ''}</span><br>${esc(o.power)} ${esc(DecisionGraph.KIND[o.kind] || o.kind)}: ${esc(o.label)}</button>${(e.evidence || []).map((s) => `<blockquote>${esc(s)}</blockquote>`).join('')}</li>`; };
    $('i-body').innerHTML = `${model ? `<p class="note">Model ${esc(model)}</p>` : ''}${n.tone ? `<p style="margin:8px 0">${n.kind === 'message' ? (n.tone === 'bad' ? `<span class="pill neg">${ICON_WARN}Sender labelled it deceptive</span>` : '') : n.kind === 'judgment' ? outcomePill(n.tone === 'good' ? 'caught' : n.tone === 'bad' ? 'fooled' : 'false_alarm') : ''}</p>` : ''}
      <p class="detail">${esc(n.detail || n.label)}</p>
      ${inc.length ? `<p class="sect">Comes from</p><ul class="links">${inc.map((e) => link(e, e.from)).join('')}</ul>` : ''}
      ${out.length ? `<p class="sect">Leads to</p><ul class="links">${out.map((e) => link(e, e.to)).join('')}</ul>` : ''}
      ${viaChain ? '<p class="note" style="margin-top:12px">Path lit in red. Click empty canvas to clear.</p>' : ''}`;
    $('i-body').querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', () => graph.select(b.dataset.go)));
  }

  // ---- routing ---------------------------------------------------------------------------------------------
  function route() {
    const [v, a, b] = location.hash.replace(/^#/, '').split('/').map(decodeURIComponent);
    const view = v === 'standings' ? 'deception' : NEEDS[v] ? v : 'overview';
    if (view === 'graph') {
      if (a && a !== S.g.game) { S.g.game = a; S.g.powers = null; S.g.version = null; }
      const ph = b || '';
      if (ph !== S.g.phase) { S.g.phase = ph; S.g.version = null; }
    }
    const switched = view !== S.view;
    S.view = view;
    for (const sec of document.querySelectorAll('.view')) sec.hidden = sec.id !== 'view-' + view;
    for (const a2 of document.querySelectorAll('.rail [data-view]')) {
      if (a2.dataset.view === view) a2.setAttribute('aria-current', 'page'); else a2.removeAttribute('aria-current');
    }
    if (switched) { S.raw = Object.fromEntries(Object.entries(S.raw).filter(([k]) => k.startsWith('graph:') || k.startsWith('review:'))); window.scrollTo(0, 0); }
    tick(true);
  }

  // ---- theme ------------------------------------------------------------------------------------------------
  const toggleTheme = () => {
    const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('rsi-theme', next); } catch (e) { /* private mode */ }
    if (S.view === 'f1') renderF1();
  };
  $('theme').addEventListener('click', toggleTheme);
  $('theme-sm').addEventListener('click', toggleTheme);
  $('dec-scope').addEventListener('change', (e) => { S.decScope = e.target.value; renderDeception(); });

  // charts measure their width: redraw on resize
  let rz;
  addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => {
    if (S.view === 'overview' && S.data.run && S.data.gates) renderTrend(S.data.run, S.data.gates);
  }, 150); });
  addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(true); });
  route();
  setInterval(() => tick(false), POLL_MS);
})();
