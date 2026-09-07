"""State authority: what moves between the host and the sandbox, and when.

Model: the host owns ``~/.hermes`` (config, credentials, skills, cache); the
sandbox owns its own workspace. Hermes' own ``FileSyncManager`` does the
moving — this plugin only supplies the transport — so these tests drive the
real manager over the plugin's E2B transport rather than asserting call shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_e2b import FakeE2B, install


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def fake(monkeypatch, api_key):
    return install(monkeypatch, FakeE2B())


def make_env(persistent=True, task_id="default"):
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    return E2BEnvironment(
        task_id=task_id,
        settings=E2BSettings(),
        timeout=30,
        persistent_filesystem=persistent,
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_host_state_is_pushed_on_first_use(fake, hermes_home):
    env = make_env()
    env._ensure_ready()

    written = fake.only().files_written
    assert any(path.endswith("/skills/demo/SKILL.md") for path in written), written
    assert all(path.startswith(env.remote_hermes_home) for path in written)


def test_nested_paths_need_no_separate_mkdir(fake, hermes_home):
    """``files.write_files`` creates missing parents, so there is no
    "fresh sandbox is missing a parent directory" failure mode to guard: a
    half-succeeded mkdir pass cannot exist because there is no mkdir pass.
    """
    env = make_env()
    env._ensure_ready()

    commands = [call.cmd for call in fake.only().commands_run]
    assert not any("mkdir" in cmd for cmd in commands), commands
    assert any("/skills/demo/SKILL.md" in path for path in fake.only().files_written)


def test_uploads_are_batched_so_file_descriptors_cannot_run_out(fake, hermes_home):
    from hermes_plugin_e2b.environment import _UPLOAD_BATCH

    skills = hermes_home / "skills" / "many"
    skills.mkdir(parents=True)
    for index in range(_UPLOAD_BATCH * 2 + 5):
        (skills / f"s{index}.md").write_text("x", encoding="utf-8")

    env = make_env()
    env._ensure_ready()

    calls = fake.only().write_files_calls
    assert len(calls) > 1
    assert max(len(batch) for batch in calls) <= _UPLOAD_BATCH


def test_a_failed_upload_does_not_advance_the_sync_state(fake, hermes_home):
    """Hermes' manager rolls back on failure; the next cycle must retry."""
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()

    (hermes_home / "skills" / "demo" / "NEW.md").write_text("new\n", encoding="utf-8")
    original = sandbox.files.write_files
    sandbox.files.write_files = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upload failed"))
    env._sync_manager.sync(force=True)
    sandbox.files.write_files = original

    env._sync_manager.sync(force=True)
    assert any(path.endswith("NEW.md") for path in sandbox.files_written)


# ---------------------------------------------------------------------------
# Sync-back
# ---------------------------------------------------------------------------


def edit_in_sandbox(sandbox, remote_path: str, content: str) -> None:
    """Simulate the agent editing a file inside the sandbox.

    The double builds its sync-back tar from its own filesystem, so writing
    here is what a real in-sandbox edit looks like to the pull.
    """
    sandbox.files_written[remote_path] = content.encode("utf-8")


def test_sync_back_pulls_remote_changes_on_teardown(fake, hermes_home, tmp_path):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(
        sandbox,
        f"{env.remote_hermes_home}/skills/demo/SKILL.md",
        "# edited in the sandbox\n",
    )

    env.cleanup()

    assert (hermes_home / "skills" / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# edited in the sandbox\n"


def test_sync_back_is_skipped_when_nothing_was_ever_pushed(fake, tmp_path, monkeypatch):
    """No baseline means no way to tell remote-authored from remote-default."""
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(empty_home))

    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    env.cleanup()

    assert not any("tar cf" in call.cmd for call in sandbox.commands_run)


def test_the_remote_archive_is_removed_even_when_the_read_fails(fake, hermes_home):
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()

    def _unreadable(*args, **kwargs):
        raise RuntimeError("envd read failed")

    sandbox.files.read = _unreadable

    with pytest.raises(RuntimeError, match="envd read failed"):
        env._download_hermes_tar(Path("/dev/null"))

    assert any("rm -f" in call.cmd for call in sandbox.commands_run)


def test_an_oversized_archive_is_refused_mid_transfer(fake, hermes_home, tmp_path, monkeypatch):
    """A sandbox is untrusted: it must not be able to fill the host disk."""
    from hermes_plugin_e2b import environment as env_mod
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    monkeypatch.setattr(env_mod, "MAX_SYNC_BACK_BYTES", 1024)

    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    import os

    sandbox.readable_files[f"/tmp/hermes-sync-back.{os.getpid()}.{env._session_id}.tar"] = (
        b"z" * 8192
    )
    sandbox.maybe_make_tar = lambda cmd: None  # keep the oversized payload

    dest = tmp_path / "out.tar"
    with pytest.raises(EnvironmentConnectionError):
        env._download_hermes_tar(dest)

    assert dest.stat().st_size <= 1024 + 4096
    assert sandbox.stream_readers_closed >= 1


def test_the_stream_reader_is_closed_after_a_successful_download(fake, hermes_home, tmp_path):
    import os

    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    sandbox.maybe_make_tar = lambda cmd: None
    sandbox.readable_files[f"/tmp/hermes-sync-back.{os.getpid()}.{env._session_id}.tar"] = (
        b"tar-bytes"
    )

    dest = tmp_path / "out.tar"
    env._download_hermes_tar(dest)

    assert dest.read_bytes() == b"tar-bytes"
    assert sandbox.stream_readers_closed == 1


def test_a_credential_file_is_uploaded_but_never_pulled_back(
    fake, hermes_home, tmp_path, monkeypatch
):
    """Remote data must not be able to overwrite a host credential file.

    Hermes' manager enforces this; the test proves the plugin's transport does
    not route around it, because a resurrection here would be silent.
    """
    from tools import credential_files

    secret = hermes_home / "creds.json"
    secret.write_text('{"token": "host-value"}', encoding="utf-8")
    monkeypatch.setattr(
        credential_files,
        "get_credential_file_mounts",
        lambda: [{"host_path": str(secret), "container_path": "/root/.hermes/creds.json"}],
    )

    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()
    assert any(path.endswith("creds.json") for path in sandbox.files_written)

    edit_in_sandbox(
        sandbox,
        f"{env.remote_hermes_home}/creds.json",
        '{"token": "sandbox-value"}',
    )
    env.cleanup()

    assert secret.read_text(encoding="utf-8") == '{"token": "host-value"}'


# ---------------------------------------------------------------------------
# Failure detection — Hermes' manager swallows everything
# ---------------------------------------------------------------------------


def _break_uploads(sandbox, message="envd write failed"):
    def _boom(*args, **kwargs):
        raise RuntimeError(message)

    sandbox.files.write_files = _boom


def test_a_failed_initial_upload_is_fatal_not_silent(fake, hermes_home):
    """``FileSyncManager.sync()`` returns None and swallows every exception.

    Without an explicit check the agent gets a sandbox holding none of its
    skills, credentials, or cached files, with nothing to say why its tools
    behave differently.
    """
    from hermes_plugin_e2b.environment import E2BEnvironment
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    original_create = fake.create

    def _create_with_broken_uploads(*args, **kwargs):
        sandbox = original_create(*args, **kwargs)
        _break_uploads(sandbox)
        return sandbox

    fake.create = _create_with_broken_uploads

    env = E2BEnvironment(task_id="default", timeout=30, persistent_filesystem=False)
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        env._ensure_ready()

    assert "initial state upload" in str(excinfo.value)


