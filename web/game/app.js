/* Diplomacy Live: polls the viewer's JSON API and patches the page in place.
   Nothing is re-rendered wholesale on a poll: cards, phases, messages and judgments
   are created once and updated when their own data changes, so scroll positions,
   open details and focus all survive. */
(() => {
  'use strict';

  const POLL_MS = 1800;
  const POLL_HIDDEN_MS = 8000;
  const SIDE_MS = 4000;
  const ORDER = ['AUSTRIA', 'ENGLAND', 'FRANCE', 'GERMANY', 'ITALY', 'RUSSIA', 'TURKEY'];
  const SEASON = { S: 'Spring', F: 'Fall', W: 'Winter' };
  const SEASON_SHORT = { S: 'Spr', F: 'Fall', W: 'Win' };
  const KIND = { M: 'Movement', R: 'Retreats', A: 'Adjustments' };
  const TONE = { good: 'good', bad: 'bad', warn: 'warn' };

  const $ = (id) => document.getElementById(id);
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const pinned = new URLSearchParams(location.search).get('f');
  const STICKY_AFTER_END_MS = 20000;   // keep a finished game (winner banner) on screen this long

  const S = {
    path: null,
    d: null,
    agents: [],
    phases: [],
    sel: null,
    follow: true,
    pair: 'all',
    pairKeys: '',
    nodes: new Map(),      // msg id -> { el, sig }
    lastSep: null,
    mapOk: new Map(),      // phase -> true | false
    mapProbe: 0,
    mapShown: null,
    mapWant: null,
    ordersSig: '',
    cards: new Map(),
    sc: {},
    newCount: 0,
    rsi: null,
    st: null,             // /api/standings
    stSig: '',
    lastOk: 0,
    failed: false,
  };

  /* ── helpers ─────────────────────────────────────────── */
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }
  const cap = (s) => s ? s.charAt(0) + s.slice(1).toLowerCase() : '';
  const cvar = (p) => `var(--${p}, var(--ink3))`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
  function dots() { const s = el('span', 'dots'); s.setAttribute('aria-hidden', 'true'); s.append(el('i'), el('i'), el('i')); return s; }

  function phaseWords(p) {
    if (!p || p.length < 6) return { season: p || '', kind: '', year: '' };
    return { season: `${SEASON[p[0]] || p[0]} ${p.slice(1, 5)}`, kind: KIND[p.slice(-1)] || '', year: p.slice(1, 5) };
  }
  const phaseLabel = (p) => { const w = phaseWords(p); return w.kind ? `${w.season} · ${w.kind}` : w.season; };

  const VENDOR_WORDS = { deepseek: 'DeepSeek', gpt: 'GPT', claude: 'Claude', mistral: 'Mistral', llama: 'Llama', qwen: 'Qwen', gemini: 'Gemini', inkling: 'Inkling' };
  function modelName(id) {
    if (!id) return 'Unassigned';
    const base = id.split('/').pop();
    return base.split(/[-_]/).map((w) => {
      const lw = w.toLowerCase();
      if (VENDOR_WORDS[lw]) return VENDOR_WORDS[lw];
      if (/^v?\d/.test(w)) return w.toUpperCase().replace(/^V(?=\d)/, 'V');
      return w.charAt(0).toUpperCase() + w.slice(1);
    }).join(' ');
  }
  // Short model name: the standings' own short name when it has one.
  function shortModel(id) {
    const m = S.st && (S.st.by_model || []).find((x) => x.name === id);
    return (m && m.short) || modelName(id);
  }
  const fmtNum = (v, dp) => (v == null || isNaN(v)) ? '–' : (Number.isInteger(v) ? String(v) : v.toFixed(dp));

  // Who holds the most supply centres among the agent countries, from one sc map.
  function leadersOf(sc, among) {
    let best = -1, who = [];
    for (const p of among) {
      const v = (sc || {})[p];
      if (v == null) continue;
      if (v > best) { best = v; who = [p]; } else if (v === best) who.push(p);
    }
    return who.length ? { who, sc: best } : null;
  }
  // phase -> leaders after that phase resolved.
  function phaseLeaders() {
    const out = new Map();
    for (const h of (S.d && S.d.sc_history) || []) {
      const l = leadersOf(h.sc, S.agents);
      if (l) out.set(h.phase, l);
    }
    if (!out.size) {
      const g = currentStanding();
      for (const pl of (g && g.phase_leaders) || []) out.set(pl.phase, { who: pl.leaders, sc: pl.sc });
    }
    return out;
  }
  // This game's entry in /api/standings, and the one played before it.
  function standingIndex() {
    const games = (S.st && S.st.games) || [];
    const d = S.d;
    if (!d) return -1;
    for (let i = games.length - 1; i >= 0; i--) {
      const g = games[i];
      if (g.game_id === d.game_id && (!g.group || !d.dir || d.dir.includes(g.group))) return i;
    }
    return -1;
  }
  function currentStanding() { const i = standingIndex(); return i >= 0 ? S.st.games[i] : null; }
  function previousStanding() {
    const games = (S.st && S.st.games) || [];
    const i = standingIndex();
    if (i > 0) return games[i - 1];
    if (i < 0 && games.length) return games[games.length - 1];
    return null;
  }

  function playbookOf(seat) { const parts = (seat || '').split(' · '); return parts.length > 1 ? parts[parts.length - 1] : ''; }

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    const text = await r.text();
    try { return JSON.parse(text); } catch (e) {
      // Python's json.dumps writes bare NaN / Infinity for undefined metrics; read them as null.
      return JSON.parse(text.replace(/([:\[,]\s*)-?(?:NaN|Infinity)(?=\s*[,\]}])/g, '$1null'));
    }
  }

  /* ── main poll ───────────────────────────────────────── */
  async function tick() {
    try {
      let path = pinned;
      if (!path) {
        // Sticky: stay on the game being shown while it runs, show its result for a while after it
        // ends, and only then follow the server to the next game (avoids hopping between parallel games).
        const st = S.d && S.d.status;
        if (S.path && st === 'running') path = S.path;
        else if (S.path && st && Date.now() - (S.endedAt = S.endedAt || Date.now()) < STICKY_AFTER_END_MS) path = S.path;
        else path = (await getJSON('/api/current')).path;
      }
      if (path) {
        if (path !== S.path) resetGame(path);
        const d = await getJSON('/api/game?f=' + encodeURIComponent(path));
        if (path === S.path) apply(d);
      }
      S.lastOk = Date.now();
      S.failed = false;
    } catch (e) {
      S.failed = true;
      console.warn('poll failed', e);
    }
    renderLive();
    setTimeout(tick, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
  }

  async function sideTick() {
    try {
      const [rsi, stats] = await Promise.all([getJSON('/api/rsi').catch(() => null), getJSON('/api/stats').catch(() => null)]);
      if (rsi) { S.rsi = rsi; renderRSI(); }
      if (stats) renderStats(stats);
    } catch (e) { /* the main poll reports connectivity */ }
    setTimeout(sideTick, document.hidden ? POLL_HIDDEN_MS * 2 : SIDE_MS);
  }

  async function standTick() {
    try {
      const st = await getJSON('/api/standings');
      const sig = JSON.stringify(st);
      if (sig !== S.stSig) {
        S.stSig = sig;
        S.st = st;
        renderStandings();
        if (S.d) { renderCards(); renderScrub(); renderIntermission(); }
      }
    } catch (e) { setText($('lbNote'), 'Standings unavailable'); }
    setTimeout(standTick, document.hidden ? 15000 : 5000);
  }

  function resetGame(path) {
    S.path = path;
    S.endedAt = 0;
    S.d = null;
    S.phases = [];
    S.sel = null;
    S.follow = true;
    S.pair = 'all';
    S.pairKeys = '';
    S.nodes.clear();
    S.lastSep = null;
    S.mapOk.clear();
    S.mapShown = null;
    S.mapWant = null;
    S.ordersSig = '';
    S.cards.clear();
    S.sc = {};
    S.newCount = 0;
    $('powers').replaceChildren();
    $('scrub').replaceChildren();
    $('pairs').replaceChildren();
    $('thread').replaceChildren();
    $('orders').replaceChildren();
    for (const id of ['mapA', 'mapB']) { const im = $(id); im.classList.remove('front'); im.removeAttribute('src'); }
    $('newPill').hidden = true;
  }

  function apply(d) {
    const first = !S.d;
    S.d = d;
    S.agents = Object.keys(d.models || {}).sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
    S.phases = d.phases || [];
    // A finished game lists the next, never-played phase; end the timeline at the last one played.
    const fin = d.status !== 'running' && d.summary && d.summary.final_phase;
    if (fin && S.phases.includes(fin)) S.phases = S.phases.slice(0, S.phases.indexOf(fin) + 1);
    const last = S.phases[S.phases.length - 1] || null;
    if (S.follow || !S.sel || !S.phases.includes(S.sel)) S.sel = last;

    renderTitle();
    renderCards();
    renderScrub();
    renderBoard();
    renderOrders();
    renderPairs();
    renderThread(first);
    renderIntermission();
    if (S.st && first) renderStandings();
  }

  /* ── title + live ───────────────────────────────────── */
  function tallies() {
    const d = S.d, out = { lies: 0, caught: 0, fooled: 0, fa: 0 };
    for (const id in d.sends || {}) if (d.sends[id].intent === 'deceptive') out.lies++;
    for (const id in d.outcomes || {}) {
      const label = d.outcomes[id][0];
      if (label === 'lie caught') out.caught++;
      else if (label === 'fooled') out.fooled++;
      else if (label === 'false alarm') out.fa++;
    }
    return out;
  }

  function renderTitle() {
    const d = S.d;
    const last = S.phases[S.phases.length - 1];
    const w = phaseWords(last);
    setText($('phaseSeason'), w.season || 'Starting');
    setText($('phaseKind'), w.kind ? `· ${w.kind}` : '');
    document.title = `${w.season || 'Game'} · Diplomacy Live`;

    const n = (d.messages || []).length;
    const t = tallies();
    const prefix = d.status === 'finished' ? 'Final count: ' : d.status === 'error' ? 'Stopped: ' : '';
    const html = `${prefix}<b>${plural(n, 'message')}</b> · ` +
      `<b class="${t.lies ? 'lie' : ''}">${plural(t.lies, 'lie')}</b> told · ` +
      `<b>${t.caught}</b> caught · <b>${t.fooled}</b> fooled · <b>${plural(t.fa, 'false alarm')}</b>`;
    if ($('finding').innerHTML !== html) $('finding').innerHTML = html;

    $('gameId').innerHTML = `Game <b></b>`;
    $('gameId').querySelector('b').textContent = d.game_id || '–';
    const gl = $('graphLink');
    gl.hidden = !S.path;
    if (S.path && gl.getAttribute('href') !== S.path) gl.href = S.path;
  }

  function renderLive() {
    const live = $('live');
    let state = 'connecting', text = 'Connecting';
    if (S.failed) { state = 'error'; text = 'Reconnecting'; }
    else if (S.d) {
      if (S.d.status === 'running') { state = 'live'; text = 'Live'; }
      else if (S.d.status === 'finished') { state = 'finished'; text = pinned ? 'Finished' : 'Between games'; }
      else if (S.d.status === 'error') { state = 'error'; text = 'Game error'; }
    }
    if (live.dataset.state !== state) live.dataset.state = state;
    setText($('liveText'), text);
  }

  function renderRSI() {
    const r = S.rsi;
    const name = r.incumbent && r.incumbent.name ? `playbook ${r.incumbent.name}` : '';
    const stage = r.stage ? cap(r.stage) : 'RSI';
    setText($('rsiText'), [stage, name].filter(Boolean).join(' · '));
    if (S.d && S.d.status !== 'running') renderIntermission();
  }

  function renderStats(st) {
    const spend = ((st.player && st.player.est_cost_usd) || 0) + ((st.judge && st.judge.est_cost_usd) || 0);
    setText($('spend'), spend < 10 ? `$${spend.toFixed(2)}` : `$${spend.toFixed(1)}`);
    const lat = st.player && st.player.latency_ms && st.player.latency_ms.high;
    setText($('p50'), lat && lat.count ? `${(lat.p50 / 1000).toFixed(1)} s` : '–');
  }

  /* ── country cards ───────────────────────────────────── */
  function activityText(a) {
    if (!a) return null;
    const round = (a.match(/round (\d+)/) || [])[1];
    const r = round ? ` · round ${round}` : '';
    if (/writ|compos|messag/.test(a) && !/judg/.test(a)) return `Composing${r}`;
    if (/judg/.test(a)) return `Judging${r}`;
    if (/order/.test(a)) return 'Choosing orders';
    return cap(a);
  }

  function spark(svg, values) {
    const W = 112, H = 30, P = 3;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    if (values.length < 2) { svg.replaceChildren(); return; }
    let lo = Math.min(...values), hi = Math.max(...values);
    if (hi - lo < 3) { hi = lo + 3; }
    const x = (i) => P + (i * (W - 2 * P)) / (values.length - 1);
    const y = (v) => H - P - ((v - lo) * (H - 2 * P)) / (hi - lo);
    const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    const line = 'M' + pts.join('L');
    const area = `${line}L${x(values.length - 1).toFixed(1)},${H}L${x(0).toFixed(1)},${H}Z`;
    svg.innerHTML = `<path class="area" d="${area}"/><path class="line" d="${line}" vector-effect="non-scaling-stroke"/>`;
  }

  function renderCards() {
    const d = S.d, wrap = $('powers');
    const hist = d.sc_history || [];
    const finalSC = d.final_sc || (hist.length ? hist[hist.length - 1].sc : {});
    const msgs = d.messages || [];
    S.agents.forEach((p, i) => {
      let c = S.cards.get(p);
      if (!c) {
        const card = el('article', 'power');
        card.style.setProperty('--c', cvar(p));
        card.style.animationDelay = `${i * 70}ms`;
        const name = el('h2', 'pw-name', cap(p));
        const lead = el('span', 'pw-lead', 'Leading');
        lead.hidden = true;
        name.append(lead);
        const sc = el('div', 'pw-sc');
        const scN = el('b', null, '–');
        sc.append(scN, el('span', null, 'centres'));
        const model = el('div', 'pw-model');
        const prevM = el('div', 'pw-prev');
        const row = el('div', 'pw-row');
        const act = el('div', 'pw-act');
        act.setAttribute('aria-live', 'polite');
        const actDots = dots();
        const actT = el('span', 't');
        act.append(actDots, actT);
        const sv = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        sv.setAttribute('class', 'pw-spark');
        sv.setAttribute('aria-hidden', 'true');
        row.append(act, sv);
        const tally = el('div', 'pw-tally');
        card.append(name, sc, model, prevM, row, tally);
        wrap.append(card);
        c = { card, lead, prevM, scN, model, act, actDots, actT, sv, tally, sparkSig: '', tallySig: '' };
        S.cards.set(p, c);
      }
      // model + playbook
      const pb = playbookOf((d.seats || {})[p]);
      const mid = (d.models || {})[p];
      const label = shortModel(mid) + (pb ? ` · playbook ${pb}` : '');
      setText(c.model, label);
      c.model.title = mid || '';
      // Rotation: who held this seat last game, and where this model has played before.
      const prevG = previousStanding();
      const was = prevG && (prevG.models || {})[p];
      const mstat = S.st && (S.st.by_model || []).find((x) => x.name === mid);
      let prevText = 'First game of this run';
      if (was) prevText = was === mid ? `Same model as last game` : `Last game: ${shortModel(was)}`;
      setText(c.prevM, prevText);
      const elsewhere = mstat ? Object.entries(mstat.countries || {}).map(([k, v]) => `${cap(k)} ${v}`).join(', ') : '';
      c.prevM.title = elsewhere ? `${shortModel(mid)} has played: ${elsewhere}` : '';
      const lnow = leadersOf(finalSC, S.agents);
      const leading = !!lnow && lnow.who.includes(p) && lnow.who.length < S.agents.length;
      c.lead.hidden = !leading;
      setText(c.lead, lnow && lnow.who.length > 1 ? 'Joint lead' : (d.status === 'running' ? 'Leading' : 'Winner'));
      c.card.classList.toggle('leading', leading);

      // supply centres
      const n = finalSC[p];
      const prev = S.sc[p];
      setText(c.scN, n == null ? '–' : String(n));
      if (prev != null && n != null && n !== prev && !reduced.matches) {
        c.card.classList.remove('bump'); void c.card.offsetWidth; c.card.classList.add('bump');
      }
      S.sc[p] = n;
      const vals = hist.map((h) => (h.sc || {})[p]).filter((v) => v != null);
      const ss = vals.join(',');
      if (ss !== c.sparkSig) { c.sparkSig = ss; spark(c.sv, vals); }

      // activity
      const at = d.status === 'running' ? activityText((d.activity || {})[p]) : null;
      c.act.classList.toggle('on', !!at);
      c.actDots.hidden = !at;
      setText(c.actT, at || (d.status === 'running' ? 'Waiting for the others' : 'Done'));

      // deception tally
      let sent = 0, lies = 0, fa = 0, caught = 0;
      for (const m of msgs) {
        if (m.sender === p) { sent++; if (((d.sends || {})[m.id] || {}).intent === 'deceptive') lies++; }
        if (m.recipient === p) {
          const o = ((d.outcomes || {})[m.id] || [])[0];
          if (o === 'false alarm') fa++; else if (o === 'lie caught') caught++;
        }
      }
      const ts = [sent, lies, fa, caught].join(',');
      if (ts !== c.tallySig) {
        c.tallySig = ts;
        c.tally.innerHTML = `<b>${sent}</b> sent · <b class="${lies ? 'lie' : ''}">${plural(lies, 'lie')}</b> · ` +
          `as judge <b>${caught}</b> caught, <b>${fa}</b> false ${fa === 1 ? 'alarm' : 'alarms'}`;
      }
    });
  }

  /* ── phase timeline ─────────────────────────────────── */
  function renderScrub() {
    const box = $('scrub');
    const sig = S.phases.join(',');
    if (box.dataset.sig !== sig) {
      const had = new Set((box.dataset.sig || '').split(',').filter(Boolean));
      const fresh = had.size > 0;
      box.dataset.sig = sig;
      box.replaceChildren();
      let yr = null, group = null;
      for (const p of S.phases) {
        const y = p.slice(1, 5);
        if (y !== yr) {
          yr = y;
          group = el('div', 'yr');
          group.setAttribute('role', 'group');
          group.setAttribute('aria-label', y);
          group.append(el('span', 'yr-label', y));
          box.append(group);
        }
        const b = el('button', 'ph', (SEASON_SHORT[p[0]] || p[0]) + (p.endsWith('R') ? ' R' : ''));
        b.type = 'button';
        b.dataset.phase = p;
        b.setAttribute('role', 'option');
        b.setAttribute('aria-label', phaseLabel(p));
        b.title = phaseLabel(p);
        if (fresh && !had.has(p)) b.classList.add('new');
        group.append(b);
      }
    }
    const last = S.phases[S.phases.length - 1];
    const running = S.d && S.d.status === 'running';
    const leads = phaseLeaders();
    let selBtn = null;
    for (const b of box.querySelectorAll('.ph')) {
      const ph = b.dataset.phase;
      const L = leads.get(ph);
      let mark = b.querySelector('.lead');
      if (L && !mark) { mark = el('i', 'lead'); mark.setAttribute('aria-hidden', 'true'); b.append(mark); }
      if (mark) {
        const tie = !L || L.who.length > 1;
        mark.classList.toggle('tie', tie);
        mark.style.setProperty('--lc', tie ? 'var(--ink3)' : cvar(L.who[0]));
      }
      const leadTxt = L ? (L.who.length > 1 ? `level on ${L.sc}: ${L.who.map(cap).join(', ')}` : `${cap(L.who[0])} leads with ${L.sc}`) : '';
      const t = phaseLabel(ph) + (leadTxt ? ` · after it, ${leadTxt}` : '');
      if (b.title !== t) { b.title = t; b.setAttribute('aria-label', t); }
      const on = b.dataset.phase === S.sel;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      b.classList.toggle('live', running && b.dataset.phase === last);
      if (on) selBtn = b;
    }
    if (selBtn && S.follow) {
      const l = selBtn.offsetLeft, r = l + selBtn.offsetWidth;
      if (l < box.scrollLeft || r > box.scrollLeft + box.clientWidth) box.scrollLeft = r - box.clientWidth + 8;
    }
    $('toLive').hidden = S.follow || !last || S.sel === last;
    renderLeaderLine(leads);
  }

  function renderLeaderLine(leads) {
    const line = $('leaderLine');
    let ph = S.sel, L = leads.get(ph);
    let when = `after ${phaseWords(ph).season}`;
    if (!L) {
      // The phase in progress has no result yet: report the latest resolved one.
      const i = S.phases.indexOf(ph);
      for (let j = i - 1; j >= 0 && !L; j--) if (leads.has(S.phases[j])) { ph = S.phases[j]; L = leads.get(ph); }
      when = L ? `now, after ${phaseWords(ph).season}` : '';
    }
    const sig = ph + JSON.stringify(L) + JSON.stringify(S.d && S.d.models);
    if (line.dataset.sig === sig) return;
    line.dataset.sig = sig;
    line.replaceChildren();
    if (!L) { line.append(el('span', 'muted', 'No phase has resolved yet.')); return; }
    const models = (S.d && S.d.models) || {};
    if (L.who.length === S.agents.length && L.who.length > 1) {
      line.append(el('span', 'muted', `All level ${when}, on ${L.sc} centres each`));
      return;
    }
    line.append(el('span', 'muted', `${L.who.length > 1 ? 'Joint lead' : 'Leading'} ${when}: `));
    L.who.forEach((p, k) => {
      if (k) line.append(document.createTextNode(', '));
      const sw = el('span', 'swatch');
      sw.style.setProperty('--c', cvar(p));
      const who = el('b', null, cap(p));
      line.append(sw, who, el('span', 'muted', ` (${shortModel(models[p])})`));
    });
    line.append(el('span', 'muted', ` · ${plural(L.sc, 'centre')}`));
  }

  function select(phase, fromUser) {
    if (!phase || !S.phases.includes(phase)) return;
    const last = S.phases[S.phases.length - 1];
    S.sel = phase;
    S.follow = phase === last;
    renderScrub();
    renderBoard();
    renderOrders();
    if (fromUser) revealPhaseInThread(phase);
  }

  $('scrub').addEventListener('click', (e) => {
    const b = e.target.closest('.ph');
    if (b) select(b.dataset.phase, true);
  });
  $('scrub').addEventListener('keydown', (e) => {
    const i = S.phases.indexOf(S.sel);
    let j = null;
    if (e.key === 'ArrowLeft') j = Math.max(0, i - 1);
    else if (e.key === 'ArrowRight') j = Math.min(S.phases.length - 1, i + 1);
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = S.phases.length - 1;
    if (j == null) return;
    e.preventDefault();
    select(S.phases[j], true);
    const b = $('scrub').querySelector(`[data-phase="${S.phases[j]}"]`);
    if (b) { b.focus(); b.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }
  });
  $('toLive').addEventListener('click', () => { S.follow = true; select(S.phases[S.phases.length - 1], false); scrollThreadBottom(); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'l' && e.key !== 'L') return;
    if (e.metaKey || e.ctrlKey || e.altKey || /input|textarea|select/i.test(e.target.tagName)) return;
    if (!$('toLive').hidden) $('toLive').click();
  });

  /* ── board ──────────────────────────────────────────── */
  function mapUrl(p) { return `${S.d.dir}/${S.d.maps}/${p}.svg`; }

  function renderBoard() {
    const d = S.d;
    const has = d && d.maps && d.dir && S.phases.length;
    $('boardEmpty').hidden = !!S.mapShown;
    if (!has) { setText($('boardNote'), ''); return; }
    // A missing newest map may appear once its phase resolves: re-probe now and then.
    if (d.status === 'running' && Date.now() - S.mapProbe > 6000) {
      S.mapProbe = Date.now();
      for (const [p, ok] of S.mapOk) if (ok === false) S.mapOk.delete(p);
    }
    show(S.phases.indexOf(S.sel));
  }

  function show(idx) {
    let i = idx;
    // The phase being played has no board image until it resolves; don't probe it (avoids 404 noise).
    const lastI = S.phases.length - 1;
    if (i === lastI && S.d.status === 'running' && S.mapOk.get(S.phases[i]) !== true) i--;
    while (i >= 0 && S.mapOk.get(S.phases[i]) === false) i--;
    if (i < 0) { setText($('boardNote'), 'No board image yet'); return; }
    const p = S.phases[i];
    const url = mapUrl(p);
    if (S.mapOk.get(p) === true) { swapMap(url, p); return; }
    if (S.mapWant === url) return;
    S.mapWant = url;
    const probe = new Image();
    probe.onload = () => {
      S.mapOk.set(p, true);
      if (S.mapWant === url) S.mapWant = null;
      if (mapUrl(p) === url) renderBoard();
    };
    probe.onerror = () => {
      S.mapOk.set(p, false);
      if (S.mapWant === url) S.mapWant = null;
      if (S.d && mapUrl(p) === url) renderBoard();
    };
    probe.src = url;
  }

  function swapMap(url, p) {
    const want = S.sel;
    const note = p === want
      ? `${phaseLabel(p)} · units with the orders given`
      : `${phaseWords(want).season} in progress · showing ${phaseWords(p).season} until it resolves`;
    setText($('boardNote'), note);
    if (S.mapShown === url) return;
    const a = $('mapA'), b = $('mapB');
    const front = a.classList.contains('front') ? a : b.classList.contains('front') ? b : null;
    const back = front === a ? b : a;
    back.alt = `Board, ${phaseLabel(p)}`;
    back.src = url;
    const done = () => { back.classList.add('front'); if (front) front.classList.remove('front'); };
    if (back.decode) back.decode().then(done, done); else done();
    S.mapShown = url;
    $('boardEmpty').hidden = true;
  }

  /* ── orders ─────────────────────────────────────────── */
  function orderNode(o) {
    const li = el('li');
    const t = o.trim().split(/\s+/);
    const unit = t[0], loc = t[1], verb = t[2];
    const u = el('span', 'u', unit === 'F' ? 'F' : unit === 'A' ? 'A' : '');
    u.title = unit === 'F' ? 'Fleet' : unit === 'A' ? 'Army' : '';
    const L = el('b', null, loc || o);
    const rest = t.slice(3).join(' ').replace(/ - /g, ' → ');
    let txt;
    switch (verb) {
      case 'H': txt = ' holds'; break;
      case '-': txt = ` → ${t.slice(3).join(' ').replace(/ - /g, ' → ')}`; break;
      case 'S': txt = ` supports ${rest}`; break;
      case 'C': txt = ` convoys ${rest}`; break;
      case 'B': txt = ' build'; break;
      case 'D': txt = ' disband'; break;
      case 'R': txt = ` retreats → ${rest}`; break;
      default: txt = t.length > 2 ? ' ' + t.slice(2).join(' ') : '';
    }
    li.append(u, L, document.createTextNode(txt));
    return li;
  }

  function renderOrders() {
    const d = S.d, box = $('orders');
    const last = S.phases[S.phases.length - 1];
    let ph = S.sel;
    let ords = (d.orders || {})[ph] || null;
    // The phase being played keeps its orders secret until it resolves: show the last resolved
    // phase instead, matching what the board shows.
    let note = ph ? phaseLabel(ph) : '';
    if (!ords && ph === last && d.status === 'running') {
      for (let i = S.phases.indexOf(ph) - 1; i >= 0; i--) {
        const o = (d.orders || {})[S.phases[i]];
        if (o && Object.keys(o).length) {
          note = `${phaseWords(ph).season} orders stay secret until it resolves · showing ${phaseLabel(S.phases[i])}`;
          ph = S.phases[i];
          ords = o;
          break;
        }
      }
    }
    const pending = !ords && ph === last && d.status === 'running';
    if (pending) note = `${phaseLabel(ph)} · revealed when the phase resolves`;
    const sig = ph + note + JSON.stringify(ords) + S.agents.join(',');
    if (sig === S.ordersSig) return;
    S.ordersSig = sig;
    setText($('ordersNote'), note);
    const powers = S.agents.slice();
    for (const p in ords || {}) if (!powers.includes(p)) powers.push(p);
    const frag = document.createDocumentFragment();
    for (const p of powers) {
      const col = el('div', 'ord');
      const h = el('h3');
      const sw = el('span', 'swatch');
      sw.style.setProperty('--c', cvar(p));
      h.append(sw, document.createTextNode(cap(p)));
      col.append(h);
      const list = (ords || {})[p];
      if (list && list.length) {
        const ul = el('ul');
        for (const o of list) ul.append(orderNode(o));
        col.append(ul);
      } else {
        col.append(el('p', 'none', pending ? 'Not yet revealed' : 'No orders'));
      }
      frag.append(col);
    }
    box.replaceChildren(frag);
  }

  /* ── conversations ──────────────────────────────────── */
  const pairKey = (a, b) => [a, b].sort((x, y) => ORDER.indexOf(x) - ORDER.indexOf(y)).join('|');
  const inPair = (m) => S.pair === 'all' || pairKey(m.sender, m.recipient) === S.pair;

  function renderPairs() {
    const msgs = S.d.messages || [];
    const counts = new Map();
    for (const m of msgs) { const k = pairKey(m.sender, m.recipient); counts.set(k, (counts.get(k) || 0) + 1); }
    const keys = [...counts.keys()].sort((a, b) => {
      const [a1, a2] = a.split('|'), [b1, b2] = b.split('|');
      return ORDER.indexOf(a1) - ORDER.indexOf(b1) || ORDER.indexOf(a2) - ORDER.indexOf(b2);
    });
    const box = $('pairs');
    const sig = keys.join(',');
    if (sig !== S.pairKeys) {
      S.pairKeys = sig;
      box.replaceChildren();
      const mk = (key, label, duo) => {
        const b = el('button', 'pair');
        b.type = 'button';
        b.dataset.key = key;
        b.setAttribute('role', 'tab');
        b.setAttribute('aria-controls', 'thread');
        if (duo) {
          const dd = el('span', 'duo');
          dd.setAttribute('aria-hidden', 'true');
          for (const p of duo) { const i = el('i'); i.style.setProperty('--c', cvar(p)); dd.append(i); }
          b.append(dd);
        }
        b.append(el('span', null, label), el('span', 'n'));
        box.append(b);
      };
      mk('all', 'All messages', null);
      for (const k of keys) { const [a, b] = k.split('|'); mk(k, `${cap(a)} · ${cap(b)}`, [a, b]); }
    }
    for (const b of box.children) {
      const k = b.dataset.key;
      const n = k === 'all' ? msgs.length : (counts.get(k) || 0);
      const nn = b.querySelector('.n');
      if (nn.textContent !== String(n)) {
        if (nn.textContent && !reduced.matches) { b.classList.remove('ping'); void b.offsetWidth; b.classList.add('ping'); }
        nn.textContent = String(n);
      }
      const on = k === S.pair;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    }
  }

  $('pairs').addEventListener('click', (e) => {
    const b = e.target.closest('.pair');
    if (!b || b.dataset.key === S.pair) return;
    S.pair = b.dataset.key;
    renderPairs();
    rebuildThread();
  });
  $('pairs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const tabs = [...$('pairs').children];
    const i = tabs.findIndex((t) => t.dataset.key === S.pair);
    const j = Math.max(0, Math.min(tabs.length - 1, i + (e.key === 'ArrowRight' ? 1 : -1)));
    e.preventDefault();
    tabs[j].click();
    tabs[j].focus();
    tabs[j].scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });

  const thread = $('thread');
  const atBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 64;
  function scrollThreadBottom(smooth) {
    thread.scrollTo({ top: thread.scrollHeight, behavior: smooth && !reduced.matches ? 'smooth' : 'auto' });
    S.newCount = 0;
    $('newPill').hidden = true;
  }
  thread.addEventListener('scroll', () => { if (atBottom()) { S.newCount = 0; $('newPill').hidden = true; } }, { passive: true });
  $('newPill').addEventListener('click', () => scrollThreadBottom(true));

  function typingNode() {
    let t = $('typing');
    if (!t) { t = el('div'); t.id = 'typing'; }
    return t;
  }

  function rebuildThread() {
    thread.replaceChildren();
    S.nodes.clear();
    S.lastSep = null;
    S.newCount = 0;
    $('newPill').hidden = true;
    renderThread(true);
  }

  function renderThread(initial) {
    const d = S.d;
    const msgs = (d.messages || []).filter(inPair);
    const stick = initial || atBottom();
    const typing = typingNode();
    if (typing.parentNode !== thread) thread.append(typing);
    let added = 0;
    const [pa] = S.pair === 'all' ? [] : S.pair.split('|');

    for (const m of msgs) {
      let rec = S.nodes.get(m.id);
      if (!rec) {
        const sepKey = `${m.phase}|${m.round}`;
        if (sepKey !== S.lastSep) {
          S.lastSep = sepKey;
          const sep = el('div', 'sep');
          sep.dataset.phase = m.phase;
          sep.append(el('span', null, `${phaseLabel(m.phase)}${m.round ? ` · round ${m.round}` : ''}`));
          thread.insertBefore(sep, typing);
        }
        rec = { el: buildMsg(m, pa), sig: '' };
        if (!initial) rec.el.classList.add('enter');
        S.nodes.set(m.id, rec);
        thread.insertBefore(rec.el, typing);
        added++;
      }
      updateSpec(rec, m);
    }
    if (!msgs.length && !thread.querySelector('.empty-thread')) {
      thread.insertBefore(el('p', 'empty-thread', d.status === 'running' ? 'No messages yet. The first round of talks is being written.' : 'No messages in this game.'), typing);
    } else if (msgs.length) {
      const e = thread.querySelector('.empty-thread');
      if (e) e.remove();
    }
    renderTyping(typing);

    if (stick) scrollThreadBottom(!initial);
    else if (added) {
      S.newCount += added;
      const pill = $('newPill');
      pill.textContent = `${plural(S.newCount, 'new message')} ↓`;
      pill.hidden = false;
    }
  }

  function renderTyping(node) {
    const d = S.d;
    const act = d.status === 'running' ? (d.activity || {}) : {};
    const who = [];
    for (const p of S.agents) {
      const a = act[p];
      if (!a) continue;
      if (S.pair !== 'all' && !S.pair.split('|').includes(p)) continue;
      if (/judg/.test(a)) who.push([p, 'is judging what it received']);
      else if (/writ|compos|messag/.test(a)) who.push([p, 'is writing']);
    }
    const sig = who.map((w) => w.join(':')).join(',');
    if (node.dataset.sig === sig) return;
    node.dataset.sig = sig;
    node.replaceChildren();
    for (const [p, what] of who) {
      const row = el('div', 'typing');
      const bub = el('span', 'bub');
      bub.style.setProperty('--c', cvar(p));
      bub.append(dots());
      row.append(bub, el('span', null, `${cap(p)} ${what}`));
      node.append(row);
    }
  }

  function buildMsg(m, leftPower) {
    const a = el('article', 'msg');
    a.id = `m-${m.id}`;
    a.style.setProperty('--c', cvar(m.sender));
    if (leftPower && m.sender !== leftPower) a.classList.add('right');
    const h = el('header', 'm-head');
    h.append(el('b', null, cap(m.sender)), el('span', 'to', `to ${cap(m.recipient)}`));
    const r = el('span', 'r', m.round ? `round ${m.round}` : '');
    h.append(r);
    const txt = el('p', 'm-text', m.text || '');
    const spec = el('div', 'm-spec');
    const more = el('details', 'm-more');
    const sum = el('summary', null, 'Reasoning and private plan');
    const body = el('div', 'more-body');
    more.append(sum, body);
    a.append(h, txt, spec, more);
    return a;
  }

  function updateSpec(rec, m) {
    const d = S.d;
    const send = (d.sends || {})[m.id] || null;
    const j = (d.judgments || {})[m.id] || null;
    const out = (d.outcomes || {})[m.id] || null;
    const last = S.phases[S.phases.length - 1];
    const pending = !j && d.status === 'running' && m.phase === last && S.agents.includes(m.recipient);
    const sig = JSON.stringify([send && send.intent, send && send.plan, j && j.p_lie, j && j.decision, out, pending]);
    if (sig === rec.sig) return;
    rec.sig = sig;
    const a = rec.el, spec = a.querySelector('.m-spec'), body = a.querySelector('.more-body'), more = a.querySelector('.m-more');
    const deceptive = send && send.intent === 'deceptive';
    a.classList.toggle('deceptive', !!deceptive);

    spec.replaceChildren();
    if (send && send.intent) {
      const t = el('span', deceptive ? 'tag lie' : 'tag', deceptive ? 'Deceptive' : 'Truthful');
      t.title = `${cap(m.sender)}'s private label for this message`;
      spec.append(t);
    }
    if (j) {
      const jw = el('span', 'judge');
      const p = typeof j.p_lie === 'number' ? j.p_lie : null;
      const g = el('span', 'gauge' + (p == null ? '' : p >= 0.6 ? ' hi' : p >= 0.35 ? ' mid' : ''));
      g.setAttribute('role', 'img');
      g.setAttribute('aria-label', `Probability of a lie ${p == null ? 'unknown' : Math.round(p * 100) + '%'}`);
      const fill = el('i');
      fill.style.width = `${Math.round((p || 0) * 100)}%`;
      g.append(fill);
      const pv = el('span', 'p', p == null ? '–' : p.toFixed(2));
      jw.append(g, pv, document.createTextNode(` ${j.decision === 'distrust' ? 'distrusted' : 'trusted'} by ${cap(j.receiver || m.recipient)}`));
      spec.append(jw);
    } else if (pending) {
      const pw = el('span', 'pending');
      pw.append(dots(), document.createTextNode(`Waiting for ${cap(m.recipient)}'s judgment`));
      spec.append(pw);
    }
    if (out && out[0] && out[0] !== 'no judgment' && j) {
      spec.append(el('span', `tag ${TONE[out[1]] || ''}`.trim(), cap(out[0])));
    } else if (!j && !pending) {
      spec.append(el('span', 'pending', 'Not judged'));
    }

    // Expandable detail: receiver's reasoning, sender's private plan.
    body.replaceChildren();
    if (j && (j.reasoning || (j.cues || []).length)) {
      const s = el('section');
      s.append(el('h4', null, `${cap(j.receiver || m.recipient)}'s reasoning`));
      if (j.reasoning) s.append(el('p', null, j.reasoning));
      if ((j.cues || []).length) {
        const c = el('div', 'cues');
        for (const cue of j.cues) c.append(el('span', null, cue));
        s.append(c);
      }
      body.append(s);
    }
    if (send && ((send.plan || []).length || send.analysis)) {
      const s = el('section');
      s.append(el('h4', null, `${cap(m.sender)}'s private plan`));
      if ((send.plan || []).length) {
        const ul = el('ul');
        for (const p of send.plan) ul.append(el('li', null, p));
        s.append(ul);
      }
      if (send.analysis) s.append(el('p', null, send.analysis));
      body.append(s);
    }
    more.hidden = !body.childElementCount;
  }

  function revealPhaseInThread(phase) {
    const sep = thread.querySelector(`.sep[data-phase="${phase}"]`);
    if (!sep) return;
    thread.scrollTo({ top: sep.offsetTop - 8, behavior: reduced.matches ? 'auto' : 'smooth' });
    sep.classList.remove('flash'); void sep.offsetWidth; sep.classList.add('flash');
  }

  /* ── between games ──────────────────────────────────── */
  function renderWinner() {
    const d = S.d, box = $('winner');
    if (!d || d.status !== 'finished') { box.hidden = true; return; }
    const g = currentStanding();
    let who, sc, models, solo = false;
    if (g && g.status !== 'running' && (g.winners || []).length) {
      who = g.winners; sc = g.winner_sc; solo = !!g.solo;
      models = g.models || d.models || {};
    } else {
      const L = leadersOf(d.final_sc || {}, S.agents);
      if (!L) { box.hidden = true; return; }
      who = L.who; sc = L.sc; models = d.models || {};
    }
    box.hidden = false;
    box.style.setProperty('--c', cvar(who[0]));
    box.classList.toggle('shared', who.length > 1);
    setText($('winnerKicker'), solo ? 'Solo victory' : who.length > 1 ? 'Shared first place' : `Winner of game ${d.game_id}`);
    setText($('winnerName'), who.length > 1 ? who.map(cap).join(' and ') : `${cap(who[0])} wins`);
    setText($('winnerSub'), `${who.map((p) => shortModel(models[p])).join(' and ')} · ${plural(sc, 'supply centre')}${who.length > 1 ? ' each' : ''}`);
  }

  /* ── model leaderboard ──────────────────────────────── */
  function countryChips(countries) {
    const wrap = el('span', 'cc');
    const keys = Object.keys(countries || {}).sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
    for (const k of keys) {
      const c = el('span', 'cc-i');
      c.style.setProperty('--c', cvar(k));
      c.title = `${cap(k)}: ${plural(countries[k], 'game')}`;
      c.append(el('i'), document.createTextNode(`${cap(k).slice(0, 3)}${countries[k] > 1 ? ' ×' + countries[k] : ''}`));
      wrap.append(c);
    }
    return wrap;
  }

  function renderStandings() {
    const st = S.st || {};
    const rows = st.by_model || [];
    const games = st.games || [];
    const done = games.filter((g) => g.status !== 'running');
    setText($('lbNote'), done.length ? `${plural(done.length, 'finished game')} · ties split the win` : 'Waiting for the first game to finish');
    const body = $('lbBody');
    body.replaceChildren();
    $('lbEmpty').hidden = rows.length > 0;
    const nowModels = (S.d && S.d.models) || {};
    const seatOf = {};
    for (const p in nowModels) seatOf[nowModels[p]] = p;
    rows.forEach((m, i) => {
      const tr = el('tr');
      if (i === 0 && m.wins > 0) tr.className = 'top';
      const name = el('th');
      name.scope = 'row';
      name.append(el('span', 'lb-name', m.short || modelName(m.name)));
      const seat = seatOf[m.name];
      if (seat) {
        const now = el('span', 'lb-now');
        const sw = el('span', 'swatch');
        sw.style.setProperty('--c', cvar(seat));
        now.append(sw, document.createTextNode(`${cap(seat)} this game`));
        name.append(now);
      }
      const wins = el('td', 'num');
      wins.append(el('b', null, fmtNum(m.wins, 1)), el('span', 'muted', ` of ${m.games}`));
      const mean = el('td', 'num', fmtNum(m.mean_sc, 1));
      const cc = el('td');
      cc.append(countryChips(m.countries));
      tr.append(name, wins, mean, cc);
      body.append(tr);
    });

    const ol = $('results');
    ol.replaceChildren();
    const recent = games.slice().reverse().slice(0, 8);
    if (!recent.length) ol.append(el('li', 'muted', 'No games yet.'));
    for (const g of recent) {
      const li = el('li');
      if (S.d && g.game_id === S.d.game_id) li.classList.add('current');
      li.append(el('span', 'r-id', g.game_id));
      const w = el('span', 'r-win');
      const who = g.winners || [];
      if (g.status === 'running') {
        w.append(el('span', 'muted', 'in progress'));
        const L = (g.phase_leaders || [])[(g.phase_leaders || []).length - 1];
        if (L && L.leaders.length === 1) {
          const sw = el('span', 'swatch'); sw.style.setProperty('--c', cvar(L.leaders[0]));
          w.append(el('span', 'muted', ' · leading '), sw, el('b', null, cap(L.leaders[0])));
        }
      } else if (who.length) {
        who.forEach((p, k) => {
          if (k) w.append(el('span', 'muted', ' and '));
          const sw = el('span', 'swatch'); sw.style.setProperty('--c', cvar(p));
          w.append(sw, el('b', null, cap(p)), el('span', 'muted', ` ${shortModel((g.models || {})[p])}`));
        });
      } else w.append(el('span', 'muted', g.status || '–'));
      li.append(w, el('span', 'r-sc', g.status === 'running' ? '' : `${g.winner_sc}${g.solo ? ' · solo' : ''}`));
      ol.append(li);
    }
  }

  function renderIntermission() {
    const d = S.d, box = $('intermission');
    if (!d || d.status === 'running') { box.hidden = true; return; }
    box.hidden = false;
    const last = S.phases[S.phases.length - 1];
    const sc = d.final_sc || {};
    const ranked = Object.keys(sc).sort((a, b) => sc[b] - sc[a] || ORDER.indexOf(a) - ORDER.indexOf(b));
    const top = ranked[0];
    renderWinner();
    if (d.status === 'error') {
      setText($('imTitle'), 'This game stopped with an error');
      setText($('imLine'), (d.summary && d.summary.error) || 'The run will move on to the next game.');
    } else {
      setText($('imTitle'), last ? `Game over · final board, ${phaseWords(last).season}` : 'Game over');
      const stage = S.rsi && S.rsi.stage ? ` Right now: ${S.rsi.stage}.` : '';
      setText($('imLine'), `${top ? `${cap(top)} finished on ${sc[top]} centres. ` : ''}The judge reviews the game and the gate tests candidate playbooks.${stage} ${pinned ? '' : 'The next game opens here by itself.'}`.trim());
    }
    const fin = $('imFinal');
    const fsig = ranked.map((p) => p + sc[p]).join(',');
    if (fin.dataset.sig !== fsig) {
      fin.dataset.sig = fsig;
      fin.replaceChildren();
      const max = Math.max(18, ...ranked.map((p) => sc[p]));
      for (const p of ranked) {
        const li = el('li');
        li.style.setProperty('--c', cvar(p));
        const bar = el('span', 'bar-sc');
        bar.style.width = `${Math.max(4, (sc[p] / max) * 120)}px`;
        li.append(el('span', 'nm', cap(p)), bar, el('span', 'n', String(sc[p])));
        fin.append(li);
      }
    }
    const ev = $('imEvents');
    const events = (S.rsi && S.rsi.events) || [];
    const esig = events.slice(0, 7).map((e) => e.t + e.text).join('|');
    if (ev.dataset.sig !== esig) {
      ev.dataset.sig = esig;
      ev.replaceChildren();
      if (!events.length) ev.append(el('li', null, 'Waiting for the next step of the run.'));
      for (const e of events.slice(0, 7)) {
        const li = el('li');
        li.append(el('time', null, e.t || ''), el('span', null, e.text || ''));
        ev.append(li);
      }
    }
  }

  /* ── theme ──────────────────────────────────────────── */
  $('themeBtn').addEventListener('click', () => {
    const root = document.documentElement;
    const cur = root.dataset.theme || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    const next = cur === 'light' ? 'dark' : 'light';
    root.dataset.theme = next;
    try { localStorage.setItem('dip-theme', next); } catch (e) { /* storage unavailable */ }
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) renderLive(); });

  tick();
  sideTick();
  standTick();
})();
