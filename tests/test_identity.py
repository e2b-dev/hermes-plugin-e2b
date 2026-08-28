"""Sandbox identity, discovery, and the cross-process creation lock.

These exercise ``sandbox.py`` against a fake ``e2b`` module injected into
``sys.modules``, so the real ``from e2b import Sandbox, SandboxQuery`` calls
and the real ``SandboxQuery`` dataclass are on the path.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest
from fake_e2b import FakeE2B


@pytest.fixture
def fake_sdk(monkeypatch):
    """Replace only ``e2b.Sandbox``; everything else stays the real module."""
    import e2b

    registry = FakeE2B()
    module = types.ModuleType("e2b")
    for attr in dir(e2b):
        if not attr.startswith("__"):
            setattr(module, attr, getattr(e2b, attr))
    module.Sandbox = registry
    monkeypatch.setitem(sys.modules, "e2b", module)
    return registry


def settings(**kwargs):
    from hermes_plugin_e2b.config import E2BSettings

    return E2BSettings(**kwargs)


# ---------------------------------------------------------------------------
# scope_id
# ---------------------------------------------------------------------------


def test_scope_is_stable_for_the_same_inputs():
    from hermes_plugin_e2b.sandbox import scope_id

    assert scope_id("default", settings(), persistent=True) == scope_id(
        "default", settings(), persistent=True
    )


def test_scope_separates_tasks_templates_and_profiles(monkeypatch, tmp_path):
    from hermes_plugin_e2b import sandbox as sandbox_api

    base = sandbox_api.scope_id("default", settings(), persistent=True)
    assert base != sandbox_api.scope_id("session:abc", settings(), persistent=True)
    assert base != sandbox_api.scope_id(
        "default", settings(template="other-template"), persistent=True
    )

    monkeypatch.setattr(sandbox_api, "_profile_key", lambda: str(tmp_path / "other"))
    assert base != sandbox_api.scope_id("default", settings(), persistent=True)


def test_tightening_the_sandbox_policy_forces_a_new_sandbox():
    """An adopted sandbox keeps the policy it was created with, for life."""
    from hermes_plugin_e2b.sandbox import scope_id

    open_net = scope_id("default", settings(allow_internet_access=True), persistent=True)
    closed_net = scope_id("default", settings(allow_internet_access=False), persistent=True)
    assert open_net != closed_net

    secured = scope_id("default", settings(secure=True), persistent=True)
    unsecured = scope_id("default", settings(secure=False), persistent=True)
    assert secured != unsecured


def test_a_persistent_run_never_adopts_an_ephemeral_sandbox():
    """An ephemeral sandbox's lease expiry kills it; adopting one as
    "persistent" would silently lose the filesystem it promised to keep."""
    from hermes_plugin_e2b.sandbox import scope_id

    assert scope_id("default", settings(), persistent=True) != scope_id(
        "default", settings(), persistent=False
    )


def test_cosmetic_settings_do_not_churn_the_sandbox():
    """Changing a starting directory or a lease must not orphan a sandbox."""
    from hermes_plugin_e2b.sandbox import scope_id

    base = scope_id("default", settings(), persistent=True)
    assert base == scope_id("default", settings(cwd="/srv/work"), persistent=True)
    assert base == scope_id("default", settings(lease_seconds=1800), persistent=True)
    assert base == scope_id("default", settings(metadata={"team": "infra"}), persistent=True)


def test_a_subagent_sharing_the_parent_container_key_shares_the_sandbox(monkeypatch):
    """Hermes collapses a delegated child onto the parent's container key.

    The plugin is handed the *resolved* key, so this asserts the real
    mechanism: a child resolving to the parent's key lands in the parent's
    sandbox, and one that does not resolve there gets its own.
    """
    from hermes_plugin_e2b.sandbox import scope_id
    from tools import terminal_tool

    monkeypatch.setenv("TERMINAL_ENV", "e2b")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")

    parent_key = terminal_tool._resolve_container_task_id(None)
    terminal_tool.register_container_alias("subagent-1", None)
    child_key = terminal_tool._resolve_container_task_id("subagent-1")

    assert child_key == parent_key, "core no longer collapses subagents onto the parent"
    assert scope_id(child_key, settings(), persistent=True) == scope_id(
        parent_key, settings(), persistent=True
    )
    assert scope_id("unrelated-session", settings(), persistent=True) != scope_id(
        parent_key, settings(), persistent=True
    )


def test_scope_does_not_leak_the_profile_path_or_session_id():
    from hermes_plugin_e2b.sandbox import identity_metadata, scope_id

    metadata = identity_metadata(
        scope_id("session:deadbeef", settings(), persistent=True), settings()
    )
    blob = repr(metadata)
    assert "session:deadbeef" not in blob
    assert "/.hermes" not in blob


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_user_metadata_cannot_override_the_identity_keys():
    from hermes_plugin_e2b.sandbox import (
        METADATA_PLUGIN_KEY,
        METADATA_PLUGIN_VALUE,
        METADATA_SCOPE_KEY,
        identity_metadata,
    )

    hostile = settings(metadata={METADATA_SCOPE_KEY: "someone-elses", METADATA_PLUGIN_KEY: "nope"})
    metadata = identity_metadata("my-scope", hostile)

    assert metadata[METADATA_SCOPE_KEY] == "my-scope"
    assert metadata[METADATA_PLUGIN_KEY] == METADATA_PLUGIN_VALUE


def test_user_metadata_is_still_carried_through():
    from hermes_plugin_e2b.sandbox import identity_metadata

    metadata = identity_metadata("scope", settings(metadata={"team": "infra"}))
    assert metadata["team"] == "infra"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_find_existing_returns_none_when_nothing_matches(fake_sdk):
    from hermes_plugin_e2b.sandbox import find_existing

    assert find_existing("scope-a", "key", "base") is None


def test_find_existing_ignores_sandboxes_that_are_not_ours(fake_sdk):
    from hermes_plugin_e2b.sandbox import find_existing

    fake_sdk.create(template="base", timeout=300, metadata={"someone": "else"})
    assert find_existing("scope-a", "key", "base") is None


def test_find_existing_finds_a_paused_sandbox(fake_sdk):
    """This is what removes the need for a local pointer store."""
    from hermes_plugin_e2b.sandbox import find_existing, identity_metadata

    scope = "scope-a"
    sandbox = fake_sdk.create(
        template="base", timeout=300, metadata=identity_metadata(scope, settings())
    )
    sandbox.pause()
    assert sandbox.state == "paused"

    assert find_existing(scope, "key", "base") == sandbox.sandbox_id


def test_find_existing_prefers_the_newest_of_several(fake_sdk, caplog):
    from hermes_plugin_e2b.sandbox import find_existing, identity_metadata

    scope = "scope-a"
    metadata = identity_metadata(scope, settings())
    fake_sdk.create(template="base", timeout=300, metadata=metadata)
    newest = fake_sdk.create(template="base", timeout=300, metadata=metadata)

    with caplog.at_level("WARNING"):
        found = find_existing(scope, "key", "base")

    assert found == newest.sandbox_id
    assert any("share scope" in record.message for record in caplog.records)


def test_find_existing_skips_a_killed_sandbox(fake_sdk):
    from hermes_plugin_e2b.sandbox import find_existing, identity_metadata

    scope = "scope-a"
    sandbox = fake_sdk.create(
        template="base", timeout=300, metadata=identity_metadata(scope, settings())
    )
    sandbox.kill()

    assert find_existing(scope, "key", "base") is None


def test_find_existing_wraps_api_failures_as_backend_failures(fake_sdk):
    from hermes_plugin_e2b.errors import EnvironmentConnectionError
    from hermes_plugin_e2b.sandbox import find_existing

    fake_sdk.list_error = RuntimeError("E2B API 503")
    with pytest.raises(EnvironmentConnectionError):
        find_existing("scope-a", "key", "base")


# ---------------------------------------------------------------------------
# lifecycle config
# ---------------------------------------------------------------------------


def test_lifecycle_pauses_persistent_and_kills_ephemeral():
    from hermes_plugin_e2b.sandbox import lifecycle_for

    persistent = lifecycle_for(True)
    assert persistent["on_timeout"] == {"action": "pause", "keep_memory": False}
    # keep_memory=False cannot be combined with auto_resume, and an implicit
    # resume would let any caller wake a sandbox it does not own.
    assert persistent["auto_resume"] is False

    assert lifecycle_for(False)["on_timeout"] == "kill"


def test_command_renewal_uses_the_current_key_and_returns_a_fresh_object(monkeypatch):
    import e2b
    from hermes_plugin_e2b.sandbox import renew_lease

    stale = SimpleNamespace(
        sandbox_id="sbx-old",
        connection_config=SimpleNamespace(
            get_api_params=lambda: {"api_key": "old-key", "domain": "example.test"}
        ),
    )
    fresh = SimpleNamespace(sandbox_id="sbx-old", generation=2)
    calls = []

    def _class_connect(cls, sandbox_id, timeout=None, **kwargs):
        calls.append((cls, sandbox_id, timeout, kwargs))
        return fresh

    monkeypatch.setattr(e2b.Sandbox, "connect", classmethod(_class_connect))

    result = renew_lease(stale, 420, "rotated-key")

    assert result is fresh
    assert calls == [(e2b.Sandbox, "sbx-old", 420, {"api_key": "rotated-key"})]


def test_cleanup_reconnect_inherits_the_attached_connection(monkeypatch):
    import e2b
    from hermes_plugin_e2b.sandbox import reconnect_for_cleanup

    attached_params = {
        "api_key": "attached-key",
        "domain": "attached.example.test",
        "request_timeout": 17,
    }
    stale = SimpleNamespace(
        sandbox_id="sbx-old",
        connection_config=SimpleNamespace(get_api_params=lambda: dict(attached_params)),
    )
    fresh = SimpleNamespace(sandbox_id="sbx-old", generation=2)
    calls = []

    def _class_connect(cls, sandbox_id, timeout=None, **kwargs):
        calls.append((cls, sandbox_id, timeout, kwargs))
        return fresh

    monkeypatch.setattr(e2b.Sandbox, "connect", classmethod(_class_connect))

    result = reconnect_for_cleanup(stale, 420)

    assert result is fresh
    assert calls == [(e2b.Sandbox, "sbx-old", 420, attached_params)]


# ---------------------------------------------------------------------------
# cross-process creation lock
# ---------------------------------------------------------------------------


def test_scope_lock_serialises_concurrent_holders(monkeypatch, tmp_path):
    """Two cold-starting processes must not both create a sandbox for one scope."""
    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setattr(sandbox_api, "_hermes_home", lambda: tmp_path)
    overlaps = []
    active = []
    guard = threading.Lock()
    start = threading.Barrier(4)

    def _worker():
        start.wait(timeout=10)
        with sandbox_api.scope_lock("shared-scope"):
            with guard:
                active.append(1)
                overlaps.append(len(active))
            time.sleep(0.05)
            with guard:
                active.pop()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert overlaps, "the lock body never ran"
    assert max(overlaps) == 1, "scope_lock allowed concurrent holders"


def test_scope_lock_degrades_when_the_lock_file_is_unusable(monkeypatch, tmp_path):
    """A missing lock costs duplicate prevention, never correctness."""
    from hermes_plugin_e2b import sandbox as sandbox_api

    def _unwritable():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(sandbox_api, "_hermes_home", _unwritable)
    ran = []
    with sandbox_api.scope_lock("scope"):
        ran.append(True)
    assert ran == [True]


def test_probe_scope_is_recognised():
    from hermes_plugin_e2b.sandbox import is_probe_scope

    assert is_probe_scope("prompt-backend-probe") is True
    assert is_probe_scope("default") is False


def test_scope_lock_gives_up_rather_than_blocking_on_a_wedged_holder(monkeypatch, tmp_path, caplog):
    """A stuck peer must not block every other session's first command."""
    import time as time_mod

    from hermes_plugin_e2b import sandbox as sandbox_api

    monkeypatch.setattr(sandbox_api, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sandbox_api, "_LOCK_TIMEOUT_SECONDS", 0.3)

    holder_has_lock = threading.Event()
    release_holder = threading.Event()

    def _hold():
        with sandbox_api.scope_lock("wedged"):
            holder_has_lock.set()
            release_holder.wait(timeout=30)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_has_lock.wait(timeout=10)

    started = time_mod.monotonic()
    with caplog.at_level("WARNING"), sandbox_api.scope_lock("wedged"):
        elapsed = time_mod.monotonic() - started

    release_holder.set()
    holder.join(timeout=10)

    assert elapsed < 10, f"the lock blocked for {elapsed:.1f}s"
    assert any("creation lock" in record.message for record in caplog.records)
