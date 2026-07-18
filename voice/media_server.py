"""HTTP server for frontend media assets and kiosk API routes. Runs on port 8080."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs, urlparse

from voice.facebook_feed import get_facebook_posts
from voice.map_api import get_map_graph, reload_navigator, save_map_graph
from voice.upload_handler import get_upload_status, handle_upload_poster, serve_image
from voice.weather_feed import get_weather

if TYPE_CHECKING:
    from core.blackboard import Blackboard
    from voice.map_navigation import MapNavigator


class MediaServer:
    """Serve static media from assets/ and kiosk JSON APIs for the v2 frontend."""

    def __init__(
        self,
        assets_dir: Path,
        app_dir: Path | None = None,
        port: int = 8080,
        host: str = "0.0.0.0",
        *,
        kiosk_config: dict | None = None,
        map_navigator: MapNavigator | None = None,
        on_reindex: Callable[[], None] | None = None,
        blackboard: Blackboard | None = None,
    ):
        self.assets_dir = assets_dir
        self.app_dir = app_dir or assets_dir.parent
        self.port = port
        self.host = host
        self.server: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._server_host: str | None = None
        self.map_navigator = map_navigator
        self.on_reindex = on_reindex
        self.blackboard = blackboard

        kiosk = kiosk_config or {}
        self.facebook_cache_minutes = int(kiosk.get("facebook_cache_minutes", 10))
        self.weather_cache_minutes = int(kiosk.get("weather_cache_minutes", 10))
        cats = kiosk.get("upload_categories") or ["events", "competitions", "posts"]
        self.upload_categories = tuple(str(c) for c in cats)

        self.data_dir = self.app_dir / "data"
        self.cache_dir = self.app_dir / "voice"
        self.facebook_cache_path = self.app_dir / ".facebook-cache.json"
        self.weather_cache_path = self.cache_dir / ".weather-cache.json"
        self.extracted_events_path = self.app_dir / "voice" / "event_db" / "extracted_events.json"

    def set_map_navigator(self, navigator: MapNavigator | None) -> None:
        self.map_navigator = navigator

    def set_on_reindex(self, callback: Callable[[], None] | None) -> None:
        self.on_reindex = callback

    def set_blackboard(self, blackboard: Blackboard | None) -> None:
        self.blackboard = blackboard

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    def start(self) -> None:
        if self.server is not None and self.thread is not None and self.thread.is_alive():
            print(f"Media server already running on port {self.port}")
            return

        self._server_host = self._get_local_ip() if self.host == "0.0.0.0" else self.host
        parent_dir = self.app_dir
        media_server = self

        class AssetHandler(BaseHTTPRequestHandler):
            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")

            def _json_response(self, status: int, payload: dict | list) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _bytes_response(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:
                self.send_response(200)
                self._cors()
                self.end_headers()

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/":
                    self._serve_index()
                elif path == "/api/network-ip":
                    self._json_response(200, {"ip": media_server._get_local_ip()})
                elif path == "/api/voice-config":
                    local_speaker = False
                    local_mic = False
                    if media_server.blackboard is not None:
                        state = media_server.blackboard.read(
                            "local_speaker_active",
                            "local_mic_active",
                        )
                        local_speaker = bool(state.get("local_speaker_active"))
                        local_mic = bool(state.get("local_mic_active"))
                    self._json_response(
                        200,
                        {"localSpeaker": local_speaker, "localMic": local_mic},
                    )
                elif path == "/api/upload-status":
                    status = get_upload_status(
                        media_server.assets_dir,
                        media_server.extracted_events_path,
                        media_server.upload_categories,
                    )
                    self._json_response(200, status)
                elif path == "/api/facebook":
                    posts = get_facebook_posts(
                        media_server.facebook_cache_path,
                        media_server.facebook_cache_minutes,
                    )
                    self._json_response(200, posts)
                elif path == "/api/weather":
                    weather = get_weather(
                        media_server.weather_cache_path,
                        media_server.weather_cache_minutes,
                    )
                    self._json_response(200, weather)
                elif path == "/api/map":
                    graph = get_map_graph(media_server.data_dir, self.path)
                    self._json_response(200, graph)
                elif path == "/api/image":
                    params = parse_qs(urlparse(self.path).query)
                    rel = (params.get("path") or [""])[0]
                    code, body, ctype = serve_image(media_server.assets_dir, rel)
                    self._bytes_response(code, body, ctype)
                else:
                    self._serve_static(parent_dir)

            def do_POST(self) -> None:
                path = urlparse(self.path).path

                if path == "/api/upload-poster":
                    try:
                        code, payload = handle_upload_poster(
                            self.rfile,
                            self.headers,
                            media_server.assets_dir,
                            media_server.upload_categories,
                        )
                        if code == 200 and media_server.on_reindex:
                            try:
                                media_server.on_reindex()
                            except Exception as exc:
                                print(f"[MediaServer] post-upload reindex failed: {exc}")
                        self._json_response(code, payload)
                    except Exception as exc:
                        self._json_response(500, {"error": str(exc)})
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""

                if path == "/trigger-index":
                    try:
                        if media_server.on_reindex:
                            media_server.on_reindex()
                        self._bytes_response(200, b"OK", "text/plain")
                    except Exception as exc:
                        print(f"[MediaServer] trigger-index error: {exc}")
                        self._bytes_response(500, str(exc).encode(), "text/plain")
                    return
                    
                if path == "/api/mic-clicked":
                    print("[MediaServer] Received /api/mic-clicked !!")
                    if media_server.blackboard is not None:
                        media_server.blackboard.write(voice_session_active=True)
                    self._json_response(200, {"success": True})
                    return

                if path == "/api/mic-aborted":
                    print("[MediaServer] Received /api/mic-aborted !!")
                    if media_server.blackboard is not None:
                        media_server.blackboard.write(voice_session_active=False)
                    self._json_response(200, {"success": True})
                    return

                if path == "/api/eye-color":
                    try:
                        from core.eye_themes import resolve_eye_color

                        data = json.loads(body.decode("utf-8"))
                        theme = data.get("theme", "default")
                        rgb = resolve_eye_color(theme)
                        if media_server.blackboard is None:
                            self._json_response(
                                503, {"error": "Robot blackboard not available"}
                            )
                            return
                        media_server.blackboard.write(eye_color=rgb)
                        print(f"[MediaServer] eye-color -> {theme!r} {rgb}")
                        self._json_response(
                            200,
                            {"success": True, "theme": theme, "rgb": list(rgb)},
                        )
                    except Exception as exc:
                        self._json_response(500, {"error": str(exc)})
                    return

                if path == "/api/map":
                    try:
                        def _on_saved(floor: str) -> None:
                            reload_navigator(media_server.map_navigator, floor)

                        result = save_map_graph(
                            media_server.data_dir,
                            self.path,
                            body,
                            on_saved=_on_saved,
                        )
                        self._json_response(200, result)
                    except Exception as exc:
                        self._json_response(500, {"error": "Failed to save map data", "detail": str(exc)})
                    return

                self.send_response(404)
                self._cors()
                self.end_headers()

            def _serve_index(self) -> None:
                html = f"""
                <html><head><title>Voice Agent Media Server</title></head>
                <body style="font-family:sans-serif;padding:20px">
                <h1>Voice Agent Media Server</h1>
                <p>Serving events, competitions, posts, maps, and kiosk APIs on port {media_server.port}.</p>
                <p>Robot debug dashboard: <code>http://&lt;pi-ip&gt;:8082/</code></p>
                <ul>
                  <li><code>GET /api/network-ip</code></li>
                  <li><code>GET /api/facebook</code></li>
                  <li><code>GET /api/weather</code></li>
                  <li><code>GET/POST /api/map?floor=floor_1</code></li>
                  <li><code>POST /api/upload-poster</code></li>
                  <li><code>GET /api/upload-status</code></li>
                  <li><code>POST /api/eye-color</code></li>
                  <li><code>POST /trigger-index</code></li>
                </ul>
                </body></html>
                """
                self._bytes_response(200, html.encode(), "text/html")

            def _serve_static(self, base_dir: Path) -> None:
                try:
                    if self.path.startswith("/assets/"):
                        file_path = base_dir / self.path[1:]
                    else:
                        file_path = base_dir / self.path.lstrip("/")

                    file_path.resolve().relative_to(base_dir.resolve())

                    if not file_path.is_file():
                        self._bytes_response(404, b"File not found", "text/plain")
                        return

                    suffix = file_path.suffix.lower()
                    content_type = {
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".png": "image/png",
                        ".gif": "image/gif",
                        ".webp": "image/webp",
                    }.get(suffix, "application/octet-stream")
                    self._bytes_response(200, file_path.read_bytes(), content_type)
                except ValueError:
                    self._bytes_response(403, b"Forbidden", "text/plain")
                except Exception as exc:
                    self._bytes_response(500, f"Error: {exc}".encode(), "text/plain")

            def log_message(self, format, *args) -> None:
                pass

        try:
            self.server = HTTPServer((self.host, self.port), AssetHandler)
        except OSError as e:
            if e.errno == 98:
                print(f"Port {self.port} already in use (media server)")
                return
            raise

        def serve() -> None:
            print(f"Media server started: http://{self._server_host}:{self.port}")
            self.server.serve_forever()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            print("Media server stopped")

    def get_image_url(self, category: str, filename: str) -> str:
        host = self._server_host or self._get_local_ip()
        return f"http://{host}:{self.port}/assets/{category}/{filename}"


# Backward-compatible alias
ImageServer = MediaServer
