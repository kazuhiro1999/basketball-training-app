'use strict';
// スマホ側: カメラ → VideoEncoder(H.264/VP8) → WebSocket(バイナリ) 送信
//
// チャンクのバイナリ形式 (little endian, 16バイトヘッダ + ペイロード)
//   0: u8   version (=1)
//   1: u8   flags   (bit0 = キーフレーム)
//   2: u16  予約
//   4: f64  timestamp (µs, 送信側の単調時計)
//  12: u32  duration  (µs, 0 可)
//  16:      エンコード済みデータ (H.264 は Annex B)

const $ = (s) => document.querySelector(s);
const els = {
  camera: $('#camera'), res: $('#res'), fps: $('#fps'), bitrate: $('#bitrate'), test: $('#test'),
  start: $('#start'), stop: $('#stop'), status: $('#status'), stats: $('#stats'),
  preview: $('#preview'), log: $('#log'),
};

const KEY_INTERVAL_SEC = 1;      // キーフレーム間隔。短いほど遅延変更/再接続の復帰が速い
const MAX_WS_BACKLOG = 4 << 20;  // 送信待ちがこれを超えたら間引く

let running = false;
let ws = null, wsOpen = false;
let stream = null, reader = null, testRaf = 0;
let encoder = null, encCfg = null, session = null;
let frameCount = 0, forceKey = true, skipUntilKey = false, reconfiguring = false;
let wakeLock = null;
let recOnPc = false;
const stats = { frames: 0, bytes: 0, dropped: 0, prevFrames: 0, prevBytes: 0 };

// ---------------------------------------------------------------- UI 補助

function logLine(msg) {
  const t = new Date().toLocaleTimeString('ja-JP');
  els.log.textContent = `${t} ${msg}\n` + els.log.textContent.slice(0, 4000);
}
function setStatus(msg, cls = '') { els.status.textContent = msg; els.status.className = cls; }

function savePrefs() {
  localStorage.setItem('delaycam.cam', JSON.stringify({
    camera: els.camera.value, res: els.res.value, fps: els.fps.value, bitrate: els.bitrate.value,
  }));
}
function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('delaycam.cam') || '{}');
    for (const k of ['res', 'fps', 'bitrate']) if (p[k]) els[k].value = p[k];
    if (p.camera) {
      els.camera.innerHTML += `<option value="${p.camera}">前回のカメラ</option>`;
      els.camera.value = p.camera;
    }
  } catch { /* ignore */ }
}

async function listCameras() {
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput');
  const cur = els.camera.value;
  els.camera.innerHTML = '<option value="">自動（背面）</option>' +
    devs.map((d, i) => `<option value="${d.deviceId}">${d.label || 'カメラ ' + (i + 1)}</option>`).join('');
  els.camera.value = cur;
  if (els.camera.value !== cur) els.camera.value = '';
}

// ---------------------------------------------------------------- 映像ソース

async function openStream(w, h, fps) {
  const video = { width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: fps } };
  if (els.camera.value) video.deviceId = { exact: els.camera.value };
  else video.facingMode = { ideal: 'environment' };
  return navigator.mediaDevices.getUserMedia({ audio: false, video });
}

// カメラ無しで配線を確認するためのテスト映像。経過秒・時刻・フレーム番号を描く
function makeTestStream(w, h, fps) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  const t0 = performance.now();
  let n = 0;
  const draw = () => {
    const t = (performance.now() - t0) / 1000;
    n++;
    ctx.fillStyle = '#12233a'; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = '#f80'; ctx.fillRect((t * w / 4) % w, 0, w * 0.02, h);           // 流れるバー
    const by = h * 0.85 - Math.abs(Math.sin(t * Math.PI)) * h * 0.5;               // 跳ねるボール
    ctx.fillStyle = '#e63'; ctx.beginPath(); ctx.arc(w * 0.15, by, h * 0.05, 0, 7); ctx.fill();
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
    ctx.font = `bold ${h * 0.2 | 0}px monospace`;
    ctx.fillText(t.toFixed(1) + ' s', w / 2, h * 0.45);
    const d = new Date();
    ctx.font = `${h * 0.09 | 0}px monospace`;
    ctx.fillText(d.toLocaleTimeString('ja-JP') + '.' + ((d.getMilliseconds() / 100) | 0), w / 2, h * 0.62);
    ctx.font = `${h * 0.06 | 0}px monospace`;
    ctx.fillText(`frame ${n}   TEST PATTERN ${w}x${h}`, w / 2, h * 0.75);
    testRaf = requestAnimationFrame(draw);
  };
  draw();
  return c.captureStream(fps);
}

// ---------------------------------------------------------------- エンコーダ

