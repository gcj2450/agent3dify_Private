import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import httpx
from openai import InternalServerError
from PIL import Image

from agent3dify.diagnostics import Journal, diagnostic_zip, sanitize
from agent3dify.provider import Provider
from agent3dify.provider_transport import RetryAsyncTransport, RetryTransport
from agent3dify.shell_backend import AgentShellBackend
from agent3dify.web import WebServer
from agent3dify.web_sessions import SessionManager


class RetryTests(unittest.TestCase):
    def test_sync_transient_failures_retry_same_payload(self):
        bodies, events = [], []
        statuses = iter([503, 429, 200])

        def upstream(request):
            bodies.append(request.content)
            return httpx.Response(next(statuses), json={"ok": True}, headers={"retry-after": "0"})

        with httpx.Client(transport=RetryTransport(lambda **e: events.append(e), transport=httpx.MockTransport(upstream), delays=(0, 0))) as client:
            self.assertEqual(client.post("http://proxy/v1/chat/completions", json={"messages": ["hello"]}).status_code, 200)
        self.assertEqual(bodies, [bodies[0]] * 3)
        self.assertEqual([e["status_code"] for e in events], [503, 429, 200])
        self.assertEqual([e["attempt"] for e in events], [1, 2, 3])
        self.assertEqual(len({e["request_id"] for e in events}), 1)

    def test_permanent_error_is_not_retried(self):
        for status in [400, 401, 404]:
            events = []
            with httpx.Client(transport=RetryTransport(lambda **e: events.append(e), transport=httpx.MockTransport(lambda r: httpx.Response(status)), delays=(0,))) as client:
                self.assertEqual(client.get("http://proxy").status_code, status)
            self.assertEqual(len(events), 1)

    def test_exhausted_service_error_is_bounded(self):
        events = []
        with httpx.Client(transport=RetryTransport(lambda **e: events.append(e), transport=httpx.MockTransport(lambda r: httpx.Response(503)), delays=(0, 0))) as client:
            self.assertEqual(client.get("http://proxy").status_code, 503)
        self.assertEqual(len(events), 3)
        self.assertIsNone(events[-1]["retry_in_seconds"])

    def test_http_200_overload_json_is_retried(self):
        calls = []
        events = []

        def upstream(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={"error": {"message": "Our servers are currently overloaded. Please try again later."}})
            return httpx.Response(200, json={"id": "ok"})

        with httpx.Client(transport=RetryTransport(lambda **e: events.append(e), transport=httpx.MockTransport(upstream), delays=(0,))) as client:
            self.assertEqual(client.get("http://proxy").json(), {"id": "ok"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[0]["status_code"], 200)
        self.assertIn("overloaded", events[0]["error"])

    def test_connection_failure_then_success(self):
        calls = []

        def upstream(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("connection lost")
            return httpx.Response(200)

        with httpx.Client(transport=RetryTransport(transport=httpx.MockTransport(upstream), delays=(0,))) as client:
            self.assertEqual(client.get("http://proxy").status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_error_body_read_failure_can_retry(self):
        class BrokenBody(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.ReadError("error body disconnected")
                yield b""

        calls = []

        def upstream(request):
            calls.append(1)
            return httpx.Response(503, stream=BrokenBody()) if len(calls) == 1 else httpx.Response(200)

        with httpx.Client(transport=RetryTransport(transport=httpx.MockTransport(upstream), delays=(0,))) as client:
            self.assertEqual(client.get("http://proxy").status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_partial_success_stream_is_never_replayed(self):
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"data: partial result\n"
                raise httpx.ReadError("stream disconnected")

        calls = []

        def upstream(request):
            calls.append(1)
            return httpx.Response(200, stream=BrokenStream(), headers={"content-type": "text/event-stream"})

        with httpx.Client(transport=RetryTransport(transport=httpx.MockTransport(upstream), delays=(0,))) as client:
            with self.assertRaises(httpx.ReadError), client.stream("GET", "http://proxy") as response:
                list(response.iter_bytes())
        self.assertEqual(len(calls), 1)

    def test_json_response_body_read_failure_can_retry(self):
        class BrokenBody(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.ReadTimeout("JSON response body timed out")
                yield b""

        calls = []

        def upstream(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(200, stream=BrokenBody(), headers={"content-type": "application/json"})
            return httpx.Response(200, json={"ok": True})

        events = []
        with httpx.Client(transport=RetryTransport(lambda **e: events.append(e), transport=httpx.MockTransport(upstream), delays=(0,))) as client:
            self.assertEqual(client.get("http://proxy").json(), {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[0]["status_code"], 200)
        self.assertIn("JSON response body timed out", events[0]["error"])


class AsyncRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_retry_and_cancellation(self):
        events = []
        transport = RetryAsyncTransport(lambda **e: events.append(e), transport=httpx.MockTransport(lambda r: httpx.Response(503)), delays=(30,))
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(client.get("http://proxy"))
            while not events:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(events), 1)

    async def test_chatopenai_payload_and_callback_compatibility(self):
        events, bodies, clients = [], [], []

        def upstream(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return httpx.Response(503, json={"error": {"message": "busy", "type": "api_error"}}, headers={"retry-after": "0"})
            return httpx.Response(200, json={"id": "chat-test", "object": "chat.completion", "created": 1, "model": "gpt-5.6-sol", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

        transport = RetryAsyncTransport(lambda **e: events.append(e), transport=httpx.MockTransport(upstream), delays=(0,))
        with patch("agent3dify.provider.RetryAsyncTransport", return_value=transport):
            model = Provider("http://proxy/v1", "test-key", "gpt-5.6-sol").chat_model(clients=clients)
        try:
            result = await model.ainvoke("hello")
            self.assertEqual(result.content, "ok")
            self.assertEqual(len(events), 2)
            self.assertEqual(bodies[0], bodies[1])
            self.assertEqual(bodies[0]["model"], "gpt-5.6-sol")
            self.assertNotIn("max_tokens", bodies[0])
            self.assertNotIn("max_completion_tokens", bodies[0])
            self.assertTrue(model.disable_streaming)
        finally:
            for client in clients:
                if hasattr(client, "aclose"):
                    await client.aclose()
                else:
                    client.close()


class DiagnosticTests(unittest.TestCase):
    def test_journal_persists_traceback_and_masks_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root), "custom-credential")
            journal.write("failure", error=True, message="custom-credential sk-sensitive Bearer abc", api_key="other", image="data:image/png;base64,aGVsbG8=", has_api_key=True)
            journal.close()
            error = (Path(root) / "errors.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("custom-credential", error)
            self.assertNotIn("sk-sensitive", error)
            self.assertNotIn("aGVsbG8", error)
            data = json.loads(error)
            self.assertTrue(data["has_api_key"])
            self.assertEqual(data["api_key"], "[REDACTED]")
        self.assertNotIn("aGVsbG8", sanitize("{'base64': 'aGVsbG8='}"))

    def test_shell_failure_is_logged(self):
        with tempfile.TemporaryDirectory() as root:
            events = []
            backend = AgentShellBackend(root, log_callback=lambda **e: events.append(e))
            result = backend.execute('python -c "import sys; print(123); sys.exit(7)"')
            self.assertEqual(result.exit_code, 7)
            self.assertTrue(events[-1]["error"])
            self.assertEqual(events[-1]["exit_code"], 7)
            self.assertIn("123", events[-1]["output"])

    def test_failed_turn_persists_and_downloads_without_replaying_tools(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            provider = Provider("http://proxy/v1", "sk-test-secret", "gpt-5.6-sol")
            manager = SessionManager(root, provider, agent_factory=lambda *a, **kw: object())
            image = io.BytesIO()
            Image.new("RGB", (10, 10), "white").save(image, "PNG")
            image_data = base64.b64encode(image.getvalue()).decode()

            async def fail(agent, *, workspace, event_callback, **kwargs):
                (workspace.root / "generated/model.py").write_text("# keep completed work", encoding="utf-8")
                event_callback({"event": "on_tool_start", "name": "execute", "data": {"input": {"command": "build"}}})
                session = manager.sessions[workspace.root.name]
                manager._on_request(session, kind="model_request", request_id="request-123", attempt=1, status_code=503, error="temporarily unavailable", retry_in_seconds=0)
                response = httpx.Response(503, request=httpx.Request("POST", "http://proxy"), headers={"x-request-id": "upstream-123"})
                raise InternalServerError("service unavailable sk-test-secret", response=response, body=None)

            try:
                with patch("agent3dify.web_sessions._invoke_with_progress", new=AsyncMock(side_effect=fail)) as invoke:
                    session = manager.create({"images": [{"data": image_data}], "message": "create model"})
                    session.future.result(timeout=15)
                    self.assertEqual(invoke.await_count, 1)
                self.assertEqual(session.status, "error")
                self.assertTrue((session.workspace.root / "generated/model.py").exists())
                self.assertEqual(session.clients, [])
                records = [json.loads(line) for line in (session.journal.root / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
                failure = next(r for r in records if r["kind"] == "turn_error")
                self.assertEqual(failure["status_code"], 503)
                self.assertIn("InternalServerError", failure["traceback"])
                self.assertEqual(failure["request_id"], "upstream-123")
                (session.workspace.root / ".env").write_text("should-not-export")
                server = WebServer(("127.0.0.1", 0), manager, root / ".env")
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with httpx.Client(trust_env=False) as client:
                        response = client.get(f"http://127.0.0.1:{server.server_port}/api/sessions/{session.id}/diagnostics")
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("attachment", response.headers["content-disposition"])
                        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                            self.assertIn("logs/errors.jsonl", archive.namelist())
                            self.assertNotIn(".env", archive.namelist())
                            exported = "".join(archive.read(name).decode() for name in archive.namelist())
                            self.assertNotIn("sk-test-secret", exported)
                            self.assertNotIn(image_data, exported)
                            self.assertNotIn("should-not-export", exported)
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join()
            finally:
                manager.close()
            restored = SessionManager(root, provider)
            try:
                previous = restored.sessions[session.id]
                self.assertEqual(previous.status, "error")
                previous.events = [{"id": 1700, "kind": "status", "text": "old"}]
                previous.emit("status", "new")
                self.assertEqual(previous.events[-1]["id"], 1701)
                self.assertIn("logs/errors.jsonl", zipfile.ZipFile(io.BytesIO(diagnostic_zip(previous))).namelist())
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
