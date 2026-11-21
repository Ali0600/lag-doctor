'use strict';
// lag-doctor's screens: Library, a game's page, Add a game, Results, Compare, Settings and a running recording.
// Everything shown comes from the app over JSON web messages (lagdoctor/app/bridge.py). Text from a capture or the PC
// (a game's title, a process name, evidence) is put on the page as text only: el() makes text nodes, and nothing here
// parses markup.

const wv = window.chrome && window.chrome.webview;
const $ = (id) => document.getElementById(id);
const view = () => $('view');

// ------------------------------------------------------------------------------------------------- the DOM --

// el('p', {class: 'muted'}, 'text', child, ...): an element; strings become text nodes, never markup.
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (k === 'hidden') node.hidden = true;
    else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
  return node;
}

// Text with `commands` in backticks shown as code, as the HTML report does.
function prose(text) {
  const out = [];
  String(text ?? '').split('`').forEach((part, i) => { if (part) out.push(i % 2 ? el('code', null, part) : part); });
  return out;
}

function show(...nodes) {
  view().replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
  view().scrollTop = 0;
}

// A part of a screen drawn again in place: its children, leaving out the empty ones (as el() does).
function fill(node, ...nodes) {
  node.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// --------------------------------------------------------------------------------------------- the bridge --

let nextId = 1;
const waiting = new Map();       // request id -> {resolve, reject}
const frameSets = new Map();     // request id -> {t, ms} handed over in a shared buffer

function ask(kind, args) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    waiting.set(id, {resolve, reject});
    wv.postMessage(Object.assign({type: 'request', id, kind}, args || {}));
  });
}

if (wv) {
  wv.addEventListener('message', (e) => {
    const m = e.data;
    if (m && typeof m === 'object' && m.type === 'push' && m.kind === 'recording') { recordingPushed(m.data); return; }
    if (!m || typeof m !== 'object' || m.type !== 'reply' || !waiting.has(m.id)) return;
    const w = waiting.get(m.id);
    waiting.delete(m.id);
    if (m.ok) w.resolve(m.data); else w.reject(new Error(String(m.error || 'No answer.')));
  });
  // Frame times arrive in a read-only shared buffer: all times, then all frame times, as little-endian doubles.
  wv.addEventListener('sharedbufferreceived', (e) => {
    const meta = e.additionalData || {};
    const buffer = e.getBuffer();
    try {
      const n = Number(meta.count) || 0;
      const all = new Float64Array(buffer, 0, 2 * n);
      frameSets.set(meta.request, {t: all.slice(0, n), ms: all.slice(n, 2 * n)});
    } finally {
      wv.releaseBuffer(buffer);
      wv.postMessage({type: 'frames_done', token: meta.token});
    }
  });
}

async function framesFor(requestId, token) {
  if (!token) return null;
  for (let i = 0; i < 200 && !frameSets.has(requestId); i++) await new Promise((r) => setTimeout(r, 25));
  const f = frameSets.get(requestId) || null;
  frameSets.delete(requestId);
  return f;
}

// ------------------------------------------------------------------------------------------ the navigation --

const stack = [];               // [{name, args}] of the screens behind this one
let current = null;
let token = 0;                  // a screen still loading when another opens never draws over it

function go(name, args, push = true) {
  if (push && current) stack.push(current);
  current = {name, args: args || {}};
  $('back').hidden = stack.length === 0;
  for (const b of document.querySelectorAll('#bar nav button')) b.classList.toggle('on', b.dataset.go === name);
  if (recClock) { clearInterval(recClock); recClock = null; }
  charts.forEach((c) => c.destroy());
  charts.length = 0;
  const mine = ++token;
  show(el('p', {class: 'muted loading'}, 'Loading'));
  SCREENS[name](current.args, () => mine === token).catch((err) => {
    if (mine === token) show(el('p', {class: 'error'}, String(err && err.message || err)));
  });
}

function back() {
  if (!stack.length) return;
  const prev = stack.pop();
  go(prev.name, prev.args, false);
}

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-go]');
  if (b) { stack.length = 0; go(b.dataset.go, {}, false); }
});
$('back').addEventListener('click', back);
document.addEventListener('keydown', (e) => {
  if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); back(); }
});
document.addEventListener('mouseup', (e) => { if (e.button === 3) { e.preventDefault(); back(); } });
document.addEventListener('contextmenu', (e) => e.preventDefault());

// ------------------------------------------------------------------------------------------------- charts --

const charts = [];

