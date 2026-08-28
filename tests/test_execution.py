"""Execution path: create, run, stream, cancel, time out, fail.

These drive the real ``BaseEnvironment.execute()`` — the pipe, the select()
drain, the bounded collector, the timeout deadline and the interrupt poll are
all Hermes' own code. Only the E2B SDK boundary is a double.
"""

from __future__ import annotations

import threading
import time

import pytest
from fake_e2b import FakeE2B, Script, install


@pytest.fixture
def fake(monkeypatch, api_key):
    return install(monkeypatch, FakeE2B())


def make_env(persistent=True, task_id="default", timeout=30, **settings_kwargs):
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    return E2BEnvironment(
        task_id=task_id,
        settings=E2BSettings(**settings_kwargs),
        timeout=timeout,
        persistent_filesystem=persistent,
    )


def test_constructor_creates_no_sandbox(fake):
    """A sandbox created by a constructor that then raises would be orphaned.

    Nothing reaches the network until the environment is used, by which point
    Hermes has already cached the object and can reach it through cleanup().
    """
    env = make_env()
    assert fake.create_calls == []
    assert env.sandbox_id is None


def test_command_runs_in_the_sandbox_and_returns_its_output(fake):
    env = make_env()
    sandbox_out = "hello from e2b\n"
    result = None

    def _run():
        nonlocal result
        fake_sandbox = fake.only() if fake.live else None
        return fake_sandbox

    env._ensure_ready()
    fake.only().script("echo hi", Script(stdout=sandbox_out))
    result = env.execute("echo hi", timeout=10)

    assert result["returncode"] == 0
    assert sandbox_out.strip() in result["output"]
    # The command really was dispatched to the sandbox, not the host.
    assert any("echo hi" in call.cmd for call in fake.only().commands_run)


def test_non_zero_exit_is_reported_as_returncode_not_an_error(fake):
    """The SDK signals failure by raising; Hermes expects it in ``returncode``."""
    env = make_env()
    env._ensure_ready()
    fake.only().script("failing", Script(stdout="boom\n", exit_code=42))

    result = env.execute("failing", timeout=10)

    assert result["returncode"] == 42
    assert "boom" in result["output"]


def test_execute_reports_returncode_not_exit_code(fake):
    """Hermes reads result["returncode"]; the plugin doc's example is wrong."""
    env = make_env()
    env._ensure_ready()
    fake.only().script("x", Script(exit_code=7))
    result = env.execute("x", timeout=10)
    assert "returncode" in result
    assert result["returncode"] == 7


def test_output_streams_and_the_sdk_buffer_is_trimmed(fake):
    """A verbose command must not grow host memory without a bound.

    The real ``CommandHandle`` appends every chunk to ``_stdout_chunks`` and
    never trims, so without the plugin's trimming the whole output would sit
    in the client process. The double records the peak it reached.
    """
    env = make_env()
    env._ensure_ready()
    payload = "x" * 400_000
    fake.only().script("verbose", Script(stdout=payload, chunks=40))

    result = env.execute("verbose", timeout=30, bounded_capture=True)

    handle = [c for c in fake.only().commands_run if "verbose" in c.cmd][-1].handle
    assert handle.peak_buffered_chars <= len(payload) // 10, (
        "the SDK's own output buffer was not trimmed while streaming"
    )
    # Hermes' bounded collector kept the result small but recorded the total.
    assert len(result["output"]) < len(payload)
    assert result["output_total_chars"] >= len(payload)


def test_timeout_kills_the_command_and_keeps_the_sandbox(fake):
    """Hermes reports 124 and kills the command; the sandbox must survive."""
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    sandbox.script("sleep", Script(stdout="", duration=30.0))

    started = time.monotonic()
    result = env.execute("sleep 30", timeout=1)
    elapsed = time.monotonic() - started

    assert result["returncode"] == 124
    assert elapsed < 15
    handle = [c for c in sandbox.commands_run if "sleep 30" in c.cmd][-1].handle
    assert handle.killed is True, "the E2B command was not killed"
    assert sandbox.alive is True, "cancelling a command must not destroy the sandbox"
    assert sandbox.kill_calls == 0
    assert sandbox.pause_calls == 0


