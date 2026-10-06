// ov.js — shared by dashboard.html and forward.html: helpers, the chart
// block (candles + indicators + MACD), GitHub read/write, the token panel.

const LOWER_TF_MAP = { '1H': ['15m', '5m'], '4H': ['1H', '15m'], '1D': ['4H', '1H'], '1W': ['1D', '4H'] };
const dirClass = (d) => (d || '').toLowerCase();
const dirArrow = (d) => d === 'BULLISH' ? '▲' : '▼';
const isS50 = (w) => w.setup === 'sma50';
const pickKey = (p) => `${p.asset_class}:${p.ticker}:${p.higher_tf}${isS50(p) ? ':sma50' : ''}`;
const TAG_MARKET = { currency: 'forex', gold: 'metals', uranium: 'energy' };
const marketsOf = (a) => new Set([a.asset_class, TAG_MARKET[a.tag]].filter(Boolean));
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// scan.py ships candles as {fields, rows}; expand back to objects. Plain
// arrays are the older layout, still accepted.
function toCandles(c) {
  if (!c) return [];
  if (Array.isArray(c)) return c;
  return c.rows.map(r => Object.fromEntries(c.fields.map((f, i) => [f, r[i]])));
}
// Crypto and PSE times come without a timezone but are UTC — read them as
// UTC, not as the browser's local time (which put every marker 8h early).
const parseTime = (iso) => new Date(/[zZ]$|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
const toUnixTime = (iso) => iso ? Math.floor(parseTime(iso).getTime() / 1000) : null;

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
let activeCharts = [];     // { chart, el } so resizes match the right element
let indicatorSeries = [];  // { key, series } — every indicator line on screen
function teardownCharts() {
  activeCharts.forEach(c => { try { c.chart.remove(); } catch (e) {} });
  activeCharts = []; indicatorSeries = [];
}

// Chart indicators the user can switch on/off (legend toggles). The choice
// applies to every chart and is remembered per browser.
const LINE_STYLE = (window.LightweightCharts && LightweightCharts.LineStyle) || { Solid: 0, Dotted: 1, Dashed: 2 };
const INDICATORS = [
  { key: 'ema10', label: 'EMA10', color: () => '#D9A441', style: LINE_STYLE.Solid, width: 1 },
  { key: 'ema20', label: 'EMA20', color: () => cssVar('--violet'), style: LINE_STYLE.Solid, width: 1 },
  { key: 'lsma', label: 'LSMA 50,3', color: () => cssVar('--blue'), style: LINE_STYLE.Dotted, width: 2 },
  { key: 'sma50', label: 'SMA 50', color: () => cssVar('--ink'), style: LINE_STYLE.Dashed, width: 1 },
  { key: 'macd', label: 'MACD' },
];
const IND_KEY = 'ovs-indicators';
const shown = (() => {
  const all = Object.fromEntries(INDICATORS.map(i => [i.key, true]));
  try { return { ...all, ...JSON.parse(localStorage.getItem(IND_KEY) || '{}') }; } catch (e) { return all; }
})();

function setIndicator(key, on) {
  shown[key] = on;
  try { localStorage.setItem(IND_KEY, JSON.stringify(shown)); } catch (e) {}
  indicatorSeries.filter(s => s.key === key).forEach(s => s.series.applyOptions({ visible: on }));
  if (key === 'macd') {
    document.querySelectorAll('.macd-box').forEach(el => el.classList.toggle('is-hidden', !on));
    // A pane created while hidden has zero width and height — size it now
    // that it's visible.
    if (on) activeCharts.forEach(({ chart, el }) => chart.resize(el.clientWidth, el.clientHeight));
  }
  document.querySelectorAll(`.ind[data-key="${key}"]`).forEach(b => { b.classList.toggle('off', !on); b.setAttribute('aria-pressed', on); });
}

function indicatorToggles() {
  return INDICATORS.map(i => {
    const sw = i.key === 'macd'
      ? `<span class="ind-sw" style="border-color:var(--gain)"></span>`
      : `<span class="ind-sw" style="border-color:${i.color()};border-top-style:${i.style === LINE_STYLE.Dotted ? 'dotted' : i.style === LINE_STYLE.Dashed ? 'dashed' : 'solid'}"></span>`;
    return `<button class="ind${shown[i.key] ? '' : ' off'}" type="button" data-key="${i.key}" aria-pressed="${shown[i.key]}" title="Show or hide ${i.label}">${sw}${i.label}</button>`;
  }).join('');
}

const css = getComputedStyle(document.documentElement);
const cssVar = (name) => css.getPropertyValue(name).trim();
const RIGHT_GAP = 4;       // empty bars after the latest candle, off the price axis
const AXIS_WIDTH = 54;

const chartTheme = {
  layout: { background: { type: 'solid', color: 'transparent' }, textColor: cssVar('--ink-dim'), fontFamily: "'IBM Plex Mono', monospace", fontSize: 9 },
  grid: { vertLines: { visible: false }, horzLines: { visible: false } },
  rightPriceScale: { borderColor: cssVar('--border'), scaleMargins: { top: 0.1, bottom: 0.14 }, minimumWidth: AXIS_WIDTH },
  timeScale: { borderColor: cssVar('--border'), timeVisible: false, secondsVisible: false, fixLeftEdge: true, rightOffset: RIGHT_GAP },
  crosshair: { mode: 0 },
  // Drag to pan, pinch / drag the price axis to scale. The plain mouse
  // wheel is left to the page — Ctrl+wheel zooms (attachCtrlWheelZoom).
  handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
};
const followerInteraction = { handleScroll: false, handleScale: false };

function attachCtrlWheelZoom(chart, el, lastIndex) {
  const ts = chart.timeScale();
  // A new range only applies on the next paint, so fast wheel events
  // (trackpad pinch) build on the last range asked for, not a stale one.
  let pending = null;
  ts.subscribeVisibleLogicalRangeChange(() => { pending = null; });
  el.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    const range = pending || ts.getVisibleLogicalRange();
    if (!range) return;
    const x = e.clientX - el.getBoundingClientRect().left;
    const anchor = ts.coordinateToLogical(x) ?? (range.from + range.to) / 2;
    const dy = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
    const factor = Math.exp(Math.max(-100, Math.min(100, dy)) * 0.002);
    const from = Math.max(0, anchor - (anchor - range.from) * factor);
    const to = Math.min(lastIndex + RIGHT_GAP, anchor + (range.to - anchor) * factor);
    if (to - from < 5) return;
    pending = { from, to };
    ts.setVisibleLogicalRange(pending);
  }, { passive: false });
}

