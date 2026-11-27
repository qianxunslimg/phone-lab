"""Browser Cloud Phone bridge: official scrcpy-server (hardware H.264) <-> RFC 6455 WebSocket.

Zero external Python dependencies, zero ADB daemon required, zero extra ports.
Connects directly to scrcpy-server over Android's shared abstract Unix socket
namespace and streams Annex-B H.264 frames + native touch/key/clipboard control
inside the authenticated dashboard HTTP server.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

SCRCPY_VERSION = "3.3.4"
SCRCPY_JAR_ANDROID = "/data/local/tmp/scrcpy-server.jar"
SCRCPY_JAR_BACKUP = Path("/opt/phone-dashboard/scrcpy-server.jar")
NSENTER_BIN = "/usr/bin/nsenter"
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# One-time WebSocket auth tickets: token -> expiry monotonic timestamp
_tickets_lock = threading.Lock()
_tickets = {}

# Single active stream session lock so reconnecting cleanly tears down the previous one
_session_lock = threading.Lock()
_active_session = None


def issue_ticket(ttl: float = 30.0) -> str:
    now = time.monotonic()
    token = secrets.token_urlsafe(24)
    with _tickets_lock:
        expired = [k for k, exp in _tickets.items() if exp < now]
        for k in expired:
            _tickets.pop(k, None)
        _tickets[token] = now + ttl
    return token


def consume_ticket(token: str) -> bool:
    if not token:
        return False
    now = time.monotonic()
    with _tickets_lock:
        exp = _tickets.pop(token, None)
    return exp is not None and exp >= now


def is_available() -> bool:
    if Path("/proc/1/root" + SCRCPY_JAR_ANDROID).is_file():
        return True
    return SCRCPY_JAR_BACKUP.is_file()


def _ensure_android_jar() -> bool:
    android_view = Path("/proc/1/root" + SCRCPY_JAR_ANDROID)
    if android_view.is_file() and android_view.stat().st_size > 10000:
        return True
    if SCRCPY_JAR_BACKUP.is_file():
        try:
            data = SCRCPY_JAR_BACKUP.read_bytes()
            proc = subprocess.Popen(
                [NSENTER_BIN, "-t", "1", "-m", "--", "/system/bin/sh", "-c",
                 f"cat > {SCRCPY_JAR_ANDROID} && chmod 644 {SCRCPY_JAR_ANDROID}"],
                stdin=subprocess.PIPE,
            )
            proc.communicate(data, timeout=10)
            return proc.returncode == 0
        except Exception:
            return False
    return False


def _collect_android_env() -> dict:
    """Read Android ART/BOOTCLASSPATH env vars from zygote64/adbd for app_process64."""
    wanted = {
        "ANDROID_ROOT", "ANDROID_DATA", "ANDROID_ART_ROOT", "ANDROID_I18N_ROOT",
        "ANDROID_TZDATA_ROOT", "BOOTCLASSPATH", "DEX2OATBOOTCLASSPATH",
        "SYSTEMSERVERCLASSPATH", "ANDROID_STORAGE", "EXTERNAL_STORAGE", "PATH",
    }
    env = {}
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
                if b"zygote64" not in cmd and b"adbd" not in cmd:
                    continue
                raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\x00")
                for item in raw:
                    if b"=" not in item:
                        continue
                    k, v = item.decode("utf-8", errors="ignore").split("=", 1)
                    if k in wanted:
                        env[k] = v
                if "BOOTCLASSPATH" in env:
                    break
            except OSError:
                continue
    except OSError:
        pass
    env.setdefault("ANDROID_ROOT", "/system")
    env.setdefault("ANDROID_DATA", "/data")
    env["CLASSPATH"] = SCRCPY_JAR_ANDROID
    return env


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed")
        buf.extend(chunk)
    return bytes(buf)


class WebSocketConn:
    """Minimal thread-safe RFC 6455 WebSocket wrapper over an existing TCP socket."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    def send_frame(self, opcode: int, payload: bytes):
        if self.closed:
            return
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        with self._send_lock:
            if self.closed:
                return
            self.sock.sendall(bytes(header) + payload)

    def send_text(self, text: str):
        self.send_frame(0x1, text.encode("utf-8"))

    def send_json(self, obj: dict):
        self.send_text(json.dumps(obj, ensure_ascii=False))

    def send_binary(self, data: bytes):
        self.send_frame(0x2, data)

    def send_pong(self, data: bytes = b""):
        self.send_frame(0xA, data)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            with self._send_lock:
                self.sock.sendall(b"\x88\x00")
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def recv_frame(self):
        """Read one WebSocket frame; returns (opcode, payload) or (None, None) on close."""
        first2 = _recv_exact(self.sock, 2)
        opcode = first2[0] & 0x0F
        masked = bool(first2[1] & 0x80)
        length = first2[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]
        if length > 4 * 1024 * 1024:
            raise ValueError("WebSocket frame too large")
        mask_key = _recv_exact(self.sock, 4) if masked else b""
        payload = _recv_exact(self.sock, length) if length else b""
        if masked and payload:
            payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
        return opcode, payload


