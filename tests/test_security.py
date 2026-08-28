"""Credential boundary.

``E2B_API_KEY`` authenticates the SDK on the host. It must not reach the
sandbox (env, files, command lines), must not reach disk, and must not reach
anything a user or a model reads: logs, errors, doctor/status rows.
"""

from __future__ import annotations

import logging

import pytest
from fake_e2b import FakeE2B, install

SECRET = "e2b_" + "9f3c" * 10


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", SECRET)
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
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"401 for {SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"https://api.e2b.dev/sandboxes?api_key={SECRET}&x=1",
        f'{{"api_key": "{SECRET}"}}',
        f"api-key={SECRET}",
    ],
)
def test_redact_removes_credentials_from_any_message(text):
    from hermes_plugin_e2b.errors import redact

    cleaned = redact(text)
    assert SECRET not in cleaned
    assert "<redacted>" in cleaned


def test_redact_leaves_ordinary_text_alone():
    from hermes_plugin_e2b.errors import redact

    assert redact("sandbox sbx-001 not found") == "sandbox sbx-001 not found"


def test_a_backend_error_carrying_the_key_is_redacted(fake):
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    fake.create_error = RuntimeError(f"connect failed (key {SECRET})")
    env = make_env()

    with pytest.raises(EnvironmentConnectionError) as excinfo:
        env._ensure_ready()

    assert SECRET not in str(excinfo.value)
    assert SECRET not in excinfo.value.retry_hint


def test_teardown_failures_are_logged_without_the_key(fake, caplog):
    env = make_env(persistent=False)
    env._ensure_ready()
    sandbox = fake.only()

    def _explode(**kwargs):
        raise RuntimeError(f"kill rejected for {SECRET}")

    sandbox.kill = _explode

    with caplog.at_level(logging.DEBUG):
        env.cleanup()

    assert SECRET not in caplog.text


# ---------------------------------------------------------------------------
# The key does not enter the sandbox
# ---------------------------------------------------------------------------


def test_the_key_is_not_in_the_sandbox_environment(fake):
    env = make_env()
    env._ensure_ready()
    assert fake.create_calls[-1]["envs"] == {}


def test_the_key_is_not_in_sandbox_metadata(fake):
    env = make_env()
    env._ensure_ready()
    assert SECRET not in repr(fake.create_calls[-1]["metadata"])


def test_the_key_is_not_in_any_command_sent_to_the_sandbox(fake):
    env = make_env()
    env._ensure_ready()
    env.execute("printenv", timeout=10)
    for call in fake.only().commands_run:
        assert SECRET not in call.cmd
        assert SECRET not in repr(call.envs)