def test_a_failed_per_command_sync_fails_the_command_closed(fake, hermes_home, caplog, monkeypatch):
    """A failed cycle is transactional over uploads AND deletes, and the sync
    set includes credential files — so the pending change may be a credential
    deletion or a skill update. Running the command anyway would execute it
    against known-stale, potentially security-sensitive state.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    # Non-forced cycles are rate-limited to one per 5s and bring-up just ran
    # one. HERMES_FORCE_FILE_SYNC is Hermes' own escape hatch for that.
    monkeypatch.setenv("HERMES_FORCE_FILE_SYNC", "1")
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()

    (hermes_home / "skills" / "demo" / "NEW.md").write_text("new\n", encoding="utf-8")
    _break_uploads(sandbox)

    with caplog.at_level("WARNING"), pytest.raises(EnvironmentConnectionError) as excinfo:
        env.execute("echo hi", timeout=10)

    assert "state sync before the command" in str(excinfo.value)
    assert not any("echo hi" in c.cmd for c in sandbox.commands_run), (
        "the command reached the sandbox despite the failed state push"
    )
    assert any("refusing to run the command" in r.message for r in caplog.records)


def test_a_host_deleted_credential_blocks_commands_until_the_delete_propagates(
    fake, hermes_home, tmp_path, monkeypatch
):
    """The concrete hazard behind failing closed: the host revoked a
    credential, the sandbox still holds it, and the delete could not be
    propagated. A command run in that window uses the revoked credential."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError
    from tools import credential_files
    from tools.environments import file_sync

    monkeypatch.delenv("HERMES_FORCE_FILE_SYNC", raising=False)
    monkeypatch.setattr(file_sync, "_monotonic", lambda: 100.0)
    secret = hermes_home / "creds.json"
    secret.write_text('{"token": "live-token"}', encoding="utf-8")
    mounts = [{"host_path": str(secret), "container_path": "/root/.hermes/creds.json"}]
    monkeypatch.setattr(credential_files, "get_credential_file_mounts", lambda: list(mounts))

    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    remote_cred = next(path for path in sandbox.files_written if path.endswith("creds.json"))

    # The host revokes the credential; propagating the delete fails.
    secret.unlink()
    mounts.clear()
    original_run = sandbox.commands.run

    def _no_deletes(cmd, **kwargs):
        if cmd.startswith("rm -f") and "creds.json" in cmd:
            raise RuntimeError("delete rejected")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(sandbox.commands, "run", _no_deletes)

    with pytest.raises(EnvironmentConnectionError):
        env.execute("echo hi", timeout=10)

    assert remote_cred in sandbox.files_written, (
        "test setup drifted: the revoked credential is not in the sandbox, "
        "so the assertion below would be vacuous"
    )
    assert not any("echo hi" in c.cmd for c in sandbox.commands_run), (
        "a command ran while a revoked credential was still live in the sandbox"
    )

    monkeypatch.setattr(sandbox.commands, "run", original_run)
    calls_before_retry = len(sandbox.commands_run)
    assert env.execute("echo deletion-propagated", timeout=10)["returncode"] == 0
    assert any(
        call.cmd == file_sync.quoted_rm_command([remote_cred])
        for call in sandbox.commands_run[calls_before_retry:]
    )
    assert env._dirty_state.reason() is None


def test_credential_changes_are_synced_without_waiting_or_reuploading_unchanged_files(
    fake, hermes_home, monkeypatch
):
    from tools import credential_files
    from tools.environments import file_sync

    monkeypatch.delenv("HERMES_FORCE_FILE_SYNC", raising=False)
    monkeypatch.setattr(file_sync, "_monotonic", lambda: 100.0)
    secret = hermes_home / "creds.json"
    secret.write_text("old-token", encoding="utf-8")
    monkeypatch.setattr(
        credential_files,
        "get_credential_file_mounts",
        lambda: [{"host_path": str(secret), "container_path": "/root/.hermes/creds.json"}],
    )
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    remote = f"{env.remote_hermes_home}/creds.json"

    secret.write_text("new-rotated-token", encoding="utf-8")
    assert env.execute("echo rotated", timeout=10)["returncode"] == 0
    assert sandbox.files_written[remote] == b"new-rotated-token"
    calls = len(sandbox.write_files_calls)
    assert env.execute("echo unchanged", timeout=10)["returncode"] == 0
    assert len(sandbox.write_files_calls) == calls