def _encode_control(msg: dict) -> list:
    """Encode browser JSON control event into one or more scrcpy binary packets."""
    mtype = msg.get("type")
    if mtype == "touch":
        action = int(msg.get("action", 0))  # 0=DOWN, 1=UP, 2=MOVE
        x = max(0, int(msg.get("x", 0)))
        y = max(0, int(msg.get("y", 0)))
        w = max(1, int(msg.get("w", 1080)))
        h = max(1, int(msg.get("h", 2400)))
        pressure = 0x0000 if action == 1 else 0xFFFF
        pointer_id = int(msg.get("pointer_id", 1))
        pkt = struct.pack(">BBqiiHHHII", 2, action, pointer_id, x, y, w, h, pressure, 0, 0)
        return [pkt]

    if mtype == "scroll":
        x = max(0, int(msg.get("x", 0)))
        y = max(0, int(msg.get("y", 0)))
        w = max(1, int(msg.get("w", 1080)))
        h = max(1, int(msg.get("h", 2400)))
        # scrcpy v2/v3 fixed-point 16-bit signed float in [-1.0, 1.0] -> [-32768, 32767]
        def _clamp_f16(v):
            v = max(-1.0, min(1.0, float(v)))
            return max(-32768, min(32767, int(v * 32767)))
        hs = _clamp_f16(msg.get("dx", 0.0))
        vs = _clamp_f16(msg.get("dy", 0.0))
        pkt = struct.pack(">BiiHHhhI", 3, x, y, w, h, hs, vs, 0)
        return [pkt]

    if mtype == "key":
        keycode = int(msg.get("keycode", 0))
        meta = int(msg.get("meta", 0))
        act = msg.get("action", "click")
        if act == "down":
            return [struct.pack(">BBiii", 0, 0, keycode, 0, meta)]
        if act == "up":
            return [struct.pack(">BBiii", 0, 1, keycode, 0, meta)]
        return [
            struct.pack(">BBiii", 0, 0, keycode, 0, meta),
            struct.pack(">BBiii", 0, 1, keycode, 0, meta),
        ]

    if mtype == "back_or_wake":
        return [struct.pack(">BB", 4, 0), struct.pack(">BB", 4, 1)]

    if mtype == "panel":
        act = msg.get("action", "expand")
        if act == "expand":
            return [struct.pack(">B", 5)]
        if act == "settings":
            return [struct.pack(">B", 6)]
        return [struct.pack(">B", 7)]

    if mtype == "paste":
        text = str(msg.get("text", ""))
        if not text:
            return []
        raw = text.encode("utf-8")[:65536]
        seq = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
        paste_flag = 1 if msg.get("paste", True) else 0
        pkt = struct.pack(">BqBI", 9, seq, paste_flag, len(raw)) + raw
        return [pkt]

    if mtype == "text":
        text = str(msg.get("text", ""))
        if not text:
            return []
        raw = text.encode("utf-8")[:300]
        return [struct.pack(">BI", 1, len(raw)) + raw]

    if mtype == "screen_power":
        on = 1 if msg.get("on", True) else 0
        return [struct.pack(">BB", 10, on)]

    if mtype == "rotate":
        return [struct.pack(">B", 11)]

    return []


