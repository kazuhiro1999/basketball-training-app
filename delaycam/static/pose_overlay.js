'use strict';
// 骨格オーバーレイ (view.js から疎結合で呼ばれる)。
//
// サーバ経由でアナライザから届く {type:'pose', session, ts_us, persons:[{id, bbox, score, kp:[[x,y,c]x17]}]} を
// セッションごとに ts 順で溜め、表示中フレームの ts に合わせて描く。
// アナライザは N フレームに 1 回しか推論しないので、前後の結果を ID ごとに線形補間して間を埋める。
// 何も届いていなければ何も描かない (= アナライザ無しでも表示側は今まで通り動く)。

const PoseOverlay = (() => {
  const SKELETON = [
    [0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10],
    [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
  ];
  const PALETTE = ['#ff5050', '#50c8ff', '#50ff78', '#ffc83c', '#dc64ff', '#ff8c00', '#00dcdc', '#b4b4ff', '#ffff78', '#78ffc8'];
  const HOLD_US = 250_000;      // 次の結果が無いとき、直前の結果をこれだけの間は流用する
  const MAX_GAP_US = 600_000;   // これ以上離れた 2 結果の間は補間しない (アナライザが詰まった区間)
  const KEEP_US = 120_000_000;  // 溜めておく長さ

  const settings = { enabled: true, interpolate: true, showId: true, showBox: false, lineWidth: 3, kpThr: 0.3 };
  try { Object.assign(settings, JSON.parse(localStorage.getItem('delaycam.pose') || '{}')); } catch { /* ignore */ }
  const save = () => { try { localStorage.setItem('delaycam.pose', JSON.stringify(settings)); } catch { /* ignore */ } };

  const store = new Map();     // session id → [{ts_us, persons}] (ts 昇順)
  const stats = { received: 0, lastTs: 0, lastRecvAt: 0 };

  function onMessage(m) {
    if (m.type !== 'pose' || !m.session) return;
    let arr = store.get(m.session);
    if (!arr) { arr = []; store.set(m.session, arr); }
    const rec = { ts_us: m.ts_us, persons: m.persons || [] };
    if (arr.length && arr[arr.length - 1].ts_us >= m.ts_us) {   // 順序が乱れたら挿入
      let i = arr.length;
      while (i > 0 && arr[i - 1].ts_us > m.ts_us) i--;
      if (i > 0 && arr[i - 1].ts_us === m.ts_us) arr[i - 1] = rec; else arr.splice(i, 0, rec);
    } else arr.push(rec);
    stats.received++; stats.lastTs = m.ts_us; stats.lastRecvAt = performance.now();
    // 古いものを捨てる
    const cutoff = m.ts_us - KEEP_US;
    let k = 0;
    while (k < arr.length && arr[k].ts_us < cutoff) k++;
    if (k > 0) arr.splice(0, k);
    if (store.size > 4) { const first = store.keys().next().value; if (first !== m.session) store.delete(first); }
  }

  function lastIndexAtOrBefore(arr, ts) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid].ts_us <= ts) lo = mid + 1; else hi = mid; }
    return lo - 1;
  }

  // 表示フレーム (session, ts) に対応する人物リスト。補間結果は新しい配列で返す
  function personsAt(session, ts) {
    const arr = store.get(session);
    if (!arr || !arr.length) return null;
    const i = lastIndexAtOrBefore(arr, ts);
    const a = i >= 0 ? arr[i] : null, b = i + 1 < arr.length ? arr[i + 1] : null;
    if (!a) return b && b.ts_us - ts <= HOLD_US ? b.persons : null;
    if (a.ts_us === ts) return a.persons;
    if (!b || b.ts_us - a.ts_us > MAX_GAP_US) return ts - a.ts_us <= HOLD_US ? a.persons : null;
    if (!settings.interpolate) return (ts - a.ts_us <= b.ts_us - ts) ? a.persons : b.persons;
    const f = (ts - a.ts_us) / (b.ts_us - a.ts_us);
    const byId = new Map();
    for (const p of b.persons) if (p.id != null) byId.set(p.id, p);
    const out = [];
    const used = new Set();
    for (const p of a.persons) {
      const q = p.id != null ? byId.get(p.id) : null;
      if (!q) { if (f < 0.5) out.push(p); continue; }
      used.add(p.id);
      out.push({
        id: p.id, score: p.score,
        bbox: p.bbox.map((v, k) => v + (q.bbox[k] - v) * f),
        kp: p.kp.map((k, j) => {
          const l = q.kp[j];
          if (k[2] < settings.kpThr) return l;            // 片側でしか見えない関節は見えている方
          if (l[2] < settings.kpThr) return k;
          return [k[0] + (l[0] - k[0]) * f, k[1] + (l[1] - k[1]) * f, Math.min(k[2], l[2])];
        }),
      });
    }
    if (f >= 0.5) for (const q of b.persons) if (q.id == null || !used.has(q.id)) out.push(q);
    return out;
  }

  // ctx は「フレームを (0,0)-(fw,fh) のピクセル座標で描ける」変換が掛かった状態で渡される
  function draw(ctx, session, ts, scale) {
    if (!settings.enabled) return false;
    const persons = personsAt(session, ts);
    if (!persons || !persons.length) return false;
    const lw = settings.lineWidth / Math.max(scale, 0.01);   // 画面上で一定の太さになるように
    ctx.lineCap = 'round';
    for (const p of persons) {
      const col = p.id == null ? '#a0a0a0' : PALETTE[(p.id - 1) % PALETTE.length];
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = lw;
      const kp = p.kp;
      ctx.beginPath();
      for (const [a, b] of SKELETON) {
        if (kp[a][2] < settings.kpThr || kp[b][2] < settings.kpThr) continue;
        ctx.moveTo(kp[a][0], kp[a][1]); ctx.lineTo(kp[b][0], kp[b][1]);
      }
      ctx.stroke();
      for (const k of kp) {
        if (k[2] < settings.kpThr) continue;
        ctx.beginPath(); ctx.arc(k[0], k[1], lw * 0.9, 0, Math.PI * 2); ctx.fill();
      }
      if (settings.showBox && p.bbox) {
        ctx.lineWidth = lw * 0.4;
        ctx.strokeRect(p.bbox[0], p.bbox[1], p.bbox[2] - p.bbox[0], p.bbox[3] - p.bbox[1]);
      }
      if (settings.showId && p.id != null && p.bbox) {
        const fs = 14 / Math.max(scale, 0.01);
        ctx.font = `bold ${fs}px system-ui, sans-serif`;
        ctx.fillStyle = col;
        ctx.fillText(`#${p.id}`, p.bbox[0], Math.max(fs, p.bbox[1] - fs * 0.3));
      }
    }
    return true;
  }

  // 表示位置 (ts) がまだ推論済みの範囲に届いていない時、あと何秒で骨格が出るか
  function waitSec(session, ts) {
    const arr = store.get(session);
    if (!arr || !arr.length || ts == null) return null;
    const first = arr[0].ts_us;
    return ts < first ? (first - ts) / 1e6 : null;
  }

  function toggle() { settings.enabled = !settings.enabled; save(); return settings.enabled; }
  function set(key, value) { settings[key] = value; save(); }
  function status() {
    const age = stats.lastRecvAt ? (performance.now() - stats.lastRecvAt) / 1000 : null;
    return { received: stats.received, lastTs: stats.lastTs, ageSec: age, sessions: store.size };
  }

  return { onMessage, draw, personsAt, waitSec, toggle, set, settings, status };
})();
