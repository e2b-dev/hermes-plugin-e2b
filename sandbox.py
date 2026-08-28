"""Sandbox identity, discovery, creation, and lease renewal.

Identity model
--------------
One Hermes environment object owns exactly one sandbox. The sandbox is
identified by *metadata stored on the E2B side*, derived from the ``task_id``
Hermes already resolved (``_resolve_container_task_id``) plus the active
profile. There is deliberately no local pointer store:

* ``Sandbox.list()`` returns running **and** paused sandboxes by default, so
  the server is the source of truth for "does my sandbox still exist";
* a sandbox whose creation response was lost in flight is still discoverable
  on the next bring-up instead of being billed forever with nobody holding
  its id.

Lease model
-----------
Persistent sandboxes are created with
``lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}}`` so E2B
pauses them — preserving the filesystem — when the lease runs out. Nothing in
this plugin ever pauses or kills a persistent sandbox, so two Hermes processes
sharing one can never destroy it under each other. State synchronisation
between concurrent owners is arbitrated separately, by the per-scope
:class:`WriterLease` (who may synchronise) and :class:`DirtyState` (whether
what a reader would run against is currently trustworthy) — see the ownership
model note in ``environment.py``.

Both live in ``~/.hermes/cache/e2b/``, which Hermes does not mirror into
remote backends (see :func:`_scope_state_dir`), so they are host-local truth
about a sandbox whose own transport may be the thing that is broken.

Leases are renewed with the class-level ``Sandbox.connect(id, timeout=…)``,
which the SDK documents as extend-only for a running sandbox. It also returns a
new SDK object carrying the current envd connection metadata when a paused
sandbox resumes. ``set_timeout()`` is NOT used: it can *reduce* a lease, so on
a shared sandbox a process running a short command could cut short a long
command in another process.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import PROBE_TASK_ID, E2BSettings
from .errors import connection_error

try:  # pragma: no cover - Windows has no fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Marks a sandbox as belonging to this plugin. Never adopt anything else.
METADATA_PLUGIN_KEY = "hermes_plugin"
METADATA_PLUGIN_VALUE = "e2b-terminal"
#: Opaque digest of (profile, task_id). Hashed so a session key or a home path
#: never leaves the host as readable E2B metadata.
METADATA_SCOPE_KEY = "hermes_scope"

_LOCK_TIMEOUT_SECONDS = 60

#: A scope as :func:`scope_id` produces it: lowercase hex, fixed length.
_SCOPE_PATTERN = re.compile(r"\A[0-9a-f]{8,64}\Z")


def _profile_key() -> str:
    """Identifier for the active Hermes profile (its home directory)."""
    try:
        from hermes_constants import hermes_home_key

        return str(hermes_home_key())
    except Exception:
        return os.path.expanduser("~/.hermes")


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _scope_state_dir() -> Path:
    """Where the plugin keeps its host-side, per-scope coordination files.

    Under ``cache/`` but deliberately **not** one of the ``cache/<subdir>``
    names Hermes mirrors into remote backends: ``_CACHE_DIRS`` in
    ``tools/credential_files.py`` is a closed enumeration
    (``documents``, ``images``, ``audio``, ``videos``, ``screenshots``,
    ``web``, ``delegation``, ``spillover``) and ``e2b`` is not in it. So
    nothing here is ever uploaded into a sandbox, pulled back out of one, or
    visible to the agent — which is what makes it usable as host-side truth
    about a sandbox whose transport may itself be failing.
    """
    return _hermes_home() / "cache" / "e2b"


def _scope_state_path(scope: str, suffix: str) -> Path:
    """A per-scope file name that cannot escape :func:`_scope_state_dir`.

    ``scope`` is always a hex digest from :func:`scope_id`, but this is the
    one place a scope string becomes a filesystem path, so a value that is
    not of that shape (a future caller, a hand-edited config) is hashed into
    one instead of being interpolated raw. Hashing rather than raising keeps
    a surprising scope from bricking the backend while still making
    traversal (``../``), separators, and absolute paths impossible.
    """
    if not _SCOPE_PATTERN.match(scope):
        scope = hashlib.sha256(scope.encode("utf-8", "surrogateescape")).hexdigest()[:32]
    return _scope_state_dir() / f"{scope}{suffix}"


def scope_id(task_id: str, settings: E2BSettings, *, persistent: bool) -> str:
    """Stable, opaque identity for a sandbox this environment may adopt.

    Everything that makes an existing sandbox **not a substitute** for the one
    we would create belongs in this digest, because adoption is silent:

    * ``template`` — a different image is a different toolchain;
    * ``allow_internet_access`` and ``secure`` — a sandbox created under a
      laxer policy keeps that policy for life. Tightening the config must not
      re-attach to a sandbox that still has the old egress rules or a
      still-unauthenticated envd;
    * ``persistent`` — the ``on_timeout`` action is fixed at creation. Without
      this, a run configured as persistent could adopt a sandbox created as
      ephemeral, whose lease expiry *kills* it, and quietly lose the very
      filesystem persistence was asked for.

    ``cwd``, the lease durations, and user ``metadata`` are deliberately out:
    none of them makes an existing sandbox unsafe or unsuitable to reuse.
    """
    raw = "\0".join((
        _profile_key(),
        task_id or "default",
        settings.template or "",
        "persistent" if persistent else "ephemeral",
        "net" if settings.allow_internet_access else "nonet",
        "secure" if settings.secure else "insecure",
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def identity_metadata(scope: str, settings: E2BSettings) -> dict[str, str]:
    """Metadata written on create and used as the discovery filter.

    User-supplied ``metadata`` is merged first so the identity keys always win
    — a typo in config cannot make two scopes collide.
    """
    metadata: dict[str, str] = dict(settings.metadata)
    metadata[METADATA_PLUGIN_KEY] = METADATA_PLUGIN_VALUE
    metadata[METADATA_SCOPE_KEY] = scope
    return metadata


def discovery_filter(scope: str) -> dict[str, str]:
    return {METADATA_PLUGIN_KEY: METADATA_PLUGIN_VALUE, METADATA_SCOPE_KEY: scope}


def lifecycle_for(persistent: bool) -> dict[str, Any]:
    """E2B ``lifecycle`` config for a persistent or ephemeral sandbox.

    ``keep_memory=False`` persists the filesystem only; a resumed sandbox
    cold-boots. Hermes already tells the model that live processes do not
    survive a sandbox pause, and a filesystem-only snapshot resumes faster and
    cannot resurrect a half-dead process tree.

    ``auto_resume`` is pinned to ``False`` and cannot be combined with
    ``keep_memory=False`` anyway: resumes happen explicitly through
    ``connect()`` so a sandbox is only ever woken by the environment that owns
    it.
    """
    if persistent:
        return {
            "on_timeout": {"action": "pause", "keep_memory": False},
            "auto_resume": False,
        }
    return {"on_timeout": "kill", "auto_resume": False}


@contextmanager
def scope_lock(scope: str) -> Iterator[None]:
    """Serialize discover-or-create for one scope across Hermes processes.

    Without this, two processes that cold-start together (gateway + CLI + the
    cron ticker) both find no sandbox and both create one; the loser's sandbox
    is then billed with nobody holding a reference to it. This is a mutex, not
    a pointer store — there is no state that can go stale, and a missing or
    unusable lock only costs the duplicate-prevention guarantee.

    The wait is bounded. A process that wedges while holding the lock must not
    block every other session's first command forever: after
    ``_LOCK_TIMEOUT_SECONDS`` we proceed unlocked and accept the (logged) risk
    of a duplicate sandbox. The lock is released by the OS if a holder dies,
    so this only fires for a live-but-stuck peer.
    """
    if fcntl is None:  # pragma: no cover - Windows has no flock
        yield
        return

    handle = None
    try:
        path = _scope_state_path(scope, ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "w", encoding="utf-8")
    except OSError as exc:
        logger.debug("E2B: scope lock unavailable (%s); continuing unlocked", exc)
        handle = None

    acquired = False
    if handle is not None:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "E2B: another process has held the sandbox creation lock "
                        "for %ds; continuing without it, which risks a duplicate "
                        "sandbox for this scope",
                        _LOCK_TIMEOUT_SECONDS,
                    )
                    break
                time.sleep(0.1)

    try:
        yield
    finally:
        if handle is not None:
            if acquired:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


class WriterLease:
    """The per-scope, cross-process "state-sync writer" role.

    At most one live environment per scope (and host) holds it, and only the
    holder synchronises Hermes state with the sandbox: resume recovery, the
    host push, the per-command sync, and the teardown pull. Every other owner
    attaches as a *reader* — commands run normally, state stays the writer's
    job — and tries to promote on each command once the role is free.

    Non-blocking by design: attaching must never wait on another session. The
    OS releases the flock when its holder dies, so a crashed writer's role is
    reclaimed by the next command of any surviving owner.

    Where no exclusive lock can be taken at all — Windows (no ``fcntl``) or a
    lock file that cannot be created — the single-writer guarantee CANNOT be
    provided. That is an explicit, warned-about degradation, not a silent
    one: the lease acts as writer-always (the pre-lease behaviour, in which
    concurrent same-scope sessions can overwrite each other's in-sandbox
    Hermes state) and says so at WARNING level once per environment. The
    README documents this contract. Failing closed instead would brick the
    whole backend on those platforms over a hazard that only exists when a
    user actually runs concurrent same-scope sessions.

    This is a different lock from :func:`scope_lock`: that one serializes the
    short discover-or-create window; this one is held for the whole
    attachment, from acquisition until teardown completes, so a promotion can
    never interleave with a live writer's recovery, push, or pull.
    """

    def __init__(self, scope: str) -> None:
        self._scope = scope
        self._handle: Any = None
        self._held = False
        self._warned_degraded = False

    @property
    def held(self) -> bool:
        return self._held

    def _degrade(self, reason: str) -> bool:
        """Writer-always fallback: loud, once, and documented."""
        if not self._warned_degraded:
            self._warned_degraded = True
            logger.warning(
                "E2B: no exclusive lock is available for the state-sync writer "
                "lease (%s). Acting as the writer WITHOUT single-writer "
                "protection: concurrent same-scope persistent sessions are NOT "
                "SUPPORTED here and are not protected against overwriting each "
                "other's in-sandbox ~/.hermes state; a single Hermes session is "
                "unaffected and remains fully supported. See the README's "
                "concurrency section.",
                reason,
            )
        self._held = True
        return True

    def try_acquire(self) -> bool:
        """Take the writer role if it is free. Never blocks, never raises."""
        if self._held:
            return True
        if fcntl is None:  # pragma: no cover - Windows has no flock
            return self._degrade("this platform has no flock")
        if self._handle is None:
            try:
                path = _scope_state_path(self._scope, ".writer.lock")
                path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = open(path, "w", encoding="utf-8")
            except OSError as exc:
                return self._degrade(f"the lease file cannot be created: {exc}")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                # EWOULDBLOCK (== EAGAIN) is the one errno that means
                # "genuinely held by another owner"; EACCES is what some
                # platforms report for the same condition.
                return False
            # ENOLCK, EOPNOTSUPP, and friends mean this filesystem cannot
            # take the lock at all (NFS/SMB/FUSE without a lock manager).
            # Reading that as contention would report a phantom "another
            # session holds the writer role" forever — degrade loudly
            # instead, like the other no-lock cases.
            return self._degrade(f"flock is not supported here: {exc}")
        self._held = True
        return True

    def release(self) -> None:
        """Give the role up. Idempotent, never raises."""
        handle, self._handle = self._handle, None
        held, self._held = self._held, False
        if handle is None:
            return
        if held:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            handle.close()
        except OSError:
            pass


class DirtyState:
    """Host-side record that a scope's sandbox Hermes state is not usable.

    The writer lease says *who* may synchronise state; this says *whether the
    state a reader would run against is currently trustworthy*. It is set
    while a writer generation is bringing the sandbox up or taking the role
    over — after the readiness stamp is invalidated and before the force-push
    completes — and after any incremental sync that failed, and it is cleared
    once a push or an incremental sync has actually established the host's
    state in the sandbox.

    Readers consult it at every command admission. That closes two windows
    the in-sandbox readiness stamp cannot:

    * a reader that already bootstrapped (so it will never probe the stamp
      again) executing while a *new* writer generation is mid-transition;
    * a writer whose *incremental* sync failed — the stamp attests its
      earlier bring-up push and stays valid, but the sandbox now holds state
      the host knows to be stale (a deleted credential still live in it, for
      instance).

    Host-side on purpose. The failure it reports is often a failure of the
    sandbox transport itself, so a marker written *into* the sandbox could
    not be trusted to land. It also survives the process that set it, so a
    writer killed mid-transition leaves the scope marked rather than silently
    admitting readers to a half-prepared sandbox; the next session to win the
    lease runs recovery and a push and clears it.

    Not atomic with command launch, and not claimed to be: a reader can pass
    this check microseconds before a writer marks the scope. What it
    guarantees is that a *known* bad or in-flight state is never knowingly
    run against — see the README's concurrency section.
    """

    def __init__(self, scope: str) -> None:
        self._path = _scope_state_path(scope, ".state-dirty")
        self._warned = False

    @property
    def path(self) -> Path:
        return self._path

    def _warn_unavailable(self, exc: OSError) -> None:
        """Say once that the admission gate is not working, and why.

        Same contract as a missing ``flock`` (see :meth:`WriterLease._degrade`)
        and said in the same terms: this is one of the two host facilities the
        reader-admission guarantee is built on, so losing it means concurrent
        same-scope persistent sessions are unsupported — not merely degraded.
        A single session is unaffected and stays fully supported.
        """
        if self._warned:
            return
        self._warned = True
        logger.warning(
            "E2B: the per-scope state marker %s is not usable (%s), so reader "
            "sessions cannot be held back from a sandbox whose state is being "
            "rebuilt or is known to be stale. Concurrent same-scope persistent "
            "sessions are NOT SUPPORTED without it and are not protected "
            "against running against stale in-sandbox ~/.hermes state; a "
            "single Hermes session is unaffected. See the README's concurrency "
            "section.",
            self._path,
            exc,
        )

    def mark(self, reason: str) -> None:
        """Record that this scope's sandbox state must not be run against.

        Never raises: a writer that cannot mark the scope has still done the
        more important half of its job (its own sync is fail-closed), and
        failing bring-up because a marker file could not be written would
        trade a concurrency guarantee for an availability outage.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(f"{reason}\npid={os.getpid()}\n", encoding="utf-8")
        except OSError as exc:
            self._warn_unavailable(exc)

    def clear(self) -> None:
        """Record that the state is usable again. Idempotent, never raises."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._warn_unavailable(exc)

    def reason(self) -> str | None:
        """Why this scope is marked dirty, or None when it is clean.

        An unreadable marker counts as dirty. The file only exists when
        something set it, so "it is there but cannot be read" is not evidence
        of a usable state — and on a host where this file cannot be read the
        writer lease could not be taken either, so every session is already
        running as a warned writer-always fallback with no readers to gate.
        """
        try:
            recorded = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._warn_unavailable(exc)
            return "the state marker exists but could not be read"
        first_line = recorded.strip().splitlines()
        return first_line[0] if first_line else "no reason recorded"


def sandbox_id_of(sandbox: Any) -> str:
    value = getattr(sandbox, "sandbox_id", None)
    if not isinstance(value, str) or not value:
        raise RuntimeError("E2B returned a sandbox without an id")
    return value


def find_existing(scope: str, api_key: str, template: str) -> str | None:
    """Return the id of the newest live sandbox for *scope*, or None.

    ``Sandbox.list`` returns running and paused sandboxes, newest first. The
    metadata filter is applied server-side; it is re-checked client-side so a
    future change in filter semantics cannot make us adopt a sandbox that is
    not ours.
    """
    from e2b import Sandbox, SandboxQuery

    wanted = discovery_filter(scope)
    try:
        paginator = Sandbox.list(
            query=SandboxQuery(metadata=dict(wanted)),
            limit=10,
            order="desc",
            api_key=api_key,
        )
        found: list[Any] = paginator.next_items()
    except Exception as exc:
        raise connection_error("sandbox lookup", exc) from exc

    matches = [
        info
        for info in found
        if all((getattr(info, "metadata", None) or {}).get(k) == v for k, v in wanted.items())
    ]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "E2B: %d sandboxes share scope %s; adopting the newest (%s). The "
            "others will pause on their own leases — check the E2B dashboard "
            "if this repeats.",
            len(matches),
            scope,
            getattr(matches[0], "sandbox_id", "?"),
        )
    return getattr(matches[0], "sandbox_id", None)


def connect(sandbox_id: str, lease_seconds: int, api_key: str) -> Any:
    """Attach to an existing sandbox, resuming it if paused, extending the lease."""
    from e2b import Sandbox

    return Sandbox.connect(sandbox_id, timeout=lease_seconds, api_key=api_key)


def create(
    scope: str,
    settings: E2BSettings,
    *,
    persistent: bool,
    lease_seconds: int,
    api_key: str,
) -> Any:
    """Create a new sandbox carrying this scope's identity metadata."""
    from e2b import Sandbox

    return Sandbox.create(
        template=settings.template,
        timeout=lease_seconds,
        metadata=identity_metadata(scope, settings),
        lifecycle=lifecycle_for(persistent),
        secure=settings.secure,
        allow_internet_access=settings.allow_internet_access,
        api_key=api_key,
    )


