# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp>=3.9",
#     "qrcode>=7.4",
#     "cryptography>=42",
# ]
# ///
"""
DelayCam サーバ

  /cam   スマホ側ページ  : カメラ → WebCodecs(H.264) → WebSocket 送信
  /view  PC側ページ      : 受信 → 遅延バッファ(エンコード済) → WebCodecs 復号 → canvas

サーバは「静的ファイル配信」「cam → view の WebSocket 中継」「録画」を行う。
遅延バッファはブラウザ(/view)側にあり、遅延秒数の変更・一時停止・スロー再生も
すべてブラウザ側で完結する。

録画は /view の REC ボタンからのみ開始/停止できる(起動オプションでは開始しない)。
受信したチャンクを再エンコードせずそのまま recordings/ に書く。詳細は README「録画データ形式」。

起動:  uv run server.py            (依存関係は自動で入る)
       uv run server.py --kiosk    (PC側の表示ページを全画面ブラウザで自動起動)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
import json
import os
import platform
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path

import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web

VERSION = "1.1.0"
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
CERT_DIR = BASE / "certs"
LOG_DIR = BASE / "logs"
PROFILE_DIR = BASE / ".browser-profile"
LOCALHOST = ("127.0.0.1", "::1")

CHUNK_HDR = struct.Struct("<BBHdI")   # version, flags(bit0=key), reserved, ts_us, duration_us  (16 bytes)
MIN_FREE_BYTES = 1 << 30              # 空き容量がこれを切ったら録画を止める

_logfile = None


def log(msg: str) -> None:
    """コンソールと logs/delaycam-YYYYMMDD.log の両方に出す。"""
    global _logfile
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        path = LOG_DIR / f"delaycam-{time.strftime('%Y%m%d')}.log"
        if _logfile is None or _logfile.name != str(path):
            LOG_DIR.mkdir(exist_ok=True)
            if _logfile:
                _logfile.close()
            _logfile = open(path, "a", encoding="utf-8")
        _logfile.write(line + "\n")
        _logfile.flush()
    except OSError:
        pass


def iso_now(unix: float | None = None) -> str:
    return dt.datetime.fromtimestamp(unix if unix is not None else time.time()).astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- ネットワーク

def local_ipv4s() -> list[str]:
    """このPCのIPv4アドレス一覧。スマホから届きやすい順に並べる。"""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    try:  # デフォルトルート側のアドレス
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips.discard("127.0.0.1")

    def prio(ip: str) -> tuple:
        # Windows モバイルホットスポット(192.168.137.x) を最優先
        if ip.startswith("192.168.137."):
            return (0, ip)
        if ip.startswith("192.168."):
            return (1, ip)
        if ip.startswith("10."):
            return (2, ip)
        if ip.startswith("172."):
            return (3, ip)
        return (4, ip)

    return sorted(ips, key=prio)


def ensure_cert(ips: list[str]) -> tuple[Path, Path]:
    """自己署名証明書を certs/ に生成(既にあれば再利用)。"""
    cert_path, key_path = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    CERT_DIR.mkdir(exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "delaycam")])
    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log(f"自己署名証明書を生成しました: {cert_path}")
    return cert_path, key_path


# ---------------------------------------------------------------- 録画

def ivf_header(codec: str, width: int, height: int) -> bytes:
    fourcc = b"VP90" if codec.startswith("vp09") else b"VP80"
    # timebase 1/1000000 → pts は µs
    return struct.pack("<4sHH4sHHIII", b"DKIF", 0, 32, fourcc, width, height, 1_000_000, 1, 0, 0)


class Recorder:
    """受信チャンクをそのままファイルに書く。プリロール用に直近数十秒をメモリに持つ。

    録画フォルダ:  meta.json / segNN.h264 (or .ivf) / frames.csv / events.jsonl
    カメラの再接続(=新しい config)ごとにセグメントを分ける。
    """

    def __init__(self, rec_dir: Path, preroll_sec: float) -> None:
        self.rec_dir = rec_dir
        self.preroll_ms = preroll_sec * 1000
        self.ring: deque[tuple[float, str, object]] = deque()   # (recv_unix_ms, 'config'|'chunk', obj|bytes)
        self.ring_bytes = 0
        self.ring_base_config: dict | None = None   # リング先頭のチャンクを支配する config
        self.cur_config: dict | None = None
        self.active = False
        self.dir: Path | None = None
        self.meta: dict = {}
        self.seg: dict | None = None
        self.video = None
        self.frames = None
        self.events = None
        self.session_seg: dict[str, int] = {}
        self.daily_events = None
        self.rec_dir.mkdir(parents=True, exist_ok=True)

    # ---- 受信データ (録画中でなくても常に呼ぶ: プリロール用)

    def feed_config(self, obj: dict) -> None:
        self.cur_config = obj
        self._ring_push("config", obj)
        if self.active:
            self._new_segment(obj)

    def feed_chunk(self, data: bytes) -> None:
        self._ring_push("chunk", data)
        if self.active:
            self._write_chunk(data)

    def _ring_push(self, kind: str, payload) -> None:
        now = time.time() * 1000
        self.ring.append((now, kind, payload))
        if kind == "chunk":
            self.ring_bytes += len(payload)
        while self.ring and now - self.ring[0][0] > self.preroll_ms + 2000:
            _, k, p = self.ring.popleft()
            if k == "chunk":
                self.ring_bytes -= len(p)
            else:
                self.ring_base_config = p

    # ---- 開始 / 停止

    def start(self, setup: dict, delay: float | None, viewer: dict | None) -> dict:
        if self.active:
            return self.status()
        now = time.time()
        rid = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now))
        self.dir = self.rec_dir / rid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = {
            "recording_id": rid,
            "format_version": 1,
            "app_version": VERSION,
            "started_at": iso_now(now),
            "started_unix_ms": int(now * 1000),
            "ended_at": None,
            "ended_unix_ms": None,
            "duration_sec": None,
            "preroll_sec": self.preroll_ms / 1000,
            "delay_sec_at_start": delay,
            "setup": setup,
            "viewer": viewer,
            "pc": {"host": socket.gethostname(), "platform": platform.platform()},
            "segments": [],
            "frames_total": 0,
            "bytes_total": 0,
        }
        self.frames = open(self.dir / "frames.csv", "w", encoding="utf-8", newline="")
        self.frames.write("seg,n,ts_us,key,offset,size,recv_unix_ms\n")
        self.events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self.session_seg = {}
        self.seg = None
        self.active = True
        preroll_frames = self._write_preroll()
        self.log_event({"type": "rec_start", "preroll_frames": preroll_frames, "setup": setup})
        self.save_meta()
        log(f"録画開始: {self.dir}  (プリロール {preroll_frames} フレーム)")
        return self.status()

    def stop(self, reason: str) -> dict:
        if not self.active:
            return self.status()
        self.log_event({"type": "rec_stop", "reason": reason})
        self._close_segment()
        self.active = False
        now = time.time()
        self.meta["ended_at"] = iso_now(now)
        self.meta["ended_unix_ms"] = int(now * 1000)
        self.meta["duration_sec"] = round(now - self.meta["started_unix_ms"] / 1000, 3)
        self.save_meta()
        for f in (self.frames, self.events):
            if f:
                f.close()
        self.frames = self.events = None
        if self.meta["frames_total"] == 0:   # カメラ未接続のまま止めた等: 空フォルダは残さない
            shutil.rmtree(self.dir, ignore_errors=True)
            log(f"録画停止 ({reason}): 映像が無いため {self.dir.name} は削除しました")
            return self.status()
        log(f"録画停止 ({reason}): {self.meta['frames_total']} フレーム {self.meta['bytes_total'] / 1e6:.1f} MB "
            f"{self.meta['duration_sec']:.0f} 秒 → {self.dir}")
        return self.status()

    def _write_preroll(self) -> int:
        """リングバッファから、プリロール範囲内の最初のキーフレーム以降を書く。"""
        cutoff = time.time() * 1000 - self.preroll_ms
        items = list(self.ring)
        start = None
        for i, (t, k, p) in enumerate(items):
            if k == "chunk" and t >= cutoff and (p[1] & 1):
                start = i
                break
        if start is None:
            if self.cur_config:
                self._new_segment(self.cur_config)
            return 0
        cfg = self.ring_base_config
        for _, k, p in items[:start]:
            if k == "config":
                cfg = p
        if cfg:
            self._new_segment(cfg)
        n = 0
        for t, k, p in items[start:]:
            if k == "config":
                self._new_segment(p)
            else:
                self._write_chunk(p, recv_ms=t)
                n += 1
        return n

    # ---- セグメント / フレーム書き込み

    def _new_segment(self, cfg: dict) -> None:
        self._close_segment()
        n = len(self.meta["segments"]) + 1
        codec = str(cfg.get("codec", ""))
        ivf = not codec.startswith("avc")
        fname = f"seg{n:02d}." + ("ivf" if ivf else "h264")
        self.video = open(self.dir / fname, "wb")
        if ivf:
            self.video.write(ivf_header(codec, int(cfg.get("width") or 0), int(cfg.get("height") or 0)))
        self.seg = {
            "seg": n,
            "file": fname,
            "container": "ivf" if ivf else "annexb",
            "session": cfg.get("session"),
            "codec": codec,
            "width": cfg.get("width"),
            "height": cfg.get("height"),
            "fps": cfg.get("fps"),
            "bitrate": cfg.get("bitrate"),
            "test": bool(cfg.get("test")),
            "phone": {k: cfg.get(k) for k in ("ua", "orientation", "camera", "settings")},
            "started_unix_ms": int(time.time() * 1000),
            "first_ts_us": None,
            "last_ts_us": None,
            "frames": 0,
            "bytes": 0,
        }
        self.meta["segments"].append(self.seg)
        if cfg.get("session"):
            self.session_seg[cfg["session"]] = n
        self.log_event({"type": "segment_start", "seg": n, "codec": codec,
                        "width": cfg.get("width"), "height": cfg.get("height"), "fps": cfg.get("fps")})

    def _close_segment(self) -> None:
        if self.video is None:
            return
        if self.seg and self.seg["container"] == "ivf":
            self.video.seek(24)
            self.video.write(struct.pack("<I", self.seg["frames"]))
        self.video.close()
        self.video = None
        if self.seg:
            self.log_event({"type": "segment_end", "seg": self.seg["seg"], "frames": self.seg["frames"], "bytes": self.seg["bytes"]})
        self.seg = None

    def _write_chunk(self, data: bytes, recv_ms: float | None = None) -> None:
        if self.video is None:
            if not self.cur_config:
                return
            self._new_segment(self.cur_config)
        if len(data) < CHUNK_HDR.size:
            return
        _ver, flags, _res, ts_us, _dur = CHUNK_HDR.unpack_from(data)
        payload = memoryview(data)[CHUNK_HDR.size:]
        s = self.seg
        if s["first_ts_us"] is None:
            s["first_ts_us"] = ts_us
        if s["container"] == "ivf":
            self.video.write(struct.pack("<IQ", len(payload), int(ts_us - s["first_ts_us"])))
        offset = self.video.tell()
        self.video.write(payload)
        s["frames"] += 1
        s["bytes"] += len(payload)
        s["last_ts_us"] = ts_us
        self.meta["frames_total"] += 1
        self.meta["bytes_total"] += len(payload)
        self.frames.write(f"{s['seg']},{s['frames']},{int(ts_us)},{flags & 1},{offset},{len(payload)},"
                          f"{int(recv_ms if recv_ms is not None else time.time() * 1000)}\n")

    # ---- イベント / メタ

    def log_event(self, ev: dict) -> None:
        """録画中なら録画フォルダの events.jsonl へ。常に logs/events-YYYYMMDD.jsonl にも残す(映像は含まない)。"""
        now = time.time()
        ev = {"unix_ms": int(now * 1000), "iso": iso_now(now), **ev}
        sess = ev.get("session")
        if sess and sess in self.session_seg:
            ev["seg"] = self.session_seg[sess]
        line = json.dumps(ev, ensure_ascii=False) + "\n"
        if self.active and self.events:
            self.events.write(line)
        try:
            path = LOG_DIR / f"events-{time.strftime('%Y%m%d')}.jsonl"
            if self.daily_events is None or self.daily_events.name != str(path):
                LOG_DIR.mkdir(exist_ok=True)
                if self.daily_events:
                    self.daily_events.close()
                self.daily_events = open(path, "a", encoding="utf-8")
            self.daily_events.write(line)
        except OSError:
            pass

    def flush(self) -> None:
        for f in (self.video, self.frames, self.events, self.daily_events):
            if f:
                f.flush()
        if self.active:
            self.save_meta()

    def save_meta(self) -> None:
        if not self.dir:
            return
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "meta.json")

    def free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.rec_dir).free
        except OSError:
            return 0

    def status(self) -> dict:
        return {
            "active": self.active,
            "id": self.meta.get("recording_id") if self.active else None,
            "started_unix_ms": self.meta.get("started_unix_ms") if self.active else None,
            "frames": self.meta.get("frames_total", 0) if self.active else 0,
            "bytes": self.meta.get("bytes_total", 0) if self.active else 0,
            "dir": str(self.dir) if self.active else None,
            "rec_dir": str(self.rec_dir),
            "free_gb": round(self.free_bytes() / 1e9, 1),
            "preroll_sec": self.preroll_ms / 1000,
        }


# ---------------------------------------------------------------- 中継ハブ

class Hub:
    """接続中の cam(1台) と viewer(複数) を保持し、cam のデータを viewer へ流す。"""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.cam: web.WebSocketResponse | None = None
        self.cam_queue: asyncio.Queue | None = None
        self.viewers: dict[web.WebSocketResponse, asyncio.Queue] = {}
        self.last_config: str | None = None
        self.cam_url = ""
        self.all_urls: list[str] = []
        self.rx_frames = 0
        self.rx_bytes = 0
        self.dropped = 0
        self.viewer_proc: subprocess.Popen | None = None   # --kiosk で起動したブラウザ
        self.stop_event: asyncio.Event | None = None

    def status_json(self) -> str:
        return json.dumps(
            {
                "type": "status",
                "cam": self.cam is not None,
                "cam_url": self.cam_url,
                "urls": self.all_urls,
                "viewers": len(self.viewers),
                "recording": self.recorder.status(),
                "version": VERSION,
            }
        )

    def broadcast(self, data: str | bytes) -> None:
        for q in list(self.viewers.values()):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                self.dropped += 1

    def broadcast_status(self) -> None:
        s = self.status_json()
        self.broadcast(s)
        if self.cam_queue is not None:
            try:
                self.cam_queue.put_nowait(s)
            except asyncio.QueueFull:
                pass


async def _sender(ws: web.WebSocketResponse, q: asyncio.Queue) -> None:
    try:
        while True:
            msg = await q.get()
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_str(msg)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception as e:  # noqa: BLE001
        log(f"送信エラー: {e!r}")


async def ws_cam(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    rec = hub.recorder
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=32 * 1024 * 1024)
    await ws.prepare(request)

    old = hub.cam
    hub.cam = ws
    hub.last_config = None
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    hub.cam_queue = q
    sender = asyncio.create_task(_sender(ws, q))
    if old is not None and not old.closed:
        await old.close(code=4000, message=b"replaced by new camera")
    hub.broadcast_status()
    log(f"カメラ接続: {request.remote}")
    rec.log_event({"type": "cam_connect", "remote": request.remote})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                hub.rx_frames += 1
                hub.rx_bytes += len(msg.data)
                hub.broadcast(msg.data)
                rec.feed_chunk(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "config":
                    hub.last_config = msg.data
                    log(
                        f"カメラ設定: {obj.get('codec')} {obj.get('width')}x{obj.get('height')}"
                        f"@{obj.get('fps')}fps {int(obj.get('bitrate', 0)) / 1e6:.1f}Mbps"
                        f"{' (テストパターン)' if obj.get('test') else ''}"
                    )
                    rec.feed_config(obj)
                    rec.log_event({"type": "cam_config", **{k: obj.get(k) for k in ("session", "codec", "width", "height", "fps", "bitrate", "test", "orientation", "camera")}})
                hub.broadcast(msg.data)
            elif msg.type == WSMsgType.ERROR:
                log(f"カメラWSエラー: {ws.exception()!r}")
                break
    finally:
        sender.cancel()
        if hub.cam is ws:
            hub.cam = None
            hub.cam_queue = None
            hub.last_config = None
            hub.broadcast_status()
            log(f"カメラ切断: {request.remote}")
            rec.log_event({"type": "cam_disconnect", "remote": request.remote})
    return ws


async def ws_view(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    rec = hub.recorder
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)

    # 遅い viewer が cam 側を詰まらせないよう、viewer ごとにキュー+送信タスクを持つ
    q: asyncio.Queue = asyncio.Queue(maxsize=900)  # 30fps × 30秒
    hub.viewers[ws] = q
    sender = asyncio.create_task(_sender(ws, q))
    is_local = request.remote in LOCALHOST
    log(f"ビューア接続: {request.remote}")

    q.put_nowait(hub.status_json())
    if hub.cam is not None and hub.last_config:
        q.put_nowait(hub.last_config)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "rec":
                    # 録画の開始/停止は PC 本体 (localhost) の画面からのみ
                    if not is_local:
                        q.put_nowait(json.dumps({"type": "toast", "text": "録画はPC本体の画面からのみ操作できます"}))
                        continue
                    if obj.get("action") == "start":
                        if rec.free_bytes() < MIN_FREE_BYTES:
                            q.put_nowait(json.dumps({"type": "toast", "text": "空き容量が足りません (1GB 未満)"}))
                            continue
                        rec.start(obj.get("setup") or {}, obj.get("delay"), obj.get("viewer"))
                    elif obj.get("action") == "stop":
                        rec.stop("user")
                    hub.broadcast_status()
                elif t == "event":
                    ev = {k: v for k, v in obj.items() if k != "type"}
                    rec.log_event({"type": "ui", "remote": request.remote, **ev})
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.viewers.pop(ws, None)
        sender.cancel()
        log(f"ビューア切断: {request.remote}")
    return ws


# ---------------------------------------------------------------- HTTP

def page(name: str):
    async def handler(_: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / name)

    return handler


async def qr_svg(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    img = qrcode.make(hub.cam_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return web.Response(body=img.to_string(), content_type="image/svg+xml")


async def info(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    return web.json_response(
        {
            "version": VERSION,
            "cam_url": hub.cam_url,
            "urls": hub.all_urls,
            "cam_connected": hub.cam is not None,
            "viewers": len(hub.viewers),
            "rx_frames": hub.rx_frames,
            "rx_bytes": hub.rx_bytes,
            "dropped": hub.dropped,
            "recording": hub.recorder.status(),
        }
    )


async def quit_app(request: web.Request) -> web.StreamResponse:
    """表示ページの「終了」ボタン。録画を確定し、kiosk ブラウザを閉じてサーバも止める。"""
    hub: Hub = request.app["hub"]
    if request.remote not in LOCALHOST:
        raise web.HTTPForbidden(text="localhost からのみ終了できます")
    log("終了ボタンが押されました")
    loop = asyncio.get_running_loop()
    loop.call_later(0.3, lambda: loop.create_task(shutdown(hub)))
    return web.json_response({"ok": True})


async def shutdown(hub: Hub) -> None:
    hub.recorder.stop("quit")
    await asyncio.get_running_loop().run_in_executor(None, close_viewer, hub)
    if hub.stop_event:
        hub.stop_event.set()


def close_viewer(hub: Hub) -> None:
    """--kiosk で開いたブラウザを閉じる。PID で消し、念のため専用プロファイルを使う残骸も掃除する。"""
    if hub.viewer_proc is not None and hub.viewer_proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(hub.viewer_proc.pid)], capture_output=True)
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*--user-data-dir=%s*' } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    ) % str(PROFILE_DIR).replace("'", "''")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


@web.middleware
async def no_cache(request: web.Request, handler):
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def make_app(hub: Hub) -> web.Application:
    app = web.Application(middlewares=[no_cache])
    app["hub"] = hub
    app.router.add_get("/", lambda r: web.HTTPFound("/view"))
    app.router.add_get("/cam", page("cam.html"))
    app.router.add_get("/view", page("view.html"))
    app.router.add_get("/ws/cam", ws_cam)
    app.router.add_get("/ws/view", ws_view)
    app.router.add_get("/qr.svg", qr_svg)
    app.router.add_get("/info.json", info)
    app.router.add_post("/quit", quit_app)
    app.router.add_static("/static", STATIC)
    return app


# ---------------------------------------------------------------- 補助

def print_terminal_qr(url: str) -> None:
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001  (コードページ等で描けない環境は無視)
        pass


def launch_viewer(url: str, kiosk: bool) -> subprocess.Popen | None:
    """PC側の表示ページを Chrome / Edge で開く。kiosk=True なら全画面の専用ウィンドウ。"""
    candidates = [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    ]
    exe = next((p for p in map(os.path.expandvars, candidates) if os.path.exists(p)), None)
    if exe and kiosk:
        # 専用プロファイルで起動すると、既に開いているブラウザの影響を受けずに kiosk になる
        args = [
            exe,
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",  # 他のウィンドウに隠れても再生を止めない
            "--kiosk",
            f"--app={url}",
        ]
        proc = subprocess.Popen(args, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        log(f"表示ページを全画面で起動: {Path(exe).name}  (終了は画面の「終了」ボタン or Esc)")
        return proc
    webbrowser.open(url)
    return None


async def stats_loop(hub: Hub) -> None:
    rec = hub.recorder
    prev_f, prev_b = 0, 0
    while True:
        await asyncio.sleep(5)
        f, b = hub.rx_frames - prev_f, hub.rx_bytes - prev_b
        prev_f, prev_b = hub.rx_frames, hub.rx_bytes
        if hub.cam is not None:
            log(
                f"受信 {f / 5:.0f}fps {b * 8 / 5 / 1e6:.2f}Mbps  viewer={len(hub.viewers)}"
                + (f"  破棄={hub.dropped}" if hub.dropped else "")
                + (f"  REC {rec.meta.get('bytes_total', 0) / 1e6:.0f}MB" if rec.active else "")
            )
            rec.log_event({"type": "stats", "rx_fps": f / 5, "rx_mbps": round(b * 8 / 5 / 1e6, 3),
                           "viewers": len(hub.viewers), "dropped": hub.dropped})
        if rec.active:
            rec.flush()
            if rec.free_bytes() < MIN_FREE_BYTES:
                log("空き容量が 1GB を切ったため録画を停止します")
                rec.log_event({"type": "disk_low", "free_bytes": rec.free_bytes()})
                rec.stop("disk_low")
            hub.broadcast_status()


async def run(args: argparse.Namespace) -> None:
    ips = [args.ip] if args.ip else local_ipv4s()
    if not ips:
        ips = ["127.0.0.1"]

    recorder = Recorder(Path(args.rec_dir).expanduser().resolve(), args.preroll)
    hub = Hub(recorder)
    scheme, port = ("http", args.http_port) if args.no_tls else ("https", args.https_port)
    hub.cam_url = f"{scheme}://{ips[0]}:{port}/cam"
    hub.all_urls = [f"{scheme}://{ip}:{port}/cam" for ip in ips]

    app = make_app(hub)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.http_port).start()
    if not args.no_tls:
        cert, key = ensure_cert(ips)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        await web.TCPSite(runner, "0.0.0.0", args.https_port, ssl_context=ctx).start()

    view_url = f"http://localhost:{args.http_port}/view"
    print()
    print("=" * 60)
    print(f"  DelayCam {VERSION} 起動")
    print(f"  PC側(表示)      : {view_url}")
    print(f"  スマホ側(カメラ) : {hub.cam_url}")
    if len(hub.all_urls) > 1:
        print("  他の候補        : " + ", ".join(hub.all_urls[1:]))
    print(f"  録画の保存先     : {recorder.rec_dir}  (空き {recorder.free_bytes() / 1e9:.1f} GB)")
    if not args.no_tls:
        print("  ※ スマホで証明書の警告が出たら「詳細設定」→「アクセスする」")
    else:
        print("  ※ http のため、スマホの chrome://flags で")
        print("     #unsafely-treat-insecure-origin-as-secure に上記URLを登録してください")
    print("=" * 60)
    if not args.no_qr:
        print_terminal_qr(hub.cam_url)
    print()
    log(f"起動 v{VERSION}  rec_dir={recorder.rec_dir}")

    hub.stop_event = asyncio.Event()
    asyncio.get_running_loop().create_task(stats_loop(hub))
    if args.kiosk or args.open:
        hub.viewer_proc = launch_viewer(view_url, args.kiosk)

    try:
        await hub.stop_event.wait()
        log("終了します")
    finally:
        recorder.stop("shutdown")
        await runner.cleanup()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
    p = argparse.ArgumentParser(description="DelayCam: スマホカメラ → PC ディレイ再生")
    p.add_argument("--http-port", type=int, default=8080, help="PC側表示用 (既定 8080)")
    p.add_argument("--https-port", type=int, default=8443, help="スマホ接続用 (既定 8443)")
    p.add_argument("--ip", help="QRコードに使うこのPCのIPを固定する")
    p.add_argument("--no-tls", action="store_true", help="https を使わない(chrome://flags 方式)")
    p.add_argument("--kiosk", action="store_true", help="表示ページを全画面ブラウザで自動起動")
    p.add_argument("--open", action="store_true", help="表示ページを既定ブラウザで開く")
    p.add_argument("--no-qr", action="store_true", help="ターミナルにQRを描かない")
    p.add_argument("--rec-dir", default=str(BASE / "recordings"), help="録画の保存先 (既定 delaycam/recordings)")
    p.add_argument("--preroll", type=float, default=30, help="REC 開始時に遡って保存する秒数 (既定 30)")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log("終了 (Ctrl+C)")


if __name__ == "__main__":
    main()
