from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import platform
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .agent_factory import build_agent
from .app import _invoke_with_progress
from .config import AgentModels
from .diagnostics import Journal, exception_details, runtime_info, sanitize
from .progress import ROOT_AGENT_NAME
from .prompts import MAIN_USER_PROMPT
from .provider import Provider
from .shell_backend import AgentShellBackend
from .workspace import Workspace, prepare_local_workspace


def message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(message_text(item) for item in value if isinstance(item, (dict, str)))
    if isinstance(value, dict):
        if value.get("type") in {"image", "image_url"}:
            return "[图像已传递给模型]"
        return message_text(value.get("text", value.get("content", "")))
    return message_text(getattr(value, "content", ""))


def prepare_images(workspace: Workspace, images: list[dict]) -> list[str]:
    if not 1 <= len(images) <= 3:
        raise ValueError("请上传 1 张组合三视图，或最多 3 张独立视图")
    loaded = []
    for item in images:
        encoded = str(item.get("data", "")).split(",")[-1]
        try:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 12 * 1024 * 1024:
                raise ValueError("单张图片请小于 12 MB")
            image = Image.open(io.BytesIO(raw))
            if image.width * image.height > 24_000_000:
                raise ValueError("图片分辨率过大，请缩小后上传")
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2400, 2400))
            loaded.append(image)
        except (UnidentifiedImageError, OSError, base64.binascii.Error) as exc:
            raise ValueError("无法读取图片，请上传有效的 PNG / JPG / WEBP 图片") from exc
    # prepare_local_workspace is destructive, so it is used only with a fresh UUID directory.
    first = io.BytesIO()
    loaded[0].save(first, format="PNG")
    temp = workspace.root.parent / f".{workspace.root.name}.png"
    temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        temp.write_bytes(first.getvalue())
        prepare_local_workspace(workspace, temp)
    finally:
        temp.unlink(missing_ok=True)
    names = []
    for i, image in enumerate(loaded):
        name = f"input/view_{i + 1}.png"
        image.save(workspace.resolve_path(name))
        names.append(name)
    if len(loaded) > 1:
        board = Image.new("RGB", (sum(im.width for im in loaded) + 24 * (len(loaded) + 1), max(im.height for im in loaded) + 60), "white")
        draw = ImageDraw.Draw(board)
        x = 24
        for i, image in enumerate(loaded):
            draw.text((x, 12), f"View {i + 1} (input/view_{i + 1}.png)", fill="black")
            board.paste(image, (x, 40))
            x += image.width + 24
        board.save(workspace.resolve_path("input/reference.png"))
    return names


class Session:
    def __init__(self, workspace: Workspace, provider: Provider, *, title: str, session_id: str | None = None):
        self.id = session_id or workspace.root.name
        self.workspace = workspace
        self.provider = provider
        self.title = title[:80]
        self.model = provider.model
        self.created = time.time()
        self.updated = self.created
        self.status = "idle"
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.pending: list[dict] = []
        self.lock = threading.RLock()
        self.agent = None
        self.backend: AgentShellBackend | None = None
        self.future: Future | None = None
        self.stop_requested = False
        self.turn = 0
        self.recovered = False
        self.preview_signature = ""
        self.preview_error_signature = ""
        self.journal = Journal(workspace.root / ".logs", provider.api_key)
        self.clients: list = []

    def record(self, kind: str, *, error: bool = False, **fields: Any) -> None:
        self.journal.secret = self.provider.api_key
        self.journal.write(kind, error=error, session_id=self.id, turn=self.turn, model=self.model, **fields)

    def emit(self, kind: str, text: str, **extra: Any) -> None:
        with self.lock:
            event = sanitize({"id": self.events[-1]["id"] + 1 if self.events else 0, "kind": kind, "text": text, "time": time.time(), **extra}, self.provider.api_key)
            self.events.append(event)
            self.events = self.events[-1500:]
            self.record("event", error=kind == "error", event=event)

    def save(self) -> None:
        with self.lock:
            data = self.summary() | {"messages": self.messages, "events": self.events[-1500:], "turn": self.turn}
            path = self.workspace.root / ".session.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(sanitize(data, self.provider.api_key), ensure_ascii=False), encoding="utf-8")
            os.replace(temp, path)

    def summary(self) -> dict:
        with self.lock:
            return {"id": self.id, "title": self.title, "model": self.model, "created": self.created, "updated": self.updated, "status": self.status, "queue_count": len(self.pending)}

    def manifest(self) -> list[dict]:
        paths = [*self.workspace.list_interesting_files(), *self.workspace.root.glob("input/*.png")]
        files = []
        for path in sorted(set(paths)):
            try:
                stat = path.stat()
                if stat.st_size and not path.name.startswith("."):
                    files.append({"path": path.relative_to(self.workspace.root).as_posix(), "size": stat.st_size, "version": f"{stat.st_mtime_ns}-{stat.st_size}"})
            except OSError:
                continue
        return files

    def snapshot(self, after: int = -1) -> dict:
        with self.lock:
            return self.summary() | {"messages": list(self.messages), "events": [e for e in self.events if e["id"] > after], "files": self.manifest(), "turn": self.turn}