// Axis decimals sized to the price: ~3 significant digits (5 for forex,
// to keep pips), at least 2 decimals — 3.60, 0.0977, 1.3235.
const pricePrecision = (price, forex) => Math.min(12, Math.max(2, Math.ceil(-Math.log10(Math.abs(price) || 1)) + (forex ? 4 : 2)));

// Returns { chart, series, el, precision } for the candle chart (or null
// when there are no candles) so callers can add lines or listen to clicks.
// opts.trades: forward-test trades to draw (entry / CL / TP lines, fill
// and exit markers).
function renderBlock(candles, chartElId, macdElId, visibleCount, opts = {}) {
  const chartEl = document.getElementById(chartElId);
  const macdEl = document.getElementById(macdElId);
  if (!candles || candles.length === 0) {
    chartEl.outerHTML = `<div class="chart-placeholder" id="${chartElId}">No chart data yet — waiting for the next scan</div>`;
    macdEl.style.display = 'none';
    return null;
  }

  const chart = LightweightCharts.createChart(chartEl, { ...chartTheme, width: chartEl.clientWidth, height: chartEl.clientHeight });
  activeCharts.push({ chart, el: chartEl });
  const candleSeries = chart.addCandlestickSeries({ upColor: cssVar('--gain'), downColor: cssVar('--loss'), borderVisible: false, wickUpColor: cssVar('--gain'), wickDownColor: cssVar('--loss'), priceLineVisible: false, lastValueVisible: false });
  candleSeries.setData(candles);
  // Custom formatter: the library's own mangles >8 decimals (sub-cent crypto).
  const precision = pricePrecision(candles[candles.length - 1].close, opts.forex);
  candleSeries.applyOptions({ priceFormat: { type: 'custom', minMove: Math.pow(10, -precision), formatter: p => p.toFixed(precision) } });

  INDICATORS.filter(i => i.key !== 'macd').forEach(i => {
    const s = chart.addLineSeries({ color: i.color(), lineWidth: i.width, lineStyle: i.style, priceLineVisible: false, lastValueVisible: false, visible: shown[i.key] });
    s.setData(candles.map(c => ({ time: c.time, value: c[i.key] })).filter(p => p.value != null));
    indicatorSeries.push({ key: i.key, series: s });
  });

  const from = Math.max(0, candles.length - visibleCount);
  const home = { from, to: candles.length - 1 + RIGHT_GAP };
  chart.timeScale().setVisibleLogicalRange(home);
  attachCtrlWheelZoom(chart, chartEl, candles.length - 1);
  const resetBtn = document.getElementById(`${chartElId}-reset`);
  if (resetBtn) resetBtn.addEventListener('click', () => {
    chart.priceScale('right').applyOptions({ autoScale: true });
    chart.timeScale().setVisibleLogicalRange(home);
  });

  const ink = cssVar('--ink');
  const inChart = new Set(candles.map(c => c.time));
  const allMarks = [];
  if (opts.triggers && ((opts.triggers.marks && opts.triggers.marks.length) || (opts.triggers.times && opts.triggers.times.length))) {
    // Every qualifying candle in the window, in neutral ink so it never
    // blends into a green or red candle; only the latest gets a label.
    // An SMA 50 watch adds its removal candle and its touch candle.
    const isBuy = opts.triggers.direction === 'BULLISH';
    // Every qualifying candle on the chart ([time, RSI, ±1] from scan.py),
    // labelled with its RSI. Older data without them: the window's triggers.
    const marks = opts.triggers.marks
      ? opts.triggers.marks.map(([t, rsi, d]) => ({
          time: t, position: d > 0 ? 'belowBar' : 'aboveBar', color: ink,
          shape: d > 0 ? 'arrowUp' : 'arrowDown', text: rsi.toFixed(1),
        }))
      : opts.triggers.times.map(toUnixTime).filter(t => t != null).map(t => ({
          time: t, position: isBuy ? 'belowBar' : 'aboveBar', color: ink, shape: isBuy ? 'arrowUp' : 'arrowDown',
        }));
    const s = opts.triggers.s50;
    if (s) {
      const side = isBuy ? 'aboveBar' : 'belowBar';
      marks.push({ time: toUnixTime(s.removed_at), position: side, color: cssVar('--ink-dim'), shape: 'square', size: 0.6, text: 'EMA20' });
      marks.push({ time: toUnixTime(s.touch_at), position: isBuy ? 'belowBar' : 'aboveBar', color: cssVar('--gold-bright'), shape: 'circle', text: 'SMA 50' });
    }
    allMarks.push(...marks);
  }
  if (opts.cycles && opts.cycles.events && opts.cycles.events.length) {
    // Basket pick on a lower tf: every step of every entry cycle. Pullback
    // and armed are small dots; entries are white (ink) arrows.
    const bull = opts.cycles.direction === 'BULLISH';
    const pbSide = bull ? 'belowBar' : 'aboveBar';
    const style = {
      pullback: { position: pbSide, color: cssVar('--gold-bright'), shape: 'circle', size: 0.5 },
      armed: { position: pbSide, color: cssVar('--violet'), shape: 'circle', size: 0.5 },
      entry: { position: bull ? 'belowBar' : 'aboveBar', color: ink, shape: bull ? 'arrowUp' : 'arrowDown' },
    };
    // Entries are labelled so they don't read as trigger arrows (which
    // carry an RSI): ENTRY, or for SMA 50 picks the line crossed (S / L).
    const label = (e) => e.type !== 'entry' ? '' : opts.cycles.s50 ? (e.line === 'sma50' ? 'S' : 'L') : 'ENTRY';
    allMarks.push(...opts.cycles.events
      .map(e => ({ time: toUnixTime(e.time), ...style[e.type], text: label(e) }))
      .filter(m => m.position));
  }
  (opts.trades || []).forEach(t => {
    tradeLines(candleSeries, t);
    // Markers sit on the candle containing the time (if it's on this chart).
    const at = (iso) => { const u = toUnixTime(iso); let hit = null; for (const c of candles) { if (c.time <= u) hit = c.time; else break; } return hit; };
    const long = tradeLong(t);
    if (t.filled_at && at(t.filled_at)) allMarks.push({ time: at(t.filled_at), position: long ? 'belowBar' : 'aboveBar', color: cssVar('--gold-bright'), shape: long ? 'arrowUp' : 'arrowDown', text: 'FILL' });
    if ((t.status === 'tp' || t.status === 'cl') && at(t.closed_at)) allMarks.push({ time: at(t.closed_at), position: (t.status === 'tp') === long ? 'aboveBar' : 'belowBar', color: cssVar(t.status === 'tp' ? '--gain' : '--loss'), shape: 'circle', text: t.status.toUpperCase() });
  });
  // One setMarkers call: a lower-tf chart can have both trigger arrows
  // (when it's also a higher tf) and a basket pick's entry cycle.
  if (allMarks.length) candleSeries.setMarkers(allMarks.filter(m => inChart.has(m.time)).sort((a, b) => a.time - b.time));

  const macdChart = LightweightCharts.createChart(macdEl, { ...chartTheme, ...followerInteraction, width: macdEl.clientWidth, height: macdEl.clientHeight, rightPriceScale: { borderVisible: false, textColor: 'transparent', minimumWidth: AXIS_WIDTH }, timeScale: { visible: false, rightOffset: RIGHT_GAP } });
  activeCharts.push({ chart: macdChart, el: macdEl });
  macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false })
    .setData(candles.filter(c => c.macd_hist != null).map(c => ({ time: c.time, value: c.macd_hist, color: c.macd_hist >= 0 ? 'rgba(79,169,114,0.55)' : 'rgba(168,64,44,0.55)' })));
  macdChart.addLineSeries({ color: '#D9A441', lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
    .setData(candles.map(c => ({ time: c.time, value: c.macd })).filter(p => p.value != null));
  macdChart.addLineSeries({ color: '#6FA8DC', lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
    .setData(candles.map(c => ({ time: c.time, value: c.macd_signal })).filter(p => p.value != null));
  macdChart.timeScale().setVisibleLogicalRange(home);
  chart.timeScale().subscribeVisibleLogicalRangeChange(range => { if (range) macdChart.timeScale().setVisibleLogicalRange(range); });
  return { chart, series: candleSeries, el: chartEl, precision };
}

// ---------------------------------------------------------------------------
// Forward-test trades (trades.json): planned entry / cut loss / target.
// Long when the cut loss is below the entry. Prices snap to the PSE tick
// table for PSE, else to the chart's decimals.
// ---------------------------------------------------------------------------
const tradeLong = (t) => t.entry > t.cl;
// Where `price` sits in R: 0 at entry, -1 at the cut loss, +ratio at the target.
const tradeR = (t, price) => (price - t.entry) / Math.abs(t.entry - t.cl) * (tradeLong(t) ? 1 : -1);
const fmtR = (r) => `${r >= 0 ? '+' : '−'}${Math.abs(r).toFixed(2)}R`;

const PSE_TICKS = [[0.01, 0.0001], [0.05, 0.001], [0.25, 0.001], [0.5, 0.005], [5, 0.01], [10, 0.01], [20, 0.02],
  [50, 0.05], [100, 0.05], [200, 0.1], [500, 0.2], [1000, 0.5], [2000, 1], [5000, 2], [Infinity, 5]];
const pseTick = (price) => PSE_TICKS.find(([below]) => price < below)[1];
function snapPrice(price, assetClass, precision) {
  if (assetClass === 'pse') { const tick = pseTick(price); return +(Math.round(price / tick) * tick).toFixed(4); }
  return +price.toFixed(precision);
}

const TRADE_LINE = { entry: ['ENTRY', '--gold-bright', 0], cl: ['CL', '--loss', 2], tp: ['TP', '--gain', 2] };
// Draws (or, given existing lines, moves) a trade's three lines on a series.
function tradeLines(series, t, lines) {
  return Object.fromEntries(Object.entries(TRADE_LINE).filter(([k]) => t[k] != null).map(([k, [title, color, style]]) => {
    if (lines && lines[k]) { lines[k].applyOptions({ price: t[k] }); return [k, lines[k]]; }
    return [k, series.createPriceLine({ price: t[k], color: cssVar(color), lineWidth: 1, lineStyle: style, axisLabelVisible: true, title })];
  }));
}

// ---------------------------------------------------------------------------
// GitHub — files in the repo, read via the API, written with a
// fine-grained token kept only in this browser.
// ---------------------------------------------------------------------------
const REPO = location.hostname.endsWith('.github.io')
  ? `${location.hostname.split('.')[0]}/${location.pathname.split('/')[1]}`
  : 'osdvillar22/ov-scanner';
const BASKET_API = `https://api.github.com/repos/${REPO}/contents/basket.json`;
const TOKEN_KEY = 'ovs-github-token';
const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } };
const setToken = (t) => { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch (e) {} };

const repoApi = (path) => `https://api.github.com/repos/${REPO}/contents/${path}`;
const TRADES_FILE = 'trades.json';

function ghHeaders() {
  const h = { Accept: 'application/vnd.github+json' };
  const t = getToken();
  if (t) h.Authorization = `Bearer ${t}`;
  return h;
}
const b64decode = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, '')), c => c.charCodeAt(0)));
const b64encode = (s) => btoa(String.fromCharCode(...new TextEncoder().encode(s)));