// 端末が対応する順に試す。H.264(Annex B) を優先、無ければ VP8/VP9
const CODEC_CANDIDATES = [
  { codec: 'avc1.42E02A', avc: { format: 'annexb' } },  // Constrained Baseline 4.2
  { codec: 'avc1.42002A', avc: { format: 'annexb' } },  // Baseline 4.2
  { codec: 'avc1.4D402A', avc: { format: 'annexb' } },  // Main 4.2
  { codec: 'avc1.64002A', avc: { format: 'annexb' } },  // High 4.2
  { codec: 'vp8' },
  { codec: 'vp09.00.10.08' },
];

async function pickEncoderConfig(width, height, framerate, bitrate) {
  for (const hardwareAcceleration of ['prefer-hardware', 'no-preference']) {
    for (const c of CODEC_CANDIDATES) {
      const cfg = { ...c, width, height, framerate, bitrate, latencyMode: 'realtime', hardwareAcceleration };
      try {
        const r = await VideoEncoder.isConfigSupported(cfg);
        if (r.supported) return cfg;
      } catch { /* 次の候補へ */ }
    }
  }
  throw new Error('この端末で使えるエンコーダがありません');
}

async function configureEncoder(w, h) {
  const fps = +els.fps.value;
  const bitrate = +els.bitrate.value * 1e6;
  encCfg = await pickEncoderConfig(w, h, fps, bitrate);
  if (encoder && encoder.state !== 'closed') encoder.close();
  encoder = new VideoEncoder({
    output: onChunk,
    error: (e) => { logLine('encoder error: ' + e.message); setStatus('エンコーダエラー', 'ng'); },
  });
  encoder.configure(encCfg);
  frameCount = 0; forceKey = true; skipUntilKey = false;
  const track = stream && stream.getVideoTracks()[0];
  const st = track ? track.getSettings() : {};
  session = {
    id: Math.random().toString(36).slice(2, 10),
    codec: encCfg.codec, width: w, height: h, fps, bitrate, test: els.test.checked,
    // 録画のメタデータ用 (解析時に端末・向き・実際のカメラ設定が分かるように)
    ua: navigator.userAgent,
    orientation: (screen.orientation && screen.orientation.type) || (innerHeight > innerWidth ? 'portrait' : 'landscape'),
    camera: track && !els.test.checked ? track.label : null,
    settings: { width: st.width, height: st.height, frameRate: st.frameRate, facingMode: st.facingMode, aspectRatio: st.aspectRatio },
  };
  sendConfig();
  logLine(`encoder: ${encCfg.codec} ${w}x${h}@${fps} ${bitrate / 1e6}Mbps (${encCfg.hardwareAcceleration})`);
}

function onChunk(chunk) {
  if (!wsOpen) return;
  if (ws.bufferedAmount > MAX_WS_BACKLOG) {          // 回線が詰まっている: 次のキーフレームまで捨てる
    stats.dropped++; skipUntilKey = true; forceKey = true; return;
  }
  if (skipUntilKey) { if (chunk.type !== 'key') { stats.dropped++; return; } skipUntilKey = false; }

  const buf = new ArrayBuffer(16 + chunk.byteLength);
  const dv = new DataView(buf);
  dv.setUint8(0, 1);
  dv.setUint8(1, chunk.type === 'key' ? 1 : 0);
  dv.setUint16(2, 0, true);
  dv.setFloat64(4, chunk.timestamp, true);
  dv.setUint32(12, chunk.duration || 0, true);
  chunk.copyTo(new Uint8Array(buf, 16));
  ws.send(buf);
  stats.frames++; stats.bytes += buf.byteLength;
}

function handleFrame(frame) {
  const w = frame.displayWidth, h = frame.displayHeight;
  if (reconfiguring) { frame.close(); return; }
  if (!encCfg || encCfg.width !== w || encCfg.height !== h) {   // 初回 or 端末の向きが変わった
    frame.close();
    reconfiguring = true;
    configureEncoder(w, h)
      .catch((e) => { setStatus('エンコーダ初期化失敗: ' + e.message, 'ng'); logLine(String(e)); stop(); })
      .finally(() => { reconfiguring = false; });
    return;
  }
  if (!wsOpen) { frame.close(); return; }
  if (encoder.encodeQueueSize > 2) { stats.dropped++; frame.close(); return; }  // エンコーダが追いつかない
  const keyEvery = KEY_INTERVAL_SEC * (session.fps || 30);
  const keyFrame = forceKey || frameCount % keyEvery === 0;
  forceKey = false;
  try { encoder.encode(frame, { keyFrame }); frameCount++; }
  catch (e) { logLine('encode: ' + e.message); }
  frame.close();
}

async function frameLoop(track) {
  if ('MediaStreamTrackProcessor' in window) {
    reader = new MediaStreamTrackProcessor({ track }).readable.getReader();
    while (running) {
      const { value, done } = await reader.read();
      if (done) break;
      handleFrame(value);
    }
  } else {  // 古い Chrome 向けフォールバック
    const v = els.preview;
    const step = () => {
      if (!running) return;
      try { handleFrame(new VideoFrame(v, { timestamp: performance.now() * 1000 })); } catch { /* ignore */ }
      v.requestVideoFrameCallback(step);
    };
    v.requestVideoFrameCallback(step);
  }
}

