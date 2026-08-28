"""A streaming ``ProcessHandle`` for E2B commands.

Hermes' ``BaseEnvironment._wait_for_process`` drains ``proc.stdout`` through
``select()`` into a bounded head/tail collector, polls ``proc.poll()``, and
calls ``proc.kill()`` on interrupt or timeout. Backends that are not real
subprocesses satisfy that duck type with an adapter.

Core ships one (``_ThreadedProcessHandle``) but it only accepts a *blocking*
``exec_fn() -> (output, exit_code)`` and writes the whole result into the pipe
after the command has finished — so nothing streams and the entire output sits
in host memory first. The streaming variant of that adapter exists only on the
abandoned in-tree E2B branch, as a core change. A plugin has to bring its own,
which is what this is.

Two properties matter:

* **Output streams.** Each chunk E2B delivers is written into the pipe as it
  arrives, so Hermes' bounded collector can evict early and a verbose command
  cannot grow host memory without bound.
* **Cancellation targets the command.** ``kill()`` kills the E2B command by
  pid. The built-in Daytona and Modal backends stop the whole sandbox to
  cancel one command, which would also kill every background process Hermes
  started through ``spawn_via_env``.
"""

from __future__ import annotations

import logging
import os
import select
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: How long a single chunk may wait for Hermes to make room in the pipe before
#: the rest of the command's output is dropped. Generous relative to the drain
#: loop (which polls every 100ms) and short enough that the worker thread
#: always terminates.
_WRITE_BUDGET_SECONDS = 10.0

_chunk_drop_warned = False


def _drop_sdk_buffer(handle: Any) -> None:
    """Discard the SDK's own copy of the output chunks.

    ``e2b.sandbox_sync.commands.command_handle.CommandHandle`` appends every
    decoded chunk to ``_stdout_chunks`` / ``_stderr_chunks`` with no cap, so a
    command that prints a gigabyte holds a gigabyte in the client process even
    though we have already forwarded each chunk downstream. We drop that copy
    after forwarding.

    This reaches into SDK internals, so it is entirely optional: if a future
    SDK drops those attributes we log once and fall back to E2B's buffering
    behavior rather than breaking. The exit code we need comes from
    ``CommandResult`` / ``CommandExitException``, never from these lists.
    """
    global _chunk_drop_warned
    dropped = False
    for attr in ("_stdout_chunks", "_stderr_chunks"):
        buffer = getattr(handle, attr, None)
        if isinstance(buffer, list):
            del buffer[:]
            dropped = True
    if not dropped and not _chunk_drop_warned:
        _chunk_drop_warned = True
        logger.warning(
            "E2B: this SDK version does not expose the command output buffers "
            "this plugin trims; long-running verbose commands will hold their "
            "full output in memory inside the E2B SDK."
        )