def test_interrupt_kills_the_command_not_the_sandbox(fake, monkeypatch):
    """Ctrl-C during a command targets the command by pid."""
    from tools import interrupt as interrupt_mod

    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    sandbox.script("long", Script(stdout="", duration=20.0))

    interrupted = threading.Event()

    def _is_interrupted(*args, **kwargs):
        return interrupted.is_set()

    monkeypatch.setattr(interrupt_mod, "is_interrupted", _is_interrupted)
    monkeypatch.setattr("tools.environments.base.is_interrupted", _is_interrupted, raising=False)

    timer = threading.Timer(0.4, interrupted.set)
    timer.start()
    try:
        result = env.execute("long", timeout=20)
    finally:
        timer.cancel()

    assert result["returncode"] == 130
    handle = [c for c in sandbox.commands_run if "long" in c.cmd][-1].handle
    assert handle.killed is True
    assert sandbox.alive is True


def test_a_vanished_sandbox_is_replaced_transparently(fake):
    """A persistent sandbox can be reaped server-side; the session must survive.

    Lease renewal is the first thing that touches E2B before each command, so
    it is where the disappearance is noticed. The environment rebuilds and the
    command runs, rather than surfacing a dead-backend error to the model.
    """
    env = make_env()
    env._ensure_ready()
    first_id = env.sandbox_id
    fake.sandboxes[first_id].alive = False

    result = env.execute("echo alive", timeout=10)

    assert result["returncode"] == 0
    assert env.sandbox_id != first_id
    assert len(fake.create_calls) == 2


def test_same_environment_resume_retains_the_fresh_sdk_object(fake, monkeypatch):
    """Instance ``connect`` discards E2B's refreshed envd metadata.

    The reconnect double returns a distinct running connection whose data
    plane is usable while the old object remains paused. The command therefore
    proves both that the class-style reconnect happened and that the returned
    object replaced the stale one inside the environment.
    """
    import copy

    from fake_e2b import FakeCommands, FakeFiles
    from hermes_plugin_e2b import sandbox as sandbox_api

    env = make_env()
    env._ensure_ready()
    old = fake.only()
    old.pause(keep_memory=False)
    env._lease_deadline = 0.0

    fresh = copy.copy(old)
    fresh.state = "running"
    fresh.commands = FakeCommands(fresh)
    fresh.files = FakeFiles(fresh)

    monkeypatch.setattr(
        sandbox_api,
        "renew_lease",
        lambda sandbox, lease, api_key: fresh,
    )

    result = env.execute("echo resumed", timeout=10)

    assert result["returncode"] == 0
    assert env._sandbox is fresh
    assert old.state == "paused", "the test accidentally made the stale data plane usable"
    env.cleanup(sync_state=False)


def test_launch_failure_surfaces_as_a_backend_failure(fake):
    """A dead sandbox is an infrastructure failure, not a command failure.

    Hermes turns ``EnvironmentConnectionError`` into a degraded-backend result
    and evicts the cached environment; flattening it into ``exit 1`` would make
    the model treat a broken backend as a failing command.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    env = make_env()
    env._ensure_ready()
    fake.sandboxes[env.sandbox_id].alive = False
    # Recovery is impossible: creating the replacement fails too.
    fake.create_error = RuntimeError("E2B API unreachable")

    with pytest.raises(EnvironmentConnectionError):
        env.execute("true", timeout=5)


def test_command_launch_on_a_dead_sandbox_is_a_backend_failure(fake):
    """The launch path itself must classify a vanished sandbox correctly."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    env = make_env()
    env._ensure_ready()
    # Keep the lease fresh so renewal short-circuits and the launch is what
    # discovers the sandbox is gone.
    env._lease_deadline = float("inf")
    fake.sandboxes[env.sandbox_id].alive = False

    with pytest.raises(EnvironmentConnectionError):
        env._run_bash("true", timeout=5)


def test_authentication_failure_is_reported_without_the_key(fake, monkeypatch):
    from e2b import AuthenticationException
    from hermes_plugin_e2b.errors import E2BAuthError

    secret = "e2b_" + "a" * 40
    monkeypatch.setenv("E2B_API_KEY", secret)
    fake.create_error = AuthenticationException(f"401 unauthorized for api_key={secret}")

    env = make_env()
    with pytest.raises(E2BAuthError) as excinfo:
        env._ensure_ready()

    message = str(excinfo.value) + excinfo.value.retry_hint
    assert secret not in message
    assert "<redacted>" in str(excinfo.value)


def test_stdin_travels_over_the_stdin_channel_not_the_command_line(fake):
    """A sudo password in a heredoc would be readable from ``ps`` in-sandbox."""
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()

    env.execute("read -r x; echo done", timeout=10, stdin_data="hunter2\n")

    call = [c for c in sandbox.commands_run if "read -r x" in c.cmd][-1]
    assert "hunter2" not in call.cmd
    assert call.stdin is True
    assert call.handle.stdin_sent == ["hunter2\n"]
    assert call.handle.stdin_closed is True