def test_the_hermes_dotenv_is_not_uploaded(fake, tmp_path, monkeypatch):
    """``~/.hermes/.env`` holds the key. Nothing syncs it unless a user
    explicitly registers it as a credential file, and this proves the plugin's
    push does not widen that set."""
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / ".env").write_text(f"E2B_API_KEY={SECRET}\n", encoding="utf-8")
    (home / "skills" / "s.md").write_text("skill\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env()
    env._ensure_ready()

    written = fake.only().files_written
    assert written, "nothing was uploaded at all — the assertion would be vacuous"
    for path, payload in written.items():
        assert not path.endswith("/.env"), path
        raw = payload if isinstance(payload, bytes) else str(payload).encode()
        assert SECRET.encode() not in raw


def test_the_key_is_not_written_to_disk(fake, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env()
    env._ensure_ready()
    env.cleanup()

    for path in home.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Nothing a user reads contains the key
# ---------------------------------------------------------------------------


def test_status_surfaces_never_print_the_key(fake, plugin_pkg):
    from hermes_plugin_e2b.provider import E2BTerminalEnvironmentProvider

    provider = E2BTerminalEnvironmentProvider()
    blob = repr(provider.doctor_checks()) + repr(provider.probe())
    blob += "\n".join(provider.setup_instructions())
    blob += provider.description + provider.env_description

    assert SECRET not in blob


def test_the_settings_object_does_not_carry_the_key(fake):
    """A long-lived object holding the secret is a leak waiting for a repr()."""
    from hermes_plugin_e2b.config import resolve_settings

    settings = resolve_settings(None)
    assert SECRET not in repr(settings)

    env = make_env()
    env._ensure_ready()
    assert SECRET not in repr(vars(env))


def test_a_key_in_an_unexpected_format_is_still_redacted(monkeypatch):
    """Shape patterns can only guess; an exact match cannot miss."""
    from hermes_plugin_e2b.errors import redact

    odd_key = "XX-not-an-e2b-shaped-key-9182736450"
    monkeypatch.setenv("E2B_API_KEY", odd_key)

    cleaned = redact(f"request rejected while using {odd_key}")
    assert odd_key not in cleaned
    assert "<redacted>" in cleaned


def test_redaction_still_works_with_no_key_configured(monkeypatch):
    from hermes_plugin_e2b.errors import redact

    monkeypatch.delenv("E2B_API_KEY", raising=False)
    assert SECRET not in redact(f"boom {SECRET}")


def test_hermes_is_a_hard_dependency_of_the_error_contract():
    """A local fallback class would break the degraded-backend path silently."""
    import hermes_plugin_e2b.errors as errors_mod
    from tools.environments.base import EnvironmentConnectionError

    assert errors_mod.EnvironmentConnectionError is EnvironmentConnectionError


# ---------------------------------------------------------------------------
# Profile-scoped credential resolution under a multiplexed gateway
# ---------------------------------------------------------------------------


@pytest.fixture
def multiplexed():
    """Run under Hermes' real multiplex flag, restored afterwards.

    This drives ``agent.secret_scope`` itself — no plugin function is patched,
    because the function under test is exactly the one a patch would bypass.
    """
    from agent import secret_scope

    secret_scope.set_multiplex_active(True)
    yield secret_scope
    secret_scope.set_multiplex_active(False)


def test_key_resolution_fails_closed_when_no_profile_scope_is_installed(
    fake, multiplexed, monkeypatch
):
    """Under multiplexing with no profile scope, ``get_secret`` raises
    ``UnscopedSecretError`` by design: ``os.environ`` may hold a *different*
    profile's key. The only safe answer is "no credential" — availability,
    doctor, and environment creation must all refuse rather than fall back
    to the process-wide key."""
    from hermes_plugin_e2b.config import get_api_key
    from hermes_plugin_e2b.errors import EnvironmentConnectionError
    from hermes_plugin_e2b.provider import E2BTerminalEnvironmentProvider

    wrong_profile_key = "e2b_" + "beef" * 10
    monkeypatch.setenv("E2B_API_KEY", wrong_profile_key)

    # The premise, proven with the real machinery: this context raises.
    with pytest.raises(multiplexed.UnscopedSecretError):
        multiplexed.get_secret("E2B_API_KEY")

    assert get_api_key() is None, "get_api_key fell back to the process-wide key"

    provider = E2BTerminalEnvironmentProvider()
    assert provider.is_available() is False
    status, detail = provider.probe()
    assert status == "needs_setup"
    key_rows = [row for row in provider.doctor_checks() if "E2B_API_KEY" in row[1]]
    assert key_rows and key_rows[0][0] is False, key_rows

    env = provider.create_environment(cwd="", timeout=30, task_id="default", container_config={})
    with pytest.raises(EnvironmentConnectionError) as excinfo:
        env._ensure_ready()
    assert "E2B_API_KEY" in str(excinfo.value)
    assert fake.create_calls == [], "an SDK call was attempted with the process-wide key"


def test_a_scoped_secret_still_resolves_under_multiplexing(fake, multiplexed, monkeypatch):
    """Fail-closed must not mean fail-always: with a profile scope installed,
    the profile's own key is used — not the process environment's."""
    from hermes_plugin_e2b.config import get_api_key

    process_key = "e2b_" + "beef" * 10
    scoped_key = "e2b_" + "cafe" * 10
    monkeypatch.setenv("E2B_API_KEY", process_key)

    token = multiplexed.set_secret_scope({"E2B_API_KEY": scoped_key})
    try:
        assert get_api_key() == scoped_key
    finally:
        multiplexed.reset_secret_scope(token)

    # The discriminating case: a scope that LACKS the key. Under multiplexing
    # the scope is authoritative — an environ-first (or environ-fallback)
    # implementation would return the process key here and leak it.
    token = multiplexed.set_secret_scope({"OTHER_SECRET": "x"})
    try:
        assert get_api_key() is None, "a scoped miss borrowed the process-wide key"
    finally:
        multiplexed.reset_secret_scope(token)
