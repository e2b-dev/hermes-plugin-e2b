"""A behavioural double for the E2B SDK.

Modelled on the installed ``e2b`` 2.46.0 source, not on what the plugin
happens to call: commands are started in the background and stream through
callbacks, a non-zero exit is delivered as ``CommandExitException`` rather
than a return value, the command handle accumulates every chunk in
``_stdout_chunks`` the way the real one does, and ``Sandbox.list`` returns
paused sandboxes alongside running ones.

Anything the plugin relies on that this double gets wrong is a bug this suite
cannot catch, so the docstrings record the source line the behaviour came from.
"""

from __future__ import annotations

import builtins
import io
import re
import tarfile
import threading
import time
from dataclasses import dataclass
from typing import Any

from e2b import CommandExitException, SandboxException, SandboxNotFoundException


@dataclass
class Script:
    """What a fake command does when it runs."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    #: Seconds the command "runs" for, in small slices so a kill lands.
    duration: float = 0.0
    #: Number of chunks the stdout is split into, to exercise streaming.
    chunks: int = 1
    raises: BaseException | None = None


def _split(text: str, count: int) -> list[str]:
    if count <= 1 or not text:
        return [text] if text else []
    size = max(1, len(text) // count)
    return [text[i : i + size] for i in range(0, len(text), size)]


class FakeCommandHandle:
    """Mirrors ``e2b.sandbox_sync.commands.command_handle.CommandHandle``."""

    def __init__(self, script: Script, pid: int):
        self._script = script
        self._pid = pid
        self.killed = False
        self.stdin_sent: list[str] = []
        self.stdin_closed = False
        self.disconnected = False
        # The real handle accumulates every decoded chunk here with no cap.
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._kill_event = threading.Event()
        #: Peak length the accumulator reached — the assertion hook for
        #: "the plugin trims the SDK's copy while streaming".
        self.peak_buffered_chars = 0

    @property
    def pid(self) -> int:
        return self._pid

    def kill(self) -> bool:
        self.killed = True
        self._kill_event.set()
        return True

    def disconnect(self) -> None:
        self.disconnected = True

    def send_stdin(self, data, request_timeout=None) -> None:
        self.stdin_sent.append(data)

    def close_stdin(self, request_timeout=None) -> None:
        self.stdin_closed = True

    def _emit(self, text: str, sink: list[str], callback):
        sink.append(text)
        self.peak_buffered_chars = max(
            self.peak_buffered_chars,
            sum(len(c) for c in self._stdout_chunks) + sum(len(c) for c in self._stderr_chunks),
        )
        if callback:
            callback(text)

    def wait(self, on_pty=None, on_stdout=None, on_stderr=None):
        if self._script.raises is not None:
            raise self._script.raises

        pieces = _split(self._script.stdout, self._script.chunks)
        slices = max(1, len(pieces))
        per_slice = self._script.duration / slices if self._script.duration else 0.0

        for piece in pieces:
            if per_slice:
                if self._kill_event.wait(per_slice):
                    break
            elif self._kill_event.is_set():
                break
            self._emit(piece, self._stdout_chunks, on_stdout)

        if self._script.stderr and not self._kill_event.is_set():
            self._emit(self._script.stderr, self._stderr_chunks, on_stderr)

        # Any remaining runtime, still interruptible.
        if self._script.duration and not pieces:
            self._kill_event.wait(self._script.duration)

        if self._kill_event.is_set():
            raise CommandExitException(
                stdout="".join(self._stdout_chunks),
                stderr="".join(self._stderr_chunks),
                exit_code=137,
                error="killed",
            )

        if self._script.exit_code != 0:
            raise CommandExitException(
                stdout="".join(self._stdout_chunks),
                stderr="".join(self._stderr_chunks),
                exit_code=self._script.exit_code,
                error=None,
            )
        return FakeCommandResult(
            stdout="".join(self._stdout_chunks),
            stderr="".join(self._stderr_chunks),
            exit_code=0,
        )


@dataclass
class FakeCommandResult:
    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


class FakeCommands:
    def __init__(self, sandbox: FakeSandbox):
        self._sandbox = sandbox
        self._next_pid = 1000

    def run(
        self,
        cmd,
        background=None,
        envs=None,
        user=None,
        cwd=None,
        on_stdout=None,
        on_stderr=None,
        stdin=None,
        timeout=60,
        request_timeout=None,
    ):
        self._sandbox.require_data_plane()
        self._next_pid += 1
        record = CommandCall(
            cmd=cmd,
            background=bool(background),
            envs=dict(envs or {}),
            cwd=cwd,
            stdin=stdin,
            timeout=timeout,
        )
        self._sandbox.commands_run.append(record)
        self._sandbox.maybe_make_tar(cmd)
        probe = self._sandbox.maybe_answer_path_probe(
            cmd
        ) or self._sandbox.maybe_answer_existence_probe(cmd)
        if probe is not None:
            handle = FakeCommandHandle(probe, self._next_pid)
            record.handle = handle
            return handle if background else handle.wait(on_stdout=on_stdout)
        script = self._sandbox.resolve_script(cmd)
        handle = FakeCommandHandle(script, self._next_pid)
        record.handle = handle
        if background:
            return handle
        return handle.wait(on_stdout=on_stdout, on_stderr=on_stderr)


@dataclass
class CommandCall:
    cmd: str
    background: bool
    envs: dict[str, str]
    cwd: str | None
    stdin: bool | None
    timeout: float | None
    handle: FakeCommandHandle | None = None


class FakeFiles:
    def __init__(self, sandbox: FakeSandbox):
        self._sandbox = sandbox

    def write_files(self, files, **kwargs):
        self._sandbox.require_data_plane()
        written = []
        for entry in files:
            data = entry["data"]
            payload = data.read() if hasattr(data, "read") else data
            self._sandbox.files_written[entry["path"]] = payload
            written.append(entry["path"])
        self._sandbox.write_files_calls.append(written)
        return [{"path": path} for path in written]

    def write(self, path, data, **kwargs):
        return self.write_files([{"path": path, "data": data}], **kwargs)[0]

    def read(self, path, format="text", **kwargs):
        self._sandbox.require_data_plane()
        if path not in self._sandbox.readable_files:
            raise FileNotFoundError(path)
        payload = self._sandbox.readable_files[path]
        if format == "stream":
            return _FakeStreamReader(payload, self._sandbox)
        if format == "bytes":
            return bytearray(payload)
        return payload.decode("utf-8")

    def remove(self, path, **kwargs):
        self._sandbox.require_data_plane()
        self._sandbox.files_written.pop(path, None)


class _FakeStreamReader:
    def __init__(self, payload: bytes, sandbox: FakeSandbox, chunk: int = 4096):
        self._chunks = [payload[i : i + chunk] for i in range(0, len(payload), chunk)] or [b""]
        self._sandbox = sandbox
        self.closed = False

    def __iter__(self):
        yield from self._chunks

    def close(self):
        self.closed = True
        self._sandbox.stream_readers_closed += 1


class FakeSandbox:
    """One sandbox. Created only through :class:`FakeE2B`."""

    def __init__(
        self,
        sandbox_id: str,
        metadata: dict[str, str],
        template: str,
        lifecycle: dict[str, Any],
        timeout: int,
        envs: dict[str, str] | None,
        api_key: str | None,
        registry: FakeE2B,
    ):
        self.sandbox_id = sandbox_id
        self.metadata = dict(metadata)
        self.template = template
        self.lifecycle = lifecycle
        self.envs = dict(envs or {})
        self.api_key = api_key
        self._registry = registry

        self.alive = True
        self.state = "running"
        self.lease_seconds = timeout
        self.lease_history: list[int] = [timeout]
        self.pause_calls = 0
        self.kill_calls = 0
        self.connect_calls = 0
        self.set_timeout_calls: list[int] = []

        self.commands_run: list[CommandCall] = []
        self.files_written: dict[str, Any] = {}
        self.write_files_calls: list[list[str]] = []
        self.readable_files: dict[str, bytes] = {}
        self.stream_readers_closed = 0

        #: cmd-substring -> Script. Longest match wins; default is exit 0.
        self.scripts: dict[str, Script] = {}
        self.default_script = Script()

        #: What the sandbox reports as $HOME, and which directories exist in it.
        self.home = "/home/user"
        self.existing_dirs = {"/home/user", "/tmp", "/root"}

        self.commands = FakeCommands(self)
        self.files = FakeFiles(self)

    # -- scripting -------------------------------------------------------

    def script(self, needle: str, script: Script) -> None:
        self.scripts[needle] = script

    def resolve_script(self, cmd: str) -> Script:
        best: Script | None = None
        best_len = -1
        for needle, script in self.scripts.items():
            if needle in cmd and len(needle) > best_len:
                best, best_len = script, len(needle)
        return best if best is not None else self.default_script

    def maybe_answer_path_probe(self, cmd: str) -> Script | None:
        """Answer the plugin's `$HOME` + cwd probe from the modelled filesystem."""
        if '"$HOME"' not in cmd or "pwd -P" not in cmd:
            return None
        match = re.search(r"cd (\S+) >/dev/null", cmd)
        target = match.group(1).strip("'\"") if match else ""
        resolved = target if target in self.existing_dirs else ""
        return Script(stdout=f"{self.home}\n{resolved}\n")

    def maybe_answer_existence_probe(self, cmd: str) -> Script | None:
        """Answer the plugin's existence probes from the modelled filesystem.

        Handles both shapes the plugin sends: ``-d`` (a directory "exists"
        when any written file lives under it — the real sandbox only gains
        ``~/.hermes`` when something is written there — or when declared in
        ``existing_dirs``) and ``-f`` (a file exists when it was written).
        """
        if "__HERMES_E2B_PROBE_YES__" not in cmd:
            return None
        match = re.search(r"if \[ -([df]) (\S+) \]", cmd)
        if not match:
            return None
        kind = match.group(1)
        target = match.group(2).strip("'\"")
        if kind == "f":
            present = target in self.files_written or target in self.readable_files
        else:
            prefix = target.rstrip("/") + "/"
            present = target in self.existing_dirs or any(
                path.startswith(prefix) for path in self.files_written
            )
        marker = "__HERMES_E2B_PROBE_YES__" if present else "__HERMES_E2B_PROBE_NO__"
        return Script(stdout=f"{marker}\n")

    def maybe_make_tar(self, cmd: str) -> None:
        """Model ``tar cf <archive> -C / <dir>`` over the fake filesystem.

        Without this the sync-back transport would always read a missing file,
        so every teardown would exercise the manager's retry-and-give-up path
        instead of the pull it is supposed to perform.
        """
        match = re.search(r"tar cf (\S+) -C / (\S+)", cmd)
        if not match:
            return
        archive = match.group(1).strip("'\"")
        root = "/" + match.group(2).strip("'\"").lstrip("/")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for path, payload in sorted(self.files_written.items()):
                if not path.startswith(root.rstrip("/") + "/"):
                    continue
                raw = payload if isinstance(payload, bytes) else str(payload).encode()
                info = tarfile.TarInfo(name=path.lstrip("/"))
                info.size = len(raw)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(raw))
        self.readable_files[archive] = buffer.getvalue()

    # -- SDK surface -----------------------------------------------------

    def require_data_plane(self) -> None:
        """Reject envd operations unless this sandbox is running.

        E2B's control plane can list or reconnect a paused sandbox, but its
        command and file services are not usable until that resume completes.
        Treating pause as merely cosmetic would let lifecycle tests pass while
        production teardown fails on the first tar command.
        """
        if not self.alive or self.state == "killed":
            raise SandboxNotFoundException(f"sandbox {self.sandbox_id} not found")
        if self.state != "running":
            raise SandboxException(
                f"sandbox {self.sandbox_id} data plane is unavailable while {self.state}"
            )

    def connect(self, timeout=None, **kwargs):
        if not self.alive:
            raise SandboxNotFoundException(f"sandbox {self.sandbox_id} not found")
        self.connect_calls += 1
        self.state = "running"
        if timeout:
            # Mirrors the SDK: for a running sandbox connect only ever extends.
            self.lease_seconds = max(self.lease_seconds, int(timeout))
            self.lease_history.append(int(timeout))
        return self

    def set_timeout(self, timeout, **kwargs):
        # Present so a test can prove the plugin never calls it: unlike
        # connect(), the real set_timeout can *shorten* a lease.
        self.set_timeout_calls.append(int(timeout))
        self.lease_seconds = int(timeout)

    def pause(self, keep_memory=True, **kwargs):
        if not self.alive:
            raise SandboxNotFoundException(f"sandbox {self.sandbox_id} not found")
        self.pause_calls += 1
        if self.state == "paused":
            return False
        self.state = "paused"
        return True

    def beta_pause(self, keep_memory=True, **kwargs):
        return self.pause(keep_memory=keep_memory, **kwargs)

    def kill(self, **kwargs):
        self.kill_calls += 1
        if not self.alive:
            return False
        self.alive = False
        self.state = "killed"
        self._registry.killed.append(self.sandbox_id)
        return True


