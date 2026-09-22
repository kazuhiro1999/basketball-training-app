'use strict';
// PC側: WebSocket 受信 → エンコード済みチャンクをバッファ → 遅延後に VideoDecoder → canvas
//
// 用語
//   base : 各チャンクの「到着基準時刻」(ms, performance.now() の時間軸)。
//          送信側タイムスタンプ + オフセット。オフセットは「最も速く届いたフレーム」で決めるので、
//          base ≒ そのフレームが最短で届いたはずの時刻。ネットワークのジッタに影響されず、
//          送信側のフレーム間隔で滑らかに再生できる。
//   pos  : 再生位置 (同じ時間軸)。base <= pos になったチャンクを復号する。
//          通常は pos = now - 遅延秒 で進む。一時停止/スローは pos の進み方を変えるだけ。
// バッファには再生済みチャンクも一定時間残しているので、巻き戻し・コマ送りもできる。

const MIN_DELAY_MS = 300;        // これ以上ライブ側へは進めない
const MIN_TARGET = 1, MAX_TARGET = 60;   // ↑↓ で設定できる遅延秒数の範囲
const HISTORY_MS = 90_000;       // 再生済みチャンクを残す長さ (巻き戻し用)
const MAX_ITEMS = 30 * 60 * 12;  // 一時停止しっぱなし等でのメモリ上限 (約12分)
const OFFSET_WINDOW_MS = 60_000; // オフセット(最短伝送時間)を見直す周期。スマホとPCの時計のずれを吸収する

const params = new URLSearchParams(location.search);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const $ = (id) => document.getElementById(id);
const ui = {
  delay: $('delay'), status: $('status'), conn: $('conn'), bar: $('bar'),
  qr: $('qr'), qrimg: $('qrimg'), camurl: $('camurl'), help: $('help'), quit: $('quit'),
  playbtn: $('playbtn'), slowbtn: $('slowbtn'), mirrorbtn: $('mirrorbtn'), rotatebtn: $('rotatebtn'),
  rec: $('rec'), rectime: $('rectime'), recsize: $('recsize'), recbtn: $('recbtn'), reccard: $('reccard'),
  rectitle: $('rectitle'), recform: $('recform'), recstat: $('recstat'), recok: $('recok'), toast: $('toast'),
  posebtn: $('posebtn'), posecard: $('posecard'), poseStat: $('pose_stat'),
};

let targetDelay = clamp(parseFloat(params.get('delay')) || 15, 0.5, MAX_TARGET);
let buffer = [];          // {kind:'config'|'gap'|'chunk', base, ...}
let playIdx = 0;          // 次に処理する buffer の添字
let pos = null;
let rate = 1;             // 0 = 一時停止, 0.5 = スロー, 1 = 通常
let lastTick = performance.now();
let mirror = false, rotation = 0;
let qrForced = false, qrDismissed = false;
let quitting = false;

let decoder = null, curSession = null, needKey = true, suppressBeforeTs = -1, lastFrame = null;
let inSession = null;     // 受信中のセッション
let camConnected = false, camUrl = '', ws = null;
let recording = { active: false };   // サーバから届く録画状態
let analyzerStatus = null;           // 骨格推定プロセスの状態 (サーバ経由)
let lastFrameSession = null;         // 表示中フレームのセッション id (骨格の照合用)
const stats = { rxFrames: 0, rxBytes: 0, decoded: 0, prevRx: 0, prevRxBytes: 0, prevDecoded: 0, rxFps: 0, rxMbps: 0, decFps: 0 };

try {
  const p = JSON.parse(localStorage.getItem('delaycam.view') || '{}');
  mirror = !!p.mirror; rotation = [0, 90, 180, 270].includes(p.rotation) ? p.rotation : 0;
} catch { /* ignore */ }
function savePrefs() { try { localStorage.setItem('delaycam.view', JSON.stringify({ mirror, rotation })); } catch { /* ignore */ } }

// ---------------------------------------------------------------- 受信

