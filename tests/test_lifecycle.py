"""Lifecycle and ownership.

The adversarial cases here are the transitions most likely to destroy state or
orphan a sandbox:

1. a sibling cleanup pausing or detaching a shared sandbox in the window
   between "the sandbox is ready" and "this command is registered";
2. Hermes evicting the environment before learning that cleanup deferred for
   an active command, letting a second owner attach to the same live sandbox;
3. a successfully created sandbox orphaned because a local pointer-store write
   failed before creation returned.

This architecture closes (1) by settling admission and teardown under one
lock, (2) by never performing a destructive operation on a persistent sandbox
at all, and (3) by not having a pointer store — so these tests assert the
invariants rather than the absence of specific lines.
"""

from __future__ import annotations

import threading
import time

import pytest
from fake_e2b import FakeE2B, Script, install


@pytest.fixture
def fake(monkeypatch, api_key):
    return install(monkeypatch, FakeE2B())


def make_env(persistent=True, task_id="default", timeout=30, template="base", **settings_kwargs):
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    return E2BEnvironment(
        task_id=task_id,
        settings=E2BSettings(template=template, **settings_kwargs),
        timeout=timeout,
        persistent_filesystem=persistent,
    )


# ---------------------------------------------------------------------------
# Persistent mode: nothing this plugin does can destroy shared state
# ---------------------------------------------------------------------------


def test_persistent_cleanup_neither_kills_nor_pauses(fake):
    """The whole safety argument rests on this.

    E2B pauses the sandbox itself when the lease expires
    (``on_timeout: {"action": "pause"}``), so the plugin never has to decide
    whether some other process is still using it.
    """
    env = make_env(persistent=True)
    env._ensure_ready()
    sandbox = fake.only()

    env.cleanup()

    assert sandbox.alive is True
    assert sandbox.kill_calls == 0
    assert sandbox.pause_calls == 0


def test_persistent_teardown_removes_only_its_own_scratch_files(fake):
    """A persistent sandbox outlives many sessions; snapshots must not pile up."""
    first = make_env(persistent=True, task_id="default")
    first._ensure_ready()
    sandbox = fake.only()

    first.cleanup()

    removals = [c.cmd for c in sandbox.commands_run if "rm -f" in c.cmd]
    assert removals, "no scratch cleanup ran"
    assert any(first._session_id in cmd for cmd in removals)
    # Nothing destructive happened to the sandbox itself.
    assert sandbox.alive is True
    assert sandbox.kill_calls == 0


def test_persistent_sandboxes_are_created_to_pause_on_timeout(fake):
    env = make_env(persistent=True)
    env._ensure_ready()
    lifecycle = fake.create_calls[-1]["lifecycle"]
    assert lifecycle["on_timeout"] == {"action": "pause", "keep_memory": False}
    assert lifecycle["auto_resume"] is False


def test_a_second_environment_adopts_the_same_persistent_sandbox(fake):
    """Restart-safety: the sandbox is found again with no local state."""
    first = make_env(persistent=True, task_id="default")
    first._ensure_ready()
    first_id = first.sandbox_id
    first.cleanup()

    second = make_env(persistent=True, task_id="default")
    second._ensure_ready()

    assert second.sandbox_id == first_id
    assert len(fake.create_calls) == 1
    assert fake.connect_calls and all(sandbox_id == first_id for sandbox_id in fake.connect_calls)


def test_natural_pause_is_resumed_before_persistent_teardown_pulls_state(
    fake, tmp_path, monkeypatch
):
    """Hermes can reap after E2B has already applied ``on_timeout: pause``.

    The behavioral double refuses every command/file call while paused, so the
    host update below proves teardown retained a running reconnect result before
    FileSyncManager issued its tar command.
    """
    home = _shared_home(tmp_path, monkeypatch)
    env = make_env(persistent=True)
    env._ensure_ready()
    sandbox = fake.only()
    remote = f"{env.remote_hermes_home}/skills/demo/SKILL.md"
    sandbox.files_written[remote] = b"# authored-before-idle-reap\n"
    sandbox.pause(keep_memory=False)

    env.cleanup()

    assert (home / "skills" / "demo" / "SKILL.md").read_bytes() == (
        b"# authored-before-idle-reap\n"
    )
    assert sandbox.state == "running", "teardown tried the paused data plane without reconnecting"
    assert sandbox.alive is True and sandbox.kill_calls == 0


# ---------------------------------------------------------------------------
# Two live owners of one persistent scope: the single-writer contract.
#
# "Cannot hurt each other" used to be asserted only for lifecycle, and was
# disproved for state sync: a second owner's bring-up force-pushed its host
# snapshot over the live sandbox's ~/.hermes. The contract now is: lifecycle
# stays non-destructive for everyone, and at most one live owner — the holder
# of the per-scope writer lease — synchronises state. Everyone else attaches
# as a reader and takes the role over once it is released.
# ---------------------------------------------------------------------------