class SessionManager:
    def __init__(self, root: Path, provider: Provider, *, agent_factory=build_agent):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.journal = Journal(self.root / ".logs", provider.api_key)
        self.journal.write("startup", provider=provider.public(), runtime=runtime_info())
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.agent_factory = agent_factory
        self.loop = asyncio.new_event_loop()
        self.worker = threading.Thread(target=self.loop.run_forever, name="cad-agent-loop", daemon=True)
        self.worker.start()
        self._restore()

    def _restore(self) -> None:
        for path in self.root.glob("*/.session.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if path.parent.name != data["id"]:
                    continue
                session = Session(Workspace(path.parent), self.provider, title=data["title"])
                for attr in ("model", "created", "updated", "messages", "events", "turn"):
                    setattr(session, attr, data.get(attr, getattr(session, attr)))
                session.provider = Provider(self.provider.base_url, self.provider.api_key, session.model)
                session.status = data.get("status", "idle")
                if session.status in {"running", "stopping"}:
                    session.status = "stopped"
                    session.emit("status", "服务已重启；文件已保留，可发送消息继续建模。")
                for message in session.messages:
                    if message.get("status") == "queued":
                        message["status"] = "interrupted"
                session.recovered = True
                # Import old failures once, so existing failed tasks also have a diagnostic download.
                history_marker = session.journal.root / ".history-imported"
                if not history_marker.exists():
                    session.record("restored_session", status=session.status)
                    for event in session.events:
                        session.record("legacy_event", error=event.get("kind") == "error", event=event)
                    history_marker.touch()
                self.sessions[session.id] = session
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.journal.write("restore_error", error=True, path=str(path), **exception_details(exc))
                continue

    def create(self, payload: dict) -> Session:
        images = payload.get("images", [])
        if not isinstance(images, list):
            raise ValueError("无效的图片列表")
        session_id = uuid.uuid4().hex
        workspace = Workspace(self.root / session_id)
        prepare_images(workspace, images)
        provider = Provider(self.provider.base_url, self.provider.api_key, str(payload.get("model") or self.provider.model).removeprefix("openai:"))
        prompt = str(payload.get("message") or "根据三视图建立三维模型，保留尺寸信息并导出 STEP 和 STL。")
        session = Session(workspace, provider, title=str(payload.get("title") or images[0].get("name") or prompt))
        with self.lock:
            self.sessions[session.id] = session
        self.enqueue(session, prompt, initial=True)
        return session

    def enqueue(self, session: Session, text: str, *, initial: bool = False) -> None:
        text = text.strip()
        if not text or len(text) > 20000:
            raise ValueError("请输入 1–20000 字的建模要求")
        with session.lock:
            if session.status == "stopping":
                raise ValueError("正在停止当前执行，请稍后继续")
            if len(session.pending) >= 20:
                raise ValueError("待处理修改过多，请等待当前修改完成")
            message = {"id": uuid.uuid4().hex, "role": "user", "text": text, "time": time.time(), "status": "queued", "initial": initial}
            session.messages.append(message)
            session.pending.append(message)
            session.updated = time.time()
            session.emit("queue", "已收到建模要求" if initial else "修改意见已加入队列，将在当前轮完成后处理")
            if session.status != "running":
                session.stop_requested = False
                session.status = "running"
                session.future = asyncio.run_coroutine_threadsafe(self._drain(session), self.loop)
            session.save()

    def stop(self, session: Session) -> None:
        with session.lock:
            if session.status != "running":
                return
            session.status = "stopping"
            session.stop_requested = True
            session.emit("status", "正在停止代理与建模进程…")
            # Cancel the loop future immediately.  The monitor task handles the
            # underlying invocation and shell process on the event loop.
            if session.future is not None:
                session.future.cancel()
            backend = session.backend
        if backend is not None:
            threading.Thread(target=backend.stop, daemon=True).start()
        session.save()

    def _initialize(self, session: Session) -> None:
        session.backend = AgentShellBackend(str(session.workspace.root), log_callback=lambda **fields: session.record(**fields))
        models = AgentModels(supervisor=session.model, builder=session.model, verifier=session.model)
        instructions = (
            "\nInteractive CAD workbench requirements (take precedence over optional export guidance):\n"
            "- Respond to the user in Chinese. User changes override conflicting old review plans.\n"
            "- Always export artifacts/model.step AND artifacts/model.stl after each successful build or revision.\n"
            "- Export front/top/right SVG projections if possible; PNG is optional if Cairo is unavailable.\n"
            "- Use mm. Preserve existing geometry except the changes requested. Explain inferred dimensions.\n"
            "- Before editing an existing generated/model.py, preserve the prior version in generated/history.\n"
            "- Avoid claiming an export succeeded until the files exist and execution succeeded.\n"
            "- Use Python and the provided file tools; do not install packages.\n"
            f"- Host OS is {platform.system()}. Shell commands run in the workspace using the active Python environment. "
            "On Windows avoid Bash heredocs, /tmp paths and Unix-only commands.\n"
            "- File tools use virtual paths like /input/reference.png; Python and shell commands MUST use "
            "relative paths like input/reference.png, generated/model.py and artifacts/model.step.\n"
            "- read_file returns actual image content for PNG/JPG. Inspect it directly; do not repeatedly "
            "read the same image or print ASCII pixel maps as a substitute for visual understanding.\n"
        )
        session.record("agent_initialize", provider=session.provider.public())
        session.agent = self.agent_factory(
            session.workspace, models=models, debug=False,
            model_factory=lambda name: session.provider.chat_model(name, request_callback=lambda **fields: self._on_request(session, **fields), clients=session.clients),
            extra_instructions=instructions, backend=session.backend,
        )

    @staticmethod
    def _on_request(session: Session, **fields) -> None:
        status = fields.get("status_code")
        failure = fields.pop("error", None)
        session.record(error=bool(failure), request_error=failure, **fields)
        delay = fields.get("retry_in_seconds")
        if delay is not None:
            session.emit("retry", f"模型请求失败（HTTP {status or '连接异常'}），{delay:g} 秒后重试当前请求（第 {fields['attempt']} 次）；已完成的建模步骤保留。", request_id=fields["request_id"])

    async def _drain(self, session: Session) -> None:
        try:
            if session.agent is None:
                await self._close_clients(session)
                self._initialize(session)
            while True:
                with session.lock:
                    if session.stop_requested:
                        raise asyncio.CancelledError
                    if not session.pending:
                        session.status = "completed"
                        session.save()
                        return
                    message = session.pending.pop(0)
                    message["status"] = "processing"
                    session.turn += 1
                    session.emit("status", f"开始第 {session.turn} 轮建模")
                session.backend.reset_cancel()
                session.workspace.archive_generated_model()
                text = message["text"]
                if message.get("initial"):
                    text = MAIN_USER_PROMPT + "\n用户要求（优先）：\n" + text
                else:
                    # Old automated review suggestions must not undo the user's current changes.
                    text = (
                        "Continue this existing CAD workspace. Inspect /generated/model.py and /input/reference.png. "
                        "Apply the user's latest feedback, which overrides conflicting prior review recommendations. "
                        "Do not rebuild from scratch or reset other features. Export updated STEP and STL.\n用户修改意见：\n" + text
                    )
                restored = session.recovered
                if restored:
                    history = "\n".join(m["role"] + ": " + m["text"][:2000] for m in session.messages[-8:] if m is not message)
                    text = "This session was restored. Files are the source of truth. Previous conversation:\n" + history + "\n\n" + text
                    session.recovered = False
                # Send images as image blocks on initial/restored turns, never as log text.
                prompt = text
                if message.get("initial") or session.turn == 1 or restored:
                    prompt = [{"type": "text", "text": text}]
                    for path in sorted((session.workspace.root / "input").glob("view_*.png")):
                        prompt.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}})
                session.record("turn_start", message_id=message["id"], image_count=len(prompt) - 1 if isinstance(prompt, list) else 0)
                result = await self._invoke_turn(session, prompt)
                await asyncio.to_thread(self._ensure_preview, session)
                answer = message_text(result) if isinstance(result, str) else message_text(result.get("messages", [])[-1]) if result.get("messages") else "本轮已完成，请查看模型和输出文件。"
                with session.lock:
                    message["status"] = "done"
                    session.messages.append({"id": uuid.uuid4().hex, "role": "assistant", "text": session.provider.redact(answer), "time": time.time()})
                    session.updated = time.time()
                    session.workspace.archive_generated_model()
                    session.emit("status", "本轮建模完成" if (session.workspace.root / "artifacts/model.step").exists() else "本轮已结束，尚未生成有效模型；请查看代理回复。")
                    session.save()
        except asyncio.CancelledError:
            if session.backend:
                await asyncio.to_thread(session.backend.stop)
            with session.lock:
                session.status = "stopped"
                for m in session.messages:
                    if m.get("status") in {"queued", "processing"}:
                        m["status"] = "interrupted"
                session.pending.clear()
                session.emit("status", "已停止。已生成的模型与文件已保留。")
                # Cancelled tool calls can leave unfinished checkpoint messages; rebuild from files next time.
                session.agent = None
                session.recovered = True
                session.save()

        except Exception as exc:
            if session.backend:
                await asyncio.to_thread(session.backend.stop)
            with session.lock:
                session.status = "error"
                error = session.provider.redact(str(exc))
                session.record("turn_error", error=True, **exception_details(exc))
                session.emit("error", error[:4000])
                status = getattr(exc, "status_code", None)
                hint = "代理服务限流（429），重试后仍未恢复。" if status == 429 else "代理服务暂不可用（503），重试后仍未恢复。" if status == 503 else "模型请求超时或连接失败。" if "timeout" in type(exc).__name__.lower() or "connection" in type(exc).__name__.lower() else "本轮执行失败。"
                session.messages.append({"id": uuid.uuid4().hex, "role": "error", "text": hint + "已保留生成文件，可稍后发送消息继续。请点击“下载诊断日志”查看详情。\n" + error[:2000], "time": time.time()})
                for m in session.messages:
                    if m.get("status") in {"queued", "processing"}:
                        m["status"] = "interrupted"
                session.pending.clear()
                session.agent = None
                session.recovered = True
                session.save()

        finally:
            if session.agent is None:
                await self._close_clients(session)

    @staticmethod
    async def _close_clients(session: Session) -> None:
        clients, session.clients = session.clients, []
        for client in clients:
            try:
                if hasattr(client, "aclose"):
                    await client.aclose()
                else:
                    client.close()
            except Exception as exc:
                session.record("client_close_error", error=True, **exception_details(exc))

    async def _invoke_turn(self, session: Session, prompt: Any) -> Any:
        # HTTP retries happen inside the provider; never replay a graph/tool sequence.
        invocation = asyncio.create_task(_invoke_with_progress(
            session.agent, workspace=session.workspace,
            config={"configurable": {"thread_id": session.id}, "recursion_limit": 100},
            prompt=prompt, event_callback=lambda event: self._on_event(session, event),
        ))
        monitor = asyncio.create_task(self._monitor(session, invocation))
        try:
            return await invocation
        finally:
            invocation.cancel()
            monitor.cancel()
            await asyncio.gather(invocation, monitor, return_exceptions=True)

    async def _monitor(self, session: Session, invocation: asyncio.Task) -> None:
        try:
            while not invocation.done():
                if session.stop_requested:
                    invocation.cancel()
                    return
                await asyncio.sleep(.7)
                await asyncio.to_thread(self._ensure_preview, session)
        except Exception as exc:
            session.record("preview_monitor_error", error=True, **exception_details(exc))
            session.emit("error", f"预览更新失败：{exc}")

    @staticmethod
    def _on_event(session: Session, event: dict) -> None:
        name = event.get("name", "agent")
        kind = event.get("event", "")
        data = event.get("data", {})
        if kind == "on_tool_start":
            args = data.get("input", {})
            session.record("tool_start", tool=name, run_id=event.get("run_id"), input=args)
            detail = json.dumps(sanitize(args, session.provider.api_key), ensure_ascii=False, default=str)
            session.emit("tool", f"{name} · {detail[:1200]}", tool=name, phase="start", run_id=event.get("run_id"))
        elif kind in {"on_tool_end", "on_tool_error"}:
            content = message_text(data.get("output"))
            err = data.get("error")
            session.record("tool_end", error=kind.endswith("error"), tool=name, run_id=event.get("run_id"), output=content[:50000], failure=exception_details(err) if isinstance(err, BaseException) else str(err) if err else None)
            session.emit("error" if kind.endswith("error") else "tool", f"{name} · " + (str(data.get("error")) if kind.endswith("error") else "完成"), detail=content[:2000], tool=name, phase="end", run_id=event.get("run_id"))
        elif name in {ROOT_AGENT_NAME, "cadquery-builder", "render-verifier"} and kind in {"on_chain_start", "on_chain_end"}:
            session.emit("agent", f"{name} · {'开始' if kind.endswith('start') else '完成'}")

    @staticmethod
    def _ensure_preview(session: Session) -> None:
        step = session.workspace.root / "artifacts/model.step"
        if not step.exists():
            return
        stat = step.stat()
        signature = f"{stat.st_mtime_ns}-{stat.st_size}"
        if signature == session.preview_signature or time.time() - stat.st_mtime < .5:
            return
        try:
            import cadquery as cq
            shape = cq.importers.importStep(str(step))
            dest = step.with_name(".preview.stl")
            cq.exporters.export(shape, str(dest), exportType="STL", tolerance=.1, angularTolerance=.1)
            os.replace(dest, step.with_name("preview.stl"))
            session.preview_signature = signature
            session.emit("artifact", "三维模型已更新", version=signature)
        except Exception as exc:
            if signature != session.preview_error_signature:
                session.record("preview_error", error=True, **exception_details(exc))
                session.preview_error_signature = signature
                session.emit("error", f"STEP 预览转换暂未成功：{exc}")

    def close(self) -> None:
        for session in self.sessions.values():
            self.stop(session)
        async def finish():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for session in self.sessions.values():
                await self._close_clients(session)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(finish(), self.loop).result(timeout=20)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.worker.join(timeout=3)
        self.loop.close()
        for session in self.sessions.values():
            session.journal.close()
        self.journal.close()
