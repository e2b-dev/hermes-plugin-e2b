"""Configuration resolution for the E2B terminal backend.

Three sources, in precedence order:

1. ``plugins.entries.e2b.settings.<key>`` in ``config.yaml`` — the public
   per-plugin config namespace (``PluginContext.get_config``). This is the
   only place plugin-specific settings belong.
2. ``TERMINAL_*`` environment variables that Hermes itself bridges from
   ``terminal.*`` in ``config.yaml`` (see ``_ensure_terminal_env_bridged``).
   Used for the two settings the plugin needs but that core does not forward
   through ``container_config`` — notably ``terminal.lifetime_seconds``.
3. Built-in defaults.

``E2B_API_KEY`` is resolved separately through Hermes' profile-aware
``agent.secret_scope.get_secret`` so a multiplexed gateway reads the right
profile's key. Its value is never stored on any object that gets logged,
never written to disk, and never passed into a sandbox.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

API_KEY_ENV = "E2B_API_KEY"

#: Single source of truth for the supported SDK range, kept in step with the
#: ``dependencies`` entry in pyproject.toml. The floor is the version whose
#: create / list / lifecycle behaviour this plugin was verified against by
#: reading the installed SDK source.
SDK_REQUIREMENT = "e2b>=2.46,<3"

DEFAULT_TEMPLATE = "base"
DEFAULT_CWD = "/home/user"
DEFAULT_LEASE_SECONDS = 300
#: E2B rejects shorter sandbox lifetimes. Kept explicit because cleanup uses
#: the same floor when it has to resume a naturally paused sandbox.
MIN_LEASE_SECONDS = 60
#: Extra sandbox lease granted beyond a command's own timeout so the E2B
#: ``on_timeout`` lifecycle action can never fire while a command is running.
DEFAULT_COMMAND_GRACE_SECONDS = 30
#: Hermes' idle-environment reaper checks once per minute. A normal ephemeral
#: sandbox must survive one complete check interval after its idle deadline so
#: Hermes still gets a chance to pull state before E2B's ``on_timeout: kill``
#: backstop fires.
HERMES_IDLE_REAPER_INTERVAL_SECONDS = 60
#: E2B 2.46's default control-plane request timeout
#: (``e2b.connection_config.REQUEST_TIMEOUT``). After Hermes' worst-case
#: reaper tick, the sandbox must remain alive long enough for a reconnect
#: request to reach E2B and renew the transfer lease. This is a lifecycle
#: invariant, independent of user-configurable command grace.
E2B_CONTROL_PLANE_MARGIN_SECONDS = 60
#: Lease for the environment Hermes builds for its system-prompt backend probe
#: (see ``notes`` in ``sandbox.py``). Short and always ``on_timeout: kill``.
PROBE_LEASE_SECONDS = 120
#: ``task_id`` Hermes uses for the system-prompt backend probe. That
#: environment is never registered in ``_active_environments`` and never
#: reaped, so we make it cheap and self-destructing.
PROBE_TASK_ID = "prompt-backend-probe"

_TRUE = {"true", "1", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def get_api_key() -> str | None:
    """Return the E2B API key for the active Hermes profile, or None.

    Never raises — ``is_available()`` and ``probe()`` must not — but it never
    trades that for a wrong answer either.

    ``agent.secret_scope.get_secret`` raises ``UnscopedSecretError`` **by
    design** when a multiplexed gateway is running and this code path has no
    profile secret scope installed. That is Hermes failing closed: under
    multiplexing ``os.environ`` may hold a *different* profile's credential.
    Catching that and reading ``os.environ`` anyway would hand one profile
    another profile's key, so the only safe answer there is "no key".

    The ``os.environ`` fallback is reserved for the one case where it is
    correct: a Hermes build with no secret-scope module at all.
    """
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        value: str | None = os.getenv(API_KEY_ENV)
    else:
        try:
            value = get_secret(API_KEY_ENV)
        except Exception:
            # Fail closed. Includes UnscopedSecretError; anything unexpected
            # gets the same treatment, because guessing at a credential is
            # worse than reporting the backend as unconfigured.
            return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def has_api_key() -> bool:
    return get_api_key() is not None


@dataclass(frozen=True)
class E2BSettings:
    """Resolved, immutable settings for one environment.

    Deliberately does NOT hold the API key — it is read at the moment of each
    SDK call so a rotated key takes effect without rebuilding environments,
    and so no long-lived object carries the secret.
    """

    template: str = DEFAULT_TEMPLATE
    cwd: str = DEFAULT_CWD
    #: Baseline sandbox lease. Renewed upward per command; never shortened.
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    command_grace_seconds: int = DEFAULT_COMMAND_GRACE_SECONDS
    #: Hermes' own idle deadline. Distinct from ``lease_seconds`` because a
    #: user may tune the E2B baseline down while Hermes still reaps on the
    #: configured terminal lifetime.
    terminal_lifetime_seconds: int = DEFAULT_LEASE_SECONDS
    #: ``False`` puts the sandbox behind E2B's egress deny-all.
    allow_internet_access: bool = True
    #: E2B's envd access-token protection. Leave on.
    secure: bool = True
    #: Extra ``metadata`` merged into the sandbox record. Identity keys always
    #: win, so a user cannot accidentally collide two scopes.
    metadata: dict[str, str] = field(default_factory=dict)

    def lease_for(self, command_timeout: int | None) -> int:
        """Lease that covers *command_timeout* plus grace, never below the baseline."""
        needed = self.lease_seconds
        if command_timeout and command_timeout > 0:
            needed = max(needed, int(command_timeout) + self.command_grace_seconds)
        return max(needed, MIN_LEASE_SECONDS)

    def ephemeral_lease_for(self, command_timeout: int | None) -> int:
        """Lease long enough for Hermes to reap and clean an idle sandbox.

        ``on_timeout: kill`` is the last-resort cleanup for an abandoned
        ephemeral sandbox. It must not win the normal race against Hermes'
        own idle reaper, which pulls state before explicitly destroying it.
        """
        cleanup_window = (
            max(0, self.terminal_lifetime_seconds)
            + HERMES_IDLE_REAPER_INTERVAL_SECONDS
            + E2B_CONTROL_PLANE_MARGIN_SECONDS
        )
        return max(self.lease_for(command_timeout), cleanup_window)

    def cleanup_lease_for(self, command_timeout: int) -> int:
        """Lease for a lifecycle cleanup transfer, with non-configurable headroom."""
        return max(
            self.lease_for(command_timeout),
            command_timeout + E2B_CONTROL_PLANE_MARGIN_SECONDS,
        )


def resolve_settings(
    get_config: Callable[[str, Any], Any] | None = None,
    *,
    is_probe: bool = False,
) -> E2BSettings:
    """Build :class:`E2BSettings` from plugin config, env, and defaults.

    *get_config* is ``PluginContext.get_config`` (captured at ``register()``).
    It is optional so the environment stays constructible in tests and when a
    provider is instantiated outside a plugin context.
    """

    def cfg(key: str, default: Any) -> Any:
        if get_config is None:
            return default
        try:
            value = get_config(key, default)
        except Exception:
            return default
        return default if value is None else value

    template = str(cfg("template", DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE).strip()
    cwd = str(cfg("cwd", DEFAULT_CWD) or DEFAULT_CWD).strip() or DEFAULT_CWD

    # terminal.lifetime_seconds is the idle window after which Hermes reaps the
    # environment. Matching the sandbox lease to it means E2B pauses the
    # sandbox at roughly the moment Hermes stops using it.
    terminal_lifetime = _env_int("TERMINAL_LIFETIME_SECONDS", DEFAULT_LEASE_SECONDS)
    default_lease = terminal_lifetime
    lease = _as_int(cfg("lease_seconds", default_lease), default_lease)
    if is_probe:
        lease = min(lease, PROBE_LEASE_SECONDS)
    lease = max(lease, MIN_LEASE_SECONDS)

    grace = max(
        0,
        _as_int(
            cfg("command_grace_seconds", DEFAULT_COMMAND_GRACE_SECONDS),
            DEFAULT_COMMAND_GRACE_SECONDS,
        ),
    )

    raw_metadata = cfg("metadata", {})
    metadata: dict[str, str] = {}
    if isinstance(raw_metadata, dict):
        for key, value in raw_metadata.items():
            if isinstance(key, str) and value is not None:
                metadata[key] = str(value)

    return E2BSettings(
        template=template or DEFAULT_TEMPLATE,
        cwd=cwd,
        lease_seconds=lease,
        command_grace_seconds=grace,
        terminal_lifetime_seconds=max(0, terminal_lifetime),
        allow_internet_access=_as_bool(cfg("allow_internet_access", True), True),
        secure=_as_bool(cfg("secure", True), True),
        metadata=metadata,
    )
