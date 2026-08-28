"""The plugin must satisfy the public Hermes terminal-backend contract.

These assert against the real Hermes modules, not a mock of them: the point is
to catch a contract drift in Hermes as a failing test here rather than as a
broken backend in production.
"""

from __future__ import annotations

import inspect

from agent import terminal_env_registry
from agent.terminal_env_provider import TerminalEnvironmentProvider


def _provider(plugin_pkg, get_config=None):
    from hermes_plugin_e2b.provider import E2BTerminalEnvironmentProvider

    return E2BTerminalEnvironmentProvider(get_config=get_config)


class _Ctx:
    """Minimal stand-in for PluginContext's terminal-provider surface."""

    def __init__(self, settings=None):
        self.registered = []
        self._settings = settings or {}

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def register_terminal_environment_provider(self, provider):
        terminal_env_registry.register_provider(provider)
        self.registered.append(provider)


def test_register_puts_the_backend_in_the_real_registry(plugin_pkg):
    ctx = _Ctx()
    plugin_pkg.register(ctx)

    assert terminal_env_registry.plugin_backend_names() == ["e2b"]
    resolved = terminal_env_registry.get_provider("e2b")
    assert resolved is ctx.registered[0]
    assert isinstance(resolved, TerminalEnvironmentProvider)


def test_backend_name_is_not_reserved_by_core():
    """A reserved name would make register_provider raise and the plugin vanish."""
    assert "e2b" not in terminal_env_registry.BUILTIN_BACKEND_NAMES


def test_classification_flags_reach_core_helpers(plugin_pkg):
    plugin_pkg.register(_Ctx())
    flag = terminal_env_registry.provider_flag

    assert flag("e2b", "is_remote") is True
    assert flag("e2b", "is_container") is True
    assert flag("e2b", "session_isolated_when_nonpersistent") is True
    # skip_container_guards defaults to is_container on the ABC.
    assert flag("e2b", "skip_container_guards") is True
    assert flag("e2b", "cache_path_base", None) == "~/.hermes"


def test_api_key_is_stripped_from_agent_subprocesses(plugin_pkg):
    plugin_pkg.register(_Ctx())
    assert "E2B_API_KEY" in terminal_env_registry.plugin_strip_env_keys()


def test_create_environment_accepts_unknown_kwargs(plugin_pkg):
    """The forward-compat contract: unknown factory kwargs must be ignored."""
    signature = inspect.signature(_provider(plugin_pkg).create_environment)
    kinds = {p.kind for p in signature.parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD in kinds


def test_availability_checks_make_no_network_calls(plugin_pkg, monkeypatch):
    """`is_available()`/`probe()` run on every UI paint — they must stay cheap."""
    import socket

    def _forbidden(*args, **kwargs):  # pragma: no cover - only on failure
        raise AssertionError("availability check opened a socket")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    provider = _provider(plugin_pkg)
    provider.is_available()
    provider.probe()
    provider.doctor_checks()


def test_probe_and_doctor_never_raise(plugin_pkg, monkeypatch):
    from hermes_plugin_e2b import config as plugin_config

    def _boom():
        raise RuntimeError("secret scope unavailable")

    monkeypatch.setattr(plugin_config, "get_api_key", _boom)
    provider = _provider(plugin_pkg)

    # This only pins "probe/doctor never raise" against a hostile stand-in.
    # The real fail-closed contract of get_api_key itself is pinned by
    # test_security.py::test_key_resolution_fails_closed_when_no_profile_scope_is_installed,
    # which drives agent.secret_scope for real instead of patching the plugin.
    status, _detail = provider.probe()
    assert status in {"ready", "needs_setup", "unavailable"}
    assert isinstance(provider.doctor_checks(), list)


def test_probe_reports_needs_setup_without_a_key(plugin_pkg, monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    provider = _provider(plugin_pkg)
    status, detail = provider.probe()
    assert status == "needs_setup"
    assert "E2B_API_KEY" in detail or "SDK" in detail


def test_probe_is_ready_with_sdk_and_key(plugin_pkg, api_key):
    provider = _provider(plugin_pkg)
    assert provider.is_available() is True
    assert provider.probe() == ("ready", "")


def test_plugin_manifest_matches_the_provider(plugin_pkg):
    """The manifest name is the install dir, the registry key, and the backend."""
    import pathlib

    import yaml

    manifest = yaml.safe_load(
        (pathlib.Path(plugin_pkg.__file__).parent / "plugin.yaml").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "e2b"
    assert manifest["kind"] == "backend"
    assert manifest["version"] == plugin_pkg.__version__
    assert _provider(plugin_pkg).name == manifest["name"]


def test_the_documented_sdk_requirement_matches_the_declared_dependency():
    """The setup hint a user follows must install a version this plugin supports."""
    import pathlib
    import tomllib

    from hermes_plugin_e2b.config import SDK_REQUIREMENT

    pyproject = tomllib.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert SDK_REQUIREMENT in pyproject["project"]["dependencies"]


def test_setup_instructions_point_at_the_supported_sdk(plugin_pkg):
    from hermes_plugin_e2b.config import SDK_REQUIREMENT

    lines = "\n".join(_provider(plugin_pkg).setup_instructions())
    assert SDK_REQUIREMENT in lines


def test_registration_survives_a_context_without_get_config(plugin_pkg):
    """Provider interfaces grow through defaults; settings fall back cleanly."""

    class _MinimalCtx:
        def register_terminal_environment_provider(self, provider):
            terminal_env_registry.register_provider(provider)

    plugin_pkg.register(_MinimalCtx())

    provider = terminal_env_registry.get_provider("e2b")
    assert provider is not None
    environment = provider.create_environment(
        cwd="", timeout=60, task_id="default", container_config={}
    )
    assert environment._settings.template == "base"