def _shared_home(tmp_path, monkeypatch, content="# host-v1\n"):
    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text(content, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_a_second_live_owner_attaches_as_a_reader_and_cannot_bury_live_state(
    fake, tmp_path, monkeypatch
):
    """The reproduced A2 scenario, inverted: while owner A is live, owner B's
    bring-up must not recover-and-quarantine A's live state, and must not push
    the host snapshot over it. Lifecycle stays non-destructive throughout."""
    home = _shared_home(tmp_path, monkeypatch)

    a = make_env(persistent=True, task_id="default")
    a._ensure_ready()
    sandbox = fake.only()
    remote = f"{a.remote_hermes_home}/skills/demo/SKILL.md"
    sandbox.files_written[remote] = b"# active-owner-v2\n"  # A's live in-sandbox edit
    pushes_before = len(sandbox.write_files_calls)

    b = make_env(persistent=True, task_id="default")
    b._ensure_ready()
    result = b.execute("echo hi", timeout=10)

    assert b.sandbox_id == a.sandbox_id
    assert result["returncode"] == 0, "a reader must still run commands"
    assert b._is_writer is False
    assert sandbox.files_written[remote] == b"# active-owner-v2\n", (
        "the second owner buried the live owner's remote state"
    )
    assert len(sandbox.write_files_calls) == pushes_before, (
        "the second owner pushed its host snapshot while the first was live"
    )
    assert not (home / "cache" / "e2b-recovered").exists(), (
        "the second owner treated the live sandbox as crashed state"
    )
    # The lifecycle half of the old claim still holds.
    assert sandbox.alive is True
    assert sandbox.kill_calls == 0
    assert sandbox.pause_calls == 0


def test_the_reader_takes_over_state_sync_once_the_writer_releases(fake, tmp_path, monkeypatch):
    """Promotion runs the writer's full bring-up: recover what the sandbox
    holds (quarantining divergence), then push — so the role hand-off has the
    same data-loss guarantees as a fresh resume."""
    home = _shared_home(tmp_path, monkeypatch)

    a = make_env(persistent=True, task_id="default")
    a._ensure_ready()
    sandbox = fake.only()
    remote = f"{a.remote_hermes_home}/skills/demo/SKILL.md"
    sandbox.files_written[remote] = b"# active-owner-v2\n"

    b = make_env(persistent=True, task_id="default")
    b._ensure_ready()
    assert b._is_writer is False

    a.cleanup()  # the writer detaches; its teardown pull applies A's edit host-side
    # The host then moves on (a user edit between sessions).
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v3\n", encoding="utf-8")

    result = b.execute("echo promoted", timeout=10)

    assert result["returncode"] == 0
    assert b._is_writer is True, "the reader never took the writer role over"
    assert sandbox.files_written[remote] == b"# host-v3\n", (
        "promotion did not push the host snapshot"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined and quarantined[0].read_bytes() == b"# active-owner-v2\n", (
        "promotion pushed over divergent remote state without quarantining it"
    )
    assert sandbox.alive is True and sandbox.kill_calls == 0


def test_reader_attachment_during_writer_teardown_stays_non_destructive(
    fake, tmp_path, monkeypatch
):
    """Reconnect-and-pull must keep the writer role until teardown is complete."""
    _shared_home(tmp_path, monkeypatch)
    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()
    sandbox_id = writer.sandbox_id
    reader = make_env(persistent=True, task_id="default")
    observations: list[tuple[bool, bool]] = []

    original_sync_back = writer._sync_manager.sync_back

    def _attach_reader_while_the_pull_is_in_progress(*args, **kwargs):
        reader._ensure_ready()
        observations.append((reader._is_writer, writer._writer_lease.held))
        return original_sync_back(*args, **kwargs)

    writer._sync_manager.sync_back = _attach_reader_while_the_pull_is_in_progress

    writer.cleanup()

    assert observations == [(False, True)]
    assert reader.sandbox_id == sandbox_id
    assert sandbox.alive is True and sandbox.kill_calls == 0 and sandbox.pause_calls == 0
    assert reader.execute("echo promoted", timeout=10)["returncode"] == 0
    assert reader._is_writer is True


def test_a_failed_promotion_fails_the_command_closed_and_is_retried(fake, tmp_path, monkeypatch):
    """Promotion inherits the fail-closed recovery rules: if the sandbox's
    state cannot be verified, the command must not run — and the takeover is
    retried on a later command rather than bricking the environment."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    a = make_env(persistent=True, task_id="default")
    a._ensure_ready()
    sandbox = fake.only()

    b = make_env(persistent=True, task_id="default")
    b._ensure_ready()
    a.cleanup()

    working = sandbox.files.read

    def _broken(*args, **kwargs):
        raise RuntimeError("read failed")

    sandbox.files.read = _broken
    with pytest.raises(EnvironmentConnectionError):
        b.execute("echo hi", timeout=10)
    assert b._is_writer is False
    assert not any("echo hi" in c.cmd for c in sandbox.commands_run), (
        "the command ran although the takeover could not verify sandbox state"
    )

    sandbox.files.read = working
    result = b.execute("echo hi", timeout=10)
    assert result["returncode"] == 0
    assert b._is_writer is True


def test_a_reader_cannot_run_commands_before_the_writer_has_prepared_the_sandbox(
    fake, tmp_path, monkeypatch
):
    """A reader must not execute against a sandbox its writer has not stamped
    ready: the sandbox holds no (or half-written) Hermes state. The competing
    writer is a lease held from "another process" — held directly, because an
    in-process writer that *fails* releases the role rather than sitting on
    it, so only a live external holder produces a durable reader."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError
    from hermes_plugin_e2b.sandbox import WriterLease

    _shared_home(tmp_path, monkeypatch)

    # Epoch 1: a writer prepares the sandbox and detaches cleanly.
    seed = make_env(persistent=True, task_id="default")
    seed._ensure_ready()
    sandbox = fake.only()
    marker = seed._state_ready_marker()
    assert marker in sandbox.files_written, "the premise is wrong — no stamp was written"
    seed.cleanup()

    # Epoch 2: another process's writer holds the lease mid-work — it has
    # invalidated the stamp and not (yet) completed its push.
    other_process = WriterLease(seed._scope)
    assert other_process.try_acquire() is True
    try:
        del sandbox.files_written[marker]

        b = make_env(persistent=True, task_id="default")
        with pytest.raises(EnvironmentConnectionError) as excinfo:
            b.execute("echo hi", timeout=10)
        assert "not ready" in str(excinfo.value)
        assert not any("echo hi" in c.cmd for c in sandbox.commands_run), (
            "a command ran in a sandbox the writer had not finished preparing"
        )

        # The external writer completes its push and stamps the sandbox.
        sandbox.files_written[marker] = b"someone-elses-push\n"

        result = b.execute("echo hi", timeout=10)
        assert result["returncode"] == 0
        assert b._is_writer is False, "B should still be a reader — the lease is held"
    finally:
        other_process.release()


def test_a_new_writer_invalidates_the_previous_epochs_readiness_stamp(fake, tmp_path, monkeypatch):
    """The stamp must attest the CURRENT writer generation's completed push.
    Reproduced pre-fix: a stale epoch-1 stamp let readers pass their gate and
    execute while epoch 2's writer's push had failed — the exact "its push
    failed" case the gate exists to block."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    # Epoch 1: prepared and stamped.
    first = make_env(persistent=True, task_id="default")
    first._ensure_ready()
    sandbox = fake.only()
    marker = first._state_ready_marker()
    assert marker in sandbox.files_written
    first.cleanup()

    # Epoch 2: the new writer's push fails mid-bring-up.
    def _boom(*args, **kwargs):
        raise RuntimeError("push failed")

    sandbox.files.write_files = _boom
    second = make_env(persistent=True, task_id="default")
    with pytest.raises(EnvironmentConnectionError):
        second._ensure_ready()

    assert marker not in sandbox.files_written, (
        "the stale epoch-1 stamp survived the new writer's failed bring-up — "
        "readers would pass their gate against half-pushed or stale state"
    )
    # Fail-closed holds scope-wide until a push completes: even a fresh
    # session (which wins the released lease and becomes the writer) cannot
    # run commands against the unprepared sandbox.
    third = make_env(persistent=True, task_id="default")
    with pytest.raises(EnvironmentConnectionError):
        third.execute("echo hi", timeout=10)
    assert not any("echo hi" in c.cmd for c in sandbox.commands_run)

    del sandbox.files.write_files  # the transport heals
    result = third.execute("echo hi", timeout=10)
    assert result["returncode"] == 0
    assert marker in sandbox.files_written, "the healed writer did not re-stamp"


def test_a_reader_does_not_resurrect_a_destroyed_sandbox(fake, tmp_path, monkeypatch):
    """When the scope's sandbox is destroyed server-side, only the writer may
    create the replacement: a reader-created one would be an empty sandbox no
    writer is preparing, and commands would run against no Hermes state."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    a = make_env(persistent=True, task_id="default")
    a._ensure_ready()
    b = make_env(persistent=True, task_id="default")
    b._ensure_ready()
    assert b._is_writer is False

    fake.sandboxes[a.sandbox_id].alive = False  # destroyed out from under both
    b._lease_deadline = 0.0  # force the next command to renew and notice

    with pytest.raises(EnvironmentConnectionError):
        b.execute("echo hi", timeout=10)
    assert len(fake.create_calls) == 1, "the reader created a replacement sandbox"

    # The writer rebuilds on its own next command...
    a._lease_deadline = 0.0
    assert a.execute("true", timeout=10)["returncode"] == 0
    assert len(fake.create_calls) == 2
    replacement = fake.only()
    assert any(path.startswith(a.remote_hermes_home + "/") for path in replacement.files_written), (
        "the writer's replacement was not prepared"
    )

    # ...and the reader lands on it, prepared, as a reader.
    b._lease_deadline = 0.0
    result = b.execute("echo hi", timeout=10)
    assert result["returncode"] == 0
    assert b.sandbox_id == a.sandbox_id
    assert b._is_writer is False


def test_a_failed_sandbox_replacement_hands_the_writer_role_straight_over(
    fake, tmp_path, monkeypatch
):
    """A writer whose sandbox vanished and whose replacement cannot be created
    holds no sandbox, so it cannot prepare one, stamp one, or push to one. If it
    kept the writer lease until its own teardown, every other live session would
    be locked out of the role while unable to do anything as a reader either —
    the whole scope frozen behind a session that has nothing to offer it."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    a = make_env(persistent=True, task_id="default")
    a._ensure_ready()
    assert a._is_writer is True

    fake.sandboxes[a.sandbox_id].alive = False
    fake.create_error = RuntimeError("creation is down")
    a._lease_deadline = 0.0
    with pytest.raises(EnvironmentConnectionError):
        a.execute("true", timeout=10)
    assert a._sandbox is None, "the premise is wrong — a sandbox is still attached"
    assert a._writer_lease.held is False, "the failed replacement kept the writer role"

    # Immediately, with no teardown of the failed session at all.
    fake.create_error = None
    successor = make_env(persistent=True, task_id="default")
    successor._ensure_ready()
    assert successor._is_writer is True, "the retained lease locked the successor out"


def test_a_failed_first_attach_releases_the_writer_role(fake, tmp_path, monkeypatch):
    """The same guarantee on the very first attach: creation is what fails most
    often (quota, outage, a rejected key), and it runs *after* the lease has been
    taken. Retrying must converge instead of leaving a phantom writer holding a
    role it can never fulfil."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    fake.create_error = RuntimeError("creation is down")
    a = make_env(persistent=True, task_id="default")
    with pytest.raises(EnvironmentConnectionError):
        a._ensure_ready()
    assert a._writer_lease.held is False, "a failed create kept the writer role"
    assert a._is_writer is False

    fake.create_error = None
    b = make_env(persistent=True, task_id="default")
    b._ensure_ready()
    assert b._is_writer is True, "the phantom writer locked the next session out"

    # And the failed session converges as a reader against the prepared sandbox
    # rather than staying wedged.
    a._ensure_ready()
    assert a.sandbox_id == b.sandbox_id
    assert a._is_writer is False


def test_a_malformed_sandbox_result_releases_the_writer_role(fake, tmp_path, monkeypatch):
    """Creation can also "succeed" and hand back something unusable. The id is
    read after the lease is taken, so that failure path needs the same release —
    and the sandbox reference must still be reachable for cleanup, or a live
    sandbox is left billing with nobody holding it."""
    from hermes_plugin_e2b import sandbox as sandbox_api

    _shared_home(tmp_path, monkeypatch)

    class Unusable:
        """What a partial create response looks like: no id."""

        sandbox_id = None

    installed_create = sandbox_api.create
    attempts: list[int] = []

    def create_once_malformed(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return Unusable()
        return installed_create(*args, **kwargs)

    monkeypatch.setattr(sandbox_api, "create", create_once_malformed)

    a = make_env(persistent=True, task_id="default")
    with pytest.raises(RuntimeError, match="without an id"):
        a._ensure_ready()
    assert a._writer_lease.held is False, "a malformed create result kept the writer role"
    assert a._sandbox is not None, "the unusable sandbox is unreachable for cleanup"

    b = make_env(persistent=True, task_id="default")
    b._ensure_ready()
    assert b._is_writer is True, "the phantom writer locked the next session out"
    assert b.sandbox_id is not None


def test_cleanup_releases_a_writer_lease_held_without_a_sandbox(tmp_path, monkeypatch):
    """Defence in depth for the teardown path.

    Every acquisition site now releases on failure (see the two tests above),
    so this state should be unreachable — but ``cleanup()`` is the last thing
    that runs for every environment, and a role leaked past it is leaked for
    the life of the process. It stays pinned independently of how the lease
    came to be held."""
    _shared_home(tmp_path, monkeypatch)

    env = make_env(persistent=True, task_id="default")
    assert env._writer_lease.try_acquire() is True
    assert env._sandbox is None

    env.cleanup()

    assert env._writer_lease.held is False, "cleanup leaked the writer lease"
    successor = make_env(persistent=True, task_id="default")
    assert successor._writer_lease.try_acquire() is True, (
        "the leaked lease locked the successor out"
    )
    successor._writer_lease.release()


def test_a_missing_flock_facility_degrades_loudly_not_silently(tmp_path, monkeypatch, caplog):
    """No exclusive lock means no single-writer guarantee. That must be an
    explicit, warned-about contract — not a silent writer-always fallback."""
    import logging

    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sandbox_api, "fcntl", None)

    lease = sandbox_api.WriterLease("degrade-scope")
    with caplog.at_level(logging.WARNING):
        assert lease.try_acquire() is True
    assert any("not protected" in r.message for r in caplog.records), (
        "the degraded writer-always mode was entered silently"
    )


def test_an_uncreatable_lease_file_degrades_loudly_not_silently(tmp_path, monkeypatch, caplog):
    import logging

    from hermes_plugin_e2b import sandbox as sandbox_api

    home = tmp_path / "home"
    home.mkdir()
    (home / "cache").write_text("a file where the lock directory should be")
    monkeypatch.setenv("HERMES_HOME", str(home))

    lease = sandbox_api.WriterLease("degrade-scope")
    with caplog.at_level(logging.WARNING):
        assert lease.try_acquire() is True
    assert any("not protected" in r.message for r in caplog.records)


def test_a_lock_incapable_filesystem_degrades_loudly_not_as_phantom_contention(
    tmp_path, monkeypatch, caplog
):
    """flock on NFS/SMB/FUSE without a lock manager raises ENOLCK/EOPNOTSUPP —
    not EWOULDBLOCK. Reading that as contention reported a phantom "another
    live session holds the writer role" forever (and never created a
    sandbox); it must take the loud writer-always degrade instead."""
    import errno
    import logging

    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _no_locks(*args, **kwargs):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(sandbox_api.fcntl, "flock", _no_locks)

    lease = sandbox_api.WriterLease("nfs-scope")
    with caplog.at_level(logging.WARNING):
        assert lease.try_acquire() is True, "ENOLCK was misread as contention"
    assert any("not protected" in r.message for r in caplog.records)


def test_the_writer_lease_excludes_a_second_process(tmp_path, monkeypatch):
    """The lease's whole point is arbitrating two Hermes PROCESSES, and flock
    semantics — exclusion between processes, hand-off on release — only exist
    across process boundaries, so this runs a real child process through
    acquire → hold → release → hand-off."""
    import subprocess
    import sys
    import textwrap

    from conftest import REPO_ROOT

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_plugin_e2b.sandbox import WriterLease

    acquired = tmp_path / "child-acquired"
    release = tmp_path / "release-now"
    child_code = textwrap.dedent(f"""
        import importlib.util, sys, time
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "hermes_plugin_e2b",
            {str(REPO_ROOT / "__init__.py")!r},
            submodule_search_locations=[{str(REPO_ROOT)!r}],
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "hermes_plugin_e2b"
        module.__path__ = [{str(REPO_ROOT)!r}]
        sys.modules["hermes_plugin_e2b"] = module
        spec.loader.exec_module(module)
        from hermes_plugin_e2b.sandbox import WriterLease

        lease = WriterLease("cross-process-scope")
        assert lease.try_acquire(), "child could not acquire a free lease"
        Path({str(acquired)!r}).touch()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not Path({str(release)!r}).exists():
            time.sleep(0.05)
        lease.release()
    """)
    child = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        deadline = time.monotonic() + 30
        while not acquired.exists() and time.monotonic() < deadline:
            assert child.poll() is None, "the child process died before acquiring"
            time.sleep(0.05)
        assert acquired.exists(), "the child never acquired the lease"

        mine = WriterLease("cross-process-scope")
        assert mine.try_acquire() is False, "two processes acquired one writer lease"

        release.touch()
        assert child.wait(timeout=30) == 0
        assert mine.try_acquire() is True, "the lease did not hand off after release"
        mine.release()
    finally:
        if child.poll() is None:
            child.kill()


def test_different_task_scopes_do_not_share_a_sandbox(fake):
    one = make_env(persistent=True, task_id="session:aaa")
    two = make_env(persistent=True, task_id="session:bbb")
    one._ensure_ready()
    two._ensure_ready()

    assert one.sandbox_id != two.sandbox_id
    assert len(fake.live) == 2


def test_a_different_template_is_never_adopted(fake):
    """Adopting a sandbox built from another template swaps the toolchain."""
    one = make_env(persistent=True, task_id="default", template="base")
    one._ensure_ready()
    two = make_env(persistent=True, task_id="default", template="custom-image")
    two._ensure_ready()

    assert one.sandbox_id != two.sandbox_id


# ---------------------------------------------------------------------------
# Ephemeral mode
# ---------------------------------------------------------------------------


def test_ephemeral_cleanup_kills_the_sandbox(fake):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    env.cleanup()

    assert sandbox.alive is False
    assert sandbox.kill_calls == 1


def test_ephemeral_sandboxes_are_never_adopted(fake):
    """Sharing a nominally throwaway sandbox lets one session kill another's."""
    one = make_env(persistent=False, task_id="default")
    two = make_env(persistent=False, task_id="default")
    one._ensure_ready()
    two._ensure_ready()

    assert one.sandbox_id != two.sandbox_id
    assert fake.list_calls == [], "ephemeral mode must not look for a sandbox to adopt"

    one.cleanup()
    assert fake.sandboxes[two.sandbox_id].alive is True


def test_ephemeral_sandboxes_are_created_to_die_on_timeout(fake):
    env = make_env(persistent=False)
    env._ensure_ready()
    assert fake.create_calls[-1]["lifecycle"]["on_timeout"] == "kill"


def test_behavioral_double_rejects_paused_and_killed_data_plane_calls(fake):
    from e2b import SandboxException, SandboxNotFoundException

    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    sandbox.pause(keep_memory=False)
    with pytest.raises(SandboxException):
        sandbox.commands.run("true")
    with pytest.raises(SandboxException):
        sandbox.files.write("/tmp/x", b"x")

    sandbox.connect(timeout=60)
    sandbox.kill()
    with pytest.raises(SandboxNotFoundException):
        sandbox.commands.run("true")
    with pytest.raises(SandboxNotFoundException):
        sandbox.files.read("/tmp/x")

    env.cleanup()


def test_ephemeral_timeout_leaves_room_for_idle_reaping_and_pull_before_kill(fake):
    env = make_env(
        persistent=False,
        lease_seconds=60,
        terminal_lifetime_seconds=120,
        command_grace_seconds=15,
    )
    env._ensure_ready()
    sandbox = fake.only()

    assert fake.create_calls[-1]["timeout"] == 240

    env.cleanup()

    assert fake.connect_calls, "cleanup did not renew the transfer window before sync-back"
    assert sandbox.kill_calls == 1 and sandbox.alive is False


def test_sync_back_setup_failure_cannot_skip_ephemeral_destruction(fake):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    def _fail_before_the_manager_retry_loop(*args, **kwargs):
        raise OSError("cannot create the host sync lock")

    env._sync_manager.sync_back = _fail_before_the_manager_retry_loop

    env.cleanup()

    assert sandbox.kill_calls == 1 and sandbox.alive is False
    assert env.wait_for_cleanup(timeout=1) is True


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_process_control_is_re_raised_only_after_mandatory_cleanup(fake, exception_type):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    def _interrupt_before_the_manager_retry_loop(*args, **kwargs):
        raise exception_type()

    env._sync_manager.sync_back = _interrupt_before_the_manager_retry_loop

    with pytest.raises(exception_type):
        env.cleanup()

    assert sandbox.kill_calls == 1 and sandbox.alive is False
    assert env._sandbox is None and env._sandbox_id is None
    assert env.wait_for_cleanup(timeout=1) is True


def test_reconnect_failure_still_releases_the_writer_and_runs_lifecycle_cleanup(
    fake, tmp_path, monkeypatch
):
    from hermes_plugin_e2b import sandbox as sandbox_api

    _shared_home(tmp_path, monkeypatch)
    env = make_env(persistent=True)
    env._ensure_ready()
    sandbox = fake.only()
    assert env._writer_lease.held is True

    ephemeral = make_env(persistent=False, task_id="session:ephemeral")
    ephemeral._ensure_ready()
    ephemeral_sandbox = fake.sandboxes[ephemeral.sandbox_id]

    def _cannot_reconnect(*args, **kwargs):
        raise RuntimeError("control plane unavailable")

    monkeypatch.setattr(sandbox_api, "reconnect_for_cleanup", _cannot_reconnect)

    env.cleanup()  # cleanup is never-raise
    ephemeral.cleanup()

    assert env._writer_lease.held is False
    assert sandbox.alive is True and sandbox.kill_calls == 0 and sandbox.pause_calls == 0
    assert ephemeral_sandbox.kill_calls == 1 and ephemeral_sandbox.alive is False


# ---------------------------------------------------------------------------
# Cleanup racing an in-flight command
# ---------------------------------------------------------------------------


def test_cleanup_during_a_command_defers_until_it_finishes(fake):
    """The idle reaper does not refresh activity while a foreground command runs.

    A command that outlives ``terminal.lifetime_seconds`` is therefore reaped
    mid-flight. Destroying the sandbox underneath it would lose the command's
    work and its output.
    """
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()
    sandbox.script("slow", Script(stdout="done\n", duration=1.5))

    results = {}

    def _run():
        results["result"] = env.execute("slow", timeout=30)

    worker = threading.Thread(target=_run)
    worker.start()
    # Let the command reach the sandbox, then reap it the way core does.
    deadline = time.monotonic() + 5
    while not sandbox.commands_run and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)

    env.cleanup()
    assert sandbox.alive is True, "cleanup destroyed a sandbox mid-command"

    worker.join(timeout=20)
    assert results["result"]["returncode"] == 0
    assert "done" in results["result"]["output"]
    assert env.wait_for_cleanup(timeout=10) is True
    assert sandbox.alive is False, "the deferred teardown never settled"


def test_a_command_cannot_start_after_cleanup(fake):
    """Admission and teardown settle under one lock, in that order."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    env = make_env(persistent=False)
    env._ensure_ready()
    env.cleanup()

    with pytest.raises(EnvironmentConnectionError):
        env._run_bash("true", timeout=5)


def test_cleanup_is_idempotent(fake):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    env.cleanup()
    env.cleanup()
    env.cleanup()

    assert sandbox.kill_calls == 1


def test_cleanup_survives_a_sandbox_that_is_already_gone(fake):
    env = make_env(persistent=False)
    env._ensure_ready()
    fake.sandboxes[env.sandbox_id].alive = False

    env.cleanup()  # must not raise

    assert env.wait_for_cleanup(timeout=5) is True


def test_wait_for_cleanup_is_true_before_a_sandbox_exists(fake):
    env = make_env()
    assert env.wait_for_cleanup(timeout=0.1) is True


def test_concurrent_first_use_creates_exactly_one_sandbox(fake):
    """Parallel subagents share one environment object and must not double-create."""
    env = make_env(persistent=True)
    barrier = threading.Barrier(4)
    errors = []

    def _use():
        try:
            barrier.wait(timeout=10)
            env.execute("echo hi", timeout=10)
        except Exception as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    threads = [threading.Thread(target=_use) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(fake.create_calls) == 1


# ---------------------------------------------------------------------------
# Partial initialisation
# ---------------------------------------------------------------------------


def test_a_sandbox_created_before_a_bootstrap_failure_is_still_reachable(fake, monkeypatch):
    """No orphans: the sandbox reference is published before anything can fail."""
    from hermes_plugin_e2b.environment import E2BEnvironment

    def _explode(self):
        raise RuntimeError("state upload failed")

    monkeypatch.setattr(E2BEnvironment, "_bootstrap", _explode)

    env = make_env(persistent=False)
    with pytest.raises(RuntimeError):
        env._ensure_ready()

    assert env.sandbox_id is not None
    sandbox = fake.only()

    env.cleanup()
    assert sandbox.alive is False, "a half-initialised sandbox was left billing"


def test_no_local_state_file_is_written(fake, tmp_path, monkeypatch):
    """Identity lives in E2B metadata; there is no pointer store to lose."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = make_env(persistent=True)
    env._ensure_ready()
    env.cleanup()

    json_files = list(tmp_path.rglob("*.json"))
    assert json_files == [], f"unexpected persisted state: {json_files}"


def test_identity_is_carried_by_sandbox_metadata(fake):
    from hermes_plugin_e2b.sandbox import (
        METADATA_PLUGIN_KEY,
        METADATA_PLUGIN_VALUE,
        METADATA_SCOPE_KEY,
    )

    env = make_env(persistent=True, task_id="session:abc")
    env._ensure_ready()

    metadata = fake.create_calls[-1]["metadata"]
    assert metadata[METADATA_PLUGIN_KEY] == METADATA_PLUGIN_VALUE
    assert metadata[METADATA_SCOPE_KEY]
    assert "session:abc" not in repr(metadata), "raw session ids must not leave the host"


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


def test_the_lease_is_extended_to_cover_a_long_command(fake):
    """The E2B lifecycle action must not fire while a command is running."""
    env = make_env(persistent=True, timeout=30)
    env._ensure_ready()
    sandbox = fake.only()

    env.execute("echo hi", timeout=540)

    assert sandbox.lease_seconds >= 540
    assert max(sandbox.lease_history) >= 540


def test_the_lease_is_never_shortened(fake):
    """set_timeout() can reduce a lease and must not be used on a shared sandbox."""
    env = make_env(persistent=True)
    env._ensure_ready()
    sandbox = fake.only()

    env.execute("long", timeout=540)
    before = sandbox.lease_seconds
    env.execute("short", timeout=5)

    assert sandbox.set_timeout_calls == []
    assert sandbox.lease_seconds >= before


def test_slow_control_plane_requests_cannot_overstate_the_local_lease_deadline(fake, monkeypatch):
    """Both creation and reconnect anchor their deadline before the request."""
    from hermes_plugin_e2b import environment as environment_module
    from hermes_plugin_e2b import sandbox as sandbox_api

    clock = [100.0]
    monkeypatch.setattr(environment_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(environment_module.E2BEnvironment, "_bootstrap", lambda self: None)

    installed_create = sandbox_api.create

    def _slow_create(*args, **kwargs):
        clock[0] += 40.0
        return installed_create(*args, **kwargs)

    monkeypatch.setattr(sandbox_api, "create", _slow_create)
    env = make_env(persistent=False, lease_seconds=300, terminal_lifetime_seconds=0)
    env._ensure_ready()

    assert env._lease_deadline == 400.0, "creation latency was added to the server-side lease"

    installed_renew = sandbox_api.renew_lease

    def _slow_renew(*args, **kwargs):
        clock[0] += 50.0
        return installed_renew(*args, **kwargs)

    monkeypatch.setattr(sandbox_api, "renew_lease", _slow_renew)
    env._renew_lease(600)

    assert env._lease_deadline == 740.0, "reconnect latency was added to the server-side lease"


def test_command_reconnect_uses_the_current_profile_key(fake, monkeypatch):
    from hermes_plugin_e2b import environment as environment_module

    current_key = ["e2b_initial-profile-key"]
    monkeypatch.setattr(environment_module, "get_api_key", lambda: current_key[0])
    env = make_env(persistent=False)
    env._ensure_ready()

    current_key[0] = "e2b_rotated-profile-key"
    env._lease_deadline = 0.0
    assert env.execute("true", timeout=10)["returncode"] == 0

    assert fake.connect_requests[-1]["api_key"] == "e2b_rotated-profile-key"


def test_teardown_reconnect_uses_the_attached_key_after_profile_scope_disappears(
    fake, tmp_path, monkeypatch
):
    from hermes_plugin_e2b import environment as environment_module

    _shared_home(tmp_path, monkeypatch)
    attached_key = "e2b_attached-profile-key"
    monkeypatch.setattr(environment_module, "get_api_key", lambda: attached_key)
    env = make_env(persistent=False)
    env._ensure_ready()

    # Cleanup must not consult the current scope: it may be gone at atexit, or
    # may now belong to another multiplexed profile.
    monkeypatch.setattr(
        environment_module,
        "get_api_key",
        lambda: (_ for _ in ()).throw(AssertionError("profile scope was consulted")),
    )
    env.cleanup()

    assert fake.connect_requests[-1]["api_key"] == attached_key
    assert fake.sandboxes[next(iter(fake.sandboxes))].alive is False


def test_each_sync_back_retry_reconnects_immediately_before_its_download(
    fake, tmp_path, monkeypatch
):
    """A failed first download cannot strand later retries on a paused client."""
    from hermes_plugin_e2b import environment as environment_module
    from hermes_plugin_e2b import sandbox as sandbox_api

    home = _shared_home(tmp_path, monkeypatch)
    env = make_env(persistent=True)
    env._ensure_ready()
    sandbox = fake.only()
    remote = f"{env.remote_hermes_home}/skills/demo/SKILL.md"
    sandbox.files_written[remote] = b"# remote-v2\n"

    clock = [1000.0]
    monkeypatch.setattr(environment_module.time, "monotonic", lambda: clock[0])
    env._lease_deadline = 0.0

    reconnects: list[tuple[float, int]] = []
    installed_reconnect = sandbox_api.reconnect_for_cleanup

    def _record_reconnect(attached, lease_seconds):
        reconnects.append((clock[0], lease_seconds))
        return installed_reconnect(attached, lease_seconds)

    monkeypatch.setattr(sandbox_api, "reconnect_for_cleanup", _record_reconnect)

    downloads: list[str] = []
    installed_download = env._download_hermes_tar

    def _fail_after_consuming_the_first_lease(dest):
        downloads.append(sandbox.state)
        if len(downloads) == 1:
            clock[0] += env._settings.cleanup_lease_for(300) + 1
            sandbox.state = "paused"
            raise RuntimeError("first transfer consumed its lease")
        assert sandbox.state == "running"
        return installed_download(dest)

    monkeypatch.setattr(env, "_download_hermes_tar", _fail_after_consuming_the_first_lease)

    env.cleanup()

    cleanup_lease = env._settings.cleanup_lease_for(300)
    assert reconnects == [(1000.0, cleanup_lease), (1000.0 + cleanup_lease + 1, cleanup_lease)]
    assert downloads == ["running", "running"]
    assert (home / "skills" / "demo" / "SKILL.md").read_bytes() == b"# remote-v2\n"


def test_finalisation_does_not_run_a_state_pull(fake, monkeypatch, tmp_path):
    """A pull from ``__del__`` deadlocks the process.

    ``BaseEnvironment.__del__`` calls ``cleanup()``, and the collector runs it
    at an arbitrary point in an arbitrary stack — including inside another
    environment's pull, which holds an exclusive ``flock`` on
    ``~/.hermes/.sync.lock``. A nested pull opens a second descriptor for that
    file and blocks on a lock this same thread already holds.
    """
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "s.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()
    before = len([c for c in sandbox.commands_run if "tar cf" in c.cmd])

    env.__del__()

    after = len([c for c in sandbox.commands_run if "tar cf" in c.cmd])
    assert after == before, "finalisation ran a state pull"
    # It still releases the resource — an orphaned sandbox bills forever.
    assert sandbox.alive is False


def test_a_nested_pull_is_skipped_rather_than_deadlocking(fake, monkeypatch, tmp_path):
    """Hermes' sync-back flock is not reentrant."""
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "s.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    outer = make_env(persistent=False, task_id="outer")
    inner = make_env(persistent=False, task_id="inner")
    outer._ensure_ready()
    inner._ensure_ready()
    inner_sandbox = fake.sandboxes[inner.sandbox_id]

    # Tear the inner environment down from inside the outer one's pull, which
    # is exactly what a garbage collection at the wrong moment does.
    original = outer._download_hermes_tar

    def _reentrant(dest):
        inner.cleanup()
        return original(dest)

    outer._download_hermes_tar = _reentrant
    outer._sync_manager._bulk_download_fn = _reentrant

    outer.cleanup()

    assert outer.wait_for_cleanup(timeout=30) is True
    assert inner.wait_for_cleanup(timeout=30) is True
    assert inner_sandbox.alive is False


# ---------------------------------------------------------------------------
# Reader admission: the host-side state marker
# ---------------------------------------------------------------------------
#
# The in-sandbox readiness stamp is probed once, at bootstrap, and it survives
# an incremental sync failure. Two windows therefore need host-side truth
# instead: a reader that already bootstrapped meeting a *new* writer
# generation, and a writer whose per-command sync failed leaving the sandbox
# holding state the host knows to be stale. Both are checked at command
# admission, and neither is claimed to be atomic with the launch that follows.


def _break_uploads(sandbox, message="envd write failed"):
    def _boom(*args, **kwargs):
        raise RuntimeError(message)

    sandbox.files.write_files = _boom


def _break_uploads_on_create(fake, message="envd write failed"):
    """Break uploads in every sandbox this fake creates from now on.

    Needed when the push to break happens during the very bring-up that
    creates the sandbox, so there is no handle to reach for beforehand.
    """
    original_create = fake.create

    def _create_with_broken_uploads(*args, **kwargs):
        sandbox = original_create(*args, **kwargs)
        _break_uploads(sandbox, message)
        return sandbox

    fake.create = _create_with_broken_uploads


def test_a_bootstrapped_reader_cannot_run_during_another_writers_transition(
    fake, tmp_path, monkeypatch
):
    """The reader has already passed the readiness stamp and will never probe it
    again. When a new writer generation starts — stamp invalidated, recovery and
    push in flight — its commands would run against a sandbox that holds neither
    the old state nor yet the new one.

    Driven as a real interleaving: the reader's command is issued from inside the
    new writer's push, so the writer genuinely holds the lease and has genuinely
    marked the scope.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    first = make_env(persistent=True, task_id="default")
    first._ensure_ready()
    sandbox = fake.only()

    reader = make_env(persistent=True, task_id="default")
    reader._ensure_ready()
    assert reader._is_writer is False
    assert reader.execute("echo before", timeout=10)["returncode"] == 0

    first.cleanup()  # the role frees up; the sandbox stays alive and stamped

    attempts: list[object] = []
    original_write_files = sandbox.files.write_files

    def push_and_let_the_reader_try(entries, **kwargs):
        if not attempts:
            try:
                attempts.append(reader.execute("echo during", timeout=10))
            except EnvironmentConnectionError as exc:
                attempts.append(exc)
        return original_write_files(entries, **kwargs)

    sandbox.files.write_files = push_and_let_the_reader_try

    second = make_env(persistent=True, task_id="default")
    second._ensure_ready()  # adopts, wins the role, marks, recovers, pushes

    assert second._is_writer is True
    assert attempts, "the premise is wrong — the reader never ran mid-transition"
    assert isinstance(attempts[0], EnvironmentConnectionError), (
        f"a reader executed during a writer transition: {attempts[0]}"
    )
    assert "is being rebuilt or is known to be stale" in str(attempts[0])
    assert not any("echo during" in call.cmd for call in sandbox.commands_run), (
        "the command reached the sandbox despite the transition"
    )

    # And once the transition completes, the reader runs again.
    assert reader.execute("echo after", timeout=10)["returncode"] == 0


def test_a_fresh_reader_cannot_bootstrap_during_a_writer_transition(fake, tmp_path, monkeypatch):
    """The stamp cannot cover a session that arrives mid-transition either: the
    writer of the *previous* generation may have left it in place."""
    from hermes_plugin_e2b import sandbox as sandbox_api
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()
    assert writer._state_ready_marker() in sandbox.files_written, (
        "the premise is wrong — no stamp, so the gate under test is not the marker"
    )

    sandbox_api.DirtyState(writer._scope).mark("a writer session is preparing this sandbox's state")

    fresh = make_env(persistent=True, task_id="default")
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        fresh._ensure_ready()

    assert "is being rebuilt or is known to be stale" in str(excinfo.value)
    assert excinfo.value.retry_hint


def test_a_readers_commands_stop_once_the_writers_sync_has_failed(fake, tmp_path, monkeypatch):
    """A failed incremental sync leaves the sandbox holding state the host knows
    is wrong — a deleted credential still live in it, a skill update missing.
    The writer fails its own command closed, but the readiness stamp still
    attests its bring-up push, so nothing else would hold the readers back."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = _shared_home(tmp_path, monkeypatch)
    # Non-forced cycles are rate-limited to one per 5s and bring-up just ran
    # one. HERMES_FORCE_FILE_SYNC is Hermes' own escape hatch for that.
    monkeypatch.setenv("HERMES_FORCE_FILE_SYNC", "1")

    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()

    reader = make_env(persistent=True, task_id="default")
    reader._ensure_ready()
    assert reader._is_writer is False
    assert reader.execute("echo before", timeout=10)["returncode"] == 0

    (home / "skills" / "demo" / "NEW.md").write_text("pending host change\n", encoding="utf-8")
    _break_uploads(sandbox)

    with pytest.raises(EnvironmentConnectionError):
        writer.execute("echo writer", timeout=10)

    assert writer._state_ready_marker() in sandbox.files_written, (
        "the premise is wrong — the stamp is gone, so this would be the stamp's gate"
    )

    with pytest.raises(EnvironmentConnectionError) as excinfo:
        reader.execute("echo stale", timeout=10)

    assert "is being rebuilt or is known to be stale" in str(excinfo.value)
    assert not any("echo stale" in call.cmd for call in sandbox.commands_run), (
        "a reader ran against state the writer had already failed to correct"
    )

    # The writer's retry heals it, and the reader resumes.
    del sandbox.files.write_files
    assert writer.execute("echo healed", timeout=10)["returncode"] == 0
    assert reader.execute("echo resumed", timeout=10)["returncode"] == 0
    assert any("echo resumed" in call.cmd for call in sandbox.commands_run)


def test_a_crashed_writers_marker_is_cleared_only_by_a_completed_takeover(
    fake, tmp_path, monkeypatch
):
    """A writer killed mid-transition cannot clean up after itself: the OS
    releases its lease, but the marker stays. That is deliberate — the state
    really is unusable — so the next session must be able to heal it by
    completing the work, and must NOT be able to clear it by failing at it."""
    from hermes_plugin_e2b import sandbox as sandbox_api
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()

    reader = make_env(persistent=True, task_id="default")
    reader._ensure_ready()
    assert reader._is_writer is False

    # The writer process dies mid-transition: lease gone with it, marker left.
    writer.cleanup()
    marker = sandbox_api.DirtyState(reader._scope)
    marker.mark("a writer session is preparing this sandbox's state")
    assert marker.path.exists()

    # A takeover that fails must leave the marker exactly as it was.
    _break_uploads(sandbox)
    with pytest.raises(EnvironmentConnectionError):
        reader.execute("echo during", timeout=10)
    assert marker.path.exists(), "a failed takeover cleared the marker"
    assert reader._is_writer is False
    assert reader._writer_lease.held is False, "a failed takeover kept the role"
    assert not any("echo during" in call.cmd for call in sandbox.commands_run)

    # A takeover that completes recovery and the push clears it.
    del sandbox.files.write_files
    assert reader.execute("echo healed", timeout=10)["returncode"] == 0
    assert reader._is_writer is True
    assert not marker.path.exists(), "a completed takeover left the scope marked"


def test_an_ephemeral_environment_has_no_reader_gate_to_apply(fake):
    """Ephemeral sandboxes have exactly one owner and no readers, so they must
    not acquire a lease, write a marker, or be gated by one."""
    env = make_env(persistent=False, task_id="default")
    assert env._writer_lease is None
    assert env._dirty_state is None

    env._ensure_ready()

    assert env._is_writer is True
    env.execute("true", timeout=10)


def test_a_promotion_marks_the_scope_before_it_touches_anything(fake, tmp_path, monkeypatch):
    """The takeover path needs the same mark as a fresh bring-up, and for the
    same reason: from the moment the readiness stamp is invalidated until the
    push completes, a reader that bootstrapped earlier must not execute.

    Driven as a real interleaving — the second reader's command is issued from
    inside the promoting session's push, so the mark under test is the one
    production actually writes, not one planted by the test.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()

    promoting = make_env(persistent=True, task_id="default")
    promoting._ensure_ready()
    bystander = make_env(persistent=True, task_id="default")
    bystander._ensure_ready()
    assert promoting._is_writer is False and bystander._is_writer is False
    assert bystander.execute("echo before", timeout=10)["returncode"] == 0

    writer.cleanup()  # the role frees up; the sandbox stays alive

    attempts: list[object] = []
    original_write_files = sandbox.files.write_files

    def push_and_let_the_bystander_try(entries, **kwargs):
        if not attempts:
            try:
                attempts.append(bystander.execute("echo during", timeout=10))
            except EnvironmentConnectionError as exc:
                attempts.append(exc)
        return original_write_files(entries, **kwargs)

    sandbox.files.write_files = push_and_let_the_bystander_try

    assert promoting.execute("echo promoted", timeout=10)["returncode"] == 0
    assert promoting._is_writer is True

    assert attempts, "the premise is wrong — the bystander never ran mid-promotion"
    assert isinstance(attempts[0], EnvironmentConnectionError), (
        f"a reader executed during a promotion: {attempts[0]}"
    )
    assert "is being rebuilt or is known to be stale" in str(attempts[0])
    assert not any("echo during" in call.cmd for call in sandbox.commands_run)


def test_a_writer_that_takes_the_role_back_recovers_before_pushing(fake, tmp_path, monkeypatch):
    """``_resumed_existing`` records what the last attach did, which is not the
    same question as "is this generation a takeover". A session that created
    the sandbox, failed its bring-up, released the role and then re-acquired it
    is taking the sandbox over — from whatever ran in between — so it must
    recover before force-pushing, or it buries that work."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = _shared_home(tmp_path, monkeypatch)

    env = make_env(persistent=True, task_id="default")
    _break_uploads_on_create(fake)
    with pytest.raises(EnvironmentConnectionError):
        env._ensure_ready()  # created the sandbox, push failed, role released
    sandbox = fake.only()
    assert env._resumed_existing is False, "the premise is wrong — this was not a fresh create"
    assert env._writer_lease.held is False
    assert env._bootstrapped is False

    # Something happens in the sandbox before this session retries: another
    # session prepared it, and the agent authored a skill there.
    del sandbox.files.write_files
    sandbox.files_written[f"{env.remote_hermes_home}/skills/invented/SKILL.md"] = (
        b"# authored between attempts\n"
    )

    env._ensure_ready()  # re-acquires the role in _bootstrap

    assert env._is_writer is True
    assert (home / "skills" / "invented" / "SKILL.md").read_text(encoding="utf-8") == (
        "# authored between attempts\n"
    ), "the re-acquired role force-pushed without recovering first"


def test_a_demoted_session_does_not_pull_state_back_at_teardown(fake, tmp_path, monkeypatch):
    """The teardown pull is plugin-managed synchronisation, so it belongs to
    the writer alone. A session demoted after its push had already committed
    keeps a manager with a baseline; pulling with it while another session owns
    the role would apply sandbox state onto the host outside the single
    synchroniser, and could overwrite what that session has already
    superseded."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()

    reader = make_env(persistent=True, task_id="default")
    reader._ensure_ready()
    writer.cleanup()  # the role frees up

    # Promotion whose push SUCCEEDS and whose readiness stamp then fails: the
    # manager commits a baseline, and the role is released on the way out.
    def _no_stamp(path, data, **kwargs):
        raise RuntimeError("envd write failed")

    sandbox.files.write = _no_stamp
    with pytest.raises(EnvironmentConnectionError):
        reader.execute("echo promote", timeout=10)
    del sandbox.files.write

    assert reader._is_writer is False, "the premise is wrong — still the writer"
    assert reader._sync_manager is not None
    assert reader._sync_manager._pushed_hashes, (
        "the premise is wrong — no committed baseline, so sync_back would skip anyway"
    )

    # Another session now owns the role.
    successor = make_env(persistent=True, task_id="default")
    successor._ensure_ready()
    assert successor._is_writer is True

    pulls_before = len([c for c in sandbox.commands_run if "tar cf" in c.cmd])
    reader.cleanup()
    pulls_after = len([c for c in sandbox.commands_run if "tar cf" in c.cmd])

    assert pulls_after == pulls_before, (
        "a demoted session pulled state back while another session held the role"
    )


def test_an_unresolvable_command_credential_releases_the_writer_role(fake, tmp_path, monkeypatch):
    """A failed command renewal cannot freeze healthy peers behind its lease.

    The attached SDK object remains reachable: without a current profile key
    the plugin cannot know whether the sandbox vanished, and teardown must be
    able to use that attachment's own connection parameters.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)

    env = make_env(persistent=True, task_id="default")
    env._ensure_ready()
    assert env._is_writer is True

    fake.sandboxes[env.sandbox_id].alive = False
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    env._lease_deadline = 0.0

    with pytest.raises(EnvironmentConnectionError):
        env.execute("true", timeout=10)

    assert env._writer_lease.held is False, "an unresolvable credential kept the writer role"
    assert env._sandbox is not None, "the cleanup-capable attachment was discarded"


def test_a_sync_that_raises_outright_also_marks_the_scope(fake, tmp_path, monkeypatch):
    """There are two ways a per-command sync fails: Hermes' manager swallows a
    transport error and rolls back (reported through the log/callback verdict),
    or it raises before its transaction even starts — enumerating the host
    files. Both leave the sandbox holding state the host knows is stale, so
    both have to hold readers back, not just the one that is easier to reach."""
    from hermes_plugin_e2b import environment as env_mod
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    _shared_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_FORCE_FILE_SYNC", "1")

    writer = make_env(persistent=True, task_id="default")
    writer._ensure_ready()
    sandbox = fake.only()

    reader = make_env(persistent=True, task_id="default")
    reader._ensure_ready()
    assert reader.execute("echo before", timeout=10)["returncode"] == 0

    def _enumeration_fails(*args, **kwargs):
        raise RuntimeError("the host file enumeration failed")

    monkeypatch.setattr(env_mod, "iter_sync_files", _enumeration_fails)

    with pytest.raises(EnvironmentConnectionError) as excinfo:
        writer.execute("echo writer", timeout=10)
    assert "state sync before the command" in str(excinfo.value)

    with pytest.raises(EnvironmentConnectionError) as reader_failure:
        reader.execute("echo stale", timeout=10)
    assert "is being rebuilt or is known to be stale" in str(reader_failure.value)
    assert not any("echo stale" in call.cmd for call in sandbox.commands_run)


def test_an_unreadable_state_marker_counts_as_dirty_and_says_so(tmp_path, monkeypatch, caplog):
    """The marker only exists because something set it, so "present but
    unreadable" is not evidence of a usable state — and losing the admission
    gate must be as loud as losing the writer lease is."""
    import logging
    import os
    import stat

    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = sandbox_api.DirtyState("0" * 32)
    state.mark("a writer session is preparing this sandbox's state")
    assert state.reason() is not None

    os.chmod(state.path, 0)
    try:
        assert not os.access(state.path, os.R_OK), "the premise is wrong — still readable"
        with caplog.at_level(logging.WARNING):
            reason = state.reason()
    finally:
        os.chmod(state.path, stat.S_IRUSR | stat.S_IWUSR)

    assert reason is not None, "an unreadable marker was read as a clean state"
    assert any("not usable" in record.message for record in caplog.records), (
        "the admission gate stopped working silently"
    )


def test_clearing_the_marker_is_idempotent_and_never_raises(tmp_path, monkeypatch):
    """Called on every successful sync, including when nothing set it."""
    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = sandbox_api.DirtyState("0" * 32)

    state.clear()  # never marked
    state.mark("transient")
    state.clear()
    state.clear()

    assert state.reason() is None
    assert not state.path.exists()


def test_an_unwritable_state_marker_says_concurrent_sessions_are_unsupported(
    tmp_path, monkeypatch, caplog
):
    """The reader-admission guarantee rests on two host facilities: an exclusive
    writer lock and this marker. Losing either one means the same thing, so it
    must be said in the same terms — concurrent same-scope persistent sessions
    are unsupported, a single session is unaffected.

    Deliberately NOT asserted here: that reader gating still works. It cannot,
    and a test pretending otherwise would be worse than no test.
    """
    import logging

    from hermes_plugin_e2b import sandbox as sandbox_api

    home = tmp_path / "home"
    home.mkdir()
    # A file where the coordination directory needs to be: the marker cannot be
    # created, and neither can the lease file.
    (home / "cache").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    state = sandbox_api.DirtyState("0" * 32)
    with caplog.at_level(logging.WARNING):
        state.mark("a writer session is preparing this sandbox's state")

    assert not state.path.exists()
    messages = [record.getMessage() for record in caplog.records]
    assert any("NOT SUPPORTED" in message for message in messages), messages
    assert any("single Hermes session is unaffected" in message for message in messages), messages


def test_the_two_coordination_facilities_report_the_same_contract(tmp_path, monkeypatch, caplog):
    """One contract, one wording. A reader of the log should not have to work
    out whether an unavailable lock and an unavailable marker mean different
    things — they do not."""
    import logging

    from hermes_plugin_e2b import sandbox as sandbox_api

    home = tmp_path / "home"
    home.mkdir()
    (home / "cache").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    with caplog.at_level(logging.WARNING):
        assert sandbox_api.WriterLease("0" * 32).try_acquire() is True
        lock_messages = [r.getMessage() for r in caplog.records]
        caplog.clear()
        sandbox_api.DirtyState("0" * 32).mark("transition")
        marker_messages = [r.getMessage() for r in caplog.records]

    for messages in (lock_messages, marker_messages):
        assert any("NOT SUPPORTED" in message for message in messages), messages
        assert any(
            "concurrent same-scope persistent sessions" in message.lower() for message in messages
        ), messages
