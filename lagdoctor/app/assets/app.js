'use strict';
// lag-doctor's screens: Library, a game's page, Results, Compare, Settings and a running recording. Everything shown
// comes from the app over JSON web messages (lagdoctor/app/bridge.py). Text from a capture or the PC (a game's title,
// a process name, evidence) is put on the page as text only: el() makes text nodes, and nothing here parses markup.

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

// Shaded spans ([t0, t1]), vertical marks and a reference line, drawn under and over a uPlot chart.
function overlays(spans, marks, ref) {
  return {
    drawClear: [(u) => {
      const ctx = u.ctx;
      ctx.save();
      ctx.fillStyle = css('--band');
      for (const [a, b] of spans || []) {
        const x0 = u.valToPos(a, 'x', true), x1 = u.valToPos(b, 'x', true);
        ctx.fillRect(x0, u.bbox.top, Math.max(1, x1 - x0), u.bbox.height);
      }
      ctx.restore();
    }],
    draw: [(u) => {
      const ctx = u.ctx;
      ctx.save();
      if (ref) {
        const y = u.valToPos(ref, 'y', true);
        ctx.strokeStyle = css('--ref');
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(u.bbox.left, y); ctx.lineTo(u.bbox.left + u.bbox.width, y); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.strokeStyle = css('--mark');
      ctx.lineWidth = 2;
      for (const m of marks || []) {
        const x = u.valToPos(m, 'x', true);
        ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
      }
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
    hooks: overlays(opts.spans, opts.marks, opts.ref),
  }, data, box);
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

function tileCover(t, big) {
  const c = el('div', {class: 'cover'}, t.initials);
  c.style.background = `linear-gradient(160deg, hsl(${t.hue}, 55%, 46%), hsl(${(t.hue + 40) % 360}, 50%, 30%))`;
  if (big) c.style.fontSize = '54px';
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
      el('div', {class: 'sub'}, `${g.recordings} recording${g.recordings === 1 ? '' : 's'} · ${g.last}`),
      el('span', {class: 'badge ' + g.lag_key}, g.lag_type)));
}

const SCREENS = {
  async library(args, live) {
    const data = await ask('library');
    if (!live()) return;
    const r = data.readiness || {};
    const strip = el('div', {class: 'strip' + (r.ok ? '' : ' no'), title: r.detail || ''}, ...prose(r.text));
    if (!data.games.length) {
      show(strip, el('h1', null, 'Your games'), el('div', {class: 'empty'},
        el('p', null, 'No recordings yet.'),
        el('p', {class: 'muted'}, 'Start a game, then press Record the game that\'s running. When the recording is ' +
          'analysed, the game appears here with its results.'), recordButton()));
      return;
    }
    let filter = 'all', query = '';
    const grid = el('div', {class: 'grid', id: 'games'});
    const draw = () => {
      const q = query.trim().toLowerCase();
      grid.replaceChildren(...data.games.filter((g) => !q || g.name.toLowerCase().includes(q) ||
        (g.exe_name || '').toLowerCase().includes(q)).map(gameCard));
      if (!grid.children.length) grid.append(el('p', {class: 'muted'}, 'No game matches.'));
    };
    const chips = ['all', 'recorded'].map((f) => el('button', {class: 'chip' + (f === filter ? ' on' : ''), type: 'button',
      on: {click: (e) => { filter = f; for (const c of chips) c.classList.toggle('on', c === e.target); draw(); }}},
      f === 'all' ? 'All' : 'Recorded'));
    const search = el('input', {type: 'search', placeholder: 'Search your games', id: 'search',
      on: {input: (e) => { query = e.target.value; draw(); }}});
    const recent = data.games.filter((g) => data.recent.includes(g.id));
    draw();
    show(strip,
      el('div', {class: 'row tools'}, search, ...chips, el('span', {class: 'spacer'}),
        el('span', {class: 'muted'}, `${data.games.length} game${data.games.length === 1 ? '' : 's'}, ` +
          `${data.recordings} recording${data.recordings === 1 ? '' : 's'}`), recordButton()),
      // The Recent row only when the library holds more than it: with a few games it would repeat them all.
      data.games.length > recent.length ? el('section', {class: 'recent'}, el('h2', null, 'Recent'),
        el('div', {class: 'grid'}, ...recent.map(gameCard))) : null,
      el('h2', null, 'All games'), grid);
  },

  async game(args, live) {
    const g = await ask('game', {game: args.game});
    if (!live()) return;
    const chosen = new Set();
    const compareBtn = el('button', {class: 'btn', type: 'button', disabled: true, id: 'compare',
      on: {click: () => { const [a, b] = [...chosen]; go('compare', {a, b}); }}}, 'Compare the two picked');
    const rows = g.sessions.map((s) => {
      const box = el('input', {type: 'checkbox', title: 'Pick for Compare', on: {click: (e) => {
        e.stopPropagation();
        if (e.target.checked) chosen.add(s.id); else chosen.delete(s.id);
        tr.classList.toggle('chosen', e.target.checked);
        compareBtn.disabled = chosen.size !== 2;
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
    show(
      el('div', {class: 'hero'}, tileCover(g.tile, true),
        el('div', null, el('h1', {id: 'game-name'}, g.name),
          el('p', {class: 'muted'}, [g.exe_name, g.store].filter(Boolean).join(' · ')),
          el('div', {class: 'card'}, el('p', {class: 'muted'}, ...prose(g.play_note))))),
      el('h2', null, 'Last recording'),
      el('div', {class: 'card'},
        el('div', {class: 'row'}, el('span', {class: 'badge ' + last.lag_key}, last.lag_type),
          el('span', {class: 'muted'}, last.when), el('span', {class: 'spacer'}),
          el('button', {class: 'btn primary', type: 'button', on: {click: () => go('results', {session: last.session})}},
            'Open its results')),
        el('p', {class: 'headline'}, ...prose(last.headline)),
        el('div', {class: 'tiles'}, ...last.tiles.map(tileOf))),
      el('div', {class: 'row'}, el('h2', null, 'Recordings'), el('span', {class: 'spacer'}), compareBtn),
      el('p', {class: 'muted'}, 'Click a recording for its results. Tick two to compare them.'),
      el('table', null, el('thead', null, el('tr', null, ...['', 'When', 'Length', 'Verdict', 'Avg FPS', '1% low',
        'Stutters / min', 'Top cause'].map((h) => el('th', null, h)))), el('tbody', null, ...rows)));
  },

  async results(args, live) {
    const id = nextId;                 // the id ask() is about to use: the frames arrive under it
    const r = await ask('results', {session: args.session});
    const frames = await framesFor(id, r.frames_token);
    if (!live()) return;
    const head = el('div', null, el('h1', null, r.game),
      el('p', {class: 'muted'}, [r.exe_name !== r.game ? r.exe_name : null, r.when, `${r.length} recorded`,
        r.frames_text].filter(Boolean).join(' · ')),
      r.note ? el('p', {class: 'note', id: 'note'}, el('strong', null, 'Your note: '), r.note) : null);
    const verdict = el('div', {class: 'card'}, el('div', {class: 'verdict'},
      el('span', {class: 'badge ' + r.verdict.key}, r.verdict.title), el('p', {class: 'headline'}, ...prose(r.verdict.headline))));
    const tiles = el('div', {class: 'tiles'}, ...r.tiles.map(tileOf));
    const findings = r.findings.length ? r.findings.map((f, i) => el('div', {class: 'card finding'},
      el('h3', null, `${i + 1}. `, ...prose(f.title), el('span', {class: 'conf'}, f.confidence_text)),
      el('p', null, ...prose(f.summary)),
      f.evidence.length ? el('ul', null, ...f.evidence.map((x) => el('li', null, ...prose(x)))) : null,
      ...f.fixes.map((fx) => el('div', {class: 'fix'},
        el('strong', null, fx.can_apply ? 'Fix: ' : 'Try: '), ...prose(fx.title),
        fx.detail ? [': ', ...prose(fx.detail)] : null, fx.restart ? ' ' + fx.restart : null,
        fx.admin ? ' Needs admin rights.' : null,
        fx.command ? el('div', null, 'In a terminal: ', el('code', null, fx.command)) : null))))
      : [el('p', {class: 'muted'}, 'Nothing stood out. If the game felt laggy, record again and press the lag key ' +
        'the moment it happens.')];
    const lateCharts = [];
    const frameBox = chartBox({id: 'frames', title: 'Frame times'},
      'Higher is slower; spikes are stutters. Drag across the chart to zoom in, double-click to zoom out.');
    if (frames && frames.t.length) {
      const ms = Array.from(frames.ms, (v) => (Number.isNaN(v) ? null : v));
      const vals = ms.filter((v) => v !== null).sort((a, b) => a - b);
      const p999 = vals[Math.min(vals.length - 1, Math.floor(vals.length * 0.999))] || 20;
      let top = Math.min(vals[vals.length - 1] * 1.05, Math.max(p999 * 1.6, 40));
      top = Math.max(top, (r.frames.refresh_ms || 0) * 2.2, 20);
      lateCharts.push(() => plot(frameBox, [frames.t, ms], [{label: r.frames.label}],
        {unit: 'ms', yMax: top, spans: r.frames.drops, marks: r.frames.marks, ref: r.frames.refresh_ms, height: 260}));
    } else {
      frameBox.append(el('p', {class: 'muted'}, ...prose(r.frames.missing || (r.frames_token
        ? 'The frame times did not reach this window; the report page shows them.' : 'No frame times were recorded.'))));
    }
    const others = r.charts.map((spec) => {
      const [box, draw] = seriesChart(spec, {marks: r.frames.marks});
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
    show(el('h1', null, 'Settings'),
      el('h2', null, 'Ready to record'),
      el('div', {class: 'strip' + (r.ok ? '' : ' no')}, ...prose(r.text)),
      r.detail ? el('p', {class: 'muted'}, ...prose(r.detail)) : null,
      el('h2', null, 'Keys'),
      el('table', null, el('tbody', null, ...s.keys.map(([what, key]) => el('tr', null, el('td', null, what),
        el('td', null, el('code', null, key)))))),
      el('h2', null, 'Sound'),
      el('p', {class: 'muted'}, 'While a recording runs, lag-doctor plays short sounds: a double knock when Windows ' +
        'asks for permission while the game is in front, a chime when it starts, a blip when it hears the lag key, a ' +
        'chime when it ends, and a low tone when something stops working. Check that you can hear this one.'), sound,
      el('h2', null, 'Library'), el('p', null, s.library_sources),
      el('h2', null, 'Where recordings are kept'),
      el('div', {class: 'row'}, el('code', null, s.sessions_folder),
        el('button', {class: 'btn', type: 'button', on: {click: () => ask('open_folder')}}, 'Open the folder')),
      el('h2', null, 'About'),
      el('table', null, el('tbody', null, ...s.about.map(([k, v]) => el('tr', null, el('td', null, k), el('td', null, v))))));
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
  view().prepend(el('p', {class: 'error', id: 'rec-error'}, String(err && err.message || err)));
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
    buttons.push(recordButton(r.ok ? 'Record again' : 'Try again'));
    buttons.push(el('button', {class: 'btn', type: 'button', on: {click: () => { stack.length = 0; go('library', {}, false); }}},
      'Back to the library'));
  }
  const clock = r.phase === 'recording' ? el('p', {class: 'clock', id: 'rec-clock'}, clockText(r)) : null;
  show(el('div', {class: 'running', id: 'running', 'data-phase': r.phase},
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
  const cards = [];
  if (m.frames) {
    const [b, d] = seriesChart({id: 'm-frames', title: 'Frame times', unit: 'ms', t: m.frames.t, series: m.frames.series},
      {spans: span, marks: m.marks, height: 160});
    cards.push(b); if (d) draws.push(d);
  }
  for (const spec of m.charts) {
    const [b, d] = seriesChart(spec, {spans: span, marks: m.marks, height: 160});
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