def renew_lease(sandbox: Any, lease_seconds: int, api_key: str) -> Any:
    """Reconnect for a command with the active profile's current API key.

    Class-level ``connect`` is deliberate for two independent reasons:

    * it is extend-only for a running sandbox, whereas ``set_timeout()`` can
      shorten one;
    * when a paused sandbox resumes, E2B returns current domain and envd-token
      metadata. The SDK's instance ``sandbox.connect()`` discards that response
      and returns the old object, so continuing through it can retain stale
      data-plane connection metadata.

    Resolving the key at the command boundary makes rotation effective without
    retaining credentials in plugin state.
    """
    from e2b import Sandbox

    return Sandbox.connect(
        sandbox_id_of(sandbox),
        timeout=lease_seconds,
        api_key=api_key,
    )


def reconnect_for_cleanup(sandbox: Any, lease_seconds: int) -> Any:
    """Reconnect for teardown using the attached sandbox's connection params.

    Cleanup can run after Hermes has removed the profile secret scope. It must
    therefore inherit the connection that created or last resumed this exact
    attachment, rather than resolving a possibly absent or different profile's
    API key. The returned SDK object carries fresh envd connection metadata.
    """
    from e2b import Sandbox

    return Sandbox.connect(
        sandbox_id_of(sandbox),
        timeout=lease_seconds,
        **sandbox.connection_config.get_api_params(),
    )


def kill(sandbox: Any) -> bool:
    """Destroy a sandbox. Returns False when it was already gone.

    Authenticates through the instance's own connection config — teardown
    must not depend on the credential still being resolvable, or an ephemeral
    sandbox would be left billing whenever cleanup runs after the profile
    scope is gone (``atexit``, ``__del__`` during interpreter shutdown).
    """
    try:
        return bool(sandbox.kill())
    except Exception as exc:
        from .errors import is_missing_sandbox

        if is_missing_sandbox(exc):
            return False
        raise


def is_probe_scope(task_id: str) -> bool:
    """True for the throwaway environment Hermes builds for its prompt probe.

    ``agent/prompt_builder._probe_remote_backend`` constructs a real
    environment with this ``task_id``, runs one ``uname`` command, and drops
    it without ever registering it in ``_active_environments`` — so Hermes'
    idle reaper never sees it. Treating it as ephemeral with a short,
    kill-on-timeout lease keeps that from leaving a billed sandbox behind on
    every Hermes process start. If core ever renames the id, this predicate
    simply stops matching and the probe falls back to a normal environment.
    """
    return task_id == PROBE_TASK_ID