class E2BProcessHandle:
    """ProcessHandle over an already-started E2B ``CommandHandle``.

    The command is started by the caller so that a launch failure raises
    synchronously out of ``_run_bash`` (Hermes turns that into a retry, and an
    ``EnvironmentConnectionError`` into a degraded-backend result) instead of
    being flattened into a silent ``exit 1``.
    """

    def __init__(
        self,
        command_handle: Any,
        *,
        stdin_data: str | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._command = command_handle
        self._on_done = on_done
        self._done = threading.Event()
        self._returncode: int | None = None
        self._killed = False
        self._lock = threading.Lock()
        self._hermes_stdin_errors: list[BaseException] = []

        read_fd, self._write_fd = os.pipe()
        # Non-blocking: Hermes stops draining as soon as it decides a command's
        # outcome (timeout -> 124, interrupt -> 130) and returns *without*
        # closing the read end, so a blocking write would park this handle's
        # worker thread forever once the 64 KB pipe buffer filled. That thread
        # is what releases the environment's in-flight count, so a parked
        # worker would defer cleanup forever and leak the sandbox.
        os.set_blocking(self._write_fd, False)
        # Hermes' _wait_for_process drains this through select().
        self._stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._output_dropped = False

        self._stdin_data = stdin_data
        self._worker = threading.Thread(target=self._run, name="e2b-command", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------
    # ProcessHandle duck type
    # ------------------------------------------------------------------

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        # Only report completion once the worker has flushed every chunk into
        # the pipe: Hermes stops draining ~300ms after poll() goes non-None.
        return self._returncode if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout=timeout)
        return self._returncode

    def kill(self) -> None:
        """Kill the E2B command (by pid), never the sandbox."""
        with self._lock:
            if self._killed:
                return
            self._killed = True
        try:
            self._command.kill()
        except Exception as exc:
            logger.debug("E2B: killing command failed (already gone?): %s", exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send_stdin(self, data: str) -> None:
        """Feed stdin over E2B's stdin channel rather than a shell heredoc.

        A heredoc would put the payload — which for a ``sudo`` command is the
        user's password — into the command line, where anything running inside
        the sandbox could read it out of ``ps``.
        """
        try:
            self._command.send_stdin(data)
            self._command.close_stdin()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            # Hermes surfaces this as result["stdin_error"] rather than
            # silently producing a command that read nothing.
            self._hermes_stdin_errors.append(exc)
            logger.debug("E2B: sending stdin failed: %s", exc)

    def _emit(self, text: str) -> None:
        """Write one chunk into the pipe without ever blocking indefinitely.

        Three ways the consumer can go away, all of which must leave this
        thread able to finish: the read end is closed (``EPIPE``), Hermes has
        stopped draining but still holds the fd (the pipe fills and never
        drains), or the command was killed and its remaining output is moot.
        """
        if not text or self._output_dropped:
            return
        if self._killed:
            # Hermes already decided the outcome and stopped reading.
            return
        view = memoryview(text.encode("utf-8", errors="replace"))
        deadline = time.monotonic() + _WRITE_BUDGET_SECONDS
        while view:
            try:
                written = os.write(self._write_fd, view)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._output_dropped = True
                    logger.debug(
                        "E2B: dropping the remainder of a command's output; "
                        "Hermes stopped draining the pipe"
                    )
                    return
                try:
                    select.select([], [self._write_fd], [], min(0.25, remaining))
                except (OSError, ValueError):
                    self._output_dropped = True
                    return
                continue
            except OSError:
                # Read end closed: the outcome is already settled.
                self._output_dropped = True
                return
            view = view[written:]

    def _forward(self, text: str) -> None:
        self._emit(text)
        _drop_sdk_buffer(self._command)

    def _run(self) -> None:
        exit_code: int | None = None
        try:
            if self._stdin_data:
                # Deliberately here and not in __init__: the constructor runs
                # while the environment lock is held, and a stdin write that
                # blocks would stall every other command on this environment.
                self._send_stdin(self._stdin_data)
            result = self._command.wait(on_stdout=self._forward, on_stderr=self._forward)
            exit_code = int(getattr(result, "exit_code", 0) or 0)
        except BaseException as exc:  # noqa: BLE001
            exit_code = self._exit_code_from_exception(exc)
        finally:
            self._returncode = 0 if exit_code is None else exit_code
            try:
                os.close(self._write_fd)
            except OSError:
                pass
            self._done.set()
            if self._on_done is not None:
                try:
                    self._on_done()
                except Exception:  # pragma: no cover - defensive
                    logger.debug("E2B: command completion hook failed", exc_info=True)

    def _exit_code_from_exception(self, exc: BaseException) -> int:
        """Translate a terminal exception from ``wait()`` into an exit code.

        A non-zero exit is reported by the SDK as ``CommandExitException`` — a
        normal command failure, not an error. Anything else means the stream
        broke: report 1 and let the (already forwarded) output speak, since
        Hermes' own timeout/interrupt handling has usually already produced
        the user-visible outcome by then.
        """
        try:
            from e2b import CommandExitException
        except Exception:  # pragma: no cover - SDK absent
            CommandExitException = ()  # type: ignore[assignment]

        if CommandExitException and isinstance(exc, CommandExitException):
            code = getattr(exc, "exit_code", 1)
            try:
                return int(code)
            except (TypeError, ValueError):
                return 1

        if self._killed:
            # Hermes killed the command; it decides the reported code (130 for
            # an interrupt, 124 for a timeout) and ignores this value.
            return 137

        logger.debug("E2B: command stream ended abnormally: %s", exc)
        self._emit(f"\n[e2b: command stream ended: {type(exc).__name__}]")
        return 1
