'use strict';
// インスタントリプレイ (view.js から疎結合で呼ばれる)。
//
// ボタンを押した時点で、遅延再生のバッファにある「直近 N 秒」のエンコード済みチャンクを切り出し
// (カメラの今に対しての N 秒なので、遅延表示にまだ出ていない最新部分も含む)、
// 専用の VideoDecoder でモーダル内に再生する。サーバ・録画・遅延再生のバッファには手を触れない。
//
// view.js からの呼び出し:
//   InstantReplay.open(clip, { render, onClose })   clip = { items:[{ts, key, data}], decoderConfig, sessionId, capturedAt }
//   InstantReplay.isOpen() / InstantReplay.onKey(e) / InstantReplay.close()
//   render(ctx, w, h, frame, sessionId) は遅延表示と同じ描き方 (鏡・回転・骨格) をする関数

const InstantReplay = (() => {
  const $ = (id) => document.getElementById(id);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const SPEEDS = [1, 0.5, 0.25];
  const JUMP_MS = 2000;

  let ui = null;
  let clip = null;          // { items, decoderConfig, sessionId, t0, dur, capturedAt }
  let opts = {};
  let decoder = null;
  let frame = null;         // 表示中の VideoFrame
  let idx = 0;              // 次に復号する items の添字
  let cur = 0;              // 最後に表示を要求した items の添字 (コマ送りの基準)
  let pos = 0;              // 再生位置 (ms, クリップ先頭から)
  let rate = 1, playing = false, loop = true;
  let suppressBefore = -1;  // シーク中の中間フレームは表示しない
  let raf = 0, lastT = 0;
  let dragging = false, wasPlaying = false, pendingSeek = null;

  try { loop = JSON.parse(localStorage.getItem('delaycam.replay.loop') ?? 'true'); } catch { /* ignore */ }

  function init() {
    if (ui) return;
    ui = {
      root: $('replay'), canvas: $('rp_canvas'), seek: $('rp_seek'), time: $('rp_time'), info: $('rp_info'),
      play: $('rp_play'), speed: $('rp_speed'), loop: $('rp_loop'), pose: $('rp_pose'),
    };
    ui.ctx = ui.canvas.getContext('2d');
    ui.root.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-rp]');
      if (b) { act(b.dataset.rp, b.dataset.v); return; }
      if (e.target === ui.canvas) act('toggle');
    });
    // シークバー: ドラッグ中は止めて、離したら元の状態に戻す
    ui.seek.addEventListener('pointerdown', () => { dragging = true; wasPlaying = playing; playing = false; });
    ui.seek.addEventListener('input', () => { pendingSeek = +ui.seek.value; });
    const endDrag = () => { if (!dragging) return; dragging = false; if (pendingSeek !== null) { seekTo(pendingSeek); pendingSeek = null; } playing = wasPlaying; updateUi(); };
    ui.seek.addEventListener('pointerup', endDrag);
    ui.seek.addEventListener('change', endDrag);
    window.addEventListener('resize', () => { if (isOpen()) { resize(); draw(); } });
  }

  // ---------------------------------------------------------------- 開閉

  function open(c, o) {
    init();
    if (!c || !c.items.length) return false;
    clip = { ...c, t0: c.items[0].ts, dur: (c.items[c.items.length - 1].ts - c.items[0].ts) / 1000 };
    opts = o || {};
    ui.root.hidden = false;
    resize();
    ui.seek.max = Math.max(1, Math.round(clip.dur));
    const when = new Date(clip.capturedAt).toLocaleTimeString('ja-JP');
    ui.info.textContent = `直近 ${(clip.dur / 1000).toFixed(1)} 秒（${when} に押した時点まで）`;
    newDecoder();
    playing = true;
    seekTo(0);
    lastT = performance.now();
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(tick);
    updateUi();
    return true;
  }

  function close() {
    if (!clip) return;
    cancelAnimationFrame(raf);
    if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch { /* ignore */ } }
    decoder = null;
    if (frame) { frame.close(); frame = null; }
    clip = null;
    ui.root.hidden = true;
    const cb = opts.onClose;
    opts = {};
    if (cb) cb();
  }

  function isOpen() { return !!clip; }

  // ---------------------------------------------------------------- 復号

  function newDecoder() {
    if (decoder && decoder.state !== 'closed') { try { decoder.close(); } catch { /* ignore */ } }
    decoder = new VideoDecoder({
      output: (f) => {
        if (!clip || (suppressBefore >= 0 && f.timestamp < suppressBefore)) { f.close(); return; }
        if (frame) frame.close();
        frame = f;
        draw();
      },
      error: (e) => console.error('replay decoder:', e),
    });
    decoder.configure(clip.decoderConfig);
  }

  function decodeAt(i) {
    const it = clip.items[i];
    try {
      decoder.decode(new EncodedVideoChunk({ type: it.key ? 'key' : 'delta', timestamp: it.ts, data: it.data }));
    } catch (e) { console.warn('replay decode:', e); }
  }

  const tAt = (i) => (clip.items[i].ts - clip.t0) / 1000;

  // t (ms) 以下で最後の添字
  function indexAt(t) {
    let lo = 0, hi = clip.items.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (tAt(m) <= t) lo = m + 1; else hi = m; }
    return Math.max(0, lo - 1);
  }

  // 位置 t へ。直前のキーフレームから復号し直し、途中のフレームは出さない
  function seekTo(t) {
    t = clamp(t, 0, clip.dur);
    const j = indexAt(t);
    let k = j;
    while (k > 0 && !clip.items[k].key) k--;
    try { decoder.reset(); } catch { /* ignore */ }
    if (decoder.state === 'closed') newDecoder(); else decoder.configure(clip.decoderConfig);
    suppressBefore = clip.items[j].ts;
    for (let i = k; i <= j; i++) decodeAt(i);
    idx = j + 1;
    cur = j;
    pos = t;
    updateUi();
  }

  function step(dir) {
    playing = false;
    const t = clamp(cur + dir, 0, clip.items.length - 1);
    if (dir === 1 && t === idx) {          // 1 コマ進むだけなら続きを 1 枚復号すればよい
      decodeAt(idx); idx++; cur = t; pos = tAt(t);
    } else {
      seekTo(tAt(t));
    }
    updateUi();
  }

  // ---------------------------------------------------------------- 再生ループ

  function tick(now) {
    if (!clip) return;
    const dt = now - lastT;
    lastT = now;
    if (dragging && pendingSeek !== null) { seekTo(pendingSeek); pendingSeek = null; }
    if (playing) {
      pos += dt * rate;
      if (pos > clip.dur) {
        if (loop) seekTo(0);
        else { pos = clip.dur; playing = false; }
      }
      while (idx < clip.items.length && tAt(idx) <= pos) {
        if (decoder.decodeQueueSize > 20) break;   // 復号が追いつかない時は次の tick で
        decodeAt(idx); cur = idx; idx++;
      }
    }
    updateTime();
    raf = requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------- 描画 / UI

  function resize() {
    const r = ui.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    ui.canvas.width = Math.max(1, Math.round(r.width * dpr));
    ui.canvas.height = Math.max(1, Math.round(r.height * dpr));
  }

  function draw() {
    if (!frame || !clip) return;
    if (opts.render) opts.render(ui.ctx, ui.canvas.width, ui.canvas.height, frame, clip.sessionId);
  }

  const fmt = (ms) => (ms / 1000).toFixed(1);

  function updateTime() {
    if (!dragging) ui.seek.value = Math.round(pos);
    const before = clip.dur - pos;
    ui.time.textContent = `${fmt(pos)} / ${fmt(clip.dur)} 秒　押した時点の ${fmt(before)} 秒前`;
  }

  function updateUi() {
    if (!clip) return;
    ui.play.textContent = playing ? '⏸' : '▶';
    ui.play.classList.toggle('paused', !playing);
    for (const b of ui.speed.querySelectorAll('button')) b.classList.toggle('on', +b.dataset.v === rate);
    ui.loop.classList.toggle('on', loop);
    if (ui.pose && typeof PoseOverlay !== 'undefined') ui.pose.classList.toggle('on', PoseOverlay.settings.enabled);
    updateTime();
  }

  function act(name, v) {
    if (!clip) return;
    switch (name) {
      case 'toggle':
        if (!playing && pos >= clip.dur - 1) seekTo(0);   // 最後で止まっていたら頭から
        playing = !playing; break;
      case 'start': seekTo(0); break;
      case 'back': seekTo(pos - JUMP_MS); break;
      case 'fwd': seekTo(pos + JUMP_MS); break;
      case 'step-': step(-1); break;
      case 'step+': step(1); break;
      case 'speed': rate = +v; break;
      case 'speedcycle': rate = SPEEDS[(SPEEDS.indexOf(rate) + 1) % SPEEDS.length]; break;
      case 'loop':
        loop = !loop;
        try { localStorage.setItem('delaycam.replay.loop', JSON.stringify(loop)); } catch { /* ignore */ }
        break;
      case 'pose':
        if (typeof PoseOverlay !== 'undefined') { PoseOverlay.toggle(); draw(); }
        break;
      case 'close': close(); return;
    }
    if (opts.onAction) opts.onAction(name);
    updateUi();
  }

  const KEYS = {
    ' ': 'toggle', ArrowLeft: 'step-', ArrowRight: 'step+', PageUp: 'back', PageDown: 'fwd',
    Home: 'start', s: 'speedcycle', o: 'loop', p: 'pose', Escape: 'close', i: 'close',
  };

  // リプレイ表示中のキー操作。処理したら true
  function onKey(e) {
    if (!clip) return false;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    let name = KEYS[k];
    if (e.shiftKey && k === 'ArrowLeft') name = 'back';
    if (e.shiftKey && k === 'ArrowRight') name = 'fwd';
    e.preventDefault();                  // リプレイ中は遅延再生側のショートカットを無効に
    if (name) act(name);
    return true;
  }

  return { open, close, isOpen, onKey };
})();
