"""Local interactive CAD workbench. Run with ``agent3dify --web``."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv, set_key

from .provider import Provider, normalize_base_url
from .web_sessions import SessionManager
from .diagnostics import diagnostic_zip, exception_details


WEB_ROOT = Path(__file__).parent / "resources" / "web"
SAMPLE_PATH = Path(__file__).parent / "resources" / "sample.png"
MAX_BODY = 52 * 1024 * 1024


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], manager: SessionManager, env_path: Path):
        self.manager = manager
        self.env_path = env_path
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: WebServer
    server_version = "Agent3dify/1.0"

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str, *, download: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _file(self, path: Path, *, download: bool = False) -> None:
        if not path.is_file():
            self._json(404, {"error": "文件不存在"})
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "application/javascript"
        self._send(200, path.read_bytes(), content_type, download=path.name if download else None)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            parts = unquote(parsed.path).strip("/").split("/")
            query = parse_qs(parsed.query)
            manager = self.server.manager
            if parsed.path == "/api/config":
                self._json(200, manager.provider.public() | {"sample_available": SAMPLE_PATH.exists()})
            elif parsed.path == "/api/models":
                self._json(200, {"models": manager.provider.list_models()})
            elif parsed.path == "/api/sessions":
                with manager.lock:
                    sessions = sorted((s.summary() for s in manager.sessions.values()), key=lambda s: s["updated"], reverse=True)
                self._json(200, {"sessions": sessions})
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "diagnostics":
                session = manager.sessions.get(parts[2])
                if session is None:
                    self._json(404, {"error": "会话不存在"})
                else:
                    self._send(200, diagnostic_zip(session), "application/zip", download=f"agent3dify-{session.id[:8]}-diagnostics.zip")
            elif len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                session = manager.sessions.get(parts[2])
                if session is None:
                    self._json(404, {"error": "会话不存在"})
                else:
                    self._json(200, session.snapshot(int(query.get("after", ["-1"])[0])))
            elif len(parts) >= 4 and parts[:2] == ["api", "artifacts"]:
                session = manager.sessions.get(parts[2])
                relative = "/".join(parts[3:])
                if session is None:
                    self._json(404, {"error": "会话不存在"})
                    return
                if parts[3] not in {"input", "artifacts", "generated", "preprocessed", "review"} or any(p.startswith(".") or ":" in p or "\\" in p for p in parts[3:]):
                    self._json(403, {"error": "不允许访问此文件"})
                    return
                path = session.workspace.resolve_path(relative)
                self._file(path, download="download" in query)
            elif parsed.path == "/api/sample":
                self._file(SAMPLE_PATH)
            elif parsed.path == "/api/health":
                self._json(200, {"ok": True})
            elif parsed.path.startswith("/api/"):
                self._json(404, {"error": "接口不存在"})
            else:
                relative = unquote(parsed.path).lstrip("/") or "index.html"
                path = (WEB_ROOT / relative).resolve()
                path.relative_to(WEB_ROOT.resolve())
                self._file(path)
        except (ValueError, OSError) as exc:
            self._error(400, exc)
        except Exception as exc:
            self._error(502, exc)

    def do_POST(self) -> None:
        try:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                self._json(403, {"error": "不允许跨站请求"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(415, {"error": "仅接受 application/json"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                self._json(413, {"error": "请求过大或没有内容"})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("无效的 JSON 请求")
            parts = urlparse(self.path).path.strip("/").split("/")
            manager = self.server.manager
            if parts == ["api", "sessions"]:
                session = manager.create(payload)
                self._json(202, {"id": session.id})
            elif parts == ["api", "settings"]:
                current = manager.provider
                base_url = normalize_base_url(str(payload.get("base_url") or current.base_url))
                api_key = str(payload.get("api_key") or current.api_key).strip()
                model = str(payload.get("model") or current.model).strip().removeprefix("openai:")
                if not api_key or not model:
                    raise ValueError("请填写模型和 API Key")
                provider = Provider(base_url, api_key, model)
                with manager.lock:
                    self.server.env_path.parent.mkdir(parents=True, exist_ok=True)
                    for key, value in {"OPENAI_BASE_URL": base_url, "OPENAI_API_KEY": api_key, "SUPERVISOR_MODEL": model}.items():
                        set_key(str(self.server.env_path), key, value)
                    manager.provider = provider
                    for session in manager.sessions.values():
                        with session.lock:
                            if session.status not in {"running", "stopping"}:
                                session.provider = Provider(base_url, api_key, session.model)
                                if session.model != model:
                                    session.model = model
                                    session.provider = provider
                                    session.emit("status", f"连接设置已更新，后续执行使用 {model}")
                                session.agent = None
                                session.recovered = True
                                session.save()
                self._json(200, provider.public())
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                session = manager.sessions.get(parts[2])
                if session is None:
                    self._json(404, {"error": "会话不存在"})
                elif parts[3] == "messages":
                    manager.enqueue(session, str(payload.get("message", "")))
                    self._json(202, {"ok": True})
                elif parts[3] == "stop":
                    manager.stop(session)
                    self._json(202, {"ok": True})
                else:
                    self._json(404, {"error": "接口不存在"})
            else:
                self._json(404, {"error": "接口不存在"})
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self._error(400, exc)
        except Exception as exc:
            self._error(500, exc)

    def _error(self, status: int, exc: Exception) -> None:
        error_id = uuid.uuid4().hex[:12]
        manager = self.server.manager
        manager.journal.secret = manager.provider.api_key
        manager.journal.write("http_error", error=True, error_id=error_id, method=self.command, path=urlparse(self.path).path, **exception_details(exc))
        self._json(status, {"error": manager.provider.redact(str(exc)), "error_id": error_id})

    def log_message(self, format: str, *args: object) -> None:
        # Polling is frequent; journal failures and explicit server errors only.
        if len(args) > 1 and str(args[1]).isdigit() and int(str(args[1])) >= 400:
            self.server.manager.journal.write("http_status", error=True, method=self.command, path=urlparse(self.path).path, status=int(str(args[1])))


def serve(host: str = "127.0.0.1", port: int = 7860, *, workspace: Path | None = None) -> int:
    env_path = Path.cwd() / ".env"
    load_dotenv(env_path)
    if not (WEB_ROOT / "index.html").exists():
        raise RuntimeError("未找到前端构建产物，请在 frontend 目录执行 npm ci 和 npm run build")
    manager = SessionManager(workspace or Path.cwd() / "drawing_to_cad_workspace" / "web", Provider.from_env())
    server = WebServer((host, port), manager, env_path)
    print(f"Agent3dify: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent3dify interactive CAD workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    return serve(args.host, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
