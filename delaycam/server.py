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

サーバ自体は「静的ファイル配信」と「cam → view の WebSocket 中継」だけを行う。
遅延バッファはブラウザ(/view)側にあり、遅延秒数の変更・一時停止・スロー再生も
すべてブラウザ側で完結する。

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
import socket
import ssl
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
CERT_DIR = BASE / "certs"
PROFILE_DIR = BASE / ".browser-profile"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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


# ---------------------------------------------------------------- 中継ハブ

class Hub:
    """接続中の cam(1台) と viewer(複数) を保持し、cam のデータを viewer へ流す。"""

    def __init__(self) -> None:
        self.cam: web.WebSocketResponse | None = None
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
            }
        )

    def broadcast(self, data: str | bytes) -> None:
        for q in list(self.viewers.values()):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                self.dropped += 1


async def _viewer_sender(ws: web.WebSocketResponse, q: asyncio.Queue) -> None:
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
        log(f"viewer送信エラー: {e!r}")


async def ws_cam(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=32 * 1024 * 1024)
    await ws.prepare(request)

    old = hub.cam
    hub.cam = ws
    hub.last_config = None
    if old is not None and not old.closed:
        await old.close(code=4000, message=b"replaced by new camera")
    hub.broadcast(hub.status_json())
    log(f"カメラ接続: {request.remote}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                hub.rx_frames += 1
                hub.rx_bytes += len(msg.data)
                hub.broadcast(msg.data)
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
                hub.broadcast(msg.data)
            elif msg.type == WSMsgType.ERROR:
                log(f"カメラWSエラー: {ws.exception()!r}")
                break
    finally:
        if hub.cam is ws:
            hub.cam = None
            hub.last_config = None
            hub.broadcast(hub.status_json())
            log(f"カメラ切断: {request.remote}")
    return ws


async def ws_view(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)

    # 遅い viewer が cam 側を詰まらせないよう、viewer ごとにキュー+送信タスクを持つ
    q: asyncio.Queue = asyncio.Queue(maxsize=900)  # 30fps × 30秒
    hub.viewers[ws] = q
    sender = asyncio.create_task(_viewer_sender(ws, q))
    log(f"ビューア接続: {request.remote}")

    q.put_nowait(hub.status_json())
    if hub.cam is not None and hub.last_config:
        q.put_nowait(hub.last_config)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
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
            "cam_url": hub.cam_url,
            "urls": hub.all_urls,
            "cam_connected": hub.cam is not None,
            "viewers": len(hub.viewers),
            "rx_frames": hub.rx_frames,
            "rx_bytes": hub.rx_bytes,
            "dropped": hub.dropped,
        }
    )


async def quit_app(request: web.Request) -> web.StreamResponse:
    """表示ページの「終了」ボタン。kiosk ブラウザを閉じてサーバも止める。"""
    hub: Hub = request.app["hub"]
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="localhost からのみ終了できます")
    log("終了ボタンが押されました")
    loop = asyncio.get_running_loop()
    loop.call_later(0.3, lambda: loop.create_task(shutdown(hub)))
    return web.json_response({"ok": True})


async def shutdown(hub: Hub) -> None:
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
    prev_f, prev_b = 0, 0
    while True:
        await asyncio.sleep(5)
        if hub.cam is None:
            continue
        f, b = hub.rx_frames - prev_f, hub.rx_bytes - prev_b
        prev_f, prev_b = hub.rx_frames, hub.rx_bytes
        log(
            f"受信 {f / 5:.0f}fps {b * 8 / 5 / 1e6:.2f}Mbps  viewer={len(hub.viewers)}"
            + (f"  破棄={hub.dropped}" if hub.dropped else "")
        )


async def run(args: argparse.Namespace) -> None:
    ips = [args.ip] if args.ip else local_ipv4s()
    if not ips:
        ips = ["127.0.0.1"]

    hub = Hub()
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
    print("  DelayCam 起動")
    print(f"  PC側(表示)      : {view_url}")
    print(f"  スマホ側(カメラ) : {hub.cam_url}")
    if len(hub.all_urls) > 1:
        print("  他の候補        : " + ", ".join(hub.all_urls[1:]))
    if not args.no_tls:
        print("  ※ スマホで証明書の警告が出たら「詳細設定」→「アクセスする」")
    else:
        print("  ※ http のため、スマホの chrome://flags で")
        print("     #unsafely-treat-insecure-origin-as-secure に上記URLを登録してください")
    print("=" * 60)
    if not args.no_qr:
        print_terminal_qr(hub.cam_url)
    print()

    hub.stop_event = asyncio.Event()
    asyncio.get_running_loop().create_task(stats_loop(hub))
    if args.kiosk or args.open:
        hub.viewer_proc = launch_viewer(view_url, args.kiosk)

    try:
        await hub.stop_event.wait()
        log("終了します")
    finally:
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
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log("終了")


if __name__ == "__main__":
    main()
