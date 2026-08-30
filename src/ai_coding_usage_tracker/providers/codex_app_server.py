"""Small synchronous client for Codex app-server's JSONL stdio transport.

The app-server is deliberately kept on its default stdio transport.  This
module never starts a listener or exposes credentials on the network.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any


class CodexAppServerError(RuntimeError):
    """Base error for a local Codex app-server interaction."""


class CodexAppServerUnavailable(CodexAppServerError):
    """The Codex CLI could not be started or stopped unexpectedly."""


class CodexAppServerProtocolError(CodexAppServerError):
    """The server sent malformed JSON-RPC or an error response."""


class CodexAppServerTimeout(CodexAppServerError):
    """The server did not respond before the configured timeout."""


def _unsupported_error(request_id: Any) -> dict[str, Any]:
    """A JSON-RPC error reply for server-initiated requests this client cannot handle."""
    return {"id": request_id, "error": {"code": -32601, "message": "Unsupported"}}


_CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "TZ",
)


def _child_env(codex_home: Path | None) -> dict[str, str]:
    """Build a minimal environment for the spawned Codex CLI process.

    Only an allowlist of variables a child process genuinely needs is
    forwarded.  The parent's full environment is deliberately NOT copied:
    a PATH-resolved ``codex`` executable must never observe live secrets
    such as PLANTRACK_CLAUDE_SESSION_KEY.  PLANTRACK_CODEX_EXECUTABLE is
    resolved by the parent before spawning, so the child does not need it.
    """
    child_env = {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}
    if codex_home is not None:
        child_env["CODEX_HOME"] = str(codex_home)
    return child_env


class CodexAppServer:
    """Talk to one short-lived ``codex app-server`` process over JSONL.

    Stdout and stderr are read in background threads so request deadlines work
    on Windows as well as Unix.  Only JSON-RPC responses matching the request
    id are returned; unsolicited notifications are retained for device login.
    """

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        codex_home: Path | None = None,
        timeout: float = 15.0,
        process_factory: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> None:
        configured = os.environ.get("PLANTRACK_CODEX_EXECUTABLE")
        requested = str(executable or configured or "codex")
        self.executable = shutil.which(requested) or requested
        self.codex_home = codex_home
        self.timeout = timeout
        self._process_factory = process_factory or subprocess.Popen
        self._process: subprocess.Popen[str] | None = None
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._notifications: list[dict[str, Any]] = []
        self._stderr: deque[str] = deque(maxlen=20)
        self._next_id = 0
        self._write_lock = threading.Lock()

    def __enter__(self) -> CodexAppServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        """Start and initialize the server exactly once."""
        if self._process is not None:
            return
        try:
            process = self._process_factory(
                [self.executable, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=_child_env(self.codex_home),
            )
        except (OSError, ValueError) as exc:
            raise CodexAppServerUnavailable(
                f"Could not start Codex CLI ({self.executable!r}): {exc}"
            ) from exc
        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._terminate(process)
            raise CodexAppServerUnavailable("Codex app-server did not expose stdio pipes")
        self._process = process
        threading.Thread(target=self._drain_stdout, args=(process.stdout,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True).start()
        try:
            # Imported here, not at module scope: the package __init__ imports
            # cli, which imports this module, so a top-level import is circular.
            from .. import __version__

            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "ai_coding_usage_tracker",
                        "title": "AI Coding Usage Tracker",
                        "version": __version__,
                    }
                },
            )
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its result, matching its JSON-RPC id."""
        process = self._require_process()
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerTimeout(f"Timed out waiting for Codex response to {method}")
            try:
                line = self._stdout.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if process.poll() is not None:
                    raise self._exited_error(method) from None
                continue
            if line is None:
                raise self._exited_error(method)
            message = self._parse_message(line)
            if "method" in message and "id" not in message:
                self._notifications.append(message)
                continue
            if "method" in message and "id" in message:
                # app-server can make server-initiated requests.  This tracker
                # owns no optional server capabilities, so decline safely.
                self._send(_unsupported_error(message["id"]))
                continue
            if message.get("id") != request_id:
                # A response to another request cannot happen in normal serial
                # use, but retaining it as a notification avoids returning it.
                self._notifications.append(message)
                continue
            error = message.get("error")
            if error is not None:
                detail = error.get("message") if isinstance(error, dict) else None
                raise CodexAppServerProtocolError(
                    f"Codex rejected {method}" + (f": {detail}" if isinstance(detail, str) else "")
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerProtocolError(f"Codex returned an invalid response to {method}")
            return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def wait_for_notification(
        self,
        method: str,
        *,
        timeout: float | None = None,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Wait for a named notification without treating unrelated events as errors."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            for index, message in enumerate(self._notifications):
                if message.get("method") == method and (predicate is None or predicate(message)):
                    return self._notifications.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerTimeout(f"Timed out waiting for Codex notification {method}")
            try:
                line = self._stdout.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                if self._require_process().poll() is not None:
                    raise self._exited_error(method) from None
                continue
            if line is None:
                raise self._exited_error(method)
            message = self._parse_message(line)
            if "method" in message and "id" not in message:
                if message.get("method") == method and (predicate is None or predicate(message)):
                    return message
                self._notifications.append(message)
            elif "method" in message and "id" in message:
                self._send(_unsupported_error(message["id"]))
            else:
                self._notifications.append(message)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        self._terminate(process)

    def _send(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise CodexAppServerUnavailable("Codex app-server stdin is unavailable")
        try:
            encoded = json.dumps(message, separators=(",", ":"))
            with self._write_lock:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexAppServerUnavailable("Could not write to Codex app-server") from exc

    def _drain_stdout(self, stream: Any) -> None:
        try:
            for line in stream:
                self._stdout.put(line)
        finally:
            self._stdout.put(None)

    def _drain_stderr(self, stream: Any) -> None:
        try:
            for line in stream:
                self._stderr.append(line.rstrip())
        except OSError:
            pass

    def _parse_message(self, line: str) -> dict[str, Any]:
        try:
            message = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise CodexAppServerProtocolError("Codex app-server emitted invalid JSON") from exc
        if not isinstance(message, dict):
            raise CodexAppServerProtocolError(
                "Codex app-server emitted a non-object JSON-RPC message"
            )
        return message

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise CodexAppServerUnavailable("Codex app-server is not running")
        return self._process

    def _exited_error(self, method: str) -> CodexAppServerUnavailable:
        process = self._require_process()
        detail = self._stderr[-1] if self._stderr else "no stderr output"
        return CodexAppServerUnavailable(
            f"Codex app-server exited ({process.poll()}) while handling {method}: {detail}"
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=1)
            return
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass
