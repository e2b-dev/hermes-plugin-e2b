"""The E2B-backed Hermes terminal environment.

Ownership model
---------------
Sandbox identity is derived from the ``task_id`` Hermes already resolved for
its own environment cache, so within one Hermes process one cached
``E2BEnvironment`` serves one sandbox scope. Ownership is still not
exclusive: two Hermes processes with the same scope (two CLI sessions of one
profile, a cron run beside a CLI session), and transiently two environment
objects in one process (eviction/recreation windows), attach to the same
persistent sandbox. Two guarantees make that sharing safe:

* **Lifecycle is non-destructive.** Nothing here pauses or kills a
  persistent sandbox; E2B pauses it itself at lease expiry.
* **At most one live owner is the state *synchroniser*.** A per-scope
  ``WriterLease`` (a flock held from attach until teardown completes) gates
  resume recovery, the host push, the per-command sync, and the teardown
  pull. Concurrent owners attach as *readers*: their commands run normally
  against the state the writer maintains, and each command makes a
  non-blocking attempt to take the role over once it is free — promotion
  re-runs the writer's full bring-up (recovery, then push) under the same
  fail-closed rules as a fresh resume. Only the writer ever creates a
  persistent sandbox, so a reader can never conjure an empty one that no
  writer is preparing.

Being the synchroniser is **not** exclusive mutation of the sandbox's
``~/.hermes``. Readers execute commands, and a sandbox may still be running
processes an earlier session started; any of them can write there at any
time. What the lease serialises is this plugin's own state protocol.

The guarantee readers get is therefore about **command admission**, not
duration. Two gates decide it: an in-sandbox readiness stamp the writer
refreshes after every completed push, which a session that has not yet
attached waits for, and a host-side per-scope dirty marker
(``sandbox.DirtyState``) set while a writer transition is in flight and
after any failed incremental sync, which every reader consults on every
command — including one that bootstrapped long ago and will never probe the
stamp again. Neither is atomic with the launch that follows: a reader can
be admitted microseconds before a writer marks the scope. What is ruled out
is a reader *knowingly* executing against state the host has recorded as
half-written or stale.

Three residual holes, documented in the README: two *machines* sharing one
scope (same E2B account, identically-pathed profile homes) cannot be
arbitrated by a flock, and that is unsupported; a host missing either
coordination facility — an exclusive lock (Windows, a lock-incapable
filesystem) or a writable marker file — gets a loudly-warned
writer-always fallback, which supports a single persistent session but not
concurrent ones; and a modification made inside the sandbox
to a path the host also has, in the window between a writer generation's
recovery snapshot and the completion of its force-push, is overwritten by
the host copy (files the host does *not* have survive it — a force-push
with no baseline deletes nothing).

What remains is a single race that core does create: the idle reaper pops an
environment and calls ``cleanup()`` from a daemon thread without any lock, and
``_last_activity`` is not refreshed while a foreground command is running — so
a command that runs longer than ``terminal.lifetime_seconds`` can be reaped
mid-flight. Command admission and cleanup are therefore settled under one
per-environment lock:

* ``_run_bash`` takes the lock, checks the environment is not closed, renews
  the sandbox lease to cover this command, registers the command, and starts
  it — all in one critical section, so no cleanup can interleave between
  "the sandbox is ready" and "this command is registered";
* ``cleanup()`` takes the same lock; if commands are in flight it records the
  intent and returns, and the last command to finish performs the teardown.

For a persistent sandbox teardown performs no destructive operation on the
sandbox itself — it reconnects if necessary, pulls state back, removes this
session's scratch files, releases the writer lease, and detaches; E2B pauses
the sandbox on its own lease expiry (see ``sandbox.py``). The deferral matters
for both modes: it delays that pull and the lease release for a persistent
sandbox, and the actual destruction for an ephemeral one.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import shlex
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tools.environments import file_sync as hermes_file_sync
from tools.environments.base import BaseEnvironment
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_rm_command,
)

from . import sandbox as sandbox_api
from .config import API_KEY_ENV, DEFAULT_CWD, E2BSettings, get_api_key
from .errors import EnvironmentConnectionError, connection_error, is_missing_sandbox, redact
from .process import E2BProcessHandle
from .sandbox import _hermes_home

logger = logging.getLogger(__name__)

#: Refuse to stream a sync-back archive larger than this. Mirrors the cap
#: Hermes' own FileSyncManager applies after download; enforcing it *during*
#: the transfer means a misbehaving sandbox cannot fill the host disk first.
MAX_SYNC_BACK_BYTES = 2 * 1024 * 1024 * 1024

#: Subtrees of ``~/.hermes`` the agent authors from inside the sandbox and that
#: are safe to copy back on resume. Credentials are deliberately not here:
#: remote data must never create or revive a host credential file.
RECOVERABLE_ROOTS = ("skills", "memories")

#: Where a remote copy goes when it disagrees with the host's, or when its
#: normal destination would land outside the Hermes home. Outside every tree
#: Hermes loads from *and* outside the closed set of ``cache/<subdir>`` names
#: Hermes mirrors into remote backends, so a quarantined file is inert until a
#: human looks at it.
QUARANTINE_DIR = ("cache", "e2b-recovered")

#: Files per ``write_files`` call. Each entry holds an open file object.
_UPLOAD_BATCH = 64

#: The longest command issued by the sync-back transport. Teardown reconnects
#: with a lease that covers this plus the configured command grace before the
#: manager starts its pull.
_SYNC_BACK_COMMAND_TIMEOUT_SECONDS = 300

# Exceptions that control process or generator lifetime are not operational
# backend failures. Cleanup defers them only long enough to finish mandatory
# lifecycle work, then re-raises them.
_PROCESS_CONTROL_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


class _HostOnlyPaths:
    """The places under ``~/.hermes`` that remote data must never write.

    "Inside the Hermes home" is not a sufficient boundary for recovery, and
    treating it as one is how sandbox-authored bytes end up somewhere Hermes
    will later read as something else entirely. The home also holds the
    profile's credentials, its config, the cache trees the host owns
    outright, and this plugin's own coordination files.

    Grounded in Hermes' own mapping rather than a list of guessed names,
    because a guessed list is wrong the moment the layout changes:

    * ``get_credential_file_mounts()`` is the definitive answer to "what does
      Hermes treat as a credential file" — skill-registered files plus the
      ``terminal.credential_files`` config entries. The **directories holding
      them** are host-only too: a tree that holds one credential is a
      credentials tree, and a recovery pass that may create files in it can
      create the next credential;
    * ``get_cache_directory_mounts()`` gives the cache trees Hermes mirrors
      into sandboxes. Those flow host-to-sandbox by design, so remote data
      restoring into them is always wrong;
    * ``config.yaml`` and ``.env`` are read by the profile loader itself;
    * ``platforms/`` holds gateway auth and session state — pairing records,
      WhatsApp sessions, the Matrix store (``gateway/pairing.py``,
      ``gateway/whatsapp_identity.py``, ``plugins/platforms/matrix``, all via
      ``get_hermes_dir("platforms/…")``);
    * this plugin's ``cache/e2b`` (lease + admission marker) and
      ``cache/e2b-recovered`` (quarantine) must not be writable by the data
      they are there to arbitrate.

    What this cannot do is give meaning to a directory Hermes has none for.
    ``~/.hermes/credentials`` is not a Hermes location (nothing in core reads
    it); it becomes host-only the moment a credential is registered or
    configured there, and is then covered. A bare in-home directory with no
    Hermes meaning is indistinguishable from the legitimate
    ``skills -> ~/.hermes/my-skills`` layout, and inventing a name list to
    separate them would be exactly the incomplete guess this avoids.

    Resolved once per recovery pass: the mapping involves config reads, and
    every file in the pass is checked against it.
    """

    def __init__(self, physical_home: Path) -> None:
        self.home = physical_home
        #: Trees that may never overlap a recoverable tree in EITHER
        #: direction. A recoverable root pointed at one of them, or holding
        #: one of them, is a misconfiguration this must not act on.
        self.exclusive_dirs: list[Path] = []
        #: Trees a recoverable tree must not be, or be inside. Deliberately
        #: not the other direction: a user may legitimately register a
        #: credential *inside* skills/, and that must not make the whole
        #: skills tree unrecoverable — the individual file is protected by
        #: :meth:`forbids` instead.
        self.credential_dirs: list[Path] = []
        #: Individual files remote data must never create or replace.
        self.files: list[Path] = []

        #: The quarantine tree, kept separately as well as in
        #: ``exclusive_dirs``: nothing may be *restored* into it, but it is by
        #: definition where quarantine writes go, so the two questions differ
        #: by exactly this path. See :meth:`forbids_quarantine`.
        self.quarantine_dir = physical_home.joinpath(*QUARANTINE_DIR).resolve()
        for name in (QUARANTINE_DIR, ("cache", "e2b"), ("platforms",)):
            self._add(self.exclusive_dirs, physical_home.joinpath(*name))
        for host_path in _hermes_cache_mount_paths():
            self._add(self.exclusive_dirs, host_path)
        for host_path in _hermes_credential_paths():
            self._add(self.files, host_path)
            parent = host_path.parent
            # The home itself is never a "credentials directory": ~/.hermes/.env
            # lives directly in it, and treating the home as host-only would
            # make every destination unsafe.
            if _contained(parent, physical_home) != physical_home:
                self._add(self.credential_dirs, parent)
        for name in ("config.yaml", ".env"):
            self._add(self.files, physical_home / name)

    @staticmethod
    def _add(into: list[Path], candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if resolved not in into:
            into.append(resolved)

    def allows_tree(self, tree: Path) -> bool:
        """Whether recovery may restore into *tree* at all."""
        if tree == self.home:
            # Every file under a root pointed at the home lands directly in
            # ~/.hermes, next to .env and config.yaml.
            return False
        for other in self.exclusive_dirs:
            if tree == other or _within(tree, other) or _within(other, tree):
                return False
        for other in self.credential_dirs:
            if tree == other or _within(tree, other):
                return False
        return True

    def forbids(self, destination: Path) -> bool:
        """Whether one individual destination is host-only.

        Catches what the tree check deliberately allows: a credential
        registered *inside* a recoverable tree keeps that tree recoverable,
        but the credential file itself, and the directory holding it, stay
        untouchable.
        """
        return self._forbids(destination, self.exclusive_dirs)

    def forbids_quarantine(self, destination: Path) -> bool:
        """Whether one *quarantine* destination is host-only.

        The same question as :meth:`forbids` minus one path: the quarantine
        tree, which is host-only for restores and is where quarantine writes
        belong. Everything else still applies — Hermes accepts an arbitrary
        registered credential path, so a credential can be registered at a
        path that happens to sit inside the quarantine tree, and it is still
        a credential. Parking a remote copy over it would be exactly the
        "remote data creates or replaces a host credential" outcome the
        recovery path refuses everywhere else.
        """
        return self._forbids(
            destination, [d for d in self.exclusive_dirs if d != self.quarantine_dir]
        )

    def _forbids(self, destination: Path, exclusive_dirs: list[Path]) -> bool:
        if destination in self.files:
            return True
        for other in self.credential_dirs + exclusive_dirs:
            if destination == other or _within(destination, other):
                return True
        return False


def _hermes_credential_paths() -> list[Path]:
    """Host paths Hermes treats as credential files, from its own mapping."""
    paths: list[Path] = []
    try:
        from tools.credential_files import get_credential_file_mounts

        for entry in get_credential_file_mounts() or ():
            host_path = entry.get("host_path") if isinstance(entry, dict) else None
            if host_path:
                paths.append(Path(host_path))
    except Exception as exc:  # pragma: no cover - core surface moved
        logger.warning(
            "E2B: could not read Hermes' credential mapping (%s); resume "
            "recovery is falling back to the profile's config and env files "
            "as the only known host-only paths",
            exc,
        )
    try:
        from tools.environments.file_sync import _credential_host_paths

        paths.extend(Path(p) for p in _credential_host_paths() or ())
    except Exception:  # pragma: no cover - private helper moved
        pass
    return paths


def _hermes_cache_mount_paths() -> list[Path]:
    """Host paths of the cache trees Hermes mirrors into remote backends."""
    try:
        from tools.credential_files import get_cache_directory_mounts

        return [
            Path(entry["host_path"])
            for entry in get_cache_directory_mounts() or ()
            if isinstance(entry, dict) and entry.get("host_path")
        ]
    except Exception as exc:  # pragma: no cover - core surface moved
        logger.warning(
            "E2B: could not read Hermes' cache mount mapping (%s); resume "
            "recovery cannot exclude those trees by name",
            exc,
        )
        return []


def _is_plain_absolute_path(value: str) -> bool:
    """Whether *value* is an absolute POSIX path with no traversal in it.

    Used on strings the *sandbox* supplies that then become host paths. A
    normalising check would be wrong here: the point is to refuse the value,
    not to repair it.
    """
    if not value.startswith("/") or "\x00" in value:
        return False
    return not any(part in ("..", ".") for part in value.split("/"))


def _within(candidate: Path, boundary: Path) -> bool:
    """Whether *candidate* is strictly inside *boundary*. Both pre-resolved."""
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return candidate != boundary


def _contained(candidate: Path, boundary: Path) -> Path | None:
    """*candidate* resolved, if it physically lies inside *boundary*; else None.

    Both sides are resolved, because a symlink anywhere along the way is
    enough to leave the boundary. Recovery needs this at three levels, and
    each one matters:

    * against the physically resolved Hermes **home** — comparing a candidate
      only against ``(home / root).resolve()``, its own parent, is not a
      boundary check at all: if ``~/.hermes/skills`` is a symlink to
      ``/tmp/elsewhere``, every path under it "contains" correctly while every
      write lands outside the Hermes home;
    * against the physically resolved **recoverable tree** — the home alone is
      too wide, because ``~/.hermes`` also holds trees remote data must never
      write (credentials above all). A symlink under ``skills/`` pointing at
      ``~/.hermes/credentials/`` never leaves the home;
    * against the physically resolved **quarantine tree** — same reason, one
      level further in: a symlink *inside* the quarantine tree pointing at
      the credentials directory also never leaves the home.

    Which tree is safe *as a tree* is a separate question, and a boundary
    check cannot answer it — that is :class:`_HostOnlyPaths`.

    *boundary* must already be resolved. ``resolve()`` is non-strict, so a
    destination that does not exist yet resolves through whatever part of its
    prefix does exist — which is exactly what needs checking before creating
    the rest of it.
    """
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(boundary)
    except ValueError:
        return None
    return resolved


#: ``tools.environments.file_sync`` log messages that mean the watched call
#: ultimately failed. Matched by prefix against the manager's actual wording:
#: ``sync()`` logs exactly one warning on a rolled-back transaction, and
#: ``sync_back()`` logs "all N attempts failed" only after its last retry (the
#: tar-cap message means the downloaded state was discarded unapplied).
_TERMINAL_LOG_PREFIXES = (
    "file_sync: sync failed, rolled back state",
    "sync_back: all",
    "sync_back: remote tar is",
)

#: Messages that are part of a normal, ultimately-successful call: a retried
#: attempt (only "all N attempts failed" is final) and the manager's ordinary
#: last-write-wins conflict notice. Treating these as failures reported a
#: pull that retried-and-succeeded as failed.
_BENIGN_LOG_PREFIXES = (
    "sync_back: attempt",
    "sync_back: conflict on",
)


class _SyncOutcome:
    """Whether a ``FileSyncManager`` call actually succeeded.

    Hermes' manager reports nothing: ``sync()`` and ``sync_back()`` both return
    ``None`` and swallow every exception, logging a warning and moving on
    (``file_sync.py`` — ``"file_sync: sync failed, rolled back state"`` and
    ``"sync_back: all N attempts failed"``). A backend that wraps those calls in
    ``try/except`` therefore has dead code: a failed state upload looks exactly
    like a successful one.

    That silence is not tolerable here. A failed initial upload means the agent
    is working in a sandbox with none of its skills or credentials, and a failed
    teardown pull means work done inside the sandbox is about to be overwritten
    by the host on the next resume.

    Two independent signals, because neither alone is complete:

    * transport errors recorded by this plugin's own upload/delete/download
      callbacks — precise, carries the real exception, but blind to failures
      that happen after the download (tar extraction, applying files);
    * records emitted by the manager's own logger, classified against the
      manager's known messages — a retry warning or a conflict notice is part
      of a successful call, not a failure of it.

    The verdict depends on which call was watched. ``sync()`` is one
    transaction with no internal retries, so any transport error is final.
    ``sync_back()`` retries internally, so a transport error means one failed
    *attempt*; the call as a whole failed only when the manager said so, or
    when every attempt it had was seen to fail in transport.
    """

    def __init__(self) -> None:
        self.transport_errors: list[BaseException] = []
        self.terminal_messages: list[str] = []
        self.notes: list[str] = []

    def record_log(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record must not raise
            message = "(unformattable log record)"
        if message.startswith(_BENIGN_LOG_PREFIXES):
            self.notes.append(message)
        elif message.startswith(_TERMINAL_LOG_PREFIXES) or record.levelno >= logging.ERROR:
            self.terminal_messages.append(message)
        else:
            # An unrecognised warning is context, not a verdict: inventing a
            # failure out of it would abort commands over future benign log
            # lines. Transport errors remain the text-independent signal.
            self.notes.append(message)

    @property
    def push_failed(self) -> bool:
        """Verdict for ``sync()``: one transaction, no internal retries."""
        return bool(self.transport_errors or self.terminal_messages)

    @property
    def pull_failed(self) -> bool:
        """Verdict for ``sync_back()``: the manager retries internally.

        A lone transport error is a failed *attempt* that may have been
        retried successfully, so it does not fail the call. The call failed
        when the manager logged its final give-up — or, should that log line
        ever disappear from core, when every attempt in the manager's retry
        budget was seen to fail in transport.
        """
        if self.terminal_messages:
            return True
        budget = getattr(hermes_file_sync, "_SYNC_BACK_MAX_RETRIES", None)
        return isinstance(budget, int) and budget > 0 and len(self.transport_errors) >= budget

    @property
    def detail(self) -> str:
        parts = [
            f"{type(exc).__name__}: {exc}" for exc in self.transport_errors
        ] + self.terminal_messages
        return redact("; ".join(parts))

    def as_error(self) -> BaseException:
        if self.transport_errors:
            return self.transport_errors[0]
        return RuntimeError(self.detail or "state sync failed")


class _SyncLogCapture(logging.Handler):
    """Route the file-sync logger's records to the innermost active watch.

    Attribution has to be nest-aware *per thread*, and the handler itself has
    to stay attached. Two lessons are baked in:

    * another environment's teardown — including its own ``sync_back()`` — can
      run synchronously inside our frame on our thread, so each record goes to
      the innermost watch on the *emitting* thread, which is the call that
      produced it;
    * the handler is installed once and never removed. Removal driven by any
      one thread's state is a race: the first thread to finish watching would
      detach the process-global handler while another thread's watch is still
      relying on it, and that thread's logger-only failure would be lost.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        stack = getattr(_watch_stack, "stack", None)
        if not stack:
            return
        stack[-1].record_log(record)