def test_transient_upload_open_failure_blocks_the_command_and_retries(
    fake, hermes_home, monkeypatch
):
    import builtins

    from hermes_plugin_e2b.errors import EnvironmentConnectionError
    from tools import credential_files

    # Isolate upload failure from the manager's rate limit.
    monkeypatch.setenv("HERMES_FORCE_FILE_SYNC", "1")
    secret = hermes_home / "creds.json"
    secret.write_text("old-token", encoding="utf-8")
    monkeypatch.setattr(
        credential_files,
        "get_credential_file_mounts",
        lambda: [{"host_path": str(secret), "container_path": "/root/.hermes/creds.json"}],
    )
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    remote = f"{env.remote_hermes_home}/creds.json"
    secret.write_text("new-rotated-token", encoding="utf-8")
    real_open = builtins.open
    failed = False

    def fail_once(path, mode="r", *args, **kwargs):
        nonlocal failed
        if str(path) == str(secret) and mode == "rb" and not failed:
            failed = True
            raise PermissionError("temporary credential read failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_once)
    with pytest.raises(EnvironmentConnectionError):
        env.execute("echo must-not-run", timeout=10)
    assert failed
    assert not any("must-not-run" in c.cmd for c in sandbox.commands_run)
    assert sandbox.files_written[remote] == b"old-token"
    assert env._dirty_state.reason() is not None

    assert env.execute("echo retry-upload", timeout=10)["returncode"] == 0
    assert sandbox.files_written[remote] == b"new-rotated-token"
    assert env._dirty_state.reason() is None


def test_a_failed_teardown_pull_is_reported_as_an_error(fake, hermes_home, caplog):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    def _unreadable(*args, **kwargs):
        raise RuntimeError("envd read failed")

    sandbox.files.read = _unreadable

    with caplog.at_level("ERROR"):
        env.cleanup()

    assert any("pulling state back" in r.message for r in caplog.records)


def test_another_environments_teardown_is_not_mistaken_for_our_failure(fake, hermes_home):
    """Attribution must be nest-aware, not just thread-scoped.

    Another environment's ``__del__`` runs its teardown — including its own
    state pull — synchronously on whatever thread the collector happens to be
    on. Counting those log records as ours would report a healthy upload as
    failed and make the backend unusable.
    """
    from hermes_plugin_e2b.environment import _watch_sync

    doomed = make_env(persistent=False)
    doomed._ensure_ready()

    def _unreadable(*args, **kwargs):
        raise RuntimeError("envd read failed")

    fake.sandboxes[doomed.sandbox_id].files.read = _unreadable

    with _watch_sync() as outcome:
        # Exactly what a late garbage collection does inside our frame.
        doomed.cleanup()

    assert outcome.push_failed is False, outcome.detail
    assert outcome.pull_failed is False, outcome.detail


def test_one_watchers_exit_does_not_blind_another_thread(fake, hermes_home, caplog):
    """A thread-local stack must not govern a process-global handler.

    Before the fix, the first thread to finish watching removed the shared
    log handler while another thread's watch was still relying on it, so that
    thread's logger-only failure (one the transport callbacks never see, such
    as a corrupt archive failing extraction inside the manager) was lost and
    its failed pull was reported as a success.
    """
    import logging
    import threading

    from hermes_plugin_e2b.environment import _watch_sync

    env = make_env(persistent=False)
    env._ensure_ready()

    b_inside_pull = threading.Event()
    a_watch_done = threading.Event()

    def _corrupt_download(dest):
        if not b_inside_pull.is_set():
            b_inside_pull.set()
            assert a_watch_done.wait(timeout=10)
        # The download "succeeds" — no transport error — but the archive is
        # garbage, so the manager's extraction fails on every retry. Only its
        # log line says so.
        Path(dest).write_bytes(b"this is not a tar archive")

    env._sync_manager._bulk_download_fn = _corrupt_download

    with caplog.at_level(logging.WARNING):
        worker = threading.Thread(target=env.cleanup)
        worker.start()
        assert b_inside_pull.wait(timeout=10)
        # Another environment on another thread starts and finishes a watch
        # while the pull is still in flight.
        with _watch_sync():
            pass
        a_watch_done.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert any("pulling state back" in r.message for r in caplog.records), (
        "the failed pull on the other thread went undetected"
    )


def test_a_pull_that_succeeds_after_a_retry_is_not_reported_as_failed(fake, hermes_home, caplog):
    """The manager retries sync-back internally; its per-attempt warning is
    part of a successful call. Reporting it as terminal turned every recovered
    transient into a false data-loss alarm."""
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(
        sandbox,
        f"{env.remote_hermes_home}/skills/demo/SKILL.md",
        "# edited in the sandbox\n",
    )

    original_read = sandbox.files.read
    attempts = {"n": 0}

    def _flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient envd hiccup")
        return original_read(*args, **kwargs)

    sandbox.files.read = _flaky

    with caplog.at_level("WARNING"):
        env.cleanup()

    assert attempts["n"] >= 2, "the retry never happened — the assertion is vacuous"
    assert (hermes_home / "skills" / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# edited in the sandbox\n", "the pull did not actually succeed"
    assert not any("pulling state back" in r.message for r in caplog.records), (
        "a successful pull was reported as failed"
    )


def test_a_silenced_file_sync_logger_cannot_hide_a_failed_pull(fake, hermes_home, caplog):
    """The manager's log records are the only signal for its post-download
    failures, and records are never *created* when the logger's effective
    level sits above WARNING — so a deployment that silences
    ``tools.environments.file_sync`` used to blind the failure detection
    entirely. The watch now floors that logger to WARNING for its duration
    and restores the previous level afterwards."""
    import logging

    env = make_env(persistent=False)
    env._ensure_ready()

    def _corrupt_download(dest):
        # No transport error — the failure is extraction, inside the manager,
        # visible only through its log records.
        Path(dest).write_bytes(b"this is not a tar archive")

    env._sync_manager._bulk_download_fn = _corrupt_download
    sync_logger = logging.getLogger("tools.environments.file_sync")

    with caplog.at_level(logging.ERROR, logger="tools.environments.file_sync"):
        assert not sync_logger.isEnabledFor(logging.WARNING), (
            "the premise is wrong — the logger is not silenced"
        )
        env.cleanup()
        assert sync_logger.level == logging.ERROR, "the level floor leaked past the watched call"

    assert any("pulling state back" in r.message for r in caplog.records), (
        "a silenced file_sync logger hid the failed pull"
    )


def test_an_ordinary_conflict_warning_is_not_a_sync_failure(fake, hermes_home, caplog):
    """The manager's last-write-wins conflict notice is a routine outcome of a
    successful pull, not a failure of it."""
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    # Host and sandbox both changed the same file after the push.
    (hermes_home / "skills" / "demo" / "SKILL.md").write_text("# host-edited\n", encoding="utf-8")
    edit_in_sandbox(
        sandbox,
        f"{env.remote_hermes_home}/skills/demo/SKILL.md",
        "# remote-edited\n",
    )

    with caplog.at_level("WARNING"):
        env.cleanup()

    assert any("conflict on" in r.message for r in caplog.records), (
        "no conflict occurred — the assertion below is vacuous"
    )
    assert (hermes_home / "skills" / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# remote-edited\n", "the manager did not apply its last-write-wins rule"
    assert not any("pulling state back" in r.message for r in caplog.records), (
        "an ordinary conflict was reported as a failed pull"
    )


# ---------------------------------------------------------------------------
# Recovery from a resumed sandbox
# ---------------------------------------------------------------------------


def test_state_authored_in_the_sandbox_survives_a_failed_teardown_pull(fake, hermes_home):
    """The scenario a failed pull creates, end to end.

    Session 1's pull fails, so the host never learns about a skill the agent
    wrote in the sandbox. Session 2 must not bury it under the host snapshot.
    """
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(
        sandbox,
        f"{first.remote_hermes_home}/skills/invented/SKILL.md",
        "# written by the agent\n",
    )
    first_id = first.sandbox_id
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()

    assert not (hermes_home / "skills" / "invented" / "SKILL.md").exists()

    # Session 2 resumes the same sandbox.
    del sandbox.files.read
    second = make_env()
    second._ensure_ready()

    assert second.sandbox_id == first_id
    assert (hermes_home / "skills" / "invented" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# written by the agent\n"


def test_a_divergent_file_is_preserved_rather_than_overwritten(fake, hermes_home, caplog):
    """Neither copy can be discarded when nothing says which is newer."""
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(
        sandbox,
        f"{first.remote_hermes_home}/skills/demo/SKILL.md",
        "# remote-v2\n",
    )
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read

    # The host still holds its own version.
    (hermes_home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    # The host copy is untouched...
    assert (hermes_home / "skills" / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# host-v1\n"
    # ...and the sandbox copy still exists somewhere inert.
    quarantined = list((hermes_home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined, "the sandbox version was discarded"
    assert quarantined[0].read_text(encoding="utf-8") == "# remote-v2\n"
    assert any("differ between this host" in r.message for r in caplog.records)


def test_repeated_recovery_preserves_distinct_conflicts_without_duplicate_copies(fake, hermes_home):
    import hashlib

    host_file = hermes_home / "skills/demo/SKILL.md"
    host_content = host_file.read_bytes()
    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    remote_file = f"{env.remote_hermes_home}/skills/demo/SKILL.md"
    quarantine = hermes_home / "cache/e2b-recovered"
    original_copy = quarantine / sandbox.sandbox_id / "skills/demo/SKILL.md"

    try:
        for content in ("remote-v2", "remote-v3", "remote-v3"):
            edit_in_sandbox(sandbox, remote_file, content)
            # A crash skips the pull; the next attachment must preserve its snapshot.
            env.cleanup(sync_state=False)
            env = make_env()
            env._ensure_ready()
            assert host_file.read_bytes() == host_content
            assert sandbox.files_written[remote_file] == host_content
            assert original_copy.read_text(encoding="utf-8") == "remote-v2"

        assert sorted(p.read_bytes() for p in quarantine.rglob("SKILL.md")) == [
            b"remote-v2",
            b"remote-v3",
        ]
        digest = hashlib.sha256(b"remote-v3").hexdigest()
        assert (
            quarantine / f"{sandbox.sandbox_id}-{digest}" / "skills/demo/SKILL.md"
        ).read_bytes() == b"remote-v3"
    finally:
        env.cleanup(sync_state=False)


@pytest.mark.parametrize("obstacle", ["symlink", "credential", "different_content"])
def test_quarantine_versions_fail_closed_on_unsafe_or_occupied_destinations(
    fake, hermes_home, monkeypatch, obstacle
):
    import hashlib

    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    remote = f"{env.remote_hermes_home}/skills/demo/SKILL.md"
    edit_in_sandbox(sandbox, remote, "remote-v2")
    env.cleanup(sync_state=False)
    env = make_env()
    env._ensure_ready()
    edit_in_sandbox(sandbox, remote, "remote-v3")
    env.cleanup(sync_state=False)

    quarantine = hermes_home / "cache/e2b-recovered"
    digest = hashlib.sha256(b"remote-v3").hexdigest()
    version_dir = quarantine / f"{sandbox.sandbox_id}-{digest}"
    destination = version_dir / "skills/demo/SKILL.md"
    if obstacle == "symlink":
        protected = hermes_home / "credentials"
        protected.mkdir()
        version_dir.symlink_to(protected, target_is_directory=True)
    elif obstacle == "credential":
        _register_credential(monkeypatch, destination)
    else:
        destination.parent.mkdir(parents=True)
        destination.write_text("previously-preserved", encoding="utf-8")

    env = make_env()
    try:
        with pytest.raises(EnvironmentConnectionError, match="state recovery"):
            env._ensure_ready()
        assert sandbox.files_written[remote] == b"remote-v3"
        assert (
            quarantine / sandbox.sandbox_id / "skills/demo/SKILL.md"
        ).read_bytes() == b"remote-v2"
        if obstacle == "symlink":
            assert not list(protected.iterdir())
        elif obstacle == "credential":
            assert destination.read_text(encoding="utf-8") == '{"token": "host-value"}'
        else:
            assert destination.read_bytes() == b"previously-preserved"
    finally:
        env.cleanup(sync_state=False)


def test_failed_quarantine_write_keeps_remote_state_and_can_be_retried(
    fake, hermes_home, monkeypatch
):
    import shutil
    from types import SimpleNamespace

    import hermes_plugin_e2b.environment as environment
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    env = make_env()
    env._ensure_ready()
    sandbox = fake.only()
    remote = f"{env.remote_hermes_home}/skills/demo/SKILL.md"
    edit_in_sandbox(sandbox, remote, "remote-v2")
    env.cleanup(sync_state=False)
    env = make_env()
    original_copy = shutil.copyfileobj

    def fail_copy(source, target, *args, **kwargs):
        target.write(b"partial")
        raise OSError("quarantine write failed")

    try:
        # Limit failure injection to the quarantine copy, not tar extraction.
        monkeypatch.setattr(
            environment,
            "shutil",
            SimpleNamespace(copyfileobj=fail_copy, copystat=shutil.copystat, copy2=shutil.copy2),
        )
        with pytest.raises(EnvironmentConnectionError, match="state recovery"):
            env._ensure_ready()
        assert sandbox.files_written[remote] == b"remote-v2"
        assert not list((hermes_home / "cache/e2b-recovered").rglob("SKILL.md"))

        environment.shutil.copyfileobj = original_copy
        env._ensure_ready()
        copies = list((hermes_home / "cache/e2b-recovered").rglob("SKILL.md"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == b"remote-v2"
    finally:
        env.cleanup(sync_state=False)


def test_recovery_never_creates_a_host_credential_file(fake, hermes_home):
    """Remote data must not be able to invent or revive a credential."""
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/creds.json", '{"token":"evil"}')
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/.env", "E2B_API_KEY=evil")
    first.cleanup()

    second = make_env()
    second._ensure_ready()

    assert not (hermes_home / "creds.json").exists()
    assert not (hermes_home / ".env").exists()


def test_a_fresh_sandbox_skips_recovery(fake, hermes_home):
    """Nothing to recover, and the probe would cost a round trip per session."""
    env = make_env(persistent=False)
    env._ensure_ready()

    assert not any("tar cf" in c.cmd for c in fake.only().commands_run)


def test_a_failed_recovery_blocks_the_push_that_would_bury_remote_state(fake, hermes_home):
    """Resume recovery must fail closed, not warn and proceed.

    The step after recovery is a force-push of the host snapshot. When the
    sandbox's state cannot be read, that push overwrites remote state that was
    never verified — potentially the only copy, since the reason there is
    anything to recover is that the previous pull already failed.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    remote_path = f"{first.remote_hermes_home}/skills/demo/SKILL.md"
    edit_in_sandbox(sandbox, remote_path, "# remote-v2\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("read failed"))
    first.cleanup()  # the pull fails; the sandbox holds the only copy

    (hermes_home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    uploads_before = len(sandbox.write_files_calls)

    second = make_env()
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        second._ensure_ready()

    assert "state recovery" in str(excinfo.value)
    assert sandbox.files_written[remote_path] == b"# remote-v2\n", (
        "the unverified remote copy was overwritten by the host snapshot"
    )
    assert len(sandbox.write_files_calls) == uploads_before, (
        "the host snapshot was pushed despite the failed recovery"
    )


def test_recovery_failure_is_retried_on_the_next_use(fake, hermes_home):
    """Failing closed must not brick the environment: once the sandbox is
    readable again, the same environment recovers and comes up."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(
        sandbox,
        f"{first.remote_hermes_home}/skills/invented/SKILL.md",
        "# written by the agent\n",
    )

    def _broken(*args, **kwargs):
        raise RuntimeError("read failed")

    working = sandbox.files.read
    sandbox.files.read = _broken
    first.cleanup()

    second = make_env()
    with pytest.raises(EnvironmentConnectionError):
        second._ensure_ready()

    sandbox.files.read = working
    second._ensure_ready()  # must not raise now

    assert (hermes_home / "skills" / "invented" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# written by the agent\n"


def test_the_per_command_sync_cannot_interleave_with_teardown(fake, hermes_home):
    """The incremental sync must run under the environment lock.

    Released early, a concurrently-arriving cleanup (the idle reaper calls it
    from a daemon thread while ``_inflight`` is still 0) completes the
    teardown pull first, and the sync then uploads into the sandbox AFTER the
    "final" pull — and can keep streaming past the lease release, interleaving
    with the next writer's recovery."""
    import threading

    env = make_env(persistent=True)
    env._ensure_ready()

    sync_started = threading.Event()
    release_sync = threading.Event()
    original_sync = env._sync_manager.sync

    def _slow_sync(*args, **kwargs):
        sync_started.set()
        assert release_sync.wait(timeout=10)
        return original_sync(*args, **kwargs)

    env._sync_manager.sync = _slow_sync

    worker = threading.Thread(target=env._before_execute)
    worker.start()
    assert sync_started.wait(timeout=10)

    cleaner = threading.Thread(target=env.cleanup)
    cleaner.start()
    cleaner.join(timeout=0.5)
    assert cleaner.is_alive(), (
        "cleanup completed the teardown while the pre-command sync was still "
        "in flight — the sync escaped the environment lock"
    )

    release_sync.set()
    worker.join(timeout=10)
    cleaner.join(timeout=10)
    assert not worker.is_alive() and not cleaner.is_alive()
    assert env._sandbox is None, "the deferred teardown never completed"


def test_recovery_survives_hostile_archive_members(fake, hermes_home):
    """An agent-created absolute symlink or fifo under ``~/.hermes`` must not
    brick every future resume of the scope.

    ``extractall(filter="data")`` RAISES on such members, and the member lives
    in the sandbox filesystem, so no retry ever converges — recovery now
    extracts only the regular files it would recover anyway."""
    import io
    import os
    import tarfile as tarfile_mod

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    first.cleanup()

    second = make_env()
    root = second.remote_hermes_home.lstrip("/")
    buffer = io.BytesIO()
    with tarfile_mod.open(fileobj=buffer, mode="w") as tar:
        link = tarfile_mod.TarInfo(name=f"{root}/skills/evil-link")
        link.type = tarfile_mod.SYMTYPE
        link.linkname = "/etc/hosts"
        tar.addfile(link)
        fifo = tarfile_mod.TarInfo(name=f"{root}/skills/evil-fifo")
        fifo.type = tarfile_mod.FIFOTYPE
        tar.addfile(fifo)
        payload = b"# authored in the sandbox\n"
        info = tarfile_mod.TarInfo(name=f"{root}/skills/invented/SKILL.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    sandbox.maybe_make_tar = lambda cmd: None
    sandbox.readable_files[f"/tmp/hermes-sync-back.{os.getpid()}.{second._session_id}.tar"] = (
        buffer.getvalue()
    )

    second._ensure_ready()  # must not raise: the hostile members are skipped

    assert (hermes_home / "skills" / "invented" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# authored in the sandbox\n", "the legitimate file was not recovered"
    assert not (hermes_home / "skills" / "evil-link").exists()
    assert not (hermes_home / "skills" / "evil-fifo").exists()


def test_a_resumed_sandbox_with_no_state_is_verified_clean_not_failed(fake, tmp_path, monkeypatch):
    """The fail-closed boundary: "verified: nothing to recover" must proceed.

    A sandbox that has no remote ``~/.hermes`` at all (nothing was ever pushed
    to it) has nothing a push could bury, so bring-up must not be blocked.
    """
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(empty_home))

    first = make_env()
    first._ensure_ready()  # empty host home: nothing is pushed
    first.cleanup()
    # Only the writer's readiness marker may exist — no synced state at all.
    synced = [p for p in fake.only().files_written if p.startswith(first.remote_hermes_home + "/")]
    assert not synced, "the premise is wrong — state was pushed"

    (empty_home / "skills").mkdir()
    (empty_home / "skills" / "new.md").write_text("# new\n", encoding="utf-8")

    second = make_env()
    second._ensure_ready()  # must not raise: verified clean, then push

    assert second._resumed_existing is True
    assert any(path.endswith("/skills/new.md") for path in fake.only().files_written)


# ---------------------------------------------------------------------------
# Recovery containment: sandbox-authored data never lands outside the home
# ---------------------------------------------------------------------------
#
# Everything recovery writes comes from inside the sandbox, so its
# destination is only as trustworthy as the boundary check in front of it.
# The boundary is the *physically resolved* Hermes home: a recoverable root
# (or a directory inside one) that is a symlink pointing out of the home is
# enough to put remote-authored bytes anywhere on the host filesystem, and
# checking a destination against its own possibly-symlinked parent does not
# notice.


def _home_with_symlinked_skills(tmp_path, monkeypatch):
    """A host whose ``~/.hermes/skills`` is a symlink out of the home."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-the-home"
    (outside / "demo").mkdir(parents=True)
    (outside / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    (home / "skills").symlink_to(outside)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home, outside


def _resume_with_remote_file(fake, env_factory, remote_relative, content):
    """Bring a sandbox up, author *content* inside it, and resume it.

    The pull is broken so the host never learns about the file, which is the
    situation resume recovery exists for: the sandbox holds the only copy.
    """
    first = env_factory()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/{remote_relative}", content)
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read
    return sandbox


def test_a_symlinked_recoverable_root_cannot_place_sandbox_data_outside_the_home(
    fake, tmp_path, monkeypatch, caplog
):
    """``~/.hermes/skills`` symlinked out of the home must not become a write
    channel out of the home. The file is remote-authored data that still must
    not be lost, so it is quarantined inside the home instead."""
    home, outside = _home_with_symlinked_skills(tmp_path, monkeypatch)

    _resume_with_remote_file(
        fake, make_env, "skills/invented/SKILL.md", "# authored in the sandbox\n"
    )

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    assert not (outside / "invented").exists(), (
        "recovery wrote sandbox-authored data outside the Hermes home"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined, "the sandbox copy was neither restored nor preserved"
    assert quarantined[0].read_text(encoding="utf-8") == "# authored in the sandbox\n"
    assert any("resolves outside" in r.message for r in caplog.records), (
        "the diverted restore was not reported"
    )


def test_a_symlinked_directory_inside_a_recoverable_root_cannot_escape_either(
    fake, tmp_path, monkeypatch
):
    """The escape does not need the root itself: any symlinked directory along
    the destination path is enough, and the agent controls the archive's member
    names."""
    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    outside = tmp_path / "outside-the-home"
    outside.mkdir()
    (home / "skills" / "linked").symlink_to(outside)
    monkeypatch.setenv("HERMES_HOME", str(home))

    _resume_with_remote_file(fake, make_env, "skills/linked/PLANTED.md", "# planted\n")

    second = make_env()
    second._ensure_ready()

    assert not (outside / "PLANTED.md").exists(), (
        "recovery wrote through a symlinked directory inside a recoverable root"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("PLANTED.md"))
    assert quarantined, "the sandbox copy was neither restored nor preserved"


def test_a_symlinked_cache_cannot_turn_quarantine_into_the_same_escape(fake, tmp_path, monkeypatch):
    """Quarantine hangs off ``cache/``, so it needs the identical check: a host
    that symlinks ``~/.hermes/cache`` elsewhere would otherwise have every
    quarantine write land outside the home. Recovery fails closed instead, which
    also blocks the force-push that would have buried the remote copy."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    outside = tmp_path / "outside-the-home"
    outside.mkdir()
    (home / "cache").symlink_to(outside)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # A divergence, so recovery has to quarantine something.
    sandbox = _resume_with_remote_file(fake, make_env, "skills/demo/SKILL.md", "# remote-v2\n")
    remote_path = f"{make_env().remote_hermes_home}/skills/demo/SKILL.md"

    second = make_env()
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        second._ensure_ready()

    assert "state recovery" in str(excinfo.value)
    assert not list(outside.rglob("SKILL.md")), (
        "quarantine wrote sandbox-authored data outside the Hermes home"
    )
    assert sandbox.files_written[remote_path] == b"# remote-v2\n", (
        "the push ran anyway and buried the remote copy recovery could not park"
    )


def test_a_redirected_recoverable_root_is_quarantined_not_written_through(
    fake, tmp_path, monkeypatch, caplog
):
    """Recovery restores in place only into a root's canonical location.

    A redirected root is ambiguous by construction: Hermes gives an arbitrary
    in-home directory no meaning, so ``skills -> ~/.hermes/my-skills`` (a
    deliberate layout) and ``skills -> ~/.hermes/credentials`` (a directory the
    host owns) are indistinguishable — and whether the second is *recognised*
    as host-owned would otherwise depend on whether a credential happened to be
    registered there yet, which is not a boundary. So the conservative reading
    wins for both: nothing is written through the redirection, the sandbox copy
    is preserved in quarantine, and the redirection is named in a warning.

    The layout keeps working — Hermes reads and syncs skills through the
    symlink as always. What this gives up is in-place *recovery* of
    sandbox-authored files for that layout.
    """
    home = tmp_path / "home"
    real = home / "real-skills" / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    (home / "skills").symlink_to(home / "real-skills")
    monkeypatch.setenv("HERMES_HOME", str(home))

    _resume_with_remote_file(
        fake, make_env, "skills/invented/SKILL.md", "# authored in the sandbox\n"
    )

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    assert not (home / "real-skills" / "invented" / "SKILL.md").exists(), (
        "recovery wrote through a redirected root"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined, "the sandbox copy was neither restored nor preserved"
    assert quarantined[0].read_text(encoding="utf-8") == "# authored in the sandbox\n"
    assert any("does not resolve to" in r.message for r in caplog.records)

    # The mirror case — an unredirected root under a symlinked ~/.hermes, which
    # must still restore in place — is pinned by
    # test_a_symlinked_hermes_home_is_resolved_before_the_boundary_is_applied.


def test_a_hostile_scope_cannot_place_coordination_files_outside_the_state_dir(
    tmp_path, monkeypatch
):
    """The scope is always a hex digest today, but it is interpolated into a
    filesystem path, so a value that is not one must not be able to steer that
    path."""
    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_dir = (tmp_path / "cache" / "e2b").resolve()

    for hostile in ("../../../../etc/passwd", "/etc/passwd", "a/b", "..", ""):
        for suffix in (".lock", ".writer.lock", ".state-dirty"):
            path = sandbox_api._scope_state_path(hostile, suffix)
            assert path.resolve().parent == state_dir, path
            assert path.name.endswith(suffix)


def test_the_state_marker_is_never_synced_into_the_sandbox(fake, hermes_home):
    """The marker has to be host-side truth about a possibly-broken transport,
    which only holds while Hermes does not mirror it into the sandbox. It lives
    under ``cache/e2b``, deliberately not one of the ``cache/<subdir>`` names
    ``tools/credential_files.py`` enumerates."""
    env = make_env()
    env._mark_state_dirty("pinning the synced set")
    assert env._dirty_state.path.exists(), "the premise is wrong — nothing was marked"

    env._ensure_ready()
    env.execute("true", timeout=10)

    written = list(fake.only().files_written)
    assert not any("state-dirty" in path for path in written), written
    assert not any("/cache/e2b/" in path for path in written), written


# ---------------------------------------------------------------------------
# The accepted concurrent-write window
# ---------------------------------------------------------------------------


def test_a_write_between_the_recovery_snapshot_and_the_push_is_overwritten(
    fake, hermes_home, caplog
):
    """Characterisation of the contract's one accepted state-loss window, in
    both directions.

    Recovery guarantees what its *snapshot* saw. A sandbox process — a daemon,
    or a background command an earlier session started, neither of which any
    protocol here can drain — can write after the archive is taken and before
    the force-push lands. For a path the host also has, the host copy wins and
    nothing is quarantined; the plugin must not pretend otherwise. For a path
    the host does not have, a force-push has no deletion baseline, so the file
    survives — and reaches the host either on the teardown pull or, when it is
    in a directory the pull cannot map back, on the next resume's recovery.
    """
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    remote_home = first.remote_hermes_home
    shared = f"{remote_home}/skills/demo/SKILL.md"
    # In a directory the host already syncs, so the pull can infer where it
    # goes (``FileSyncManager._infer_host_path`` matches known parents)...
    created_known_dir = f"{remote_home}/skills/demo/CREATED.md"
    # ...and in a directory that is new to the host, which the pull skips as
    # unmappable — recovery is what brings that one back.
    created_new_dir = f"{remote_home}/skills/window/CREATED.md"
    first.cleanup()

    original_make_tar = sandbox.maybe_make_tar
    snapshots: list[str] = []

    def snapshot_then_write(cmd: str) -> None:
        original_make_tar(cmd)
        if "tar cf" in cmd and not snapshots:
            # The snapshot is taken; now something inside the sandbox writes.
            snapshots.append(cmd)
            sandbox.files_written[shared] = b"# written inside the window\n"
            sandbox.files_written[created_known_dir] = b"# created inside the window\n"
            sandbox.files_written[created_new_dir] = b"# created inside the window\n"

    sandbox.maybe_make_tar = snapshot_then_write

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    assert snapshots, "the premise is wrong — no recovery snapshot was taken"

    # Direction 1: the shared path was overwritten by the host copy, and the
    # plugin does not claim to have preserved it anywhere.
    assert sandbox.files_written[shared] == b"# demo\n"
    assert not list((hermes_home / "cache" / "e2b-recovered").rglob("SKILL.md")), (
        "an in-window write was reported as quarantined when it was overwritten"
    )
    assert not any("differ between this host" in r.message for r in caplog.records)

    # Direction 2: both created paths survive the push — a force-push with no
    # baseline deletes nothing.
    assert sandbox.files_written[created_known_dir] == b"# created inside the window\n"
    assert sandbox.files_written[created_new_dir] == b"# created inside the window\n"

    second.cleanup()

    # The pull brings back the one it can map...
    assert (hermes_home / "skills" / "demo" / "CREATED.md").read_text(
        encoding="utf-8"
    ) == "# created inside the window\n"
    # ...and skips the one in a directory it has no mapping for, which is
    # core's behaviour, not this plugin's: a new remote *directory* is
    # unmappable, so it comes back through the next resume's recovery instead.
    assert not (hermes_home / "skills" / "window" / "CREATED.md").exists()

    third = make_env()
    third._ensure_ready()

    assert (hermes_home / "skills" / "window" / "CREATED.md").read_text(
        encoding="utf-8"
    ) == "# created inside the window\n", (
        "a file created in the window reached the host by neither route"
    )


def test_a_non_regular_host_path_is_never_read_or_written_through(fake, tmp_path, monkeypatch):
    """A fifo where a recoverable file belongs would hang bring-up forever if
    recovery compared contents by reading it, and be corrupted if recovery wrote
    over it. Neither happens: the sandbox copy is parked instead."""
    import os

    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    _resume_with_remote_file(fake, make_env, "skills/demo/PIPE.md", "# from the sandbox\n")
    os.mkfifo(home / "skills" / "demo" / "PIPE.md")

    second = make_env()
    second._ensure_ready()  # must return, not block

    quarantined = list((home / "cache" / "e2b-recovered").rglob("PIPE.md"))
    assert quarantined, "the sandbox copy was discarded"
    assert quarantined[0].read_text(encoding="utf-8") == "# from the sandbox\n"
    # The fifo is untouched and still a fifo.
    import stat

    assert stat.S_ISFIFO((home / "skills" / "demo" / "PIPE.md").stat().st_mode)


def test_a_symlink_out_of_a_recoverable_root_cannot_reach_a_host_only_tree(
    fake, tmp_path, monkeypatch, caplog
):
    """The Hermes home is not a fine-grained enough boundary on its own.

    ``~/.hermes`` also holds trees remote data must never write — credentials
    above all — so a symlink under ``skills/`` pointing at
    ``~/.hermes/credentials/`` never leaves the home while leaving the only
    tree recovery is allowed to restore into. Sandbox bytes must not land
    there just because the destination is technically inside the home.
    """
    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    (home / "credentials").mkdir()
    (home / "skills" / "planted").symlink_to(home / "credentials")
    monkeypatch.setenv("HERMES_HOME", str(home))

    _resume_with_remote_file(
        fake, make_env, "skills/planted/openai.json", '{"key": "from the sandbox"}'
    )

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    assert not (home / "credentials" / "openai.json").exists(), (
        "sandbox bytes were written into a host-only tree inside the Hermes home"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("openai.json"))
    assert quarantined, "the sandbox copy was neither restored nor preserved"
    assert any("resolves outside" in r.message for r in caplog.records)


def test_a_symlinked_hermes_home_is_resolved_before_the_boundary_is_applied(
    fake, tmp_path, monkeypatch
):
    """The boundary is the *physically resolved* home. If it were the
    unresolved path, a user whose ``~/.hermes`` is itself a symlink — a
    perfectly ordinary dotfiles layout — would have every restore read as an
    escape, and every resume of a persistent scope would quarantine instead of
    recovering."""
    physical = tmp_path / "elsewhere" / "hermes-data"
    (physical / "skills" / "demo").mkdir(parents=True)
    (physical / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    linked_home = tmp_path / "home"
    linked_home.symlink_to(physical)
    monkeypatch.setenv("HERMES_HOME", str(linked_home))

    _resume_with_remote_file(
        fake, make_env, "skills/invented/SKILL.md", "# authored in the sandbox\n"
    )

    second = make_env()
    second._ensure_ready()

    assert (physical / "skills" / "invented" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# authored in the sandbox\n", "a symlinked Hermes home was treated as an escape"
    assert not list((physical / "cache" / "e2b-recovered").rglob("SKILL.md"))


def test_an_archive_member_this_host_cannot_write_does_not_brick_the_scope(
    fake, hermes_home, caplog
):
    """Every failure mode of extraction originates in the *sandbox's*
    filesystem, where the user cannot reach it — bring-up is what is failing.
    So one unwritable member must never be fatal: a name longer than the host
    allows (or one that is not valid UTF-8 for the host filesystem) is legal
    inside a Linux sandbox, and a fatal error here would make every future
    resume of the scope fail with nothing the user could do about it."""
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    remote_home = first.remote_hermes_home
    # 400 bytes in one component: ENAMETOOLONG on both macOS and Linux.
    edit_in_sandbox(sandbox, f"{remote_home}/skills/{'x' * 400}.md", "# unwritable\n")
    edit_in_sandbox(sandbox, f"{remote_home}/skills/fine/GOOD.md", "# writable\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()  # must not raise

    assert (hermes_home / "skills" / "fine" / "GOOD.md").read_text(encoding="utf-8") == (
        "# writable\n"
    ), "one unwritable member stopped the rest of the recovery"
    assert any("could not be recovered and were skipped" in r.message for r in caplog.records)


def test_a_host_file_where_the_sandbox_has_a_directory_does_not_brick_the_scope(
    fake, hermes_home, caplog
):
    """The mirror of the fifo case, and the same requirement: recovery has to
    converge. Creating the parent directory for the sandbox's file raises
    because the host holds a regular file at that path; that must cost one
    file, not the whole bring-up (which would also leave the scope marked
    dirty, blocking every reader as well)."""
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    remote_home = first.remote_hermes_home
    edit_in_sandbox(sandbox, f"{remote_home}/skills/notes/INNER.md", "# inside\n")
    edit_in_sandbox(sandbox, f"{remote_home}/skills/fine/GOOD.md", "# writable\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read
    # The host holds a regular file exactly where the sandbox holds a directory.
    (hermes_home / "skills" / "notes").write_text("not a directory\n", encoding="utf-8")

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()  # must not raise

    assert (hermes_home / "skills" / "notes").read_text(encoding="utf-8") == (
        "not a directory\n"
    ), "the host file was replaced"
    assert (hermes_home / "skills" / "fine" / "GOOD.md").exists(), (
        "one unplaceable file stopped the rest of the recovery"
    )
    assert any("could not be written to this host" in r.message for r in caplog.records)


def test_an_unreadable_host_copy_is_treated_as_divergence_not_as_redundant(fake, hermes_home):
    """Discarding remote data on the strength of a host file we could not read
    is the one unrecoverable outcome, so an unreadable host copy counts as a
    conflict."""
    import os
    import stat

    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/skills/demo/SKILL.md", "# remote-v2\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read

    host_copy = hermes_home / "skills" / "demo" / "SKILL.md"
    os.chmod(host_copy, 0)
    try:
        assert not os.access(host_copy, os.R_OK), "the premise is wrong — still readable"
        second = make_env()
        # Bring-up does not survive this: the file recovery could not read is
        # in the sync set, so the force-push cannot read it either and fails
        # closed. What matters here is what recovery decided *before* that —
        # it runs first, precisely so nothing is buried by the push.
        with pytest.raises(EnvironmentConnectionError):
            second._ensure_ready()
    finally:
        os.chmod(host_copy, stat.S_IRUSR | stat.S_IWUSR)

    quarantined = list((hermes_home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined, "the sandbox copy was discarded because the host copy was unreadable"
    assert quarantined[0].read_text(encoding="utf-8") == "# remote-v2\n"


def _register_credential(monkeypatch, host_path: Path) -> None:
    """Make Hermes report *host_path* as a credential file, as it really would.

    ``get_credential_file_mounts`` is the definitive mapping (skill-registered
    files plus ``terminal.credential_files``); patching it is how a test says
    "the profile has a credential here".
    """
    from tools import credential_files

    host_path.parent.mkdir(parents=True, exist_ok=True)
    host_path.write_text('{"token": "host-value"}', encoding="utf-8")
    monkeypatch.setattr(
        credential_files,
        "get_credential_file_mounts",
        lambda: [{"host_path": str(host_path), "container_path": "/root/.hermes/creds.json"}],
    )


def test_a_whole_root_pointed_at_a_credential_tree_receives_no_sandbox_bytes(
    fake, tmp_path, monkeypatch, caplog
):
    """The escape does not need an inner symlink: redirecting the *entire*
    recoverable root at a tree the host owns would redefine that tree as
    recoverable. Both the "Hermes knows this is a credentials tree" case and
    the "Hermes has no idea what this directory is" case must be refused —
    a rule that only fires once a credential happens to be registered would
    depend on timing, not on the layout."""
    home = tmp_path / "home"
    (home / "credentials").mkdir(parents=True)
    (home / "skills").symlink_to(home / "credentials")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _register_credential(monkeypatch, home / "credentials" / "openai.json")

    _resume_with_remote_file(fake, make_env, "skills/invented/SKILL.md", "# sandbox-authored\n")

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    assert not (home / "credentials" / "invented").exists(), (
        "sandbox bytes were written into the credentials tree"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined, "the sandbox copy was neither restored nor preserved"
    assert quarantined[0].read_text(encoding="utf-8") == "# sandbox-authored\n"
    assert any("does not resolve to" in r.message for r in caplog.records)


def test_a_credential_registered_inside_skills_is_never_created_by_recovery(
    fake, tmp_path, monkeypatch
):
    """The tree check is deliberately one-directional: a credential registered
    *inside* skills/ must not make the whole skills tree unrecoverable. The
    credential itself still has to be untouchable, so the per-file check is what
    covers it."""
    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _register_credential(monkeypatch, home / "skills" / "secret" / "token.json")
    (home / "skills" / "secret" / "token.json").unlink()  # the host does not have it yet

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    remote_home = first.remote_hermes_home
    edit_in_sandbox(sandbox, f"{remote_home}/skills/secret/token.json", '{"token": "evil"}')
    edit_in_sandbox(sandbox, f"{remote_home}/skills/fine/GOOD.md", "# ordinary\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read

    second = make_env()
    second._ensure_ready()

    assert not (home / "skills" / "secret" / "token.json").exists(), (
        "recovery created a host credential file"
    )
    assert (home / "skills" / "fine" / "GOOD.md").exists(), (
        "one protected path made the whole tree unrecoverable"
    )
    quarantined = list((home / "cache" / "e2b-recovered").rglob("token.json"))
    assert quarantined, "the sandbox copy was discarded instead of quarantined"


def test_a_symlink_inside_the_quarantine_tree_cannot_redirect_a_park(fake, tmp_path, monkeypatch):
    """Quarantine's boundary is the quarantine tree, not the Hermes home. A
    symlink *inside* it — planted at ``<id>/skills`` — stays inside the home
    while putting sandbox bytes exactly where they must never go, so the home
    is far too wide a boundary here. Recovery fails closed rather than write
    through it, which also blocks the push that would have buried the copy it
    could not park."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    (home / "credentials").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # A divergence, so a park is required.
    sandbox = _resume_with_remote_file(fake, make_env, "skills/demo/SKILL.md", "# remote-v2\n")
    remote_path = f"{make_env().remote_hermes_home}/skills/demo/SKILL.md"

    planted = home / "cache" / "e2b-recovered" / sandbox.sandbox_id / "skills"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(home / "credentials")

    second = make_env()
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        second._ensure_ready()

    assert "state recovery" in str(excinfo.value)
    assert not list((home / "credentials").rglob("SKILL.md")), (
        "a park was redirected into the credentials tree"
    )
    assert sandbox.files_written[remote_path] == b"# remote-v2\n", (
        "the push ran anyway and buried the copy quarantine could not park"
    )


def test_an_unsafe_quarantine_ancestor_fails_closed_without_burying_anything(
    fake, tmp_path, monkeypatch
):
    """The same requirement one level up: the quarantine *root* itself must be
    a real, dedicated directory inside the home."""
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    sandbox = _resume_with_remote_file(fake, make_env, "skills/demo/SKILL.md", "# remote-v2\n")
    remote_path = f"{make_env().remote_hermes_home}/skills/demo/SKILL.md"

    # The quarantine root resolves to the Hermes home itself.
    quarantine_root = home / "cache" / "e2b-recovered"
    quarantine_root.parent.mkdir(parents=True, exist_ok=True)
    quarantine_root.symlink_to(home)

    second = make_env()
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        second._ensure_ready()

    assert "state recovery" in str(excinfo.value)
    assert sandbox.files_written[remote_path] == b"# remote-v2\n", (
        "the push ran anyway and buried the copy quarantine could not park"
    )


def test_ordinary_quarantine_still_works_after_the_boundary_tightening(fake, hermes_home, caplog):
    """The tightening must not have broken the path it protects."""
    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/skills/demo/SKILL.md", "# remote-v2\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read
    (hermes_home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")

    second = make_env()
    with caplog.at_level("WARNING"):
        second._ensure_ready()

    assert (hermes_home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == (
        "# host-v1\n"
    )
    quarantined = list((hermes_home / "cache" / "e2b-recovered").rglob("SKILL.md"))
    assert quarantined and quarantined[0].read_text(encoding="utf-8") == "# remote-v2\n"
    assert any("differ between this host" in r.message for r in caplog.records)


def test_a_sandbox_reported_home_containing_traversal_is_refused(fake, hermes_home, caplog):
    """The probed ``$HOME`` is a string the *sandbox* supplies that then becomes
    a host path: the recovery archive is unpacked under it before its contents
    are copied into ``~/.hermes``. A ``..`` in it would point the recovery source
    outside the staging tree, so recovery could be aimed at arbitrary host
    directories — and the force-push that follows would upload them into the
    sandbox. The value is refused, not normalised."""
    original_create = fake.create

    def _create_with_tampered_home(*args, **kwargs):
        sandbox = original_create(*args, **kwargs)
        sandbox.home = "/home/user/../../../etc"
        return sandbox

    fake.create = _create_with_tampered_home

    env = make_env()
    with caplog.at_level("WARNING"):
        env._ensure_ready()

    assert fake.only().home == "/home/user/../../../etc", (
        "the premise is wrong — the sandbox under test does not report a traversal $HOME"
    )
    assert env.remote_hermes_home == "/home/user/.hermes", (
        f"a traversal $HOME was accepted: {env.remote_hermes_home}"
    )
    assert any("not a plain absolute path" in r.message for r in caplog.records)


def test_recovery_never_reads_outside_its_staging_directory(fake, hermes_home, tmp_path, caplog):
    """The structural backstop behind the ``$HOME`` refusal above.

    ``_recover_remote_tree`` builds its source directory from the remote home,
    which is sandbox-derived, so it verifies that directory is inside its own
    staging tree instead of assuming it. With the probe refusing a traversal
    ``$HOME`` there is no route from a real sandbox to this state, so it is
    exercised where the value actually enters — the function's own argument —
    rather than by pretending the probe could produce it. Without the check, a
    recovery pass aimed outside the staging tree would copy arbitrary host
    files into ``~/.hermes``, from where the force-push uploads them into the
    sandbox.
    """
    outside = tmp_path / "host-secrets"
    (outside / ".hermes" / "skills").mkdir(parents=True)
    (outside / ".hermes" / "skills" / "STOLEN.md").write_text("# host-only\n", encoding="utf-8")

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/skills/demo/SKILL.md", "# remote-v2\n")
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read

    second = make_env()
    second._ensure_ready()

    # The archive is unpacked into a staging directory and the remote home is
    # joined *under* it, so escaping needs leading "..": enough of them to
    # climb out of the staging tree and back down to `outside`. ".." at the
    # filesystem root is the root, so over-climbing is safe and makes this
    # independent of where the platform puts its temp directory.
    hostile_root = "/" + "../" * 40 + str(outside / ".hermes").lstrip("/")
    with caplog.at_level("WARNING"):
        second._recover_remote_tree(hostile_root)

    assert not (hermes_home / "skills" / "STOLEN.md").exists(), (
        "recovery read host files from outside its staging directory"
    )
    assert not any(path.endswith("STOLEN.md") for path in sandbox.files_written), (
        "host files outside the staging directory were pushed into the sandbox"
    )


def test_a_redirected_quarantine_root_fails_closed_without_burying_anything(
    fake, tmp_path, monkeypatch
):
    """The whole-root counterpart of the inner-symlink case.

    ``~/.hermes/cache/e2b-recovered`` pointed at an *unregistered*
    ``~/.hermes/credentials`` used to be accepted, because the root was checked
    for whether it looked host-owned rather than for being where it belongs —
    and Hermes recognises nothing about a bare in-home directory. The rule is
    canonical position, exactly as for the recoverable roots, so recovery fails
    closed before the push that would bury the copy it could not park.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    (home / "credentials").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # A divergence, so a park is required.
    sandbox = _resume_with_remote_file(fake, make_env, "skills/demo/SKILL.md", "# remote-v2\n")
    remote_path = f"{make_env().remote_hermes_home}/skills/demo/SKILL.md"

    # Nothing in Hermes maps this directory: it is host-owned only in the sense
    # that recovery has no business writing there.
    from tools import credential_files

    assert not credential_files.get_credential_file_mounts(), (
        "the premise is wrong — a credential is registered, so recognition would catch this"
    )

    quarantine_root = home / "cache" / "e2b-recovered"
    quarantine_root.parent.mkdir(parents=True, exist_ok=True)
    quarantine_root.symlink_to(home / "credentials")

    second = make_env()
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        second._ensure_ready()

    assert "state recovery" in str(excinfo.value)
    assert not list((home / "credentials").rglob("SKILL.md")), (
        "sandbox-authored bytes were written through the redirected quarantine root"
    )
    assert sandbox.files_written[remote_path] == b"# remote-v2\n", (
        "the push ran anyway and buried the copy quarantine could not park"
    )


def test_a_credential_registered_inside_the_quarantine_tree_is_never_overwritten(
    fake, tmp_path, monkeypatch
):
    """Hermes accepts arbitrary registered credential paths, so one can be
    registered at a path that happens to sit inside the canonical quarantine
    tree — including at the exact destination a park is about to use.

    Staying inside the quarantine tree is not enough of a check for that: the
    park would replace a host credential with sandbox-authored bytes, which is
    precisely what "credential paths are never restored or created from sandbox
    data" rules out. Recovery fails closed before the push, so the credential
    and the remote copy both survive.
    """
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    home = tmp_path / "home"
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# host-v1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    # A credential registered at the exact canonical quarantine destination for
    # the file that is about to diverge.
    secret = home / "cache" / "e2b-recovered" / "sbx-001" / "skills" / "demo" / "SKILL.md"
    _register_credential(monkeypatch, secret)

    first = make_env()
    first._ensure_ready()
    sandbox = fake.only()
    assert sandbox.sandbox_id == "sbx-001", (
        "the premise is wrong — the credential is not at this sandbox's quarantine path"
    )
    edit_in_sandbox(sandbox, f"{first.remote_hermes_home}/skills/demo/SKILL.md", "# remote-v2\n")
    remote_path = f"{first.remote_hermes_home}/skills/demo/SKILL.md"
    sandbox.files.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pull failed"))
    first.cleanup()
    del sandbox.files.read

    assert secret.read_text(encoding="utf-8") == '{"token": "host-value"}'

    second = make_env()
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        second._ensure_ready()

    assert "state recovery" in str(excinfo.value)
    assert secret.read_text(encoding="utf-8") == '{"token": "host-value"}', (
        "sandbox-authored bytes replaced a registered host credential"
    )
    assert sandbox.files_written[remote_path] == b"# remote-v2\n", (
        "the push ran anyway and buried the copy quarantine could not park"
    )