class ScrcpySession:
    def __init__(self, ws: WebSocketConn, max_size: int = 1080, max_fps: int = 30, bitrate: int = 4000000):
        self.ws = ws
        self.max_size = max(480, min(1920, int(max_size)))
        self.max_fps = max(10, min(60, int(max_fps)))
        self.bitrate = max(500000, min(16000000, int(bitrate)))
        self.scid = f"{secrets.randbits(31):08x}"
        self.proc = None
        self.vsock = None
        self.csock = None
        self.stop_event = threading.Event()
        self._ctrl_lock = threading.Lock()

    def stop(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        for s in (self.vsock, self.csock):
            if s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.ws.close()

    def start_and_run(self):
        if not _ensure_android_jar():
            self.ws.send_json({"type": "error", "message": "未找到 scrcpy-server.jar"})
            self.stop()
            return

        env = _collect_android_env()
        # Keep i-frame interval short & repeat static frames every 150ms so JMuxer renders immediately
        codec_opts = "i-frame-interval=1,repeat-previous-frame-after=150000"
        cmd = [
            NSENTER_BIN, "-t", "1", "-m", "--", "/system/bin/app_process64",
            "/", "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
            f"scid={self.scid}",
            "log_level=warn",
            "tunnel_forward=true",
            "audio=false",
            "video=true",
            "control=true",
            f"max_size={self.max_size}",
            f"max_fps={self.max_fps}",
            f"video_bit_rate={self.bitrate}",
            f"video_codec_options={codec_opts}",
            "send_dummy_byte=true",
            "send_device_meta=true",
            "send_codec_meta=true",
            "send_frame_meta=true",
            "stay_awake=false",
            "cleanup=false",
        ]
        try:
            self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as exc:
            self.ws.send_json({"type": "error", "message": f"启动 scrcpy-server 失败: {exc}"})
            self.stop()
            return

        # Connect video + control abstract Unix sockets
        sock_name = f"\x00scrcpy_{self.scid}"
        for _ in range(50):
            if self.stop_event.is_set() or self.proc.poll() is not None:
                break
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(4.0)
                s.connect(sock_name)
                self.vsock = s
                break
            except OSError:
                try:
                    s.close()
                except Exception:
                    pass
                time.sleep(0.08)

        if self.vsock is None:
            err_out = ""
            try:
                err_out = (self.proc.stdout.read() or b"").decode("utf-8", errors="ignore").strip()
            except Exception:
                pass
            self.ws.send_json({"type": "error", "message": f"连接投屏服务失败 {err_out[:200]}"})
            self.stop()
            return

        try:
            self.csock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.csock.settimeout(4.0)
            self.csock.connect(sock_name)
            self.vsock.settimeout(None)
            self.csock.settimeout(None)
        except OSError as exc:
            self.ws.send_json({"type": "error", "message": f"连接控制通道失败: {exc}"})
            self.stop()
            return

        # Handshake metadata on video socket
        try:
            _recv_exact(self.vsock, 1)  # dummy byte
            dev_name = _recv_exact(self.vsock, 64).split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            codec_raw, width, height = struct.unpack(">4sII", _recv_exact(self.vsock, 12))
            self.ws.send_json({
                "type": "meta",
                "device": dev_name,
                "codec": codec_raw.decode("ascii", errors="ignore"),
                "width": width,
                "height": height,
            })
        except Exception as exc:
            self.ws.send_json({"type": "error", "message": f"读取视频头失败: {exc}"})
            self.stop()
            return

        # Start control drain thread (so Android device->client clipboard messages don't block csock)
        threading.Thread(target=self._drain_ctrl_in, daemon=True).start()
        # Start video pump thread (scrcpy video -> WebSocket binary)
        vt = threading.Thread(target=self._video_loop, daemon=True)
        vt.start()

        # Run WebSocket receiver in current handler thread (browser -> scrcpy control)
        try:
            self._ws_recv_loop()
        finally:
            self.stop()
            vt.join(timeout=1.0)

    def _drain_ctrl_in(self):
        try:
            while not self.stop_event.is_set() and self.csock:
                data = self.csock.recv(4096)
                if not data:
                    break
        except OSError:
            pass

    def _video_loop(self):
        last_cfg = b""
        try:
            while not self.stop_event.is_set():
                pts_flags, pkt_len = struct.unpack(">QI", _recv_exact(self.vsock, 12))
                if pkt_len <= 0 or pkt_len > 16 * 1024 * 1024:
                    continue
                payload = _recv_exact(self.vsock, pkt_len)
                is_cfg = bool(pts_flags & (1 << 63))
                is_key = bool(pts_flags & (1 << 62))
                if is_cfg:
                    last_cfg = payload
                    continue
                if is_key and last_cfg:
                    self.ws.send_binary(last_cfg + payload)
                else:
                    self.ws.send_binary(payload)
        except Exception:
            pass
        finally:
            self.stop()

    def _ws_recv_loop(self):
        try:
            while not self.stop_event.is_set():
                opcode, payload = self.ws.recv_frame()
                if opcode == 0x8:  # Close
                    break
                if opcode == 0x9:  # Ping
                    self.ws.send_pong(payload)
                    continue
                if opcode == 0xA:  # Pong
                    continue
                if opcode == 0x1 and payload:
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except Exception:
                        continue
                    pkts = _encode_control(msg)
                    if pkts and self.csock:
                        with self._ctrl_lock:
                            for pkt in pkts:
                                self.csock.sendall(pkt)
        except Exception:
            pass


def handle_websocket_upgrade(handler, qs: dict):
    """Perform RFC 6455 upgrade on BaseHTTPRequestHandler and run scrcpy bridge."""
    global _active_session

    token = (qs.get("token") or [""])[0]
    if not consume_ticket(token):
        handler.send_json(401, {"ok": False, "error": "Invalid or expired WebSocket ticket"})
        return

    ws_key = handler.headers.get("Sec-WebSocket-Key", "").strip()
    if not ws_key:
        handler.send_error(400, "Missing Sec-WebSocket-Key")
        return

    accept = base64.b64encode(hashlib.sha1((ws_key + WS_MAGIC).encode("ascii")).digest()).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")
    handler.connection.sendall(resp)
    handler.close_connection = True

    try:
        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    max_size = int((qs.get("max_size") or ["1080"])[0])
    max_fps = int((qs.get("max_fps") or ["30"])[0])
    bitrate = int((qs.get("bitrate") or ["4000000"])[0])

    ws = WebSocketConn(handler.connection)
    session = ScrcpySession(ws, max_size=max_size, max_fps=max_fps, bitrate=bitrate)

    with _session_lock:
        old = _active_session
        _active_session = session
    if old is not None:
        old.stop()

    try:
        session.start_and_run()
    finally:
        with _session_lock:
            if _active_session is session:
                _active_session = None