_watch_stack = threading.local()
_sync_log_capture = _SyncLogCapture()
_capture_install_lock = threading.Lock()
_capture_installed = False
_sync_back_state = threading.local()


def _ensure_sync_log_capture() -> None:
    """Attach the capture handler to Hermes' file-sync logger, exactly once."""
    global _capture_installed
    if _capture_installed:
        return
    with _capture_install_lock:
        if _capture_installed:
            return
        logging.getLogger("tools.environments.file_sync").addHandler(_sync_log_capture)
        _capture_installed = True


def _current_watch() -> _SyncOutcome | None:
    stack = getattr(_watch_stack, "stack", None)
    return stack[-1] if stack else None


#: Refcounted floor on the file-sync logger's level while any watch is active.
#: The manager's warnings are the only signal for its post-download failures,
#: and log records are never *created* when the logger's effective level sits
#: above WARNING — a deployment that silences that logger would silently blind
#: the failure detection. While a sync is being watched, the logger is floored
#: to WARNING (records reach both this plugin's verdict and the user's
#: handlers); the previous level is restored when the last watch exits.
#: ``logging.disable(logging.WARNING)`` or higher is process-global and cannot
#: be counteracted — documented in contract gap 6 as the remaining constraint.
_floor_lock = threading.Lock()
_floor_watchers = 0
_floor_applied = False
_floor_saved_level = logging.NOTSET


def _enter_watch_floor(sync_logger: logging.Logger) -> None:
    global _floor_watchers, _floor_applied, _floor_saved_level
    with _floor_lock:
        _floor_watchers += 1
        if not _floor_applied and not sync_logger.isEnabledFor(logging.WARNING):
            _floor_saved_level = sync_logger.level
            sync_logger.setLevel(logging.WARNING)
            _floor_applied = True


def _exit_watch_floor(sync_logger: logging.Logger) -> None:
    global _floor_watchers, _floor_applied
    with _floor_lock:
        _floor_watchers -= 1
        if _floor_watchers == 0 and _floor_applied:
            sync_logger.setLevel(_floor_saved_level)
            _floor_applied = False