@dataclass
class FakeSandboxInfo:
    sandbox_id: str
    metadata: dict[str, str]
    state: str
    started_at: float


class _FakePaginator:
    def __init__(self, items: list[FakeSandboxInfo]):
        self._items = items
        self.has_next = True

    def next_items(self):
        self.has_next = False
        return list(self._items)


class FakeE2B:
    """Registry standing in for the ``e2b`` module's ``Sandbox`` class."""

    def __init__(self):
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.killed: list[str] = []
        self.create_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.connect_calls: list[str] = []
        self.connect_requests: list[dict[str, Any]] = []
        self.create_error: BaseException | None = None
        self.connect_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self._counter = 0
        self._clock = 0.0

    # -- factory ---------------------------------------------------------

    def create(
        self,
        template=None,
        timeout=None,
        metadata=None,
        envs=None,
        secure=True,
        allow_internet_access=True,
        lifecycle=None,
        api_key=None,
        **kwargs,
    ):
        self.create_calls.append({
            "template": template,
            "timeout": timeout,
            "metadata": dict(metadata or {}),
            "envs": dict(envs or {}),
            "lifecycle": lifecycle,
            "secure": secure,
            "allow_internet_access": allow_internet_access,
            "api_key": api_key,
        })
        if self.create_error is not None:
            raise self.create_error
        self._counter += 1
        self._clock += 1.0
        sandbox_id = f"sbx-{self._counter:03d}"
        sandbox = FakeSandbox(
            sandbox_id=sandbox_id,
            metadata=metadata or {},
            template=template or "base",
            lifecycle=lifecycle or {},
            timeout=int(timeout or 300),
            envs=envs,
            api_key=api_key,
            registry=self,
        )
        sandbox.started_at = self._clock
        self.sandboxes[sandbox_id] = sandbox
        return sandbox

    def connect(self, sandbox_id, timeout=None, api_key=None, **kwargs):
        self.connect_calls.append(sandbox_id)
        self.connect_requests.append({
            "sandbox_id": sandbox_id,
            "timeout": timeout,
            "api_key": api_key,
        })
        if self.connect_error is not None:
            raise self.connect_error
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None or not sandbox.alive:
            raise SandboxNotFoundException(f"sandbox {sandbox_id} not found")
        sandbox.api_key = api_key
        return sandbox.connect(timeout=timeout)

    def list(self, query=None, limit=None, order=None, api_key=None, **kwargs):
        self.list_calls.append({"query": query, "limit": limit, "order": order})
        if self.list_error is not None:
            raise self.list_error
        wanted = dict(getattr(query, "metadata", None) or {})
        items = [
            FakeSandboxInfo(
                sandbox_id=s.sandbox_id,
                metadata=dict(s.metadata),
                # Paused sandboxes are returned too — that is what makes a
                # local pointer store unnecessary.
                state=s.state,
                started_at=getattr(s, "started_at", 0.0),
            )
            for s in self.sandboxes.values()
            if s.alive and all(s.metadata.get(k) == v for k, v in wanted.items())
        ]
        items.sort(key=lambda i: i.started_at, reverse=True)
        if limit:
            items = items[:limit]
        return _FakePaginator(items)

    # -- helpers ---------------------------------------------------------

    @property
    def live(self) -> builtins.list[FakeSandbox]:
        return [s for s in self.sandboxes.values() if s.alive]

    def only(self) -> FakeSandbox:
        live = self.live
        assert len(live) == 1, f"expected exactly one live sandbox, got {len(live)}"
        return live[0]


