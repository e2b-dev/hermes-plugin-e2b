"""Configuration resolution and the prompt-probe accommodation."""

from __future__ import annotations

import pytest
from fake_e2b import FakeE2B, install


@pytest.fixture
def fake(monkeypatch, api_key):
    return install(monkeypatch, FakeE2B())


def provider(get_config=None):
    from hermes_plugin_e2b.provider import E2BTerminalEnvironmentProvider

    return E2BTerminalEnvironmentProvider(get_config=get_config)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_defaults_are_used_without_plugin_config():
    from hermes_plugin_e2b.config import DEFAULT_CWD, DEFAULT_TEMPLATE, resolve_settings

    settings = resolve_settings(None)
    assert settings.template == DEFAULT_TEMPLATE
    assert settings.cwd == DEFAULT_CWD
    assert settings.lease_seconds == 300
    assert settings.terminal_lifetime_seconds == 300
    assert settings.allow_internet_access is True
    assert settings.secure is True


def test_plugin_settings_win_over_defaults():
    from hermes_plugin_e2b.config import resolve_settings

    values = {
        "template": "my-image",
        "cwd": "/srv/work",
        "lease_seconds": 900,
        "command_grace_seconds": 5,
        "allow_internet_access": False,
        "secure": False,
        "metadata": {"team": "infra", "n": 3},
    }
    settings = resolve_settings(lambda key, default=None: values.get(key, default))

    assert settings.template == "my-image"
    assert settings.cwd == "/srv/work"
    assert settings.lease_seconds == 900
    assert settings.command_grace_seconds == 5
    assert settings.allow_internet_access is False
    assert settings.secure is False
    assert settings.metadata == {"team": "infra", "n": "3"}


def test_the_lease_defaults_to_hermes_lifetime_seconds(monkeypatch):
    """terminal.lifetime_seconds is not forwarded in container_config."""
    from hermes_plugin_e2b.config import resolve_settings

    monkeypatch.setenv("TERMINAL_LIFETIME_SECONDS", "1800")
    settings = resolve_settings(None)
    assert settings.lease_seconds == 1800
    assert settings.terminal_lifetime_seconds == 1800


def test_a_nonsense_setting_falls_back_instead_of_crashing(monkeypatch):
    from hermes_plugin_e2b.config import resolve_settings

    monkeypatch.setenv("TERMINAL_LIFETIME_SECONDS", "not-a-number")
    values = {"lease_seconds": "also-not-a-number", "template": None}
    settings = resolve_settings(lambda key, default=None: values.get(key, default))

    assert settings.lease_seconds == 300
    assert settings.template == "base"


def test_a_raising_config_reader_falls_back_to_defaults():
    from hermes_plugin_e2b.config import resolve_settings

    def _boom(key, default=None):
        raise RuntimeError("config.yaml is unreadable")

    assert resolve_settings(_boom).template == "base"


def test_the_lease_always_covers_a_command_plus_grace():
    from hermes_plugin_e2b.config import E2BSettings

    settings = E2BSettings(lease_seconds=300, command_grace_seconds=30)
    assert settings.lease_for(None) == 300
    assert settings.lease_for(60) == 300  # baseline already covers it
    assert settings.lease_for(600) == 630
    assert settings.lease_for(0) == 300


def test_the_lease_never_drops_below_a_usable_floor():
    from hermes_plugin_e2b.config import resolve_settings

    settings = resolve_settings(lambda key, default=None: 1 if key == "lease_seconds" else default)
    assert settings.lease_seconds >= 60


def test_an_ephemeral_lease_outlives_one_complete_idle_reaper_interval():
    from hermes_plugin_e2b.config import E2BSettings

    settings = E2BSettings(
        lease_seconds=60,
        terminal_lifetime_seconds=300,
        command_grace_seconds=30,
    )

    assert settings.ephemeral_lease_for(10) == 420
    assert settings.ephemeral_lease_for(600) == 630
    no_command_grace = E2BSettings(
        lease_seconds=60,
        terminal_lifetime_seconds=300,
        command_grace_seconds=0,
    )
    assert no_command_grace.ephemeral_lease_for(10) == 420
    assert no_command_grace.cleanup_lease_for(300) == 360


def test_large_ephemeral_lifetimes_are_not_capped_before_e2b_validates_them(
    fake,
    monkeypatch,
):
    from hermes_plugin_e2b.errors import EnvironmentConnectionError

    monkeypatch.setenv("TERMINAL_LIFETIME_SECONDS", "86400")
    env = provider(
        lambda key, default=None: 60 if key == "lease_seconds" else default
    ).create_environment(
        cwd="",
        timeout=30,
        task_id="large-lifetime",
        container_config={"container_persistent": False},
    )
    fake.create_error = ValueError("server rejected timeout")

    with pytest.raises(EnvironmentConnectionError, match="86520s lifecycle lease"):
        env._ensure_ready()

    assert fake.create_calls[-1]["timeout"] == 86_520


# ---------------------------------------------------------------------------
# The prompt-probe environment
# ---------------------------------------------------------------------------


def test_the_prompt_probe_gets_a_short_lived_disposable_sandbox(fake):
    """Core builds this one, runs one command, and never reaps it."""
    from hermes_plugin_e2b.config import PROBE_LEASE_SECONDS, PROBE_TASK_ID

    env = provider().create_environment(
        cwd="", timeout=180, task_id=PROBE_TASK_ID, container_config={"container_persistent": True}
    )
    env._ensure_ready()
    sandbox = fake.only()

    assert env._persistent is False, "a probe sandbox must never be persistent"
    assert fake.create_calls[-1]["lifecycle"]["on_timeout"] == "kill"
    assert fake.create_calls[-1]["timeout"] <= PROBE_LEASE_SECONDS
    assert fake.list_calls == [], "the probe must not adopt an existing sandbox"

    env.cleanup()
    assert sandbox.alive is False


def test_a_normal_task_is_unaffected_by_the_probe_accommodation(fake):
    env = provider().create_environment(
        cwd="", timeout=180, task_id="default", container_config={"container_persistent": True}
    )
    env._ensure_ready()

    assert env._persistent is True
    assert fake.create_calls[-1]["lifecycle"]["on_timeout"] == {
        "action": "pause",
        "keep_memory": False,
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_container_persistent_false_produces_an_ephemeral_environment(fake):
    env = provider().create_environment(
        cwd="", timeout=60, task_id="default", container_config={"container_persistent": False}
    )
    assert env._persistent is False


def test_the_factory_ignores_the_image_argument(fake):
    """Hermes never resolves an image for plugin backends; the template is ours."""
    env = provider(lambda key, default=None: "custom-template" if key == "template" else default)
    built = env.create_environment(
        cwd="", timeout=60, task_id="default", image="nikolaik/python-nodejs", container_config={}
    )
    assert built._settings.template == "custom-template"


def test_the_factory_tolerates_future_keyword_arguments(fake):
    built = provider().create_environment(
        cwd="",
        timeout=60,
        task_id="default",
        container_config={},
        some_future_option="whatever",
    )
    assert built is not None