// A JSON file in the repo: { data, sha } (data = `empty` if it doesn't exist yet).
async function fetchRepoJson(path, empty) {
  const resp = await fetch(repoApi(path), { headers: ghHeaders(), cache: 'no-store' });
  if (resp.status === 404) return { data: empty, sha: null };
  if (!resp.ok) throw new Error(`GitHub ${resp.status}`);
  const file = await resp.json();
  return { data: JSON.parse(b64decode(file.content)), sha: file.sha };
}

// Read-modify-write against the file's current sha; a 409 means someone
// (a workflow) wrote in between — redo it on top. Returns the new data.
async function updateRepoJson(path, empty, mutate, message) {
  if (!getToken()) throw new Error('no token');
  for (let attempt = 0; attempt < 3; attempt++) {
    const { data, sha } = await fetchRepoJson(path, empty);
    const next = mutate(data);
    const body = { message, content: b64encode(JSON.stringify(next, null, 2) + '\n') };
    if (sha) body.sha = sha;
    const resp = await fetch(repoApi(path), { method: 'PUT', headers: { ...ghHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (resp.status === 409 || resp.status === 422) continue;
    if (resp.status === 401) throw new Error('token rejected or expired — check the token settings');
    if (resp.status === 403 || resp.status === 404) throw new Error("token can't write to this repo — it needs Contents: Read and write on ov-scanner");
    if (!resp.ok) throw new Error(`GitHub ${resp.status}`);
    return next;
  }
  throw new Error(`${path} changed repeatedly, try again`);
}

const fetchTrades = () => fetchRepoJson(TRADES_FILE, { trades: [] }).then(r => r.data.trades || []);
const updateTrades = (mutate, message) => updateRepoJson(TRADES_FILE, { trades: [] }, d => ({ trades: mutate((d.trades || []).slice()) }), message).then(d => d.trades);

function requireToken() {
  if (getToken()) return true;
  document.getElementById('basketSettings').classList.remove('is-hidden');
  document.getElementById('tokenMsg').textContent = 'Add your GitHub token here first to make changes.';
  window.scrollTo({ top: 0, behavior: 'smooth' });
  return false;
}

// "4:38 PM" today, "Sep 23 4:38 PM" otherwise.
function fmtTime(iso) {
  const d = parseTime(iso), today = new Date().toDateString() === d.toDateString();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return today ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

function wireBasketSettings() {
  const panel = document.getElementById('basketSettings'), input = document.getElementById('tokenInput'), msg = document.getElementById('tokenMsg');
  const showState = () => { msg.textContent = getToken() ? 'A token is saved in this browser.' : 'No token saved — the basket is read-only here.'; };
  document.getElementById('basketSettingsBtn').addEventListener('click', () => { panel.classList.toggle('is-hidden'); showState(); });
  document.getElementById('tokenClear').addEventListener('click', () => { setToken(''); input.value = ''; showState(); });
  document.getElementById('tokenSave').addEventListener('click', async () => {
    const t = input.value.trim();
    if (!t) { msg.textContent = 'Paste a token first.'; return; }
    msg.textContent = 'Checking…';
    // Confirms the token is valid; write access shows on the first save.
    const resp = await fetch('https://api.github.com/user', { headers: { Accept: 'application/vnd.github+json', Authorization: `Bearer ${t}` }, cache: 'no-store' });
    if (!resp.ok) { msg.textContent = `GitHub rejected that token (${resp.status}).`; return; }
    setToken(t); input.value = ''; msg.textContent = 'Saved. Add a pick to confirm it can write to the repo.';
  });
}

