"""ライブ骨格推定: delaycam サーバに接続して映像を購読し、推論結果を送り返す。

  uv run live                          # ws://localhost:8080/ws/analyzer に接続、プリセット medium
  uv run live --preset light --stride 3
  uv run live --pose rtmo --size s --tracker simple --auto-stride

表示側 (/view) は届いた {"type":"pose", session, ts_us, persons} を表示フレームの ts に合わせて重ねるだけ。
このプロセスは何フレームに 1 回推論するか (stride) を自分で決め、追いつかなければ自動で間隔を広げる。
遅延再生なので推論の遅れ自体は問題にならない (表示までに間に合えばよい)。

プロセス構成
  asyncio (受信/送信) ──chunk──▶ queue ──▶ worker スレッド: 復号 → (stride ごとに) 姿勢推定 → トラッカー ──▶ 送信
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import socket
import ssl
import struct
import sys
import threading
import time

import numpy as np

from . import __version__
from .backends import REGISTRY, create_pose_backend
from .ort_config import default_threads, set_threads
from .tracking import create_tracker

CHUNK_HDR = struct.Struct("<BBHdI")   # version, flags(bit0=key), reserved, ts_us, duration_us

# 重い ← → 軽い。rtmpose を軸にしたのは、one-stage (rtmo/yolo) が小さく写る人を落としやすいため。
# det_every: 人検出は姿勢推定より重いので、ライブでは数回に 1 回だけ検出して間は枠を使い回す。
# 括弧内はノート PC (i7-14650HX, 768x576, 4-6人) での定常実測値
PRESETS = {
    "heavy": {"pose": "rtmpose", "size": "m", "max_persons": 6, "det_every": 2},    # 約 1.7 回/秒
    "medium": {"pose": "rtmpose", "size": "s", "max_persons": 6, "det_every": 3},   # 約 5.5 回/秒
    "light": {"pose": "rtmpose", "size": "s", "max_persons": 2, "det_every": 4},    # 約 11 回/秒
}


def codec_name(codec: str) -> str:
    if codec.startswith("avc"):
        return "h264"
    if codec.startswith("vp09"):
        return "vp9"
    if codec.startswith("vp8"):
        return "vp8"
    raise ValueError(f"未対応コーデック: {codec}")


class Worker(threading.Thread):
    """復号と推論。asyncio ループを止めないよう別スレッドで動く。"""

    def __init__(self, args: argparse.Namespace, out: "queue.Queue[str]") -> None:
        super().__init__(daemon=True)
        self.args = args
        self.inq: queue.Queue = queue.Queue(maxsize=600)     # (kind, payload)
        self.out = out
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.pending: dict = {}                              # 設定変更の要求 (stride / preset)
        # 状態
        self.session: dict | None = None
        self.decoder = None
        self.need_key = True
        self.stride = max(1, args.stride)
        self.auto_stride = args.auto_stride or args.stride <= 0
        self.min_stride = max(1, args.stride)
        self.frame_no = 0
        self.persons_last = 0
        self.infer_times: list[float] = []
        self.infer_count = 0
        self.last_adjust = time.monotonic()
        self.preset = args.preset
        self.pose = None
        self.tracker = None
        self.backend_desc = ""
        self._load_backend()

    # ---- 設定

    def _load_backend(self) -> None:
        a = self.args
        cfg = dict(PRESETS.get(self.preset, PRESETS["medium"]))
        if a.pose:
            cfg["pose"] = a.pose
        if a.size:
            cfg["size"] = a.size
        if a.max_persons:
            cfg["max_persons"] = a.max_persons
        if a.det_every:
            cfg["det_every"] = a.det_every
        kw = {"det_thr": a.det_thr, "kp_thr": a.kp_thr}
        if cfg["pose"] == "rtmpose":
            kw["max_persons"] = cfg.get("max_persons", 6)
            kw["det_every"] = cfg.get("det_every", 1)
        t0 = time.perf_counter()
        pose = create_pose_backend(cfg["pose"], size=cfg["size"], **kw)
        pose.warmup((720, 1280))
        self.pose = pose
        self.tracker = create_tracker(a.tracker, fps=30 / max(1, self.stride))
        self.backend_desc = f"{pose.name}-{pose.size}" + (f" (最大{kw['max_persons']}人)" if "max_persons" in kw else "")
        self.preset_label = {"heavy": "重い", "medium": "標準", "light": "軽い"}.get(self.preset, self.preset)
        print(f"モデル読み込み: {self.backend_desc}  {time.perf_counter() - t0:.1f}s", flush=True)

    def request(self, cmd: dict) -> None:
        with self.lock:
            self.pending.update(cmd)

    def _apply_pending(self) -> None:
        with self.lock:
            cmd, self.pending = self.pending, {}
        if not cmd:
            return
        if "stride" in cmd:
            s = int(cmd["stride"])
            if s <= 0:
                self.auto_stride, self.min_stride, self.stride = True, 1, max(1, self.stride)
            else:
                self.auto_stride, self.min_stride, self.stride = False, s, s
            print(f"推論間隔: {'自動' if self.auto_stride else self.stride}", flush=True)
        if "preset" in cmd and cmd["preset"] in PRESETS and cmd["preset"] != self.preset:
            self.preset = cmd["preset"]
            self.args.pose = self.args.size = None
            self.args.max_persons = self.args.det_every = 0
            try:
                self._load_backend()
            except Exception as e:  # noqa: BLE001
                print(f"モデル切替失敗: {e}", flush=True)
            self.send_status(force=True)

    # ---- 復号

    def _new_session(self, cfg: dict) -> None:
        import av

        self.session = cfg
        self.decoder = av.CodecContext.create(codec_name(cfg.get("codec", "avc1")), "r")
        self.need_key = True
        self.frame_no = 0
        self.tracker = create_tracker(self.args.tracker, fps=30 / max(1, self.stride))
        print(f"セッション {cfg.get('session')}: {cfg.get('codec')} {cfg.get('width')}x{cfg.get('height')}@{cfg.get('fps')}", flush=True)

    def _decode(self, data: bytes):
        import av

        if len(data) < CHUNK_HDR.size or self.decoder is None:
            return None, None
        _ver, flags, _r, ts_us, _d = CHUNK_HDR.unpack_from(data)
        key = bool(flags & 1)
        if self.need_key:
            if not key:
                return None, None
            self.need_key = False
        try:
            frames = self.decoder.decode(av.Packet(bytes(data[CHUNK_HDR.size:])))
        except Exception:  # noqa: BLE001  (壊れた区間は次のキーフレームまで捨てる)
            self.need_key = True
            return None, None
        if not frames:
            return None, None
        return int(ts_us), frames[-1].to_ndarray(format="bgr24")

    # ---- メインループ

    def run(self) -> None:
        last_status = 0.0
        while not self.stop_flag.is_set():
            try:
                kind, payload = self.inq.get(timeout=0.5)
            except queue.Empty:
                self._apply_pending()
                continue
            if kind == "config":
                self._new_session(payload)
                continue
            self._apply_pending()
            ts_us, frame = self._decode(payload)
            if frame is None:
                continue
            self.frame_no += 1
            backlog = self.inq.qsize()
            if self.frame_no % self.stride != 0:
                continue
            if self.auto_stride and backlog > 90:       # 3 秒以上溜まっている: 今回は飛ばす
                continue
            t0 = time.perf_counter()
            persons = self.pose.infer(frame, ts_ms=ts_us / 1000)
            persons = self.tracker.update(persons, frame)
            dt = time.perf_counter() - t0
            self.infer_times.append(dt)
            self.infer_count += 1
            self.persons_last = len(persons)
            self.out.put(json.dumps({
                "type": "pose", "session": self.session.get("session"), "ts_us": ts_us,
                "persons": [self._person_json(p) for p in persons],
                "infer_ms": round(dt * 1000, 1), "stride": self.stride,
            }, separators=(",", ":")))
            now = time.monotonic()
            if now - last_status >= 2.0:
                last_status = now
                self._adjust_stride(backlog)
                self.send_status()

    @staticmethod
    def _person_json(p) -> dict:
        kp = np.concatenate([p.keypoints, p.kp_scores[:, None]], axis=1)
        return {"id": p.track_id, "score": round(float(p.score), 2),
                "bbox": [round(float(v), 1) for v in p.bbox],
                "kp": [[round(float(x), 1), round(float(y), 1), round(float(c), 2)] for x, y, c in kp]}

    def _adjust_stride(self, backlog: int) -> None:
        if not self.auto_stride:
            return
        now = time.monotonic()
        if now - self.last_adjust < 3.0:
            return
        if backlog > 30 and self.stride < 6:
            self.stride += 1
            self.last_adjust = now
            print(f"追いつかないため推論間隔を {self.stride} に", flush=True)
        elif backlog < 5 and self.stride > self.min_stride:
            self.stride -= 1
            self.last_adjust = now
            print(f"余裕があるため推論間隔を {self.stride} に", flush=True)

    def send_status(self, force: bool = False) -> None:
        times = self.infer_times[-60:]
        avg = sum(times) / len(times) if times else 0.0
        self.infer_times = times
        self.out.put(json.dumps({
            "type": "analyzer_status", "backend": self.backend_desc, "preset": self.preset,
            "stride": self.stride, "auto_stride": self.auto_stride,
            "infer_ms": round(avg * 1000, 1), "infer_fps": round(1 / avg, 1) if avg else None,
            "backlog": self.inq.qsize(), "persons": self.persons_last, "frames": self.frame_no,
            "host": socket.gethostname(), "version": __version__,
        }))


async def run(args: argparse.Namespace) -> None:
    import aiohttp

    out: "queue.Queue[str]" = queue.Queue()
    worker = Worker(args, out)
    worker.start()
    ssl_ctx = None
    if args.url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    async def sender(ws):
        loop = asyncio.get_running_loop()
        while not ws.closed:
            msg = await loop.run_in_executor(None, out.get)
            if msg is None:
                break
            await ws.send_str(msg)

    while not worker.stop_flag.is_set():
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(args.url, ssl=ssl_ctx, max_msg_size=32 * 1024 * 1024, heartbeat=15) as ws:
                    print(f"接続: {args.url}", flush=True)
                    worker.send_status(force=True)
                    send_task = asyncio.create_task(sender(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                try:
                                    worker.inq.put_nowait(("chunk", msg.data))
                                except queue.Full:
                                    worker.need_key = True     # 溢れたら次のキーフレームから
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    obj = json.loads(msg.data)
                                except json.JSONDecodeError:
                                    continue
                                t = obj.get("type")
                                if t == "config":
                                    worker.inq.put(("config", obj))
                                elif t == "analyzer_cmd":
                                    worker.request({k: v for k, v in obj.items() if k != "type"})
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        send_task.cancel()
                    if ws.close_code == 4001:      # 表示画面から停止された
                        print("停止されました", flush=True)
                        worker.stop_flag.set()
                        break
        except (aiohttp.ClientError, OSError) as e:
            print(f"接続できません ({e.__class__.__name__}) → 3 秒後に再接続", flush=True)
        if worker.stop_flag.is_set():
            break
        await asyncio.sleep(3)


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    p = argparse.ArgumentParser(description="delaycam に骨格推定を提供するライブアナライザ")
    p.add_argument("--url", default="ws://localhost:8080/ws/analyzer", help="delaycam サーバの WebSocket")
    p.add_argument("--preset", default="medium", choices=list(PRESETS), help="重さ (heavy / medium / light)")
    p.add_argument("--pose", default=None, choices=[*REGISTRY], help="バックエンドを直接指定 (プリセットより優先)")
    p.add_argument("--size", default=None)
    p.add_argument("--max-persons", type=int, default=0, help="rtmpose: 姿勢推定する人数の上限 (大きく写る順)")
    p.add_argument("--det-every", type=int, default=0, help="rtmpose: 何回に 1 回 人検出をするか (0=プリセットのまま)")
    p.add_argument("--stride", type=int, default=2, help="何フレームに 1 回推論するか (0=自動)")
    p.add_argument("--auto-stride", action="store_true", help="追いつかない時に自動で間隔を広げる (--stride は下限)")
    p.add_argument("--tracker", default="simple", choices=["simple", "bytetrack", "ocsort", "sort", "none"])
    p.add_argument("--det-thr", type=float, default=0.5)
    p.add_argument("--kp-thr", type=float, default=0.3)
    p.add_argument("--threads", type=int, default=0, help=f"ONNX Runtime スレッド数 (0=自動: {default_threads()})")
    args = p.parse_args(argv)
    set_threads(args.threads or None)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("終了")


if __name__ == "__main__":
    main()
