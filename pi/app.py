"""Web server for controlling the Drone Boat and streaming Raspberry Pi AI Camera."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
import socket
from urllib.parse import parse_qs, unquote, urlsplit

import config
from camera_manager import CameraManager
from ev3_bridge import EV3Bridge

MEDIA_ROUTE_RE = re.compile(r"^/(?:media|api/media)/(photos|videos)/([^/]+)$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

EXTRA_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
    ".mjpg": "video/x-motion-jpeg",
}


def _guess_mime(path):
    _, ext = os.path.splitext(path.lower())
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _parse_byte_range(header_val, total_size):
    """Parse HTTP Range header into (start, end) inclusive or None if invalid."""
    if not header_val:
        return None
    match = RANGE_RE.match(header_val.strip())
    if not match or total_size <= 0:
        return None
    start_str, end_str = match.group(1), match.group(2)
    if not start_str and not end_str:
        return None

    if not start_str:
        suffix = int(end_str)
        if suffix <= 0:
            return None
        start = max(0, total_size - suffix)
        end = total_size - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str else total_size - 1

    if start >= total_size or end < start:
        return "unsatisfiable"
    end = min(end, total_size - 1)
    return (start, end)


class BoatHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying shared camera and EV3 bridge instances."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, camera, ev3, static_dir):
        self.camera = camera
        self.ev3 = ev3
        self.static_dir = static_dir
        super().__init__(server_address, handler_cls)


class BoatRequestHandler(BaseHTTPRequestHandler):
    """Serves the mobile-first FPV cockpit UI, MJPEG stream, and control API."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Suppress rapid polling/drive log noise; keep non-2xx/3xx errors visible.
        if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length_str = self.headers.get("Content-Length", "0")
        try:
            length = int(length_str)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 65536))
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return None

    def _serve_static_file(self, filename):
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(404, {"error": "not_found"})
            return
        full_path = os.path.join(self.server.static_dir, filename)
        if not os.path.isfile(full_path):
            self._send_json(404, {"error": "not_found"})
            return
        with open(full_path, "rb") as handle:
            data = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", _guess_mime(full_path))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_video_feed(self):
        self.send_response(200)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"
        )
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            for chunk in self.server.camera.frame_generator():
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_media(self, kind, filename, download):
        full_path = self.server.camera.resolve_media_file(kind, filename)
        if full_path is None:
            self._send_json(404, {"error": "not_found"})
            return

        total_size = os.path.getsize(full_path)
        range_res = _parse_byte_range(self.headers.get("Range"), total_size)
        if range_res == "unsatisfiable":
            self.send_response(416)
            self.send_header("Content-Range", "bytes */{0}".format(total_size))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if isinstance(range_res, tuple):
            start, end = range_res
            status_code = 206
        else:
            start, end = 0, max(0, total_size - 1)
            status_code = 200

        length = (end - start + 1) if total_size > 0 else 0
        self.send_response(status_code)
        self.send_header("Content-Type", _guess_mime(full_path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status_code == 206:
            self.send_header(
                "Content-Range",
                "bytes {0}-{1}/{2}".format(start, end, total_size),
            )
        if download:
            self.send_header(
                "Content-Disposition",
                'attachment; filename="{0}"'.format(filename),
            )
        self.end_headers()

        if length <= 0:
            return
        with open(full_path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/":
                self._serve_static_file("index.html")
                return
            if path.startswith("/static/"):
                self._serve_static_file(path[len("/static/"):])
                return
            if path == "/video_feed":
                self._serve_video_feed()
                return
            if path == "/api/status":
                self._send_json(
                    200,
                    {
                        "ev3": self.server.ev3.get_status(),
                        "camera": self.server.camera.get_status(),
                        "media_count": len(self.server.camera.list_media()),
                    },
                )
                return
            if path == "/api/media":
                self._send_json(200, {"items": self.server.camera.list_media()})
                return
            if path.startswith("/media/"):
                match = MEDIA_ROUTE_RE.match(path)
                if not match:
                    self._send_json(404, {"error": "not_found"})
                    return
                query = parse_qs(parsed.query)
                download = query.get("download", ["0"])[0] == "1"
                self._serve_media(match.group(1), match.group(2), download)
                return
            self._send_json(404, {"error": "not_found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/drive":
                body = self._read_json_body()
                if body is None:
                    self._send_json(400, {"error": "invalid_json"})
                    return
                if "left" in body or "right" in body:
                    try:
                        left = int(body.get("left", 0))
                        right = int(body.get("right", 0))
                    except (TypeError, ValueError):
                        self._send_json(400, {"error": "invalid_motor_values"})
                        return
                    actual_left, actual_right = self.server.ev3.set_motors(left, right)
                elif "x" in body or "y" in body:
                    try:
                        x = float(body.get("x", 0.0))
                        y = float(body.get("y", 0.0))
                    except (TypeError, ValueError):
                        self._send_json(400, {"error": "invalid_joystick_values"})
                        return
                    actual_left, actual_right = self.server.ev3.set_joystick(x, y)
                else:
                    self._send_json(400, {"error": "missing_drive_params"})
                    return
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "left_speed": actual_left,
                        "right_speed": actual_right,
                    },
                )
                return

            if path == "/api/stop":
                self.server.ev3.stop()
                self._send_json(
                    200, {"ok": True, "left_speed": 0, "right_speed": 0}
                )
                return

            if path == "/api/heartbeat":
                self.server.ev3.heartbeat()
                self._send_json(200, {"ok": True})
                return

            if path == "/api/ai":
                body = self._read_json_body()
                if body is None:
                    self._send_json(400, {"error": "invalid_json"})
                    return
                if "enabled" in body:
                    new_state = bool(body["enabled"])
                else:
                    new_state = not self.server.camera.get_status()["ai_enabled"]
                enabled = self.server.camera.set_ai_enabled(new_state)
                self._send_json(200, {"ok": True, "ai_enabled": enabled})
                return

            if path == "/api/photo":
                try:
                    item = self.server.camera.capture_photo()
                except RuntimeError as exc:
                    self._send_json(503, {"error": str(exc)})
                    return
                self._send_json(201, {"ok": True, "item": item})
                return

            if path == "/api/video/start":
                status = self.server.camera.start_recording()
                self._send_json(200, {"ok": True, "camera": status})
                return

            if path == "/api/video/stop":
                item = self.server.camera.stop_recording()
                self._send_json(200, {"ok": True, "item": item})
                return

            self._send_json(404, {"error": "not_found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_DELETE(self):
        path = unquote(urlsplit(self.path).path)
        try:
            if path.startswith("/api/media/"):
                match = MEDIA_ROUTE_RE.match(path)
                if not match:
                    self._send_json(404, {"error": "not_found"})
                    return
                ok, reason = self.server.camera.delete_media(
                    match.group(1), match.group(2)
                )
                if ok:
                    self._send_json(200, {"ok": True})
                elif reason == "recording_active":
                    self._send_json(409, {"error": reason})
                else:
                    self._send_json(404, {"error": reason})
                return
            self._send_json(404, {"error": "not_found"})
        except (BrokenPipeError, ConnectionResetError):
            pass


def create_server(
    host=config.WEB_HOST,
    port=config.WEB_PORT,
    camera=None,
    ev3=None,
    static_dir=config.STATIC_DIR,
):
    """Create and configure a BoatHTTPServer instance."""
    if camera is None:
        camera = CameraManager()
    if ev3 is None:
        ev3 = EV3Bridge(
            host=config.EV3_HOST,
            port=config.EV3_PORT,
            discovery_port=config.EV3_DISCOVERY_PORT,
            max_speed=config.MAX_SPEED,
            command_timeout_s=config.COMMAND_TIMEOUT_S,
            mock=config.MOCK_MODE,
        )
    return BoatHTTPServer((host, port), BoatRequestHandler, camera, ev3, static_dir)


def _best_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def main():
    """Start camera, EV3 bridge, and web server until interrupted."""
    config.ensure_media_dirs()
    camera = CameraManager()
    ev3 = EV3Bridge(
        host=config.EV3_HOST,
        port=config.EV3_PORT,
        discovery_port=config.EV3_DISCOVERY_PORT,
        max_speed=config.MAX_SPEED,
        command_timeout_s=config.COMMAND_TIMEOUT_S,
        mock=config.MOCK_MODE,
    )
    camera.start()
    ev3.start()
    server = create_server(config.WEB_HOST, config.WEB_PORT, camera=camera, ev3=ev3)
    host_ip = _best_local_ip()
    print("Drone Boat Cockpit listening on http://{0}:{1}".format(host_ip, config.WEB_PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Drone Boat server...")
    finally:
        server.server_close()
        ev3.close()
        camera.close()


if __name__ == "__main__":
    main()