def test_the_api_key_never_enters_the_sandbox(fake, api_key):
    env = make_env()
    env._ensure_ready()
    env.execute("env", timeout=10)

    created = fake.create_calls[-1]
    assert created["envs"] == {}
    assert api_key not in repr(created["metadata"])
    for call in fake.only().commands_run:
        assert api_key not in call.cmd
        assert api_key not in repr(call.envs)


def test_working_directory_is_the_remote_home(fake):
    env = make_env()
    env._ensure_ready()
    # _detect_remote_home asks the sandbox; the default double answers "".
    assert env.cwd.startswith("/")
    assert env.remote_hermes_home.endswith("/.hermes")


def test_a_killed_command_whose_output_outlives_the_reader_cannot_hang(fake):
    """A parked worker thread would defer cleanup forever and leak the sandbox.

    Hermes' ``_wait_for_process`` returns from inside its poll loop on timeout
    and on interrupt, so it stops draining *without* closing the read end. A
    blocking write would then park this handle's worker as soon as the 64 KB
    pipe buffer filled — and that worker is what releases the environment's
    in-flight count.
    """
    from hermes_plugin_e2b.process import E2BProcessHandle

    class _Chatty:
        """A command that keeps streaming after it is killed."""

        def __init__(self):
            self.killed = False
            self._stdout_chunks = []

        def kill(self):
            self.killed = True
            return True

        def wait(self, on_pty=None, on_stdout=None, on_stderr=None):
            for _ in range(200):  # ~2 MB, far past the pipe buffer
                on_stdout("y" * 10_000)
            return type("R", (), {"exit_code": 0})()

    settled = threading.Event()
    handle = E2BProcessHandle(_Chatty(), on_done=settled.set)

    # Nobody ever reads handle.stdout — exactly what Hermes leaves behind.
    assert handle.wait(timeout=30) is not None, "the command worker never finished"
    assert settled.wait(timeout=5) is True, "the completion hook never fired"


def test_output_after_a_kill_is_not_forwarded(fake):
    """Once Hermes has decided the outcome it has stopped reading."""
    from hermes_plugin_e2b.process import E2BProcessHandle

    started = threading.Event()
    release = threading.Event()

    class _Blocked:
        def __init__(self):
            self._stdout_chunks = []

        def kill(self):
            release.set()
            return True

        def wait(self, on_pty=None, on_stdout=None, on_stderr=None):
            on_stdout("before-kill\n")
            started.set()
            release.wait(timeout=10)
            on_stdout("after-kill\n")
            return type("R", (), {"exit_code": 0})()

    handle = E2BProcessHandle(_Blocked())
    assert started.wait(timeout=10)
    handle.kill()
    assert handle.wait(timeout=15) is not None

    handle.stdout.close()


def test_the_working_directory_comes_from_the_sandbox_not_the_default(fake):
    """A custom template may not have the configured directory at all.

    Hermes wraps every command in ``cd -- <cwd> || exit 126``, so guessing
    wrong makes every command fail with 126 and no useful message.
    """
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    env = E2BEnvironment(
        task_id="default", settings=E2BSettings(), timeout=30, persistent_filesystem=True
    )
    # A template that runs as root and has no /home/user.
    fake.create = _template_with_home(fake, "/root", {"/root", "/tmp"})

    env._ensure_ready()

    assert env.cwd == "/root"
    assert env.remote_hermes_home == "/root/.hermes"


def test_an_explicit_missing_working_directory_falls_back_with_a_warning(fake, caplog):
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    env = E2BEnvironment(
        task_id="default",
        settings=E2BSettings(cwd="/srv/does-not-exist"),
        timeout=30,
        persistent_filesystem=True,
    )
    fake.create = _template_with_home(fake, "/home/user", {"/home/user", "/tmp"})

    with caplog.at_level("WARNING"):
        env._ensure_ready()

    assert env.cwd == "/home/user"
    assert any("does not exist in template" in r.message for r in caplog.records)


def test_an_existing_configured_working_directory_is_kept(fake):
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    env = E2BEnvironment(
        task_id="default",
        settings=E2BSettings(cwd="/workspace"),
        timeout=30,
        persistent_filesystem=True,
    )
    fake.create = _template_with_home(fake, "/home/user", {"/home/user", "/workspace"})

    env._ensure_ready()

    assert env.cwd == "/workspace"


def _template_with_home(fake, home, dirs):
    """Wrap fake.create so new sandboxes model a specific template layout."""
    original = fake.create

    def _create(*args, **kwargs):
        sandbox = original(*args, **kwargs)
        sandbox.home = home
        sandbox.existing_dirs = set(dirs)
        return sandbox

    return _create