@contextmanager
def _watch_sync() -> Iterator[_SyncOutcome]:
    """Run a FileSyncManager call and find out whether it worked."""
    _ensure_sync_log_capture()
    sync_logger = logging.getLogger("tools.environments.file_sync")
    _enter_watch_floor(sync_logger)
    outcome = _SyncOutcome()
    stack = getattr(_watch_stack, "stack", None)
    if stack is None:
        stack = []
        _watch_stack.stack = stack
    stack.append(outcome)
    try:
        yield outcome
    finally:
        stack.pop()
        _exit_watch_floor(sync_logger)


def _watched(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a sync transport callback so its failure is not lost.

    The manager catches whatever these raise; recording it on the way past is
    the only way to learn the real cause. The error lands on the innermost
    watch of the calling thread — the manager runs its callbacks synchronously,
    so that is exactly the call that owns them.
    """

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            watch = _current_watch()
            if watch is not None:
                watch.transport_errors.append(exc)
            raise

    return _wrapper


def _sync_back_in_flight() -> bool:
    return getattr(_sync_back_state, "active", False)


@contextmanager
def _sync_back_guard() -> Iterator[None]:
    """Mark a state pull as running on this thread.

    Belt and braces alongside :meth:`E2BEnvironment.__del__`: Hermes' sync-back
    takes a cross-process ``flock`` that is not reentrant, so a second pull
    entered from inside the first — by any route — would block forever.
    """
    _sync_back_state.active = True
    try:
        yield
    finally:
        _sync_back_state.active = False


class E2BEnvironment(BaseEnvironment):
    """Runs Hermes terminal commands inside an E2B sandbox.

    The class name deliberately contains none of the substrings Hermes'
    ``file_tools._terminal_env_type_for_task`` sniffs for (``local``, ``ssh``,
    ``docker``, ``singularity``, ``modal``, ``daytona``) — it would otherwise
    be misclassified before the ``_hermes_backend_name`` stamp is consulted.
    """

    # Stdin travels over E2B's own stdin channel, not a shell heredoc, so a
    # sudo password never appears in the command line inside the sandbox.
    _stdin_mode = "pipe"

    def __init__(
        self,
        *,
        task_id: str = "default",
        settings: E2BSettings | None = None,
        cwd: str = "",
        timeout: int = 180,
        persistent_filesystem: bool = True,
    ) -> None:
        self._settings = settings or E2BSettings()
        # The prompt-probe environment is never registered with Hermes and
        # never reaped, so it is always ephemeral regardless of configuration.
        self._is_probe = sandbox_api.is_probe_scope(task_id)
        super().__init__(cwd=cwd or self._settings.cwd, timeout=timeout)

        self._task_id = task_id
        # Read by tools.terminal_tool.is_persistent_env to decide whether the
        # agent loop tears this environment down at end of turn.
        self._persistent = bool(persistent_filesystem) and not self._is_probe
        self._scope = sandbox_api.scope_id(task_id, self._settings, persistent=self._persistent)

        # Read by tools.image_generation_tool to place agent-visible files.
        self._remote_home = self._settings.cwd

        self._lock = threading.RLock()
        self._sandbox: Any = None
        self._sandbox_id: str | None = None
        self._bootstrapped = False
        #: True when this environment adopted a pre-existing sandbox rather
        #: than creating one, i.e. when its filesystem may hold state the host
        #: has never seen.
        self._resumed_existing = False
        self._inflight = 0
        self._closed = False
        self._close_pending = False
        self._torn_down = threading.Event()
        self._torn_down.set()  # nothing to tear down until a sandbox exists
        self._sync_manager: FileSyncManager | None = None
        #: Whether this environment holds the state-sync writer role. An
        #: ephemeral sandbox has exactly one owner, so it is always the
        #: writer; a persistent one competes for the per-scope WriterLease at
        #: attach time and may run as a reader — see ``_bootstrap``.
        self._is_writer = not self._persistent
        self._writer_lease = sandbox_api.WriterLease(self._scope) if self._persistent else None
        #: Host-side record of whether this scope's sandbox state can be run
        #: against at all. Only persistent scopes have readers to hold back.
        self._dirty_state = sandbox_api.DirtyState(self._scope) if self._persistent else None
        #: Lower bound on the server-side lease deadline. Only moves forward.
        self._lease_deadline = 0.0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def sandbox_id(self) -> str | None:
        return self._sandbox_id

    @property
    def remote_hermes_home(self) -> str:
        return f"{self._remote_home.rstrip('/')}/.hermes"

    def get_temp_dir(self) -> str:
        return "/tmp"

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------

    def _api_key(self) -> str:
        key = get_api_key()
        if not key:
            raise EnvironmentConnectionError(
                f"{API_KEY_ENV} is not set for the active Hermes profile",
                retry_hint=(
                    f"Add {API_KEY_ENV}=<your key> to ~/.hermes/.env (or the "
                    "active profile's .env) and retry."
                ),
            )
        return key

    def _ensure_ready(self, *, lease_seconds: int | None = None) -> None:
        """Attach a sandbox and bootstrap it. Caller must hold ``_lock``."""
        if self._closed:
            raise EnvironmentConnectionError(
                "This E2B environment was torn down",
                retry_hint=(
                    "The environment was cleaned up (session close or idle "
                    "reaping). Retry — Hermes will build a fresh one."
                ),
            )
        lease = lease_seconds or self._desired_lease(None)
        if self._sandbox is None:
            self._attach(lease)
        else:
            self._renew_lease(lease)
        if not self._bootstrapped:
            # Set before bootstrapping: _bootstrap runs commands, which re-enter
            # this method on the same (re-entrant) lock.
            self._bootstrapped = True
            try:
                self._bootstrap()
            except BaseException:
                self._bootstrapped = False
                raise

    def _attach(self, lease_seconds: int) -> None:
        """Adopt this scope's sandbox, or create one. Caller holds ``_lock``.

        Only ever entered with no sandbox attached (``_ensure_ready`` calls it
        when ``_sandbox`` is None; ``_replace_missing_sandbox`` clears it
        first), so anything that fails here leaves this environment with
        nothing usable — and must therefore not leave it holding the writer
        role. Sandbox creation, a malformed create/connect result, and the
        lease bookkeeping all sit after the acquisition, so the release is
        driven off "did we finish attaching" rather than off any one of them.
        """
        sandbox = None
        lease_started_at = 0.0
        self._resumed_existing = False
        attached = False

        try:
            # Inside the guard: on a replacement attach this environment may
            # already hold the lease, and an unresolvable credential here
            # would otherwise leave it holding the role with no sandbox.
            api_key = self._api_key()
            if self._persistent:
                with sandbox_api.scope_lock(self._scope):
                    attached_sandbox = self._adopt_existing(api_key, lease_seconds)
                    if attached_sandbox is not None:
                        sandbox, lease_started_at = attached_sandbox
                        # Compete for the state-sync writer role. Losing it
                        # means another live owner is synchronising state with
                        # this sandbox right now; this environment runs as a
                        # reader until the role is released (see _bootstrap
                        # and _before_execute). Idempotent when this
                        # environment already holds the lease (sandbox
                        # replacement).
                        self._is_writer = self._writer_lease.try_acquire()
                    elif self._writer_lease.try_acquire():
                        self._is_writer = True
                        sandbox, lease_started_at = self._create(api_key, lease_seconds)
                    else:
                        # No sandbox exists, and another live session holds the
                        # writer role for this scope — its sandbox was destroyed
                        # out from under it, or it is still creating one. A
                        # reader-created replacement would be an empty sandbox no
                        # writer is preparing: commands would run against no
                        # Hermes state at all. Fail closed; the writer rebuilds
                        # on its own next command, or releases the role when it
                        # ends, and either way a retry here converges.
                        raise EnvironmentConnectionError(
                            "No sandbox exists for this scope and another live "
                            "Hermes session holds its state-sync writer role",
                            retry_hint=(
                                "The session owning this scope will rebuild the "
                                "sandbox on its next command, or release the role "
                                "when it ends. Retry the same command."
                            ),
                        )
            else:
                # Ephemeral sandboxes are never discovered or adopted: a
                # nominally throwaway sandbox must not be shared with another
                # session or another Hermes process, either of which could kill
                # it mid-command.
                sandbox, lease_started_at = self._create(api_key, lease_seconds)

            # Publish the sandbox reference before anything else can fail, so a
            # later error still leaves cleanup() able to reach it.
            self._sandbox = sandbox
            self._sandbox_id = sandbox_api.sandbox_id_of(sandbox)
            self._torn_down.clear()
            self._track_lease(lease_seconds, started_at=lease_started_at)
            attached = True
        except BaseException:
            if not attached:
                # A lease held by an environment that never attached is a
                # phantom writer: no sandbox to prepare, no push to complete,
                # no readiness stamp coming — and every other session locked
                # out of the role for the rest of this process's life. Release
                # it, so the retry (this session's or another's) converges on
                # whoever can actually do the work.
                self._release_writer_lease()
            raise

    def _adopt_existing(self, api_key: str, lease_seconds: int) -> tuple[Any, float] | None:
        try:
            existing_id = sandbox_api.find_existing(self._scope, api_key, self._settings.template)
        except EnvironmentConnectionError:
            raise
        except Exception as exc:
            raise connection_error("sandbox lookup", exc) from exc
        if not existing_id:
            return None
        try:
            lease_started_at = time.monotonic()
            sandbox = sandbox_api.connect(existing_id, lease_seconds, api_key)
        except Exception as exc:
            if is_missing_sandbox(exc):
                logger.info(
                    "E2B: sandbox %s vanished between listing and connect; creating a fresh one",
                    existing_id,
                )
                return None
            raise connection_error(
                f"resuming sandbox {existing_id} with a {lease_seconds}s lifecycle lease",
                exc,
            ) from exc
        logger.info("E2B: resumed sandbox %s for task %s", existing_id, self._task_id)
        self._resumed_existing = True
        return sandbox, lease_started_at

    def _create(self, api_key: str, lease_seconds: int) -> tuple[Any, float]:
        try:
            lease_started_at = time.monotonic()
            sandbox = sandbox_api.create(
                self._scope,
                self._settings,
                persistent=self._persistent,
                lease_seconds=lease_seconds,
                api_key=api_key,
            )
        except Exception as exc:
            raise connection_error(
                f"sandbox creation with a {lease_seconds}s lifecycle lease",
                exc,
            ) from exc
        logger.info(
            "E2B: created %s sandbox %s for task %s",
            "persistent" if self._persistent else "ephemeral",
            # Not sandbox_id_of: that raises on a malformed result, and doing
            # it here would lose the reference to a sandbox E2B really did
            # create before the caller can publish it for cleanup. The id is
            # validated once, in _attach, after the reference is reachable.
            getattr(sandbox, "sandbox_id", None) or "<no id reported>",
            self._task_id,
        )
        return sandbox, lease_started_at

    def _bootstrap(self) -> None:
        """Resolve the remote home, sync Hermes state, capture the env snapshot.

        Only the state-sync writer touches state. A reader — a second live
        owner of the same persistent scope — skips recovery, the push, and
        (via the manager's own empty-state check) the eventual teardown pull:
        commands run normally against the state the live writer maintains,
        and the reader promotes itself once the writer releases the role
        (see ``_before_execute``).
        """
        self._resolve_remote_paths()
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(self.remote_hermes_home),
            # Every writer command must observe host changes, including credential deletions.
            sync_interval=0,
            upload_fn=_watched(self._upload_one),
            delete_fn=_watched(self._delete_many),
            bulk_upload_fn=_watched(self._upload_many),
            bulk_download_fn=_watched(self._download_hermes_tar_for_sync_back),
        )
        took_the_role_over = False
        if (
            not self._is_writer
            and self._writer_lease is not None
            and self._writer_lease.try_acquire()
        ):
            # The role freed up between attach and bootstrap — possibly
            # because this environment's own earlier bring-up failed and
            # released it. Take it rather than waiting, blocked, behind a
            # readiness stamp nobody is going to write.
            self._is_writer = True
            took_the_role_over = True
        if not self._is_writer:
            # Host-side first: it is a local stat, it needs no working sandbox
            # transport, and it is the only one of the two gates that sees a
            # writer whose *incremental* sync failed (that leaves the stamp
            # from its bring-up in place while the sandbox's state goes stale).
            self._require_clean_state()
            if not self._probe_exists("-f", self._state_ready_marker(), action="reader readiness"):
                # The writer holding the lease has not (yet) completed a push
                # to THIS sandbox filesystem: it is mid-bootstrap, its push
                # failed, or the sandbox is a fresh replacement it has not
                # prepared. Running a command now would execute against a
                # sandbox with no (or half-written) Hermes state. Fail closed
                # — core's command retries re-enter this bootstrap, so the
                # command proceeds as soon as the writer finishes, and if the
                # writer failed and released the role, the re-acquire above
                # takes over on the next attempt.
                raise EnvironmentConnectionError(
                    f"Sandbox {self._sandbox_id} is not ready: the session "
                    "holding this scope's state-sync writer role has not "
                    "completed its state push",
                    retry_hint=(
                        "Another Hermes session is still preparing (or failed "
                        "to prepare) this sandbox's Hermes state. Retry the "
                        "same command."
                    ),
                )
            logger.info(
                "E2B: attached to sandbox %s as a reader — another live Hermes "
                "session holds the state-sync writer role for this scope. "
                "Commands run normally; host state propagates through the "
                "writer, and this session takes the role over once it is "
                "released.",
                self._sandbox_id,
            )
        else:
            try:
                # Mark the scope before anything is touched. From here until
                # the push completes, this sandbox's Hermes state is neither
                # the previous generation's nor yet this one's, and a reader
                # in another process that bootstrapped earlier — and so will
                # never probe the readiness stamp again — must not execute
                # against it.
                self._mark_state_dirty("a writer session is preparing this sandbox's state")
                # ``_resumed_existing`` records what the last _attach did, so
                # it is not on its own the question "is this generation a
                # takeover?". Re-acquiring the role above does not re-attach,
                # so an environment that created this sandbox, failed its
                # bring-up, released the role, and then took it back would
                # otherwise force-push with no recovery pass — burying
                # whatever the sandbox gained in between, including another
                # session's completed work.
                if self._resumed_existing or took_the_role_over:
                    # Invalidate the previous epoch's readiness stamp FIRST:
                    # it attests a push that is about to stop being current.
                    # Without this, readers pass their gate on a stale stamp
                    # and execute while this writer's push is mid-flight or
                    # has failed — the stamp must always attest the *current*
                    # lease holder's completed push.
                    self._clear_state_ready()
                    # A resumed sandbox can hold the only copy of something
                    # the agent wrote there — the previous teardown's pull may
                    # have failed, and the failure is only visible in a log.
                    # Recover before pushing, because the push is what would
                    # bury it. Raises when the sandbox state cannot be
                    # verified: the force-push below overwrites every remote
                    # file the host also has, so pushing over *unverified*
                    # remote state would silently destroy the only copy.
                    self._recover_remote_state()
                self._push_host_state()
            except BaseException:
                # Give the role up rather than holding it across failures:
                # with the stamp invalidated, readers are blocked, so a
                # lease held by a session that cannot push would freeze the
                # whole scope. Released, any healthy session — including
                # this one, via the re-acquire above — can take over. The
                # dirty mark deliberately stays: the state really is
                # unusable, and whoever completes a push clears it.
                self._release_writer_lease()
                raise
        self.init_session()

    def _push_host_state(self) -> None:
        """Force-push the host's ``~/.hermes`` into the sandbox. Fail loud.

        Continuing past a failed push would hand the agent a sandbox with none
        of its skills, credentials, or cached files and no indication why its
        tools behave differently.
        """
        with _watch_sync() as outcome:
            try:
                self._sync_manager.sync(force=True)
            except Exception as exc:
                # sync() swallows transport errors but can still raise before
                # the transaction starts (enumerating the host files).
                raise connection_error("initial state upload", exc) from exc
        if outcome.push_failed:
            raise connection_error("initial state upload", outcome.as_error())
        self._mark_state_ready()
        # Last, and only once the stamp is up: the host's state is now in the
        # sandbox and readers may run against it. A stamping failure raises
        # above, leaving the scope marked — readers stay held back until a
        # generation actually finishes.
        self._clear_state_dirty()

    def _mark_state_dirty(self, reason: str) -> None:
        """Record that this scope's sandbox state must not be run against.

        Set at the start of every writer transition — bring-up and promotion,
        before the readiness stamp is invalidated and the push begins — and
        after any incremental sync that failed. Ephemeral scopes have exactly
        one owner and no readers, so they skip it.
        """
        if self._dirty_state is not None:
            self._dirty_state.mark(reason)

    def _clear_state_dirty(self) -> None:
        """Record that this scope's state is usable again.

        Called after a completed force-push and after every successful
        incremental sync, in both cases unconditionally: clearing a marker
        this environment did not set is how a writer killed mid-transition
        gets healed by the next session that actually completes the work.
        """
        if self._dirty_state is not None:
            self._dirty_state.clear()

    def _require_clean_state(self) -> None:
        """Refuse a reader's command while this scope's state is known bad.

        The in-sandbox readiness stamp cannot cover this on its own: it is
        probed once, at bootstrap, and it stays valid across an incremental
        sync failure that has already made the sandbox's state stale. This is
        a local stat — no remote call, so it still answers when the sandbox
        transport is the thing that is broken.

        Checked at every admission, but deliberately not claimed to be atomic
        with the command launch: a writer can mark the scope immediately after
        this returns. What it rules out is *knowingly* running against a state
        the host has already recorded as in-transition or failed.
        """
        state = self._dirty_state
        if state is None:
            return
        reason = state.reason()
        if reason is None:
            return
        raise EnvironmentConnectionError(
            f"Sandbox {self._sandbox_id} is not ready: this scope's Hermes "
            f"state is being rebuilt or is known to be stale ({reason})",
            retry_hint=(
                "The session holding this scope's state-sync writer role is "
                "still preparing the sandbox, or its last state sync failed. "
                "Retry the same command — it proceeds as soon as that session "
                "completes a sync, and this session takes the role over if "
                "the role is released."
            ),
        )

    def _state_ready_marker(self) -> str:
        """The path of the writer's readiness stamp inside the sandbox.

        Next to ``~/.hermes``, not inside it: the teardown pull tars the whole
        ``.hermes`` directory, and a marker inside would be inferred back to a
        host path and applied there on every pull.
        """
        return f"{self._remote_home.rstrip('/')}/.hermes-e2b-state-ready"

    def _mark_state_ready(self) -> None:
        """Stamp the sandbox as writer-prepared. Persistent sandboxes only.

        Written after every successful force-push, so its presence means "the
        current writer generation completed a push to this filesystem" —
        every new writer generation invalidates it first (see
        ``_clear_state_ready``). Readers refuse to run commands until it
        exists (see ``_bootstrap``) — it is what stops a reader from
        executing in a sandbox whose writer never finished, or never started,
        preparing it. Load-bearing for readers, so a failure to write it
        fails the writer's bring-up too; the retry heals both.
        """
        if not self._persistent:
            return  # an ephemeral sandbox has exactly one owner, no readers
        try:
            self._sandbox.files.write(self._state_ready_marker(), f"{self._sandbox_id}\n")
        except Exception as exc:
            raise connection_error("stamping the sandbox state-ready", exc) from exc

    def _clear_state_ready(self) -> None:
        """Invalidate the previous epoch's readiness stamp.

        Runs as the new writer generation's FIRST state operation, before
        recovery and the push. A stamp that outlived its writer would let
        readers pass their gate while this generation's push is mid-flight or
        has failed — the exact hazard the gate exists to block. A missing
        stamp is fine (fresh replacement, or a prior clear); any other
        failure fails the bring-up, like every other state operation.
        """
        try:
            from e2b import FileNotFoundException
        except Exception:  # pragma: no cover - SDK absent

            class FileNotFoundException(Exception):  # type: ignore[no-redef]
                pass

        try:
            self._sandbox.files.remove(self._state_ready_marker())
        except (FileNotFoundError, FileNotFoundException):
            pass  # nothing to invalidate
        except Exception as exc:
            raise connection_error("invalidating the sandbox readiness stamp", exc) from exc

    def _recover_remote_state(self) -> None:
        """Copy agent-authored files out of a resumed sandbox, additively.

        Never destructive. A file the host does not have is restored in place.
        A file both sides have, with different contents, is a genuine conflict
        — there is no baseline that says which is newer — so the host copy is
        left alone and the sandbox copy is saved under
        ``~/.hermes/cache/e2b-recovered/<sandbox id>/`` and named in a warning.
        Nothing is discarded and nothing is overwritten.

        Nothing is written outside the *physically resolved* Hermes home
        either. Every destination is checked against that boundary, and a file
        whose in-place destination escapes it — because a recoverable root, or
        a directory inside one, is a symlink pointing out of the home — is
        quarantined instead of written through the escape.

        The guarantee is over what the snapshot **captured**: this recovers
        the sandbox's state as of the archive it downloads. A file the sandbox
        writes after that archive is taken is not in it, and if the host also
        has that path the force-push that follows overwrites it (a file the
        host does *not* have survives, because a force-push with no baseline
        deletes nothing). That window is inherent to a remote backend whose
        sandbox may be running processes nobody can drain, and is documented
        as an accepted limitation in the README.

        This is what keeps a failed teardown pull from becoming silent data
        loss. Without it the next session's push buries whatever the sandbox
        still held, and the only trace is a log line from the session before.

        Fails closed. "Verified: nothing to recover" (the sandbox has no
        Hermes state directory, or holds nothing the host lacks) lets bring-up
        proceed. "Recovery could not be completed" raises instead: the very
        next step is a force-push of the host snapshot, so continuing past an
        unverified sandbox would overwrite remote state that may be the only
        copy. The environment stays attached, and the next command — or the
        rebuilt environment after Hermes evicts this one — retries recovery.
        """
        remote_root = self.remote_hermes_home
        if not self._probe_exists("-d", remote_root, action="resumed-sandbox state recovery"):
            return

        try:
            self._recover_remote_tree(remote_root)
        except Exception as exc:
            raise connection_error(
                f"resumed-sandbox state recovery (sandbox {self._sandbox_id})", exc
            ) from exc

    def _probe_exists(self, test_flag: str, path: str, *, action: str) -> bool:
        """Whether *path* exists in the sandbox (``-d`` for a dir, ``-f`` a file).

        Answered with a command that always exits 0, so a transport failure is
        distinguishable from an absent path. Only an explicit yes/no counts;
        anything else — including the transport failing — raises, because both
        callers use the answer to decide whether skipping a safety step is
        justified.
        """
        probe = (
            f"if [ {test_flag} {shlex.quote(path)} ]; "
            "then echo __HERMES_E2B_PROBE_YES__; "
            "else echo __HERMES_E2B_PROBE_NO__; fi"
        )
        try:
            # 30s for the same reason as the $HOME probe: a resumed sandbox
            # cold-boots, and this may be the first command it sees.
            result = self._sandbox.commands.run(probe, timeout=30)
            stdout = getattr(result, "stdout", "") or ""
        except Exception as exc:
            raise connection_error(f"{action} (sandbox {self._sandbox_id})", exc) from exc
        if "__HERMES_E2B_PROBE_YES__" in stdout:
            return True
        if "__HERMES_E2B_PROBE_NO__" in stdout:
            return False
        raise connection_error(
            f"{action} (sandbox {self._sandbox_id})",
            RuntimeError(f"unexpected probe answer: {redact(stdout.strip()[:200])!r}"),
        )

    def _recover_remote_tree(self, remote_root: str) -> None:
        # Resolved once, and used as the only boundary every write is checked
        # against. Everything recovered from a sandbox is remote-authored
        # data: it may land under the Hermes home or nowhere at all.
        physical_home = _hermes_home().resolve()
        # Unresolved, for *reading* an existing host copy: a recoverable root
        # the user has symlinked elsewhere is a legitimate setup, and its
        # contents are what "does the host already have this?" means.
        host_root = _hermes_home()
        # Resolved once for the whole pass: it reads Hermes' credential and
        # cache mapping, and every destination below is checked against it.
        host_only = _HostOnlyPaths(physical_home)
        recovered: list[str] = []
        diverged: list[str] = []
        escaped: list[str] = []
        unplaceable: list[str] = []

        with tempfile.TemporaryDirectory(prefix="hermes-e2b-recover-") as staging:
            archive = Path(staging) / "remote.tar"
            self._download_hermes_tar(archive)
            extracted = Path(staging) / "tree"
            extracted.mkdir()
            # Resolved, because it is a containment boundary below and the
            # platform's temp directory is itself a symlink on macOS
            # (/var/folders -> /private/var/folders).
            physical_staging = extracted.resolve()
            with tarfile.open(archive) as tar:
                # Member by member, and never fatally. Only regular files are
                # ever recovered (the copy loop below handles those), so
                # nothing else is extracted at all.
                #
                # extractall() is deliberately not used: one bad member would
                # abort the whole recovery, and every failure mode here
                # originates in the *sandbox's* filesystem, where the user
                # cannot reach it — bring-up is what is failing. That turns a
                # single ordinary agent action into a permanently unusable
                # scope. The filter raises on an absolute symlink, a fifo or a
                # device node; the extraction itself raises OSError when the
                # host filesystem cannot represent a member's name at all (a
                # name that is not valid UTF-8 is legal on Linux and rejected
                # by APFS; a path can also exceed the host's limit once the
                # staging prefix is added). Skipping such a member with a
                # warning is the only outcome that converges.
                skipped: list[str] = []
                for member in tar.getmembers():
                    if not (member.isreg() or member.isdir()):
                        # A symlink, fifo, socket or device node. Never
                        # recovered, so never extracted; not a warning,
                        # because an agent creating one is ordinary.
                        logger.debug(
                            "E2B: not recovering non-regular archive member %s",
                            member.name,
                        )
                        continue
                    try:
                        tar.extract(member, extracted, filter="data")
                    except tarfile.FilterError:
                        skipped.append(f"{member.name} (unsafe path)")
                    except OSError as exc:
                        skipped.append(f"{member.name} ({exc.strerror or exc})")
                if skipped:
                    logger.warning(
                        "E2B: %d archive member(s) from sandbox %s could not be "
                        "recovered and were skipped — the host filesystem cannot "
                        "hold them, or their path is unsafe. Whatever they hold "
                        "stays in the sandbox: %s",
                        len(skipped),
                        self._sandbox_id,
                        "; ".join(sorted(skipped)[:10]),
                    )

            for root in RECOVERABLE_ROOTS:
                # Verified inside the staging tree, not assumed to be: the
                # remote home this path is built from comes from the sandbox.
                # `_resolve_remote_paths` already refuses a $HOME containing
                # traversal, and this is the check that makes that refusal
                # load-bearing rather than advisory — pointed outside, this
                # loop would enumerate arbitrary host directories and "recover"
                # them into ~/.hermes, from where the push would upload them
                # into the sandbox.
                source_root = _contained(
                    extracted / remote_root.lstrip("/") / root, physical_staging
                )
                if source_root is None:
                    logger.warning(
                        "E2B: refusing to read recovered %s for sandbox %s — it "
                        "resolves outside the staging directory",
                        root,
                        self._sandbox_id,
                    )
                    continue
                if not source_root.is_dir():
                    continue
                # Recovery restores in place ONLY into this root's canonical
                # physical location. Anything else — the root symlinked out of
                # the home, or merely somewhere else inside it — is
                # quarantined instead.
                #
                # This is deliberately total rather than a check against a set
                # of known host-only trees. A rule of the latter shape cannot
                # be a boundary: whether ~/.hermes/credentials is recognised as
                # host-only depends on whether a skill or config has *already*
                # registered a credential there, so the same layout would be
                # refused or honoured depending on when recovery happens to
                # run. Hermes gives an arbitrary in-home directory no meaning
                # at all, which makes a redirected root indistinguishable from
                # a deliberate `skills -> ~/.hermes/my-skills`, so the safe
                # reading of an ambiguous redirection is the conservative one.
                # Nothing is lost by it: the files are preserved in quarantine
                # and named in a warning.
                canonical = physical_home / root
                physical_root = _contained(host_root / root, physical_home)
                if physical_root != canonical:
                    logger.warning(
                        "E2B: %s does not resolve to %s (it resolves to %s). "
                        "Recovery restores in place only into the canonical "
                        "tree, so nothing from sandbox %s is written through "
                        "that redirection; anything it holds under this root is "
                        "quarantined instead.",
                        host_root / root,
                        canonical,
                        physical_root
                        if physical_root is not None
                        else (host_root / root).resolve(),
                        self._sandbox_id,
                    )
                    physical_root = None
                elif not host_only.allows_tree(physical_root):
                    # The canonical tree itself is host-only — a credential
                    # registered directly at ~/.hermes/skills, say.
                    logger.warning(
                        "E2B: %s holds host-owned state, so nothing from "
                        "sandbox %s is restored into it; anything it holds "
                        "under this root is quarantined instead.",
                        physical_root,
                        self._sandbox_id,
                    )
                    physical_root = None
                for source in source_root.rglob("*"):
                    if source.is_symlink() or not source.is_file():
                        continue
                    relative = source.relative_to(source_root)
                    named = str(Path(root) / relative)
                    try:
                        category, detail = self._recover_one_file(
                            source, host_root, host_only, physical_root, root, relative
                        )
                    except OSError as exc:
                        # This one file cannot be placed on this host — an
                        # ancestor the host holds as a regular file, a name it
                        # cannot represent, a permission it will not grant.
                        # Never fatal: recovery has to converge, and the
                        # remaining files are unaffected.
                        unplaceable.append(f"{named} ({exc.strerror or exc})")
                        continue
                    if category == "recovered":
                        recovered.append(named)
                    elif category == "diverged":
                        diverged.append(detail)
                    elif category == "escaped":
                        escaped.append(detail)

        if recovered:
            logger.info(
                "E2B: recovered %d file(s) authored inside sandbox %s: %s",
                len(recovered),
                self._sandbox_id,
                ", ".join(sorted(recovered)[:10]),
            )
        if diverged:
            logger.warning(
                "E2B: %d file(s) differ between this host and sandbox %s. The "
                "host copy is kept and the sandbox copy was saved alongside it "
                "so neither is lost — reconcile them by hand: %s",
                len(diverged),
                self._sandbox_id,
                "; ".join(sorted(diverged)[:10]),
            )
        if escaped:
            logger.warning(
                "E2B: %d file(s) from sandbox %s could not be restored in "
                "place because their destination resolves outside %s, or "
                "outside the recoverable tree it belongs to — a symlink leads "
                "somewhere remote data must not be written. Nothing was "
                "written there; the sandbox copies were quarantined "
                "instead: %s",
                len(escaped),
                self._sandbox_id,
                physical_home,
                "; ".join(sorted(escaped)[:10]),
            )
        if unplaceable:
            logger.warning(
                "E2B: %d file(s) from sandbox %s could not be written to this "
                "host at all and were left in the sandbox: %s",
                len(unplaceable),
                self._sandbox_id,
                "; ".join(sorted(unplaceable)[:10]),
            )

    def _recover_one_file(
        self,
        source: Path,
        host_root: Path,
        host_only: _HostOnlyPaths,
        physical_root: Path | None,
        root: str,
        relative: Path,
    ) -> tuple[str, str]:
        """Place one file recovered from the sandbox. Never overwrites.

        Returns ``(category, detail)`` for the caller's reporting. Raises
        ``OSError`` when this particular file cannot be written on this host
        (the caller reports it and carries on) and ``RuntimeError`` only when
        the quarantine tree itself is unsafe, which must fail the whole
        recovery — see :meth:`_quarantine_path`.
        """
        named = str(Path(root) / relative)
        # Where the host copy would be, followed as the user set it up.
        # Read through freely; never written through.
        host_copy = host_root / root / relative

        if host_copy.is_file():
            try:
                identical = host_copy.read_bytes() == source.read_bytes()
            except OSError:
                # Unreadable host copy: treat as divergence rather than
                # assume the sandbox copy is redundant. Discarding remote
                # data on the strength of a file we could not read would be
                # the one unrecoverable outcome.
                identical = False
            if identical:
                return "identical", named
            # Neither copy can be discarded. Without a baseline there is
            # nothing that says which is newer, and the host copy is about to
            # be pushed over the remote one — so park the remote version
            # somewhere inert and say where.
            parked = self._park_in_quarantine(source, host_only, root, relative)
            return "diverged", f"{named} -> {parked}"

        if host_copy.exists():
            # A directory, fifo, socket, or device node where a regular file
            # belongs. It is never read (reading a fifo would hang bring-up
            # with no timeout) and never written over; park the copy instead.
            parked = self._park_in_quarantine(source, host_only, root, relative)
            return "diverged", f"{named} (host path is not a regular file) -> {parked}"

        # The host lacks it, so it can be restored in place — but only if that
        # place is physically inside the recoverable tree it belongs to, which
        # is itself a tree remote data may write, inside the Hermes home. When
        # it is not, the file is still remote-authored data that must not be
        # lost, so it goes to quarantine instead of being written through the
        # escape.
        target = None if physical_root is None else _contained(host_copy, physical_root)
        if target is not None and host_only.forbids(target):
            # Inside the recoverable tree, and still host-only: a credential
            # registered under skills/ keeps that tree recoverable, but the
            # credential itself is not a thing remote data may create.
            target = None
        if target is None:
            parked = self._park_in_quarantine(source, host_only, root, relative)
            return "escaped", f"{named} -> {parked}"

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return "recovered", named

    def _park_in_quarantine(
        self, source: Path, host_only: _HostOnlyPaths, root: str, relative: Path
    ) -> Path:
        """Preserve distinct copies, reusing an identical quarantined version."""
        try:
            with source.open("rb") as incoming:
                digest = hashlib.file_digest(incoming, "sha256").hexdigest()
            for version in (None, digest):
                destination = self._quarantine_path(host_only, root, relative, version=version)
                if destination.exists():
                    if destination.is_file():
                        with destination.open("rb") as existing:
                            if hashlib.file_digest(existing, "sha256").hexdigest() == digest:
                                return destination
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation also refuses a destination that appeared after the check.
                with destination.open("xb") as parked:
                    try:
                        with source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, parked)
                        parked.flush()
                    except BaseException:
                        destination.unlink()
                        raise
                shutil.copystat(source, destination)
                return destination
        except OSError as exc:
            raise RuntimeError(
                f"could not preserve the quarantine copy of {Path(root) / relative}: {exc}"
            ) from exc
        raise RuntimeError(
            f"the quarantine version for {Path(root) / relative} is occupied by different "
            "content; refusing to overwrite it. Move that entry aside and retry."
        )

    def _quarantine_root(self, host_only: _HostOnlyPaths) -> Path:
        """The one tree quarantine may write into: its canonical location.

        Required to resolve to exactly ``<physical home>/cache/e2b-recovered``,
        the same canonical-position rule the recoverable roots follow, and for
        the same reason. Asking instead whether the redirected target *looks*
        host-owned is not a boundary: recognition depends on whether Hermes
        happens to have something registered there, so an unregistered
        ``~/.hermes/credentials`` would be accepted while the identical layout
        with one credential in it would not.

        This covers the whole root and every ancestor of it *below the home*
        in one check — ``cache/`` symlinked away, ``e2b-recovered`` symlinked
        away — because either makes the resolved path differ from the
        canonical one. A symlinked ``HERMES_HOME`` does not: ``host_only.home``
        is already physically resolved, and that layout is supported.
        """
        canonical = host_only.home.joinpath(*QUARANTINE_DIR)
        try:
            resolved = canonical.resolve()
        except OSError as exc:
            raise RuntimeError(
                f"the quarantine directory {canonical} cannot be resolved "
                f"({exc}); refusing to write sandbox-authored state without a "
                "verified destination."
            ) from exc
        if resolved != canonical:
            raise RuntimeError(
                f"the quarantine directory {canonical} resolves to {resolved} "
                "instead of itself, so it or one of its ancestors is a symlink; "
                "refusing to write sandbox-authored state through it. Make "
                f"{canonical} a real directory (remove the redirection) and "
                "retry."
            )
        return canonical

    def _quarantine_path(
        self, host_only: _HostOnlyPaths, root: str, relative: Path, *, version: str | None = None
    ) -> Path:
        """Where one remote copy is parked, verified inside the quarantine tree.

        The boundary is the quarantine tree itself, not the Hermes home. The
        home is far too wide for this: a symlink anywhere *inside* the
        quarantine tree — ``…/e2b-recovered/<id>/skills`` pointing at
        ``~/.hermes/credentials`` — stays inside the home while putting
        sandbox-authored bytes exactly where they must never go. The sandbox
        id and the archive-derived relative path are steered through the same
        check, so neither can redirect a write either.

        Raises when the destination cannot be placed safely. That fails the
        whole recovery — deliberately: the caller's next act is a force-push of
        the host snapshot, and proceeding would bury remote state that could be
        neither preserved in place nor parked. The message names the path to
        fix, and the failure is confined to this scope's bring-up.
        """
        quarantine_root = self._quarantine_root(host_only)
        directory = self._sandbox_id or "unknown"
        if version is not None:
            directory = f"{directory}-{version}"
        candidate = quarantine_root / directory / root / relative
        # Two things have to hold. The destination must stay inside the
        # verified quarantine tree — any symlink that redirects it, into
        # credentials or another in-home tree or out of the home, leaves that
        # tree by definition. And it must not land on host-only state *nested
        # inside* that tree: Hermes accepts arbitrary registered credential
        # paths, so one can be registered at a path under the quarantine tree,
        # and parking over it would replace a host credential with remote
        # bytes. The generic ``forbids`` cannot answer the second question —
        # it treats the quarantine tree itself as host-only, correctly for
        # restores — so the quarantine-specific form is used.
        destination = _contained(candidate, quarantine_root)
        if destination is not None and host_only.forbids_quarantine(destination):
            raise RuntimeError(
                f"the quarantine destination for {Path(root) / relative} is "
                f"{destination}, which Hermes maps as host-owned state (a "
                "registered credential, or a tree the host owns); refusing to "
                "replace it with sandbox-authored bytes. Move that mapping out "
                "of the quarantine directory and retry."
            )
        if destination is None:
            raise RuntimeError(
                f"the quarantine destination for {Path(root) / relative} does "
                f"not stay inside {quarantine_root}; refusing to write "
                "sandbox-authored state through it. Remove the symlink under "
                "the quarantine directory and retry."
            )
        return destination

    def _resolve_remote_paths(self) -> None:
        """Learn the sandbox's real ``$HOME`` and settle on a usable cwd.

        Both matter and one probe answers both. E2B's ``base`` template runs as
        ``user`` with ``/home/user``, but a custom template may use ``/root`` or
        anything else — and every synced path is rooted at ``$HOME/.hermes``.

        The cwd needs checking rather than guessing: Hermes wraps every command
        in ``builtin cd -- <cwd> || exit 126``, so a configured directory that
        does not exist in this template makes *every* command fail with 126 and
        no useful message. Falling back to the home directory with a warning is
        far better than that.
        """
        # DEFAULT_CWD, not the configured cwd: it is the value that means "the
        # user did not choose a directory", so falling back from it is routine
        # rather than something to warn about.
        home = self._settings.cwd or DEFAULT_CWD
        cwd_resolved = ""
        try:
            # 30s, not the SDK's 60s default and not something tighter: a
            # resumed sandbox cold-boots, so the first command after a resume
            # waits on envd coming up.
            result = self._sandbox.commands.run(
                "printf '%s\\n%s\\n' \"$HOME\" "
                f"\"$(cd {shlex.quote(self.cwd)} >/dev/null 2>&1 && pwd -P || printf '')\"",
                timeout=30,
            )
            lines = (getattr(result, "stdout", "") or "").splitlines()
            reported = lines[0].strip() if lines else ""
            if reported.startswith("/") and _is_plain_absolute_path(reported):
                home = reported
            elif reported:
                # The sandbox is untrusted, and this string becomes a host path
                # (the recovery archive is unpacked under it before its
                # contents are copied into ~/.hermes). A ".." in it would make
                # that source directory resolve outside the staging tree, so
                # recovery could be pointed at arbitrary host files — which the
                # push that follows would then upload into the sandbox. Refuse
                # the value rather than normalise it: there is no legitimate
                # reason for $HOME to contain traversal.
                logger.warning(
                    "E2B: sandbox %s reported a $HOME that is not a plain "
                    "absolute path (%r); using %s instead",
                    self._sandbox_id,
                    redact(reported[:120]),
                    home,
                )
            if len(lines) > 1:
                cwd_resolved = lines[1].strip()
        except Exception as exc:
            # WARNING, not debug: the resolved home also anchors the readiness
            # stamp, so a one-sided fallback makes this session look for (or
            # write) the stamp at a different path than its peers on a
            # custom-home template — readers then fail closed misdiagnosing
            # the writer until a later probe succeeds.
            logger.warning(
                "E2B: could not probe the sandbox $HOME/cwd (%s); using %s",
                redact(exc),
                home,
            )

        self._remote_home = home

        if cwd_resolved.startswith("/"):
            self.cwd = cwd_resolved
            return

        if self.cwd and self.cwd not in {"", "~", "/root", DEFAULT_CWD}:
            # A directory the user asked for explicitly, that this template
            # does not have.
            logger.warning(
                "E2B: configured working directory %s does not exist in "
                "template %s; using %s instead",
                self.cwd,
                self._settings.template,
                home,
            )
        self.cwd = home

    # ------------------------------------------------------------------
    # Lease
    # ------------------------------------------------------------------

    def _desired_lease(self, command_timeout: int | None) -> int:
        """Lease for this environment's lifecycle and the next command.

        Normal ephemeral environments need to outlive Hermes' idle deadline
        and one complete reaper interval so explicit cleanup gets to pull state
        before E2B's ``on_timeout: kill`` backstop. The prompt probe is never
        registered with that reaper and deliberately keeps its short lease.
        """
        if not self._persistent and not self._is_probe:
            return self._settings.ephemeral_lease_for(command_timeout)
        return self._settings.lease_for(command_timeout)

    def _track_lease(self, lease_seconds: int, *, started_at: float) -> None:
        """Track a conservative server deadline for a create/connect request.

        E2B starts the lease while the control-plane request is in flight. The
        timestamp must therefore be captured immediately before that request;
        using response time would overstate the remaining lease by its latency.
        """
        self._lease_deadline = max(self._lease_deadline, started_at + lease_seconds)

    def _retain_reconnected_sandbox(
        self,
        sandbox: Any,
        lease_seconds: int,
        *,
        started_at: float,
    ) -> Any:
        """Validate and retain a fresh SDK object returned by reconnect."""
        expected_id = self._sandbox_id
        actual_id = sandbox_api.sandbox_id_of(sandbox)
        if expected_id is None or actual_id != expected_id:
            raise RuntimeError(
                "E2B reconnect returned a different sandbox "
                f"({actual_id!r}, expected {expected_id!r})"
            )
        self._sandbox = sandbox
        self._track_lease(lease_seconds, started_at=started_at)
        return sandbox

    def _reconnect_for_command(self, lease_seconds: int) -> Any:
        """Reconnect with the active profile key and retain the fresh object."""
        sandbox = self._require_sandbox()
        api_key = self._api_key()
        started_at = time.monotonic()
        fresh = sandbox_api.renew_lease(sandbox, lease_seconds, api_key)
        return self._retain_reconnected_sandbox(
            fresh,
            lease_seconds,
            started_at=started_at,
        )

    def _reconnect_for_cleanup(self, lease_seconds: int) -> Any:
        """Reconnect with this attachment's connection and retain the result."""
        sandbox = self._require_sandbox()
        started_at = time.monotonic()
        fresh = sandbox_api.reconnect_for_cleanup(sandbox, lease_seconds)
        return self._retain_reconnected_sandbox(
            fresh,
            lease_seconds,
            started_at=started_at,
        )

    def _renew_lease(self, lease_seconds: int) -> None:
        """Extend the sandbox lease so ``on_timeout`` cannot fire mid-command.

        Nothing in the E2B SDK extends a lease implicitly, and the environment
        outlives the lease it was created with, so this runs before every
        command. It is skipped when the tracked deadline already covers the
        request — the tracked value is a lower bound (``connect`` never
        shortens a running sandbox), so skipping is always safe.
        """
        if time.monotonic() + lease_seconds <= self._lease_deadline:
            return
        try:
            self._reconnect_for_command(lease_seconds)
        except EnvironmentConnectionError:
            # Credential resolution failed before a command was admitted. A
            # persistent writer that cannot authenticate must not freeze every
            # healthy peer behind its lease; promotion will run the full
            # recover-then-push protocol if this environment later recovers.
            self._release_writer_lease()
            raise
        except Exception as exc:
            if is_missing_sandbox(exc):
                self._replace_missing_sandbox(lease_seconds)
                return
            self._release_writer_lease()
            raise connection_error(
                f"extending the sandbox lifecycle lease to {lease_seconds}s",
                exc,
            ) from exc

    def _replace_missing_sandbox(self, lease_seconds: int) -> None:
        """Rebuild after the sandbox was destroyed out from under us.

        A persistent sandbox can disappear server-side (account purge, manual
        kill, an ``on_timeout: kill`` sandbox from an older plugin version).
        Rebuilding keeps the session alive; the filesystem is gone, so the
        Hermes state push has to run again.
        """
        logger.warning(
            "E2B: sandbox %s no longer exists; creating a replacement for task %s",
            self._sandbox_id,
            self._task_id,
        )
        self._sandbox = None
        self._sandbox_id = None
        self._bootstrapped = False
        self._sync_manager = None
        self._snapshot_ready = False
        self._lease_deadline = 0.0
        self._attach(lease_seconds)
        self._bootstrapped = True
        try:
            self._bootstrap()
        except BaseException:
            self._bootstrapped = False
            raise

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _before_execute(self) -> None:
        """Bring the sandbox up and push changed Hermes state before a command.

        A failed push is fatal to the command, not a warning. The manager's
        cycle is transactional over uploads *and* deletes, and the sync set
        includes credential files — so a failed cycle can leave a
        host-deleted credential live in the sandbox, an updated credential
        unpropagated, or a changed skill missing. There is no way to tell a
        harmless stale cache file from those from out here (the rollback is
        all-or-nothing), so the command must not run against state that is
        known to be stale. The manager rolled back, and core retries the
        command, so a transient failure heals on the retry.

        A reader does not sync — state is the live writer's job — but tries
        to take the role over first. Promotion marks the scope dirty,
        invalidates the previous epoch's readiness stamp, and runs the
        writer's full bring-up (recovery, then the force-push) under the same
        fail-closed rules; on failure the lease is released so any healthy
        session — this one included, on its next command — can take the role
        instead. A reader that stays a reader admits its command only if the
        scope is not marked: a writer transition in flight, or a writer sync
        that failed, holds it back with a retry hint.

        Everything here runs under ``_lock``, the incremental sync included:
        released early, the sync could interleave with this environment's own
        teardown (running after the "final" teardown pull and streaming past
        the lease release into the next writer's recovery).
        """
        with self._lock:
            self._ensure_ready()
            manager = self._sync_manager
            if manager is None:
                return
            if not self._is_writer:
                lease = self._writer_lease
                if lease is None or not lease.try_acquire():
                    # Still a reader: nothing to sync, but this is where a
                    # reader admits a command, so it is where the host-side
                    # state record has to be consulted. Promotion is tried
                    # first, above, so a marker left behind by a crashed
                    # writer is healed by taking the role rather than
                    # blocking on it.
                    self._require_clean_state()
                    return
                # Promoted. The sandbox has been live under another owner, so
                # run the full writer bring-up: invalidate the old readiness
                # stamp, recover what the sandbox holds — the previous
                # writer's final pull may have failed — then push.
                try:
                    self._mark_state_dirty("a writer session is taking over this sandbox's state")
                    self._clear_state_ready()
                    self._recover_remote_state()
                    self._push_host_state()
                except BaseException:
                    self._release_writer_lease()
                    raise
                self._is_writer = True
                logger.info(
                    "E2B: took over the state-sync writer role for sandbox %s",
                    self._sandbox_id,
                )
                return  # state was just force-synced; nothing incremental left
            with _watch_sync() as outcome:
                try:
                    manager.sync()
                except Exception as exc:
                    self._mark_state_dirty("the writer session's last state sync failed")
                    raise connection_error("state sync before the command", exc) from exc
            if outcome.push_failed:
                # The writer refuses its own command below; the mark is what
                # stops *readers* from running against the state this sync
                # failed to correct. Their in-sandbox readiness stamp attests
                # this writer's bring-up push and is still there, so without
                # this they would keep executing against a sandbox holding,
                # for instance, a credential the host has deleted.
                self._mark_state_dirty("the writer session's last state sync failed")
                logger.warning(
                    "E2B: pushing Hermes state to sandbox %s failed (%s); "
                    "refusing to run the command against stale state",
                    self._sandbox_id,
                    outcome.detail,
                )
                raise connection_error("state sync before the command", outcome.as_error())
            # Successful delta push. Nothing is marked around it — a reader
            # gated on every routine sync would flap for no observable gain,
            # since the host state a delta carries is exactly what the reader
            # would otherwise be waiting for — but the clear is
            # unconditional, so a marker left by a crashed predecessor is
            # healed here too.
            self._clear_state_dirty()

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        """Start one command and return a handle Hermes can drain and kill."""
        shell = "bash -l -c" if login else "bash -c"
        command = f"{shell} {shlex.quote(cmd_string)}"
        lease = self._desired_lease(timeout)
        # E2B bounds the command stream itself; give it more room than the
        # host-side deadline so Hermes' own timeout path (which reports 124 and
        # kills the command) is what the user sees.
        stream_timeout = float(timeout + self._settings.command_grace_seconds)

        with self._lock:
            self._ensure_ready(lease_seconds=lease)
            sandbox = self._sandbox
            try:
                command_handle = sandbox.commands.run(
                    command,
                    background=True,
                    timeout=stream_timeout,
                    stdin=True if stdin_data else None,
                )
            except Exception as exc:
                raise connection_error("command launch", exc) from exc
            # Registered only after the launch succeeded, and still under the
            # lock, so cleanup() cannot observe a zero in-flight count while
            # this command is starting.
            self._inflight += 1

        try:
            return E2BProcessHandle(
                command_handle,
                stdin_data=stdin_data,
                on_done=self._command_finished,
            )
        except BaseException:
            # The handle owns the in-flight release; if it never came into
            # existence, release here or cleanup would defer forever.
            self._command_finished()
            try:
                command_handle.kill()
            except Exception:
                pass
            raise

    def _command_finished(self) -> None:
        """Settle a deferred teardown once the last command has finished."""
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if self._close_pending and self._inflight == 0:
                self._close_pending = False
                self._teardown()

    # ------------------------------------------------------------------
    # File sync transport
    # ------------------------------------------------------------------

    def _require_sandbox(self):
        sandbox = self._sandbox
        if sandbox is None:
            raise EnvironmentConnectionError("No E2B sandbox is attached")
        return sandbox

    def _upload_one(self, host_path: str, remote_path: str) -> None:
        self._upload_many([(host_path, remote_path)])

    def _upload_many(self, files: list[tuple[str, str]]) -> None:
        """Upload files while bounding simultaneously open host file handles.

        ``files.write_files`` creates missing parent directories itself, so
        there is no separate ``mkdir -p`` pass to fail halfway. E2B 2.46's
        octet-stream path streams each binary file object, but may issue one
        request per entry; ``_UPLOAD_BATCH`` bounds descriptors, not requests.
        """
        if not files:
            return
        sandbox = self._require_sandbox()
        # Batched so a large skills/cache tree cannot exhaust the process file
        # descriptor limit: every entry in one call is an open file object.
        for start in range(0, len(files), _UPLOAD_BATCH):
            batch = files[start : start + _UPLOAD_BATCH]
            handles = []
            entries = []
            try:
                for host_path, remote_path in batch:
                    handle = open(host_path, "rb")
                    handles.append(handle)
                    entries.append({"path": remote_path, "data": handle})
                if entries:
                    sandbox.files.write_files(entries)
            finally:
                for handle in handles:
                    try:
                        handle.close()
                    except OSError:
                        pass

    def _delete_many(self, remote_paths: list[str]) -> None:
        if not remote_paths:
            return
        sandbox = self._require_sandbox()
        # One shell call rather than N RPCs, and `rm -f` is idempotent, so a
        # path already gone does not fail the sync transaction.
        sandbox.commands.run(quoted_rm_command(remote_paths), timeout=60)

    def _download_hermes_tar(self, dest: Path) -> None:
        """Stream the sandbox's ``~/.hermes`` to *dest* as a tar archive."""
        sandbox = self._require_sandbox()
        base = self.remote_hermes_home
        # Unique per environment *object*, not per process: two environments
        # can transiently share one persistent sandbox (the idle reaper evicts
        # one while a replacement is built), and a shared temp path would let
        # one teardown's archive overwrite the other's mid-read.
        remote_tar = f"/tmp/hermes-sync-back.{os.getpid()}.{self._session_id}.tar"
        relative = base.lstrip("/")
        sandbox.commands.run(
            f"tar cf {shlex.quote(remote_tar)} -C / {shlex.quote(relative)}",
            timeout=300,
        )
        try:
            written = 0
            reader = sandbox.files.read(remote_tar, format="stream")
            try:
                with open(dest, "wb") as out:
                    for chunk in reader:
                        written += len(chunk)
                        if written > MAX_SYNC_BACK_BYTES:
                            raise EnvironmentConnectionError(
                                "E2B sync-back archive exceeded "
                                f"{MAX_SYNC_BACK_BYTES} bytes; refusing to "
                                "download the rest"
                            )
                        out.write(chunk)
            finally:
                close = getattr(reader, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        finally:
            try:
                sandbox.commands.run(f"rm -f {shlex.quote(remote_tar)}", timeout=30)
            except Exception as exc:
                logger.debug("E2B: removing the sync-back tar failed: %s", exc)

    def _download_hermes_tar_for_sync_back(self, dest: Path) -> None:
        """Renew through a fresh SDK object immediately before one pull attempt.

        Hermes owns the retry loop and invokes this callback once per attempt.
        Keeping reconnect here gives every actual tar/download attempt a fresh
        transfer lease without duplicating or weakening FileSyncManager's retry
        policy.
        """
        cleanup_lease = self._settings.cleanup_lease_for(_SYNC_BACK_COMMAND_TIMEOUT_SECONDS)
        try:
            self._reconnect_for_cleanup(cleanup_lease)
        except Exception as exc:
            raise connection_error(
                f"renewing the teardown transfer lease to {cleanup_lease}s",
                exc,
            ) from exc
        self._download_hermes_tar(dest)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def __del__(self) -> None:
        """Last-resort release. Never syncs.

        ``BaseEnvironment.__del__`` calls ``cleanup()``, and the collector can
        run it at *any* point in *any* stack — including in the middle of
        another environment's state pull. That pull holds an exclusive
        ``flock`` on ``~/.hermes/.sync.lock``; a nested pull opens a second
        descriptor for the same file and blocks on a lock this very thread
        holds, deadlocking the process for good.

        So finalisation releases resources and nothing else. Every path Hermes
        actually drives — session close, the idle reaper, ``atexit`` — calls
        ``cleanup()`` explicitly and does sync; anything a dropped reference
        misses is recovered on the next resume.
        """
        try:
            self.cleanup(sync_state=False)
        except Exception:
            pass

    def cleanup(self, *, sync_state: bool = True) -> None:
        """Release this environment.

        Called by Hermes on session close, by the idle reaper, and at
        ``atexit``. Idempotent; operational backend failures are logged and
        suppressed. Process-control exceptions are re-raised only after
        mandatory lifecycle cleanup has completed.

        *sync_state* is False only for finalisation — see :meth:`__del__`.
        """
        with self._lock:
            already_closed = self._closed
            self._closed = True
            if already_closed and not self._close_pending:
                return
            if self._inflight > 0:
                # A command is running. Destroying the sandbox now would kill
                # it; the last command to finish performs the teardown.
                self._close_pending = True
                logger.debug(
                    "E2B: cleanup deferred for task %s (%d command(s) in flight)",
                    self._task_id,
                    self._inflight,
                )
                return
            self._close_pending = False
            self._teardown(sync_state=sync_state)

    def _teardown(self, *, sync_state: bool = True) -> None:
        """Perform the actual teardown. Caller holds ``_lock``."""
        sandbox = self._sandbox
        if sandbox is None:
            # No sandbox — but the writer lease may still be held (a failed
            # sandbox replacement leaves exactly this state). Leaking it here
            # would lock every other session out of the writer role for the
            # rest of this process's life.
            self._release_writer_lease()
            self._torn_down.set()
            return
        sandbox_id = self._sandbox_id
        manager = self._sync_manager
        self._sync_manager = None
        process_control: BaseException | None = None

        # The sandbox reference stays live through sync-back: the transport
        # callbacks run commands and read files inside it, so clearing it
        # first would make every teardown fail its pull. State pull and
        # lifecycle cleanup are deliberately separate phases: a pull failure
        # must never skip the mandatory lease release or ephemeral kill.
        try:
            try:
                if manager is not None and not sync_state:
                    logger.debug(
                        "E2B: skipping the state pull for sandbox %s "
                        "(finalisation, not an explicit teardown)",
                        sandbox_id,
                    )
                elif manager is not None and _sync_back_in_flight():
                    # Re-entered from inside another pull on this thread. Hermes'
                    # cross-process lock is not reentrant, so proceeding would
                    # deadlock against the outer hold.
                    logger.debug(
                        "E2B: skipping a nested state pull for sandbox %s",
                        sandbox_id,
                    )
                elif manager is not None and not self._is_writer:
                    # The teardown pull is plugin-managed synchronisation, so it
                    # belongs to the writer only. An environment that was demoted
                    # — its bring-up or promotion failed after a push had already
                    # committed, so the role was released while this manager kept
                    # its baseline — would otherwise pull the sandbox's state onto
                    # the host while a *different* session is pushing to it, and
                    # could apply content that session has already superseded.
                    # Nothing is stranded: whatever the sandbox still holds is
                    # recovered by the next writer generation before it pushes.
                    logger.info(
                        "E2B: skipping the teardown state pull for sandbox %s — this "
                        "session no longer holds the state-sync writer role. The "
                        "next resume recovers whatever the sandbox still holds.",
                        sandbox_id,
                    )
                elif manager is not None:
                    self._pull_state_for_teardown(manager, sandbox_id)
            except _PROCESS_CONTROL_EXCEPTIONS as exc:
                # FileSyncManager deliberately re-delivers a deferred SIGINT
                # after its transaction. Preserve that (and the other Python
                # process-control signals), but only after mandatory cleanup.
                process_control = exc

            # A successful per-attempt reconnect replaces the SDK object so
            # following operations use its current envd connection metadata.
            sandbox = self._sandbox or sandbox
            try:
                if self._persistent:
                    # Deliberately nothing destructive. E2B pauses the sandbox
                    # itself when the lease expires (`on_timeout: pause`,
                    # filesystem preserved), so no Hermes process ever performs a
                    # destructive operation on a sandbox another process may still
                    # be using. Only this environment's own scratch files go —
                    # they are named by its session id, so a sibling environment
                    # sharing the sandbox keeps its own.
                    self._remove_session_scratch(sandbox)
                    logger.info(
                        "E2B: detached from persistent sandbox %s "
                        "(E2B will pause it when its lease expires)",
                        sandbox_id,
                    )
                else:
                    try:
                        if sandbox_api.kill(sandbox):
                            logger.info("E2B: killed ephemeral sandbox %s", sandbox_id)
                        else:
                            logger.info(
                                "E2B: ephemeral sandbox %s was already gone",
                                sandbox_id,
                            )
                    except Exception as exc:
                        logger.warning(
                            "E2B: killing sandbox %s failed: %s",
                            sandbox_id,
                            redact(exc),
                        )
            except _PROCESS_CONTROL_EXCEPTIONS as exc:
                if process_control is None:
                    process_control = exc
        finally:
            # Released only now, after the pull: a reader promoting any
            # earlier could push its host snapshot while this teardown is
            # still reading the sandbox's state back.
            self._release_writer_lease()
            self._sandbox = None
            self._sandbox_id = None
            self._bootstrapped = False
            self._torn_down.set()
        if process_control is not None:
            raise process_control

    def _pull_state_for_teardown(self, manager: FileSyncManager, sandbox_id: str | None) -> None:
        """Run the teardown pull, suppressing operational failures.

        Hermes may reach cleanup one full reaper interval after the idle
        deadline. A persistent E2B sandbox can already be paused by then, so
        every bulk-download attempt reconnects through
        :meth:`_download_hermes_tar_for_sync_back` immediately before touching
        the data plane. FileSyncManager remains the sole owner of retries.
        """
        try:
            with _sync_back_guard(), _watch_sync() as outcome:
                manager.sync_back()
        except Exception as exc:
            if is_missing_sandbox(exc):
                logger.info(
                    "E2B: skipping the teardown state pull for sandbox %s — "
                    "the sandbox is already gone",
                    sandbox_id,
                )
            else:
                logger.error(
                    "E2B: pulling state back from sandbox %s failed before it completed (%s). %s",
                    sandbox_id,
                    redact(exc),
                    self._pull_failure_consequence(),
                )
            return

        if outcome.pull_failed:
            logger.error(
                "E2B: pulling state back from sandbox %s failed (%s). %s",
                sandbox_id,
                outcome.detail,
                self._pull_failure_consequence(),
            )

    def _pull_failure_consequence(self) -> str:
        if self._persistent:
            return (
                "Changes made inside the sandbox under ~/.hermes have not "
                "reached the host; the next resume will recover them before "
                "pushing anything."
            )
        return (
            "Changes made inside this ephemeral sandbox under ~/.hermes may "
            "be lost; cleanup will still destroy the sandbox so it cannot be "
            "left billing."
        )

    def _release_writer_lease(self) -> None:
        if self._writer_lease is not None:
            self._writer_lease.release()
            self._is_writer = False

    def _remove_session_scratch(self, sandbox: Any) -> None:
        """Delete this environment's env-snapshot scratch files.

        ``BaseEnvironment.init_session`` writes a snapshot per environment into
        the sandbox's temp dir. A persistent sandbox outlives many sessions, so
        without this they accumulate for the sandbox's whole life.
        """
        paths = [self._snapshot_path, self._cwd_file]
        try:
            sandbox.commands.run("rm -f " + " ".join(shlex.quote(p) for p in paths), timeout=30)
        except Exception as exc:
            logger.debug("E2B: removing session scratch files failed: %s", exc)

    def wait_for_cleanup(self, timeout: float = 30.0) -> bool:
        """Block until a deferred teardown has settled.

        Hermes calls this at ``atexit`` so the process does not exit while an
        ephemeral sandbox is still being destroyed.
        """
        return self._torn_down.wait(timeout=timeout)