def install(monkeypatch, fake: FakeE2B):
    """Point the plugin's SDK boundary at *fake*.

    Only ``sandbox.py``'s SDK entry points are redirected; everything
    above them — identity, locking, admission, teardown, and the whole
    ``BaseEnvironment`` execution path — runs for real.
    """
    from hermes_plugin_e2b import sandbox as sandbox_api

    def _find_existing(scope, api_key, template):
        from e2b import SandboxQuery

        paginator = fake.list(
            query=SandboxQuery(metadata=sandbox_api.discovery_filter(scope)),
            limit=10,
            order="desc",
            api_key=api_key,
        )
        wanted = sandbox_api.discovery_filter(scope)
        matches = [
            info
            for info in paginator.next_items()
            if all(info.metadata.get(k) == v for k, v in wanted.items())
        ]
        return matches[0].sandbox_id if matches else None

    monkeypatch.setattr(sandbox_api, "find_existing", _find_existing)
    monkeypatch.setattr(
        sandbox_api,
        "connect",
        lambda sandbox_id, lease, api_key: fake.connect(sandbox_id, timeout=lease, api_key=api_key),
    )
    monkeypatch.setattr(
        sandbox_api,
        "renew_lease",
        lambda sandbox, lease, api_key: fake.connect(
            sandbox.sandbox_id,
            timeout=lease,
            api_key=api_key,
        ),
    )
    monkeypatch.setattr(
        sandbox_api,
        "reconnect_for_cleanup",
        lambda sandbox, lease: fake.connect(
            sandbox.sandbox_id,
            timeout=lease,
            api_key=sandbox.api_key,
        ),
    )
    monkeypatch.setattr(
        sandbox_api,
        "create",
        lambda scope, settings, *, persistent, lease_seconds, api_key: fake.create(
            template=settings.template,
            timeout=lease_seconds,
            metadata=sandbox_api.identity_metadata(scope, settings),
            lifecycle=sandbox_api.lifecycle_for(persistent),
            secure=settings.secure,
            allow_internet_access=settings.allow_internet_access,
            api_key=api_key,
        ),
    )
    return fake