function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/view`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { ui.conn.style.display = 'none'; buffer.push({ kind: 'gap', base: performance.now() }); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') onText(JSON.parse(ev.data));
    else onBinary(ev.data);
  };
  ws.onclose = () => {
    inSession = null;
    if (quitting) return;
    ui.conn.style.display = 'block';
    setTimeout(connect, 1000);
  };
}

function onText(m) {
  if (m.type === 'status') {
    const was = camConnected;
    camConnected = !!m.cam;
    if (m.cam_url) camUrl = m.cam_url;
    if (!camConnected) inSession = null;
    if (camConnected && !was) qrDismissed = false;   // 次に切れた時はまた出す
    if (m.recording) { recording = m.recording; updateRecUi(); }
    analyzerStatus = m.analyzer || null;
    updatePoseCard();
    updateOverlay();
  } else if (m.type === 'pose') {
    PoseOverlay.onMessage(m);
  } else if (m.type === 'toast') {
    toast(m.text);
  } else if (m.type === 'config') {
    if (inSession && inSession.id === m.session) {   // 同じセッションの再送 (viewer 再接続時)
      buffer.push({ kind: 'gap', base: performance.now() });
      return;
    }
    const sess = {
      id: m.session, codec: m.codec, width: m.width, height: m.height, fps: m.fps || 30,
      offset: null, winMin: Infinity, prevWinMin: Infinity, winStart: 0,
      decoderConfig: { codec: m.codec, codedWidth: m.width, codedHeight: m.height, optimizeForLatency: true, hardwareAcceleration: 'prefer-hardware' },
    };
    VideoDecoder.isConfigSupported(sess.decoderConfig)
      .then((r) => { if (!r.supported) sess.decoderConfig = { ...sess.decoderConfig, hardwareAcceleration: 'no-preference' }; })
      .catch(() => {});
    inSession = sess;
    buffer.push({ kind: 'config', base: performance.now(), sess });
  }
}

// 「到着時刻 − 送信側タイムスタンプ」の最小値をオフセットとする。
// 最初のフレームは起動直後で遅れがちなので、最小値が下がったら未再生分の base を引き直す。
function updateOffset(s, ts, recv) {
  const sample = recv - ts / 1000;
  if (recv - s.winStart > OFFSET_WINDOW_MS) { s.prevWinMin = s.winMin; s.winMin = Infinity; s.winStart = recv; }
  s.winMin = Math.min(s.winMin, sample);
  const off = Math.min(s.winMin, s.prevWinMin);
  if (s.offset === null || off > s.offset) { s.offset = off; return; }
  if (off < s.offset - 5) {
    s.offset = off;
    for (let i = buffer.length - 1; i >= playIdx; i--) {
      const it = buffer[i];
      if (it.sess !== s || it.kind !== 'chunk') continue;
      it.base = it.ts / 1000 + off;
    }
    for (let i = Math.max(playIdx, 1); i < buffer.length; i++) {   // 単調性を保つ
      if (buffer[i].base < buffer[i - 1].base) buffer[i].base = buffer[i - 1].base;
    }
  }
}

function onBinary(ab) {
  if (!inSession || ab.byteLength < 16) return;
  const dv = new DataView(ab);
  if (dv.getUint8(0) !== 1) return;
  const key = (dv.getUint8(1) & 1) === 1;
  const ts = dv.getFloat64(4, true);
  const recv = performance.now();
  const s = inSession;
  if (s.offset !== null && Math.abs(ts / 1000 + s.offset - recv) > 60_000) {   // 送信側の時計が飛んだ → 取り直し
    s.offset = null; s.winMin = s.prevWinMin = Infinity; s.winStart = recv;
  }
  updateOffset(s, ts, recv);
  let base = ts / 1000 + s.offset;
  if (buffer.length && base < buffer[buffer.length - 1].base) base = buffer[buffer.length - 1].base;
  buffer.push({ kind: 'chunk', base, ts, key, data: new Uint8Array(ab, 16), sess: s });
  stats.rxFrames++; stats.rxBytes += ab.byteLength;
}

// ---------------------------------------------------------------- 復号

function onFrame(frame) {
  if (suppressBeforeTs >= 0 && frame.timestamp < suppressBeforeTs) { frame.close(); return; }  // シーク中の中間フレーム
  if (lastFrame) lastFrame.close();
  lastFrame = frame;
  lastFrameSession = curSession ? curSession.id : null;
  stats.decoded++;
  drawFrame();
}

function setupDecoder(sess) {
  if (!decoder || decoder.state === 'closed') {
    decoder = new VideoDecoder({
      output: onFrame,
      error: (e) => { console.error('decoder:', e); curSession = null; needKey = true; },
    });
  } else if (decoder.state === 'configured') {
    try { decoder.reset(); } catch { /* ignore */ }
  }
  try { decoder.configure(sess.decoderConfig); curSession = sess; }
  catch (e) { console.error('configure:', e); curSession = null; }
  needKey = true; suppressBeforeTs = -1;
}

function decodeChunk(it) {
  if (it.sess !== curSession) setupDecoder(it.sess);
  if (!decoder || decoder.state !== 'configured') return;
  if (needKey) { if (!it.key) return; needKey = false; }
  if (decoder.decodeQueueSize > 60) { needKey = true; return; }   // PC が追いつかない → 次のキーフレームまで飛ばす
  try {
    decoder.decode(new EncodedVideoChunk({ type: it.key ? 'key' : 'delta', timestamp: it.ts, data: it.data }));
  } catch (e) { console.warn('decode:', e); needKey = true; }
}

function processItem(it) {
  if (it.kind === 'config') setupDecoder(it.sess);
  else if (it.kind === 'gap') needKey = true;
  else decodeChunk(it);
}

// ---------------------------------------------------------------- 再生ループ

function tick() {
  const now = performance.now();
  const dt = now - lastTick;
  lastTick = now;
  if (pos === null) pos = now - targetDelay * 1000;
  else if (rate > 0) pos += dt * rate;
  const liveEdge = now - MIN_DELAY_MS;
  if (pos > liveEdge) pos = liveEdge;

  let n = 0;
  while (playIdx < buffer.length && buffer[playIdx].base <= pos && n < 120) {
    processItem(buffer[playIdx]);
    playIdx++; n++;
  }
  trim(now);
  requestAnimationFrame(tick);
}

function trim(now) {
  const cutoff = now - HISTORY_MS;
  let k = 0;
  while (k < playIdx && buffer[k].base < cutoff) k++;
  if (buffer.length - k > MAX_ITEMS) k = buffer.length - MAX_ITEMS;   // 停止しっぱなし対策
  if (k > 0) {
    buffer.splice(0, k);
    playIdx = Math.max(0, playIdx - k);
  }
}

// base <= v を満たす最後の添字 (無ければ -1)
function lastIndexAtOrBefore(v) {
  let lo = 0, hi = buffer.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (buffer[mid].base <= v) lo = mid + 1; else hi = mid; }
  return lo - 1;
}

// 再生位置を newPos へ移動。直前のキーフレームから復号し直し、途中のフレームは描画しない
function seek(newPos) {
  const now = performance.now();
  const lo = buffer.length ? buffer[0].base : now;
  newPos = clamp(newPos, lo, now - MIN_DELAY_MS);
  const j = lastIndexAtOrBefore(newPos);
  if (j < 0) { pos = newPos; playIdx = 0; needKey = true; return; }
  let k = j;
  while (k >= 0 && !(buffer[k].kind === 'chunk' && buffer[k].key)) k--;
  if (k < 0) { pos = newPos; playIdx = 0; needKey = true; return; }

  const sess = buffer[k].sess;
  setupDecoder(sess);
  const target = buffer[j];
  suppressBeforeTs = (target.kind === 'chunk' && target.sess === sess) ? target.ts : -1;
  for (let i = k; i <= j; i++) {
    const it = buffer[i];
    if (it.kind === 'chunk' && it.sess === sess) decodeChunk(it);
  }
  playIdx = j + 1;
  pos = newPos;
}

function stepFrame(dir) {
  rate = 0;
  let i = playIdx - 1;
  while (i >= 0 && buffer[i].kind !== 'chunk') i--;
  if (i < 0) return;
  let t = i + dir;
  while (t >= 0 && t < buffer.length && buffer[t].kind !== 'chunk') t += dir;
  if (t < 0 || t >= buffer.length) return;
  seek(buffer[t].base);
}

// ---------------------------------------------------------------- 操作

function setTargetDelay(v) {
  targetDelay = clamp(v, MIN_TARGET, MAX_TARGET);
  rate = 1;
  seek(performance.now() - targetDelay * 1000);
}

const actions = {
  'delay+': () => setTargetDelay(Math.floor(targetDelay) + 1),
  'delay-': () => setTargetDelay(Math.ceil(targetDelay) - 1),
  pause: () => { rate = rate === 0 ? 1 : 0; },
  slow: () => { rate = rate === 0.5 ? 1 : 0.5; },
  live: () => { rate = 1; seek(performance.now() - targetDelay * 1000); },
  'step-': () => stepFrame(-1),
  'step+': () => stepFrame(+1),
  back: () => { seek(pos - 5000); },
  fwd: () => { seek(pos + 5000); },
  mirror: () => { mirror = !mirror; savePrefs(); drawFrame(); },
  rotate: () => { rotation = (rotation + 90) % 360; savePrefs(); drawFrame(); },
  qr: () => { if (!ui.qr.hidden) { qrForced = false; qrDismissed = true; } else qrForced = true; updateOverlay(); },
  help: () => { ui.help.hidden = !ui.help.hidden; },
  fs: () => { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {}); },
  quit: () => { ui.quit.hidden = false; },
  rec: () => { openRecCard(); },
  pose: () => { const on = PoseOverlay.toggle(); toast(on ? '骨格表示 ON' : '骨格表示 OFF', 1200); updatePoseCard(); drawFrame(); },
  posecard: () => { updatePoseCard(); ui.posecard.hidden = false; },
};

// 画面操作をサーバに送る (録画中は録画フォルダの events.jsonl、常に logs/ にも残る)。
// その時点で画面に出ていたフレームのタイムスタンプを添えるので、後から映像と突き合わせられる
function sendEvent(action, extra = {}) {
  if (!ws || ws.readyState !== 1) return;
  const now = performance.now();
  ws.send(JSON.stringify({
    type: 'event', action,
    display_ts_us: lastFrame ? lastFrame.timestamp : null,
    session: curSession ? curSession.id : null,
    delay_target: targetDelay,
    delay_effective: pos === null ? null : Math.round(now - pos) / 1000,
    rate,
    ...extra,
  }));
}
const LOGGED_ACTIONS = ['delay+', 'delay-', 'pause', 'slow', 'live', 'step-', 'step+', 'back', 'fwd', 'mirror', 'rotate', 'pose'];
function runAction(act, source) {
  if (!actions[act]) return;
  const before = { rate, targetDelay };
  actions[act]();
  if (LOGGED_ACTIONS.includes(act)) sendEvent(act, { source, rate_before: before.rate, delay_before: before.targetDelay });
}
const keymap = {
  ArrowUp: 'delay+', ArrowDown: 'delay-', '+': 'delay+', '-': 'delay-', ' ': 'pause',
  ArrowLeft: 'step-', ArrowRight: 'step+', PageUp: 'back', PageDown: 'fwd',
  s: 'slow', l: 'live', m: 'mirror', r: 'rotate', q: 'qr', h: 'help', f: 'fs', p: 'pose',
};

function anyOverlayOpen() { return !ui.qr.hidden || !ui.help.hidden || !ui.quit.hidden || !ui.reccard.hidden || !ui.posecard.hidden; }
function closeOverlays() {
  if (!ui.qr.hidden) { qrForced = false; qrDismissed = true; }
  ui.help.hidden = true; ui.quit.hidden = true; ui.reccard.hidden = true; ui.posecard.hidden = true;
  updateOverlay();
}

async function doQuit() {
  quitting = true;
  ui.quit.querySelector('h2').textContent = '終了しています…';
  try { await fetch('/quit', { method: 'POST' }); } catch { /* サーバが既に落ちていても続行 */ }
  try { ws && ws.close(); } catch { /* ignore */ }
  setTimeout(() => {
    window.close();   // kiosk ならサーバ側が閉じる。普通のタブで閉じられなかった場合の案内
    ui.quit.querySelector('h2').textContent = '終了しました。このウィンドウを閉じてください';
  }, 300);
}

window.addEventListener('keydown', (e) => {
  if (quitting) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    if (anyOverlayOpen()) closeOverlays();     // まず開いているカードを閉じる
    else if (document.fullscreenElement) { /* F で入った全画面はブラウザが抜ける */ }
    else ui.quit.hidden = false;              // 何も開いていなければ終了確認
    return;
  }
  if (!ui.quit.hidden) { if (e.key === 'Enter') { e.preventDefault(); doQuit(); } return; }
  if (!ui.reccard.hidden) return;   // 入力フォーム中はショートカットを無効に
  const act = keymap[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (!act) return;
  e.preventDefault();
  runAction(act, 'key');
  updateHud();
});
ui.bar.addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b && actions[b.dataset.act]) { runAction(b.dataset.act, 'button'); updateHud(); }
});
$('quitok').addEventListener('click', doQuit);

// ---------------------------------------------------------------- 録画

const SETUP_KEY = 'delaycam.setup';
function readSetup() {
  const f = new FormData(ui.recform);
  const num = (v) => (v === '' || v === null ? null : Number(v));
  return { view: f.get('view'), height_m: num(f.get('height_m')), distance_m: num(f.get('distance_m')), drill: f.get('drill'), note: f.get('note') || '' };
}
function loadSetup() {
  try {
    const p = JSON.parse(localStorage.getItem(SETUP_KEY) || 'null');
    if (!p) return;
    for (const [k, v] of Object.entries(p)) { const el = ui.recform.elements[k]; if (el && v !== null && v !== undefined) el.value = v; }
  } catch { /* ignore */ }
}
function recDuration() {
  if (!recording.active || !recording.started_unix_ms) return '00:00:00';
  const s = Math.max(0, Math.floor((Date.now() - recording.started_unix_ms) / 1000));
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}
function openRecCard() {
  const on = recording.active;
  ui.rectitle.textContent = on ? '録画を停止しますか？' : '録画を開始';
  ui.recform.style.display = on ? 'none' : '';
  ui.recok.textContent = on ? '■ 録画停止' : '● 録画開始';
  ui.recstat.textContent = on
    ? `${recDuration()}  ${(recording.bytes / 1e9).toFixed(2)} GB  ${recording.frames} フレーム  →  ${recording.dir}`
    : `保存先: ${recording.rec_dir || '(サーバ既定)'}   空き ${recording.free_gb ?? '?'} GB`;
  $('preroll').textContent = recording.preroll_sec ?? 30;
  ui.reccard.hidden = false;
  if (!on) ui.recform.elements.view.focus();
}
function updateRecUi() {
  ui.rec.style.display = recording.active ? 'block' : 'none';
  ui.rectime.textContent = recDuration();
  ui.recsize.textContent = recording.active ? `${(recording.bytes / 1e9).toFixed(2)}GB` : '';
  ui.recbtn.classList.toggle('on', recording.active);
  ui.recbtn.textContent = recording.active ? '■ REC 停止' : '● REC';
}
ui.recok.addEventListener('click', () => {
  if (!ws || ws.readyState !== 1) { toast('サーバに接続していません'); return; }
  if (recording.active) {
    ws.send(JSON.stringify({ type: 'rec', action: 'stop' }));
  } else {
    const setup = readSetup();
    try { localStorage.setItem(SETUP_KEY, JSON.stringify(setup)); } catch { /* ignore */ }
    ws.send(JSON.stringify({
      type: 'rec', action: 'start', setup, delay: targetDelay,
      viewer: { ua: navigator.userAgent, screen: `${screen.width}x${screen.height}`, dpr: devicePixelRatio, mirror, rotation },
    }));
  }
  ui.reccard.hidden = true;
});
loadSetup();

// ---------------------------------------------------------------- 骨格表示の設定カード

const poseUi = {
  enabled: $('pose_enabled'), interp: $('pose_interp'), id: $('pose_id'), box: $('pose_box'), lw: $('pose_lw'),
  stride: $('pose_stride'), preset: $('pose_preset'),
};
poseUi.enabled.addEventListener('change', () => { PoseOverlay.set('enabled', poseUi.enabled.checked); updatePoseCard(); drawFrame(); });
poseUi.interp.addEventListener('change', () => PoseOverlay.set('interpolate', poseUi.interp.checked));
poseUi.id.addEventListener('change', () => { PoseOverlay.set('showId', poseUi.id.checked); drawFrame(); });
poseUi.box.addEventListener('change', () => { PoseOverlay.set('showBox', poseUi.box.checked); drawFrame(); });
poseUi.lw.addEventListener('input', () => { PoseOverlay.set('lineWidth', +poseUi.lw.value); drawFrame(); });
poseUi.stride.addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  sendAnalyzerCmd({ stride: +b.dataset.stride });
});
poseUi.preset.addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  sendAnalyzerCmd({ preset: b.dataset.preset });
  toast('モデルを切り替えています…', 2500);
});
function sendAnalyzerCmd(cmd) {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: 'analyzer_cmd', ...cmd }));
}
function updatePoseCard() {
  const st = PoseOverlay.settings;
  ui.posebtn.classList.toggle('on', st.enabled);
  poseUi.enabled.checked = st.enabled; poseUi.interp.checked = st.interpolate;
  poseUi.id.checked = st.showId; poseUi.box.checked = st.showBox; poseUi.lw.value = st.lineWidth;
  const a = analyzerStatus;
  for (const b of poseUi.stride.querySelectorAll('button')) {
    b.classList.toggle('on', !!a && ((a.auto_stride && +b.dataset.stride === 0) || (!a.auto_stride && +b.dataset.stride === a.stride)));
  }
  for (const b of poseUi.preset.querySelectorAll('button')) b.classList.toggle('on', !!a && b.dataset.preset === a.preset);
  if (!a) { ui.poseStat.textContent = 'アナライザ未接続 (analyzer/start_live.bat で起動)'; return; }
  const ps = PoseOverlay.status();
  ui.poseStat.textContent =
    `${a.backend}  推論 ${a.infer_fps ?? '?'}fps (${a.infer_ms ?? '?'}ms/回)  実効間隔 ${a.stride}${a.auto_stride ? ' (自動)' : ''}  ` +
    `待ち ${a.backlog ?? 0}フレーム  人物 ${a.persons ?? '-'}  受信 ${ps.received} 件`;
}

let toastTimer = 0;
function toast(text, ms = 3000) {
  ui.toast.textContent = text; ui.toast.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { ui.toast.style.display = 'none'; }, ms);
}
// カードの × ボタン / キャンセル、またはカードの外側クリックで閉じる
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => {
  if (b.dataset.close === 'qr') { qrForced = false; qrDismissed = true; updateOverlay(); }
  else ui[b.dataset.close].hidden = true;
}));
document.querySelectorAll('.overlay').forEach((o) => o.addEventListener('click', (e) => {
  if (e.target !== o) return;
  if (o === ui.qr) { qrForced = false; qrDismissed = true; updateOverlay(); }
  else o.hidden = true;
}));

// マウスを動かした時だけ操作バーとカーソルを出す
let hideTimer = 0;
window.addEventListener('mousemove', () => {
  ui.bar.classList.remove('hidden'); document.body.classList.remove('nocursor');
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    if (ui.bar.matches(':hover')) return;
    ui.bar.classList.add('hidden'); document.body.classList.add('nocursor');
  }, 3000);
});

// ---------------------------------------------------------------- 描画 / HUD

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(innerWidth * dpr);
  canvas.height = Math.round(innerHeight * dpr);
  drawFrame();
}
window.addEventListener('resize', resize);

function drawFrame() {
  if (!lastFrame) return;
  const cw = canvas.width, ch = canvas.height;
  const fw = lastFrame.displayWidth, fh = lastFrame.displayHeight;
  const swap = rotation % 180 !== 0;                 // 90/270° なら縦横が入れ替わる
  const bw = swap ? fh : fw, bh = swap ? fw : fh;
  const s = Math.min(cw / bw, ch / bh);
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cw, ch);
  ctx.save();
  ctx.translate(cw / 2, ch / 2);
  if (mirror) ctx.scale(-1, 1);                      // 画面上での左右反転 (回転より外側に掛ける)
  ctx.rotate(rotation * Math.PI / 180);
  ctx.drawImage(lastFrame, -fw * s / 2, -fh * s / 2, fw * s, fh * s);
  // 骨格: フレームのピクセル座標で描けるように同じ変換の上に載せる (鏡・回転もそのまま効く)
  ctx.translate(-fw * s / 2, -fh * s / 2);
  ctx.scale(s, s);
  PoseOverlay.draw(ctx, lastFrameSession, lastFrame.timestamp, s);
  ctx.restore();
}

function bufferedAhead() {
  const last = buffer.length ? buffer[buffer.length - 1] : null;
  return last && pos !== null ? Math.max(0, (last.base - pos) / 1000) : 0;
}

function updateOverlay() {
  // カメラが切れても、バッファに残っている映像を流し終えるまではQRを出さない
  const show = qrForced || (!camConnected && !qrDismissed && bufferedAhead() <= 0.2);
  if (show && camUrl) {
    ui.camurl.textContent = camUrl;
    if (ui.qrimg.dataset.url !== camUrl) { ui.qrimg.src = '/qr.svg?' + Date.now(); ui.qrimg.dataset.url = camUrl; }
  }
  ui.qr.hidden = !show;
}

function updateHud() {
  const now = performance.now();
  const eff = pos === null ? targetDelay : (now - pos) / 1000;
  ui.delay.innerHTML = `${eff.toFixed(1)}<small>s</small>` +
    (rate === 0 ? '<small>⏸</small>' : rate === 0.5 ? '<small>0.5×</small>' : '');
  ui.delay.className = rate === 0 ? 'paused' : rate === 0.5 ? 'slow' : '';

  ui.playbtn.textContent = rate === 0 ? '▶' : '⏸';
  ui.playbtn.title = rate === 0 ? '再生 (Space)' : '一時停止 (Space)';
  ui.playbtn.classList.toggle('paused', rate === 0);
  ui.slowbtn.classList.toggle('on', rate === 0.5);
  ui.mirrorbtn.classList.toggle('on', mirror);
  ui.rotatebtn.classList.toggle('on', rotation !== 0);
  ui.rotatebtn.textContent = rotation ? `回転${rotation}°` : '回転';

  const hist = buffer.length && pos !== null ? Math.max(0, (pos - buffer[0].base) / 1000) : 0;
  const sess = curSession || inSession;
  ui.status.textContent =
    `${camConnected ? '● カメラ接続中' : '○ カメラ未接続'}  受信 ${stats.rxFps}fps ${stats.rxMbps.toFixed(2)}Mbps  表示 ${stats.decFps}fps\n` +
    `${sess ? `${sess.width}×${sess.height} ${sess.codec}` : ''}  設定遅延 ${targetDelay}s  先読み ${bufferedAhead().toFixed(1)}s  巻戻し可 ${hist.toFixed(0)}s` +
    `${mirror ? '  鏡' : ''}${rotation ? `  回転${rotation}°` : ''}\n` +
    (analyzerStatus
      ? `骨格 ${PoseOverlay.settings.enabled ? 'ON' : 'OFF'}  ${analyzerStatus.backend} ${analyzerStatus.infer_fps ?? '?'}fps 間隔${analyzerStatus.stride}${analyzerStatus.auto_stride ? '(自動)' : ''}  待ち${analyzerStatus.backlog ?? 0}`
      : `骨格 ${PoseOverlay.settings.enabled ? 'ON' : 'OFF'}  (アナライザ未接続)`);
}

setInterval(() => {
  stats.rxFps = stats.rxFrames - stats.prevRx; stats.prevRx = stats.rxFrames;
  stats.rxMbps = (stats.rxBytes - stats.prevRxBytes) * 8 / 1e6; stats.prevRxBytes = stats.rxBytes;
  stats.decFps = stats.decoded - stats.prevDecoded; stats.prevDecoded = stats.decoded;
  updateHud();
  updateOverlay();
  updateRecUi();
  if (!ui.posecard.hidden) updatePoseCard();
  if (++statsTick % 5 === 0) sendEvent('viewer_stats', { rx_fps: stats.rxFps, dec_fps: stats.decFps, buffered_ahead: bufferedAhead() });
}, 1000);
let statsTick = 0;

// ---------------------------------------------------------------- 起動

if (!('VideoDecoder' in window)) {
  document.body.innerHTML = '<div style="padding:4vh;font-size:4vh">このブラウザは WebCodecs 非対応です。Chrome または Edge で開いてください。</div>';
} else {
  resize();
  fetch('/info.json').then((r) => r.json()).then((i) => { camUrl = i.cam_url; camConnected = i.cam_connected; if (i.recording) { recording = i.recording; updateRecUi(); } analyzerStatus = i.analyzer || null; updatePoseCard(); updateOverlay(); }).catch(() => {});
  connect();
  updateHud();
  requestAnimationFrame(tick);
}