// ---------------------------------------------------------------- WebSocket

function wsUrl() {
  return `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/cam`;
}
function sendConfig() {
  if (wsOpen && session) ws.send(JSON.stringify({ type: 'config', session: session.id, ...session }));
}
function connectWs() {
  if (!running) return;
  ws = new WebSocket(wsUrl());
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { wsOpen = true; setStatus('送信中', 'ok'); logLine('接続'); sendConfig(); forceKey = true; };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;
    try {
      const m = JSON.parse(ev.data);
      if (m.type === 'status') { recOnPc = !!(m.recording && m.recording.active); if (running) setStatus(recOnPc ? '送信中 ● PC側で録画中' : '送信中', 'ok'); }
    } catch { /* ignore */ }
  };
  ws.onclose = (ev) => {
    wsOpen = false;
    if (!running) return;
    if (ev.code === 4000) {   // 別のカメラが接続してきた: 取り合いにならないよう自動再接続しない
      stop();
      setStatus('別のカメラに置き換えられました。再開するには「開始」を押してください', 'ng');
      return;
    }
    setStatus('再接続中…', 'ng'); logLine(`切断 (${ev.code}) → 1.5秒後に再接続`);
    setTimeout(connectWs, 1500);
  };
  ws.onerror = () => { /* onclose が続けて呼ばれる */ };
}

// ---------------------------------------------------------------- 開始 / 停止

async function requestWakeLock() {
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch { /* 非対応でも続行 */ }
}
document.addEventListener('visibilitychange', () => {
  if (running && document.visibilityState === 'visible' && !wakeLock) requestWakeLock();
});

// 開始時の向き(縦持ち/横持ち)で画面を固定する。途中で回ると映像の向きも変わってしまうため
async function lockOrientation() {
  if (navigator.maxTouchPoints === 0) return;   // PC では何もしない
  try { await document.documentElement.requestFullscreen(); } catch { /* ignore */ }
  try {
    const portrait = (screen.orientation?.type || '').startsWith('portrait') || innerHeight > innerWidth;
    await screen.orientation.lock(portrait ? 'portrait' : 'landscape');
    logLine(`画面の向きを固定: ${portrait ? '縦' : '横'}`);
  } catch { /* 非対応でも続行 */ }
}

async function start() {
  if (!('VideoEncoder' in window)) { setStatus('このブラウザは WebCodecs 非対応です。Chrome を使ってください', 'ng'); return; }
  if (!window.isSecureContext) { setStatus('https か chrome://flags の設定が必要です (secure context ではありません)', 'ng'); return; }
  savePrefs();
  running = true; els.start.disabled = true; els.stop.disabled = false;
  stats.frames = stats.bytes = stats.dropped = stats.prevFrames = stats.prevBytes = 0;
  try {
    const [w, h] = els.res.value.split('x').map(Number);
    const fps = +els.fps.value;
    stream = els.test.checked ? makeTestStream(w, h, fps) : await openStream(w, h, fps);
    els.preview.srcObject = stream;
    els.preview.play().catch(() => {});
    const track = stream.getVideoTracks()[0];
    const s = track.getSettings();
    logLine(`source: ${s.width}x${s.height}@${s.frameRate || fps}`);
    track.onended = () => { logLine('映像トラック終了'); if (running) stop(); };
    if (!els.test.checked) await listCameras();
    setStatus('接続中…');
    connectWs();
    requestWakeLock();
    lockOrientation();
    frameLoop(track);
  } catch (e) {
    setStatus('開始できません: ' + e.message, 'ng'); logLine(String(e)); stop();
  }
}

function stop() {
  running = false;
  els.start.disabled = false; els.stop.disabled = true;
  cancelAnimationFrame(testRaf);
  if (reader) { reader.cancel().catch(() => {}); reader = null; }
  if (encoder && encoder.state !== 'closed') encoder.close();
  encoder = null; encCfg = null; session = null;
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  els.preview.srcObject = null;
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  wsOpen = false;
  if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  setStatus('停止');
}

setInterval(() => {
  if (!running) return;
  const f = stats.frames - stats.prevFrames, b = stats.bytes - stats.prevBytes;
  stats.prevFrames = stats.frames; stats.prevBytes = stats.bytes;
  els.stats.textContent =
    `${f} fps  ${(b * 8 / 1e6).toFixed(2)} Mbps  送信 ${stats.frames}  間引き ${stats.dropped}` +
    `  待ち ${ws ? (ws.bufferedAmount / 1024 | 0) : 0} KB` +
    (encCfg ? `\n${encCfg.codec} ${encCfg.width}x${encCfg.height}` : '');
}, 1000);

els.start.onclick = start;
els.stop.onclick = stop;
loadPrefs();
if (navigator.mediaDevices?.enumerateDevices) listCameras().catch(() => {});