function fmtElapsed(s) {
  if (s === null || s === undefined || !isFinite(s)) return '-';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m ${String(r).padStart(2, '0')}s`;
  if (m) return r ? `${m}m ${String(r).padStart(2, '0')}s` : `${m}m`;
  return `${r}s`;
}

function width(node) {
  return Math.max(320, (node.clientWidth || view().clientWidth - 60) - 24);
}

// Drawn under and over a uPlot chart (opts): `rest`, the time a measured part leaves out, shaded in its own colour;
// `spans` ([t0, t1]) shaded; `marks`, the lag-key presses, as lines; `outside`, the presses the analysis left out,
// dashed; and `ref`, a reference line. Each is kept inside the plot, which a zoom or a span past the data would
// otherwise paint over the axes.
function overlays(opts) {
  const fill = (u, list, colour) => {
    const left = u.bbox.left, right = u.bbox.left + u.bbox.width;
    u.ctx.fillStyle = css(colour);
    for (const [a, b] of list || []) {
      const x0 = Math.max(left, u.valToPos(a, 'x', true)), x1 = Math.min(right, u.valToPos(b, 'x', true));
      if (x1 > x0 - 1) u.ctx.fillRect(x0, u.bbox.top, Math.max(1, x1 - x0), u.bbox.height);
    }
  };
  const lines = (u, list) => {
    for (const m of list || []) {
      const x = u.valToPos(m, 'x', true);
      if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
      u.ctx.beginPath(); u.ctx.moveTo(x, u.bbox.top); u.ctx.lineTo(x, u.bbox.top + u.bbox.height); u.ctx.stroke();
    }
  };
  return {
    drawClear: [(u) => {
      u.ctx.save();
      fill(u, opts.rest, '--skip');
      fill(u, opts.spans, '--band');
      u.ctx.restore();
    }],
    draw: [(u) => {
      const ctx = u.ctx;
      ctx.save();
      if (opts.ref) {
        const y = u.valToPos(opts.ref, 'y', true);
        ctx.strokeStyle = css('--ref');
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.lineWidth = 2;
      ctx.strokeStyle = css('--mark');
      lines(u, opts.marks);
      ctx.strokeStyle = css('--ref');
      ctx.setLineDash([3, 3]);
      lines(u, opts.outside);
      ctx.setLineDash([]);
      ctx.restore();
    }],
  };
}

function plot(box, data, series, opts) {
  const colors = ['--c1', '--c2', '--c3', '--c4'];
  const u = new uPlot({
    width: width(box), height: opts.height || 220,
    scales: {x: {time: false}, y: opts.yMax ? {range: [0, opts.yMax]} : {range: (u, lo, hi) => [0, Math.max(1, hi * 1.08)]}},
    series: [{label: 'time into the recording', value: (u, v) => fmtElapsed(v)},
      ...series.map((s, i) => ({label: s.label, stroke: css(colors[i % colors.length]), width: 1.25,
        points: {show: false}, value: (u, v) => (v === null || v === undefined ? '-' : v.toFixed(1))}))],
    axes: [{values: (u, splits) => splits.map(fmtElapsed), stroke: css('--muted'), grid: {stroke: css('--line')}},
      {label: opts.unit || '', stroke: css('--muted'), grid: {stroke: css('--line')}, size: 56}],
    cursor: {drag: {x: true, y: false}},
    hooks: overlays(opts),
  }, data, box);
  // What is shaded as left out, on the chart's box too: a test reads it there, since a canvas shows it only as pixels.
  if (opts.rest && opts.rest.length) box.setAttribute('data-rest', opts.rest.map(([a, b]) => `${a}-${b}`).join(' '));
  charts.push(u);
  return u;
}

window.addEventListener('resize', () => {
  for (const u of charts) u.setSize({width: width(u.root.parentElement), height: u.height});
});

function chartBox(spec, hint) {
  const box = el('div', {class: 'chart', 'data-chart': spec.id || 'frames'}, el('h3', null, spec.title),
    hint ? el('div', {class: 'hint'}, hint) : null);
  return box;
}

function seriesChart(spec, extra) {
  const box = chartBox(spec);
  if (spec.missing) { box.append(el('p', {class: 'muted'}, ...prose(spec.missing))); return [box, null]; }
  const data = [spec.t, ...spec.series.map((s) => s.values)];
  return [box, () => plot(box, data, spec.series, Object.assign({unit: spec.unit}, extra || {}))];
}

// ------------------------------------------------------------------------------------------------ screens --

// The art host's URL for one game, exactly as the app builds it (bridge.art_url): nothing else is ever loaded.
const ART_URL = /^https:\/\/art\.lagdoctor\.example\/g-[0-9a-f]{16}$/;

// A cover: the game's initials on its colour, and its art over them once that has loaded (a probe image outside the
// page loads it first, so art that cannot load leaves the initials as they were): a store's cover fills the tile, an
// added game's program icon sits in its middle, on the colour.
function tileCover(t, big) {
  const c = el('div', {class: 'cover'}, t.initials);
  const colour = `linear-gradient(160deg, hsl(${t.hue}, 55%, 46%), hsl(${(t.hue + 40) % 360}, 50%, 30%))`;
  c.style.background = colour;
  if (big) c.style.fontSize = '54px';
  if (typeof t.art === 'string' && ART_URL.test(t.art)) {
    c.dataset.art = 'loading';
    const probe = new Image();
    probe.addEventListener('load', () => {
      c.textContent = '';
      c.style.background = t.icon === true ? `center / 50% no-repeat url("${t.art}"), ${colour}`
        : `center / cover no-repeat url("${t.art}")`;
      c.dataset.art = 'shown';
      if (t.icon === true) c.dataset.fit = 'icon';
    });
    probe.addEventListener('error', () => { c.dataset.art = 'none'; });
    probe.src = t.art;
  }
  return c;
}

// A stat tile: a number large, a few words (a cause, a limit) smaller.
function tileOf([k, v]) {
  return el('div', {class: 'tile'}, el('div', {class: 'v' + (/^[-0-9.,%\s]+(ms|Hz)?$/.test(v) ? '' : ' words')}, v),
    el('div', {class: 'k'}, k));
}

function gameCard(g) {
  return el('button', {class: 'game', type: 'button', 'data-game': g.id, on: {click: () => go('game', {game: g.id})}},
    tileCover(g.tile),
    el('div', {class: 'info'}, el('div', {class: 'name', title: g.name}, g.name),
      el('div', {class: 'sub'}, g.recordings ? `${g.recordings} recording${g.recordings === 1 ? '' : 's'} · ${g.last}`
        : `${g.store} · not recorded yet`),
      el('span', {class: 'badge ' + g.lag_key}, g.lag_type)));
}

const SCREENS = {
  async library(args, live) {
    const data = await ask('library');
    if (!live()) return;
    const said = saidNote(args);
    const r = data.readiness || {};
    const strip = el('div', {class: 'strip' + (r.ok ? '' : ' no'), title: r.detail || ''}, ...prose(r.text));
    if (!data.games.length) {
      show(said, strip, el('h1', null, 'Your games'), el('div', {class: 'empty'},
        el('p', null, 'No games yet.'),
        el('p', {class: 'muted'}, 'lag-doctor lists the games Steam and the Epic Games launcher have installed, and ' +
          'any game you add by its program. You can also start a game yourself, then press Record the game that\'s ' +
          'running: when the recording is analysed, the game appears here with its results.'),
        el('div', {class: 'row'}, recordButton(), addButton())));
      return;
    }
    let filter = 'all', query = '';
    const grid = el('div', {class: 'grid', id: 'games'});
    const draw = () => {
      const q = query.trim().toLowerCase();
      grid.replaceChildren(...data.games.filter((g) => (filter === 'all' || (filter === 'installed' ? g.installed
        : g.recordings > 0)) && (!q || g.name.toLowerCase().includes(q) ||
        (g.exe_name || '').toLowerCase().includes(q))).map(gameCard));
      if (!grid.children.length) grid.append(el('p', {class: 'muted'}, 'No game matches.'));
    };
    const chips = ['all', 'installed', 'recorded'].map((f) => el('button', {class: 'chip' + (f === filter ? ' on' : ''),
      type: 'button', 'data-filter': f,
      on: {click: (e) => { filter = f; for (const c of chips) c.classList.toggle('on', c === e.target); draw(); }}},
      {all: 'All', installed: 'Installed', recorded: 'Recorded'}[f]));
    const search = el('input', {type: 'search', placeholder: 'Search your games', id: 'search',
      on: {input: (e) => { query = e.target.value; draw(); }}});
    const recent = data.games.filter((g) => data.recent.includes(g.id));
    draw();
    show(said, strip,
      el('div', {class: 'row tools'}, search, ...chips, el('span', {class: 'spacer'}),
        el('span', {class: 'muted'}, `${data.games.length} game${data.games.length === 1 ? '' : 's'}, ` +
          `${data.recordings} recording${data.recordings === 1 ? '' : 's'}`), recordButton(), addButton()),
      // The Recent row only when the library holds more than it: with a few games it would repeat them all.
      recent.length && data.games.length > recent.length ? el('section', {class: 'recent'}, el('h2', null, 'Recent'),
        el('div', {class: 'grid'}, ...recent.map(gameCard))) : null,
      el('h2', null, 'All games'), grid);
  },

  async game(args, live) {
    const g = await ask('game', {game: args.game});
    if (!live()) return;
    const said = saidNote(args);
    const chosen = new Set();
    const compareBtn = el('button', {class: 'btn', type: 'button', disabled: true, id: 'compare',
      on: {click: () => { const [a, b] = [...chosen]; go('compare', {a, b}); }}}, 'Compare the two picked');
    const deleteBtn = deleteButton(g.id, chosen);
    const rows = g.sessions.map((s) => {
      const box = el('input', {type: 'checkbox', title: 'Pick it', on: {click: (e) => {
        e.stopPropagation();
        if (e.target.checked) chosen.add(s.id); else chosen.delete(s.id);
        tr.classList.toggle('chosen', e.target.checked);
        compareBtn.disabled = chosen.size !== 2;
        deleteBtn.picked();
      }}});
      const tr = el('tr', {class: 'pick', 'data-session': s.id, on: {click: () => go('results', {session: s.id})}},
        el('td', null, box), el('td', null, s.when, s.is_last ? el('span', {class: 'last-tag'}, 'last') : null,
          s.note ? el('div', {class: 'note'}, s.note) : null),
        el('td', null, s.length), el('td', null, el('span', {class: 'badge ' + s.lag_key}, s.lag_type)),
        el('td', {class: 'num'}, s.avg_fps), el('td', {class: 'num'}, s.low1_fps),
        el('td', {class: 'num'}, s.stutters_per_min), el('td', null, s.top_cause));
      return tr;
    });
    const last = g.last;
    const play = g.play ? el('div', {class: 'card play', id: 'play-card'},
      el('button', {class: 'btn primary big', type: 'button', id: 'play', on: {click: (e) => startPlay(e.target, g.id)}},
        'Play with lag-doctor'),
      el('p', {class: 'muted'}, ...prose(`${g.play.text} lag-doctor has ${g.play.store} start the game, and waits for ` +
        'it without touching it.')))
      : g.wait ? el('div', {class: 'card play', id: 'wait-card'},
        el('button', {class: 'btn primary big', type: 'button', id: 'wait', on: {click: (e) => startPlay(e.target, g.id,
          'wait')}}, 'Record when it starts'),
        el('p', {class: 'muted'}, ...prose(g.wait.text)),
        el('p', {class: 'muted'}, 'Its folder: ', el('code', {id: 'wait-folder'}, g.wait.folder)))
      : el('div', {class: 'card'}, el('p', {class: 'muted'}, ...prose(g.play_note)));
    const edits = g.added ? el('div', {class: 'row'}, renameButton(g), removeButton(g.id)) : null;
    const hero = el('div', {class: 'hero'}, tileCover(g.tile, true),
      el('div', null, el('h1', {id: 'game-name'}, g.name),
        el('p', {class: 'muted'}, [g.exe_name, g.store].filter(Boolean).join(' · ')), play, edits));
    if (!last) {
      show(said, hero, el('p', {class: 'muted', id: 'no-recordings'}, 'No recordings of this game yet. ' +
        (g.wait ? 'Press Record when it starts' : 'Press Play with lag-doctor') +
        ': once the recording is analysed, it appears here.'));
      return;
    }
    show(
      said,
      hero,
      el('h2', null, 'Last recording'),
      el('div', {class: 'card'},
        el('div', {class: 'row'}, el('span', {class: 'badge ' + last.lag_key}, last.lag_type),
          el('span', {class: 'muted'}, last.when), el('span', {class: 'spacer'}),
          el('button', {class: 'btn primary', type: 'button', on: {click: () => go('results', {session: last.session})}},
            'Open its results')),
        el('p', {class: 'headline'}, ...prose(last.headline)),
        el('div', {class: 'tiles'}, ...last.tiles.map(tileOf))),
      el('div', {class: 'row'}, el('h2', null, 'Recordings'), el('span', {class: 'spacer'}), compareBtn, deleteBtn),
      el('p', {class: 'muted'}, 'Click a recording for its results. Tick two to compare them, or tick any to delete ' +
        'them: they go to the Recycle Bin.'),
      el('table', null, el('thead', null, el('tr', null, ...['', 'When', 'Length', 'Verdict', 'Avg FPS', '1% low',
        'Stutters / min', 'Top cause'].map((h) => el('th', null, h)))), el('tbody', null, ...rows)));
  },

  // Screen 6: the program the player chose in Windows' Open dialog (args.draft, the app's answer to pick_game), with
  // the name it will show by, which they may change; or the game of the Library's it already is.
  async add(args, live) {
    const d = args.draft || {};
    if (!live()) return;
    if (!d.draft) {
      show(el('h1', null, 'Add a game'), el('p', {id: 'add-why'}, ...prose(d.why || '')),
        el('div', {class: 'row'}, d.existing ? el('button', {class: 'btn primary', type: 'button', id: 'add-open',
          on: {click: () => { stack.length = 0; go('game', {game: d.existing}, false); }}}, 'Open its page') : null,
        addButton('Choose another program')));
      return;
    }
    const name = el('input', {type: 'text', id: 'add-name', maxlength: 200, value: d.name, spellcheck: 'false'});
    const confirm = el('button', {class: 'btn primary', type: 'button', id: 'add-confirm',
      on: {click: (e) => addGame(e.target, d.draft, name.value)}}, 'Add to the Library');
    name.addEventListener('keydown', (e) => { if (e.key === 'Enter') confirm.click(); });
    show(el('h1', null, 'Add a game'),
      el('div', {class: 'hero'}, tileCover(d.tile, true),
        el('div', {class: 'add'}, el('label', {for: 'add-name'}, 'Its name in the Library'), name,
          d.note ? el('p', {class: 'muted', id: 'add-note'}, d.note) : null,
          el('p', {class: 'muted'}, 'Its program: ', el('code', {id: 'add-exe'}, d.exe)),
          el('p', {class: 'muted'}, 'Its folder: ', el('code', {id: 'add-folder'}, d.folder)),
          el('p', {class: 'muted'}, ...prose(d.text)),
          el('div', {class: 'row'}, confirm, addButton('Choose another program')))));
    name.focus();
  },

  async results(args, live) {
    const id = nextId;                 // the id ask() is about to use: the frames arrive under it
    const r = await ask('results', {session: args.session});
    const frames = await framesFor(id, r.frames_token);
    if (!live()) return;
    const head = el('div', null, el('h1', null, r.game),
      el('p', {class: 'muted'}, [r.exe_name !== r.game ? r.exe_name : null, r.when, `${r.length} recorded`,
        r.analysed, r.frames_text].filter(Boolean).join(' · ')),
      r.note ? el('p', {class: 'note', id: 'note'}, el('strong', null, 'Your note: '), r.note) : null,
      r.part ? el('p', {class: 'note', id: 'part'}, r.part) : null);
    const verdict = el('div', {class: 'card'}, el('div', {class: 'verdict'},
      el('span', {class: 'badge ' + r.verdict.key}, r.verdict.title), el('p', {class: 'headline'}, ...prose(r.verdict.headline))));
    const tiles = el('div', {class: 'tiles'}, ...r.tiles.map(tileOf));
    const findings = r.findings.length ? r.findings.map((f, i) => el('div', {class: 'card finding'},
      el('h3', null, `${i + 1}. `, ...prose(f.title), el('span', {class: 'conf'}, f.confidence_text)),
      el('p', null, ...prose(f.summary)),
      f.evidence.length ? el('ul', null, ...f.evidence.map((x) => el('li', null, ...prose(x)))) : null,
      ...f.fixes.map((fx, j) => el('div', {class: 'fix'},
        el('strong', null, fx.can_apply ? 'Fix: ' : 'Try: '), ...prose(fx.title),
        fx.detail ? [': ', ...prose(fx.detail)] : null, fx.restart ? ' ' + fx.restart : null,
        fx.admin ? ' Needs admin rights.' : null,
        fx.apply ? applyBox(args.session, i, j) : null))))
      : [el('p', {class: 'muted'}, 'Nothing stood out. If the game felt laggy, record again and press the lag key ' +
        'the moment it happens.')];
    const lateCharts = [];
    const frameBox = chartBox({id: 'frames', title: 'Frame times'},
      'Higher is slower; spikes are stutters. Drag across the chart to zoom in, double-click to zoom out.' +
      (r.frames.rest.length ? ' The grey part of each chart was not analysed; an upright dashed line is a lag-key ' +
        'press there.' : ''));
    // Every time chart shades what a measured part left out, and draws the presses there apart.
    const shade = {rest: r.frames.rest, outside: r.frames.outside, marks: r.frames.marks};
    if (frames && frames.t.length) {
      const ms = Array.from(frames.ms, (v) => (Number.isNaN(v) ? null : v));
      // The scale is the HTML report's (r.frames.top, from the frames the analysis counted), so a loading screen
      // before a measured part, or the wait it starts in, is drawn clamped at the top; from every frame without one.
      let top = r.frames.top;
      if (typeof top !== 'number') {
        const vals = ms.filter((v) => v !== null).sort((a, b) => a - b);
        const p999 = vals[Math.min(vals.length - 1, Math.floor(vals.length * 0.999))] || 20;
        top = Math.max(Math.min(vals[vals.length - 1] * 1.05, Math.max(p999 * 1.6, 40)),
          (r.frames.refresh_ms || 0) * 2.2, 20);
      }
      lateCharts.push(() => plot(frameBox, [frames.t, ms], [{label: r.frames.label}],
        Object.assign({unit: 'ms', yMax: top, spans: r.frames.drops, ref: r.frames.refresh_ms, height: 260}, shade)));
    } else {
      frameBox.append(el('p', {class: 'muted'}, ...prose(r.frames.missing || (r.frames_token
        ? 'The frame times did not reach this window; the report page shows them.' : 'No frame times were recorded.'))));
    }
    const others = r.charts.map((spec) => {
      const [box, draw] = seriesChart(spec, shade);
      if (draw) lateCharts.push(draw);
      return box;
    });
    const detail = el('div', {id: 'moment'});
    const eventRows = r.events.map((ev) => el('tr', {class: 'pick', 'data-event': ev.id,
      on: {click: () => openMoment(args.session, ev.id, detail)}},
      el('td', null, ev.when), el('td', null, ev.what), el('td', null, ev.how_bad), el('td', null, ev.causes)));
    const sections = [head, verdict, tiles,
      el('h2', null, 'Causes'), ...findings, r.fix_note ? el('p', {class: 'muted'}, ...prose(r.fix_note)) : null,
      el('h2', null, 'Over the recording'), frameBox, ...others,
      el('h2', null, 'Lag moments'),
      r.events.length ? el('p', {class: 'muted'}, 'Click a moment to see what the PC did around it.') : null,
      r.events.length ? el('table', null, el('thead', null, el('tr', null,
        ...['When', 'What', 'How bad', 'Likely cause'].map((h) => el('th', null, h)))), el('tbody', null, ...eventRows))
        : el('p', {class: 'muted'}, r.events_note),
      detail,
      r.marks_summary ? el('p', null, r.marks_summary) : null,
      r.cleared.length ? [el('h2', null, 'Checked and fine'), el('ul', null,
        ...r.cleared.map(([topic, why]) => el('li', null, el('strong', null, topic), ': ', ...prose(why))))] : null,
      r.not_measured.length || r.unchecked.length ? el('h2', null, 'Not measured') : null,
      r.not_measured.length ? el('ul', null, ...r.not_measured.map(([name, why]) =>
        el('li', null, el('strong', null, name), ': ', ...prose(why)))) : null,
      r.unchecked.length ? el('ul', null, ...r.unchecked.map(([why, topics]) =>
        el('li', null, el('strong', null, topics.join(', ')), ': ', ...prose(why)))) : null,
      r.has_report ? el('p', null, el('button', {class: 'btn', type: 'button',
        on: {click: () => ask('open_report', {session: args.session})}}, 'Open the shareable report')) : null];
    show(...sections.flat().filter(Boolean));
    for (const draw of lateCharts) draw();
  },

  async compare(args, live) {
    const c = await ask('compare', {a: args.a, b: args.b});
    if (!live()) return;
    const head = el('tr', null, el('th', null, ''), el('th', null, c.a.when), el('th', null, c.b.when));
    const numbers = c.numbers.map((n) => el('tr', null, el('td', null, n.name),
      el('td', {class: 'num' + (n.better === 'a' ? ' better' : '')}, n.a),
      el('td', {class: 'num' + (n.better === 'b' ? ' better' : '')}, n.b)));
    const conditions = c.conditions.map((n) => el('tr', null, el('td', null, n.name),
      el('td', {class: n.differs ? 'differs' : null}, n.a), el('td', {class: n.differs ? 'differs' : null}, n.b)));
    show(el('h1', null, c.game), el('p', {class: 'muted'}, 'Two recordings side by side.'),
      el('table', {id: 'compare-table'}, el('thead', null, head), el('tbody', null,
        el('tr', null, el('td', null, 'Verdict'), el('td', null, c.a.lag_type), el('td', null, c.b.lag_type)),
        ...numbers)),
      el('h2', null, 'Conditions'),
      el('p', {class: 'muted'}, 'What each recording ran under: a difference here can explain a difference above.'),
      el('table', null, el('thead', null, head.cloneNode(true)), el('tbody', null, ...conditions)),
      c.note ? el('p', {class: 'muted'}, c.note) : null);
  },

  async settings(args, live) {
    const s = await ask('settings');
    if (!live()) return;
    const r = s.readiness || {};
    const sound = el('button', {class: 'btn', type: 'button', on: {click: () => ask('test_sound')}}, 'Play a test sound');
    const changes = el('div', {id: 'changes'}, el('p', {class: 'muted loading'}, 'Loading'));
    const said = saidNote(args);
    show(el('h1', null, 'Settings'),
      el('h2', null, 'Ready to record'),
      el('div', {class: 'strip' + (r.ok ? '' : ' no')}, ...prose(r.text)),
      r.detail ? el('p', {class: 'muted'}, ...prose(r.detail)) : null,
      el('h2', null, 'Keys'),
      el('table', null, el('tbody', null, ...s.keys.map(([what, key]) => el('tr', null, el('td', null, what),
        el('td', null, el('code', null, key)))))),
      el('h2', null, 'Sound'),
      el('p', {class: 'muted'}, 'While a recording runs, lag-doctor plays short sounds: a double knock when Windows ' +
        'asks for permission while the game is in front, one note when the start key is ready, a rising chime when ' +
        'you press it and measuring starts, a blip when it hears the lag key, a falling chime when the measured time ' +
        'is over, a chime when the results are ready, and a low tone when something stops working. Check that you ' +
        'can hear this one, the rising chime.'), sound,
      el('h2', null, 'Changes lag-doctor made'), said, changes,
      el('h2', null, 'Library'), el('p', null, s.library_sources),
      el('h2', null, 'Where recordings are kept'),
      el('div', {class: 'row'}, el('code', null, s.sessions_folder),
        el('button', {class: 'btn', type: 'button', on: {click: () => ask('open_folder')}}, 'Open the folder')),
      el('h2', null, 'About'),
      el('table', null, el('tbody', null, ...s.about.map(([k, v]) => el('tr', null, el('td', null, k), el('td', null, v))))));
    // The list of changes comes in its own answer: a journal another window holds keeps the rest of Settings drawn.
    ask('changes').then((c) => { if (live()) fillChanges(changes, c); }).catch((err) => {
      if (live()) fill(changes, el('p', {class: 'error'}, String(err && err.message || err)));
    });
  },

  async recording(args, live) {
    const r = await ask('recording');
    if (!live()) return;
    recordingPushed(r);
    drawRecording();
  },
};

// ------------------------------------------------------------------------------------- a running recording --

// The app's newest view of the recording it started (lagdoctor/app/views.py, recording_view), or null. Its seq rises
// with every view the app makes, so a reply and a push that cross keep the newest.
let rec = null;
let recSeq = 0;
let recClock = null;
let endSeen = false;             // the last recording's end has been shown on its own screen (a page loaded
                                 // again after an end offers the way back to it once)

function recordingPushed(r) {
  if (!r || typeof r !== 'object' || typeof r.seq !== 'number' || r.seq <= recSeq) return;
  const wasActive = !!(rec && rec.active);
  recSeq = r.seq;
  rec = Object.assign({}, r, {at: performance.now()});
  if (r.active) endSeen = false;
  if (current && current.name === 'recording') drawRecording();
  else if (wasActive && !r.active && current && current.name === 'library') go('library', {}, false);   // it is new
  updateBar();
}

// The bar's button back to the running screen: while a recording runs, and after it ends until its end is seen.
function updateBar() {
  const bar = $('rec');
  bar.hidden = !(rec && (rec.active || (rec.phase === 'done' && !endSeen)));
  bar.textContent = rec && rec.active ? 'Recording' : 'Recording ended';
}

function recError(err) {
  const old = $('rec-error');
  if (old) old.remove();
  const p = el('p', {class: 'error', id: 'rec-error'}, String(err && err.message || err));
  view().prepend(p);
  p.scrollIntoView({block: 'nearest'});          // seen from a long page scrolled down (Delete's picks)
}

function startRecording(button) {
  if (button) button.disabled = true;
  ask('record').then((r) => {
    recordingPushed(r);
    stack.length = 0;
    go('recording', {}, false);
  }).catch((err) => {
    if (button) button.disabled = false;
    recError(err);
  });
}

// Play (how 'play') or Record when it starts (how 'wait'), for the game the page names.
function startPlay(button, game, how = 'play') {
  if (button) button.disabled = true;
  ask(how === 'wait' ? 'wait' : 'play', {game}).then((r) => {
    recordingPushed(r);
    stack.length = 0;
    go('recording', {}, false);
  }).catch((err) => {
    if (button) button.disabled = false;
    recError(err);
  });
}

function playButton(game, label, how = 'play') {
  return el('button', {class: 'btn primary', type: 'button', id: 'play-again',
    on: {click: (e) => startPlay(e.target, game, how)}}, label);
}

// Add a game (screen 6): Windows' Open dialog, opened by the app; the program chosen is shown on its own screen.
function startAdd(button) {
  if (button) button.disabled = true;
  ask('pick_game').then((r) => {
    if (button) button.disabled = false;
    if (!r || r.cancelled) return;
    go('add', {draft: r}, !(current && current.name === 'add'));
  }).catch((err) => {
    if (button) button.disabled = false;
    recError(err);
  });
}

function addButton(label) {
  return el('button', {class: 'btn', type: 'button', id: 'add-game', on: {click: (e) => startAdd(e.target)}},
    label || 'Add a game');
}

function addGame(button, draft, name) {
  button.disabled = true;
  ask('add_game', {draft, name}).then((r) => {
    stack.length = 0;
    go('game', {game: r.game}, false);
  }).catch((err) => {
    button.disabled = false;
    recError(err);
  });
}

// Remove from the Library, for a game added by hand: a second click does it, and its recordings stay.
function removeButton(game) {
  const b = el('button', {class: 'btn', type: 'button', id: 'remove-game', on: {click: () => {
    if (b.dataset.armed !== '1') {
      b.dataset.armed = '1';
      b.textContent = 'Click again to remove it (its recordings stay)';
      return;
    }
    b.disabled = true;
    ask('remove_game', {game}).then(() => { stack.length = 0; go('library', {}, false); }).catch((err) => {
      b.disabled = false;
      recError(err);
    });
  }}}, 'Remove from the Library');
  return b;
}

// Rename, for a game added by hand: its name, changed in place of the heading; Enter saves it, Escape keeps the old.
function renameButton(g) {
  const b = el('button', {class: 'btn', type: 'button', id: 'rename-game', on: {click: () => {
    const heading = $('game-name');
    const name = el('input', {type: 'text', id: 'rename-name', maxlength: 200, value: g.name, spellcheck: 'false'});
    const save = el('button', {class: 'btn primary', type: 'button', id: 'rename-save', on: {click: () => {
      save.disabled = true;
      ask('rename_game', {game: g.id, name: name.value}).then(() => go('game', {game: g.id}, false)).catch((err) => {
        save.disabled = false;
        recError(err);
      });
    }}}, 'Save the name');
    const cancel = el('button', {class: 'btn', type: 'button', id: 'rename-cancel', on: {click: () => {
      form.replaceWith(heading);
      b.hidden = false;
    }}}, 'Cancel');
    const form = el('div', {class: 'row', id: 'rename-form'}, name, save, cancel);
    name.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') save.click();
      else if (e.key === 'Escape') cancel.click();
    });
    heading.replaceWith(form);
    b.hidden = true;
    name.focus();
    name.select();
  }}}, 'Rename');
  return b;
}

// Delete, for the recordings picked on a game's page (`chosen`, at most bridge.MAX_DELETE of them): a second click
// moves them to the Recycle Bin. The page is drawn again with what was said, or the Library when the game went with
// its last recording.
const MAX_DELETE = 100;
function deleteButton(game, chosen) {
  let busy = false;                    // asked: a pick meanwhile enables nothing
  const b = el('button', {class: 'btn', type: 'button', disabled: true, id: 'delete-picked', on: {click: () => {
    const n = chosen.size;
    if (b.dataset.armed !== '1') {
      b.dataset.armed = '1';
      b.textContent = `Click again to move ${n === 1 ? 'it' : `these ${n}`} to the Recycle Bin`;
      return;
    }
    b.disabled = busy = true;
    ask('delete_recordings', {game, sessions: [...chosen]}).then((r) => {
      if (r.listed) go('game', {game, said: r.text}, false);
      else { stack.length = 0; go('library', {said: r.text}, false); }
    }).catch((err) => {
      busy = false;
      b.disabled = false;
      recError(err);
    });
  }}}, 'Delete the picked');
  b.picked = () => {                   // the picks changed: a click armed for the old ones is undone
    if (busy) return;
    delete b.dataset.armed;
    b.disabled = chosen.size === 0 || chosen.size > MAX_DELETE;
    b.textContent = chosen.size > MAX_DELETE ? `Pick at most ${MAX_DELETE} to delete` : 'Delete the picked';
  };
  return b;
}

// Apply, for a fix the last recording offers: the first click shows what would change (the app's plan, read now, as
// `lagdoctor fix` prints it), the second makes exactly that change, and its Undo puts it back. The page names the fix
// only by its place on this page; the app reads the fix and its settings from the recording itself.
function applyBox(session, finding, fix) {
  const box = el('div', {class: 'apply', id: `apply-${finding}-${fix}`});
  const start = el('button', {class: 'btn', type: 'button', id: `plan-${finding}-${fix}`}, 'Apply this fix');
  const again = (...said) => { start.disabled = false; fill(box, ...said, start); };
  start.addEventListener('click', () => {
    start.disabled = true;
    ask('plan_fix', {session, finding, fix}).then((p) => {
      const shown = [el('p', null, el('strong', null, 'Where: '), p.where),
        el('p', null, el('strong', null, 'Change: '), p.change),
        ...p.notes.map((n) => el('p', {class: 'muted'}, ...prose(n))), p.restart ? el('p', {class: 'muted'}, p.restart) : null];
      const cancel = el('button', {class: 'btn', type: 'button', id: 'apply-cancel', on: {click: () => again()}},
        p.plan ? 'Cancel' : 'Close');
      if (!p.plan) { fill(box, shown, el('p', {class: 'note', id: 'applied'}, p.text), cancel); return; }
      const confirm = el('button', {class: 'btn primary', type: 'button', id: 'apply-confirm'}, 'Apply this change');
      confirm.addEventListener('click', () => {
        confirm.disabled = cancel.disabled = true;
        ask('apply_fix', {plan: p.plan}).then((r) => {
          const said = el('p', {class: 'note', id: 'applied'}, r.text);
          if (!r.entry) { again(said); return; }
          const done = (u) => fill(box, el('p', {class: 'note', id: 'undone'}, u.text),
            u.force ? undoButton(r.entry, done, true) : null);
          fill(box, said, undoButton(r.entry, done));
        }).catch((err) => {
          confirm.disabled = cancel.disabled = false;
          recError(err);
        });
      });
      fill(box, shown, el('div', {class: 'row'}, confirm, cancel));
    }).catch((err) => {
      start.disabled = false;
      recError(err);
    });
  });
  box.append(start);
  return box;
}

// Undo, for a change lag-doctor made (its journal entry's id, from the app): a click puts back how it was. When it was
// changed since, the app leaves it alone and says so, and Put it back anyway (`force`) then takes a second click.
// done(answer) shows what the app said.
function undoButton(entry, done, force = false) {
  const b = el('button', {class: 'btn', type: 'button', 'data-entry': entry, 'data-force': force ? '1' : null},
    force ? 'Put it back anyway' : 'Undo');
  b.addEventListener('click', () => {
    if (force && b.dataset.armed !== '1') {
      b.dataset.armed = '1';
      b.textContent = 'Click again to put back how it was';
      return;
    }
    b.disabled = true;
    ask('undo_change', {entry, force}).then((u) => done(u)).catch((err) => {
      b.disabled = false;
      recError(err);
    });
  });
  return b;
}

// Settings' list of the changes lag-doctor made that are still in place, newest first: each with its Undo (Put it back
// anyway for one that is neither how it was nor lag-doctor's), or a note saying why it has none. An undo draws the
// list again with what was said, unless the change was left alone because it was changed since: then that change's
// row offers to put it back anyway.
function fillChanges(box, c) {
  if (!c.rows.length) { fill(box, el('p', {class: c.paused ? 'error' : 'muted', id: 'changes-none'}, ...prose(c.text))); return; }
  const rows = c.rows.map((r) => {
    const note = el('p', {class: 'muted'}, ...prose(r.note));
    const done = (u) => {
      if (!u.force) { go('settings', {said: u.text}, false); return; }
      fill(note, u.text);
      fill(buttons, undoButton(r.id, done, true));
    };
    const buttons = el('div', {class: 'row'}, r.undo ? undoButton(r.id, done) : null,
      r.force ? undoButton(r.id, done, true) : null);
    return el('div', {class: 'card change', 'data-entry': r.id},
      el('strong', null, r.title), r.change ? el('p', null, r.change) : null,
      el('p', {class: 'muted'}, r.where), el('p', {class: 'muted'}, `Made ${r.when}`), note, buttons);
  });
  fill(box, el('p', {class: 'muted'}, 'Newest first. Undo puts back how each setting was before lag-doctor changed it.'),
    rows);
}

// What a deletion said, shown once on the screen it led to (args.said): not again when Back returns to that screen.
function saidNote(args) {
  const text = args.said;
  delete args.said;
  return text ? el('p', {class: 'note', id: 'said'}, text) : null;
}

function recordButton(label) {
  return el('button', {class: 'btn primary', type: 'button', id: 'record', on: {click: (e) => startRecording(e.target)}},
    label || 'Record the game that\'s running');
}

// "07:12 of 10:00": the running clock, counted on from the app's last view while the recording runs.
function clockText(r) {
  const two = (v) => String(v).padStart(2, '0');
  const mmss = (v) => {
    v = Math.max(0, Math.floor(v));
    const h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60);
    return (h ? `${h}:${two(m)}` : two(m)) + `:${two(v % 60)}`;
  };
  let s = (Number(r.elapsed_s) || 0) + (r.phase === 'recording' ? (performance.now() - r.at) / 1000 : 0);
  if (r.length_s) s = Math.min(s, r.length_s);
  return r.length_s ? `${mmss(s)} of ${mmss(r.length_s)}` : mmss(s);
}

function drawRecording() {
  if (recClock) { clearInterval(recClock); recClock = null; }
  const r = rec;
  if (!r || !r.phase) {
    show(el('h1', null, 'Record a game'), el('p', {class: 'muted'}, 'No recording is running. Start a game first.'),
      recordButton());
    return;
  }
  const answer = (kind) => (e) => { e.target.disabled = true; ask(kind).then(recordingPushed).catch(recError); };
  const buttons = [];
  if (r.action === 'cancel') {
    buttons.push(el('button', {class: 'btn', type: 'button', id: 'rec-cancel', on: {click: answer('cancel_recording')}},
      'Cancel'));
  } else if (r.action === 'stop') {
    buttons.push(el('button', {class: 'btn primary', type: 'button', id: 'rec-stop', on: {click: answer('stop_recording')}},
      'Stop and save'));
  }
  if (r.phase === 'done') {
    if (r.session) {
      buttons.push(el('button', {class: 'btn primary', type: 'button', id: 'rec-results',
        on: {click: () => go('results', {session: r.session})}}, 'Open its results'));
    }
    buttons.push(r.play ? playButton(r.play.game, r.ok ? (r.play.wait ? 'Record it again' : 'Play again') : 'Try again',
      r.play.wait ? 'wait' : 'play')
      : recordButton(r.ok ? 'Record again' : 'Try again'));
    buttons.push(el('button', {class: 'btn', type: 'button', on: {click: () => { stack.length = 0; go('library', {}, false); }}},
      'Back to the library'));
  }
  const clock = r.phase === 'recording' ? el('p', {class: 'clock', id: 'rec-clock'}, clockText(r)) : null;
  show(el('div', {class: 'running', id: 'running', 'data-phase': r.phase, 'data-stage': r.stage},
    el('h1', {id: 'rec-title'}, r.title),
    r.game ? el('p', {class: 'rec-game', id: 'rec-game'}, r.game) : null,
    clock,
    r.phase === 'done' && r.elapsed_s ? el('p', {class: 'muted'}, `Recorded ${fmtElapsed(r.elapsed_s)}.`) : null,
    el('p', {id: 'rec-detail'}, ...prose(r.detail)),
    r.notes.length ? el('ul', {class: 'notes', id: 'rec-notes'}, ...r.notes.map((n) => el('li', null, ...prose(n)))) : null,
    el('div', {class: 'row'}, ...buttons),
    r.said.length ? [el('h2', null, 'What lag-doctor said'), el('pre', {class: 'said', id: 'rec-said'}, r.said.join('\n'))]
      : null));
  if (clock) recClock = setInterval(() => { clock.textContent = clockText(r); }, 1000);
  if (r.phase === 'done') endSeen = true;
  updateBar();
}

async function openMoment(session, eventId, box) {
  for (const tr of document.querySelectorAll('tr[data-event]')) tr.classList.toggle('chosen', tr.dataset.event === String(eventId));
  box.replaceChildren(el('p', {class: 'muted loading'}, 'Reading the moment'));
  let m;
  try {
    m = await ask('moment', {session, event: eventId});
  } catch (err) {
    box.replaceChildren(el('p', {class: 'error'}, String(err.message || err)));
    return;
  }
  const draws = [];
  const span = [[m.t0, Math.max(m.t1, m.t0 + 0.05)]];
  const around = {spans: span, marks: m.marks, rest: m.rest, outside: m.outside, height: 160};
  const cards = [];
  if (m.frames) {
    const [b, d] = seriesChart({id: 'm-frames', title: 'Frame times', unit: 'ms', t: m.frames.t, series: m.frames.series},
      around);
    cards.push(b); if (d) draws.push(d);
  }
  for (const spec of m.charts) {
    const [b, d] = seriesChart(spec, around);
    cards.push(b); if (d) draws.push(d);
  }
  box.replaceChildren(el('div', {class: 'card moment'},
    el('h3', null, `${m.what} at ${m.when}`),
    m.causes.length ? el('p', null, 'Likely cause: ', m.causes.join(', ')) : el('p', {class: 'muted'}, 'No clear cause.'),
    m.why.length ? el('ul', null, ...m.why.map((w) => el('li', null, ...prose(w)))) : null,
    el('div', {class: 'charts'}, ...cards),
    m.programs.length ? [el('h3', null, 'Other programs at that moment'), el('table', null,
      el('thead', null, el('tr', null, ...['Program', 'CPU', 'GPU', 'Disk'].map((h) => el('th', null, h)))),
      el('tbody', null, ...m.programs.map((p) => el('tr', null, el('td', null, p.name), el('td', {class: 'num'}, p.cpu),
        el('td', {class: 'num'}, p.gpu), el('td', {class: 'num'}, p.disk)))))] : null));
  for (const d of draws) d();
  box.scrollIntoView({block: 'nearest'});
}

// ------------------------------------------------------------------------------------------------- start --

if (wv) {
  go('library', {}, false);
  wv.postMessage({type: 'ready'});
  // A page loaded again during a recording (after a crash of its own) goes back to it.
  ask('recording').then((r) => {
    recordingPushed(r);
    if (r && r.active && current && current.name === 'library') go('recording', {}, false);
  }).catch(() => {});
} else {
  show(el('p', {class: 'error'}, 'This page only works inside lag-doctor\'s window.'));
}
