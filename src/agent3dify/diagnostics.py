"""Bounded, UTF-8 diagnostic journals. Never persist credentials or image payloads."""
from __future__ import annotations

import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from importlib.metadata import version
from typing import Any


def sanitize(value: Any, secret: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if str(k).lower() != "has_api_key" and re.search(r"api[_-]?key|authorization|password|secret|access[_-]?token|^base64$", str(k), re.I)
            else sanitize(v, secret) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, secret) for item in value]
    if not isinstance(value, (str, int, float, bool, type(None))):
        value = str(value)
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[API key]")
        value = re.sub(r"sk-[a-zA-Z0-9_-]+", "[API key]", value)
        value = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
        value = re.sub(r"data:image/[^;\s]+;base64,[a-zA-Z0-9+/=\s]+", "[image omitted]", value)
        # LangChain image blocks use a bare base64 field instead of a data URL.
        value = re.sub(r"(['\"]base64['\"]\s*:\s*['\"])[a-zA-Z0-9+/=]+", r"\1[image omitted]", value)
    return value


def exception_details(exc: BaseException) -> dict:
    response = getattr(exc, "response", None)
    return {
        "exception": type(exc).__name__, "message": str(exc),
        "status_code": getattr(exc, "status_code", None),
        "request_id": getattr(exc, "request_id", None) or (response.headers.get("x-request-id") if response is not None else None),
        "traceback": "".join(traceback.format_exception(exc)),
    }


class Journal:
    def __init__(self, root: Path, secret: str = ""):
        self.root = root
        self.secret = secret
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.logger = logging.Logger(f"agent3dify.{id(self)}", logging.DEBUG)
        for name, level in (("events.jsonl", logging.DEBUG), ("errors.jsonl", logging.ERROR)):
            handler = RotatingFileHandler(root / name, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setLevel(level)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def write(self, kind: str, *, error: bool = False, **fields: Any) -> None:
        record = sanitize({"time": time.time(), "kind": kind, **fields}, self.secret)
        with self.lock:
            self.logger.log(logging.ERROR if error else logging.INFO, json.dumps(record, ensure_ascii=False))

    def close(self) -> None:
        with self.lock:
            for handler in list(self.logger.handlers):
                handler.close()
                self.logger.removeHandler(handler)


def runtime_info() -> dict:
    packages = {}
    for name in ("agent3dify", "deepagents", "langchain-openai", "openai", "httpx", "cadquery"):
        try:
            packages[name] = version(name)
        except Exception:
            packages[name] = "unknown"
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def diagnostic_zip(session) -> bytes:
    """Only explicit diagnostic files, never .env, drawings or arbitrary workspace files."""
    output = io.BytesIO()
    with session.lock, session.journal.lock, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        snapshot = session.snapshot()
        for name, value in (("session.json", snapshot), ("runtime.json", runtime_info()),
                            ("connection.json", session.provider.public())):
            archive.writestr(name, json.dumps(sanitize(value, session.provider.api_key), ensure_ascii=False, indent=2))
        for path in session.journal.root.glob("*.jsonl*"):
            if path.is_file() and not path.is_symlink():
                archive.writestr("logs/" + path.name, sanitize(path.read_text(encoding="utf-8"), session.provider.api_key))
    return output.getvalue()
