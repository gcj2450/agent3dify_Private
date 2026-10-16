"""Local shell execution with the active Python runtime and cancellable processes."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from .diagnostics import exception_details

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse


class AgentShellBackend(LocalShellBackend):
    def __init__(self, root_dir: str, *, log_callback=None) -> None:
        env = {k: v for k, v in os.environ.items() if not any(part in k.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
        env["PATH"] = str(__import__("pathlib").Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        super().__init__(root_dir=root_dir, virtual_mode=True, env=env)
        self._process_lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()
        self._cancelled = False
        self._log = log_callback or (lambda **fields: None)

    def reset_cancel(self) -> None:
        with self._process_lock:
            self._cancelled = False

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop(self) -> None:
        with self._process_lock:
            self._cancelled = True
            processes = list(self._processes)
        for process in processes:
            self._kill(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        started = time.monotonic()
        if not command or not isinstance(command, str):
            return ExecuteResponse(output="Command must be a non-empty string", exit_code=1)
        process = None
        try:
            with self._process_lock:
                if self._cancelled:
                    return ExecuteResponse(output="Run cancelled", exit_code=130)
                process = subprocess.Popen(
                    command, shell=True, cwd=str(self.cwd), env=self._env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                self._processes.add(process)
            self._log(kind="shell_start", command=command, pid=process.pid)
            try:
                output, _ = process.communicate(timeout=min(max(timeout or 120, 1), 300))
            except subprocess.TimeoutExpired:
                self._kill(process)
                output, _ = process.communicate()
                self._log(kind="shell_end", error=True, command=command, exit_code=124, output=output[-50000:], elapsed_seconds=time.monotonic() - started)
                return ExecuteResponse(output=output[-50000:] + "\nCommand timed out", exit_code=124)
            self._log(kind="shell_end", error=process.returncode != 0, command=command, exit_code=process.returncode, output=output[-50000:], elapsed_seconds=time.monotonic() - started)
            return ExecuteResponse(output=output[-50000:] or "<no output>", exit_code=process.returncode, truncated=len(output) > 50000)
        except Exception as exc:
            self._log(kind="shell_error", error=True, command=command, **exception_details(exc))
            return ExecuteResponse(output=f"Execution failed: {exc}", exit_code=1)
        finally:
            if process is not None:
                with self._process_lock:
                    self._processes.discard(process)
