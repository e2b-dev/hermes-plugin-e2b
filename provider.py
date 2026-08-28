"""The Hermes terminal-environment provider for E2B.

Everything Hermes needs to treat ``e2b`` as a first-class ``terminal.backend``
is declared here: the classification flags that drive prompt hints, path
handling, and secret stripping; the availability checks behind ``hermes
doctor`` / ``hermes status`` and the dashboard picker; and the factory that
builds an environment.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from typing import Any

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .config import (
    API_KEY_ENV,
    DEFAULT_CWD,
    SDK_REQUIREMENT,
    E2BSettings,
    has_api_key,
    resolve_settings,
)
from .sandbox import is_probe_scope

logger = logging.getLogger(__name__)

SDK_DISTRIBUTION = "e2b"
SDK_INSTALL_HINT = f"pip install '{SDK_REQUIREMENT}'"


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    """Call *fn*, degrading to *default* instead of raising.

    The provider contract is fail-soft: ``is_available``, ``probe``, and
    ``doctor_checks`` are called from UI paints and from ``hermes doctor``,
    which iterates ``doctor_checks()`` outside its own error handling.
    ``get_secret`` in particular raises by design when a multiplexed gateway
    has no profile secret scope installed.
    """
    try:
        return fn()
    except Exception as exc:
        logger.debug("E2B: %s check failed: %s", getattr(fn, "__name__", fn), exc)
        return default


def _sdk_installed() -> bool:
    """Cheap import-free presence check for the E2B SDK.

    ``is_available()`` and ``probe()`` are contractually required to be cheap
    and to make no network calls — they run on every requirement check and
    every dashboard paint — so this must not import ``e2b`` (which builds HTTP
    transports at import time).
    """
    try:
        return importlib.util.find_spec(SDK_DISTRIBUTION) is not None
    except (ImportError, ValueError):
        return False


def _sdk_version() -> str | None:
    try:
        from importlib.metadata import version

        return version(SDK_DISTRIBUTION)
    except Exception:  # pragma: no cover - metadata unavailable
        return None


class E2BTerminalEnvironmentProvider(TerminalEnvironmentProvider):
    """Registers ``terminal.backend: e2b``."""

    name = "e2b"
    display_name = "E2B"

    #: Commands run in an E2B sandbox, never on the host: suppress host OS,
    #: home, and cwd hints in the system prompt, and skip the host Python probe.
    is_remote = True
    #: Sandbox-style filesystem rooted away from the host: container path
    #: resolution for the file tools, host-looking cwds get sanitized.
    is_container = True
    #: With ``terminal.container_persistent: false`` each session must get its
    #: own sandbox identity. Without this, two ephemeral sessions would share
    #: one sandbox and either one's teardown would destroy the other's work.
    session_isolated_when_nonpersistent = True

    def __init__(self, get_config: Callable[[str, Any], Any] | None = None) -> None:
        # ``PluginContext.get_config``, captured at register() time and called
        # lazily so a config edit takes effect without reinstalling the plugin.
        self._get_config = get_config

    # ------------------------------------------------------------------
    # Descriptions
    # ------------------------------------------------------------------

    @property
    def description(self) -> str:
        return "Run commands in an E2B cloud sandbox (e2b.dev)."

    @property
    def env_description(self) -> str:
        return "an E2B sandbox (Linux)"

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    @property
    def cache_path_base(self) -> str:
        """Where Hermes' synced cache files land inside the sandbox.

        E2B's default template runs as ``user`` with ``$HOME=/home/user``, and
        every file this plugin uploads is rooted at ``$HOME/.hermes``. Hermes
        rewrites host cache paths against this base so a path it shows the
        model actually resolves inside the sandbox.

        Declared as a literal rather than the live remote ``$HOME`` because
        Hermes reads this off the *provider*, with no environment in hand. A
        custom template with a different home is a documented limitation.
        """
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        """Keep the E2B key out of every subprocess the agent can reach.

        The key authenticates the SDK on the host and nothing else: it is
        never written into the sandbox environment, never uploaded, and never
        included in an error message (see ``errors.redact``).
        """
        return frozenset({API_KEY_ENV})

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Cheap, non-raising, no network. Runs on every requirement check."""
        return _safe(_sdk_installed, False) and _safe(has_api_key, False)

    def check_requirements(self, config: dict[str, Any]) -> bool:
        """Full pre-flight check, with an actionable log line per failure."""
        del config  # terminal.* config carries nothing E2B needs to validate
        ok = True
        if not _sdk_installed():
            logger.error(
                "The E2B terminal backend requires the e2b SDK: %s",
                SDK_INSTALL_HINT,
            )
            ok = False
        if not has_api_key():
            logger.error(
                "The E2B terminal backend requires %s in the active Hermes "
                "profile (add it to ~/.hermes/.env).",
                API_KEY_ENV,
            )
            ok = False
        return ok

    def probe(self) -> tuple[str, str]:
        """Dashboard picker health row. Never raises, never touches the network."""
        try:
            if not _safe(_sdk_installed, False):
                return ("needs_setup", f"E2B SDK not installed — {SDK_INSTALL_HINT}")
            if not _safe(has_api_key, False):
                return (
                    "needs_setup",
                    f"{API_KEY_ENV} is not set for this profile — get a key at "
                    "https://e2b.dev/dashboard",
                )
            return ("ready", "")
        except Exception as exc:  # pragma: no cover - contract: must not raise
            logger.debug("E2B probe failed: %s", exc)
            return ("unavailable", "E2B probe failed")

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        """Rows for ``hermes doctor`` and ``hermes status``.

        ``hermes doctor`` iterates this result outside its own try/except, so a
        raising implementation would take the whole command down. Every probe
        here is therefore wrapped.
        """
        sdk = _safe(_sdk_installed, False)
        version = _safe(_sdk_version, None) if sdk else None
        if version:
            sdk_detail = f"(e2b {version})"
        elif sdk:
            sdk_detail = "(installed)"
        else:
            sdk_detail = f"(missing — {SDK_INSTALL_HINT})"

        key = _safe(has_api_key, False)
        key_detail = (
            "(configured)" if key else "(not set for this profile — add it to ~/.hermes/.env)"
        )
        return [
            (bool(sdk), "E2B SDK", sdk_detail),
            (bool(key), f"E2B {API_KEY_ENV}", key_detail),
        ]

    def setup_instructions(self) -> list[str]:
        return [
            "Run commands inside an E2B cloud sandbox instead of on this machine.",
            f"1. Install the SDK:  {SDK_INSTALL_HINT}",
            "2. Create an API key at https://e2b.dev/dashboard",
            f"3. Add {API_KEY_ENV}=<your key> to ~/.hermes/.env",
            "",
            "terminal.container_persistent: true (the default) keeps one sandbox",
            "per session scope and preserves its filesystem across restarts;",
            "false gives every session a throwaway sandbox that is destroyed on",
            "teardown.",
        ]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    def create_environment(
        self,
        *,
        cwd: str = "",
        timeout: int = 180,
        task_id: str = "default",
        image: str | None = None,
        container_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        """Build an environment for *task_id*.

        Accepts and ignores unknown keyword arguments — the forward-compat
        contract that lets Hermes' factory signature grow without breaking
        installed plugins.

        ``image`` is always empty for plugin backends (Hermes only resolves
        images for its built-in backends), so the E2B template comes from
        ``plugins.entries.e2b.settings.template`` instead.
        """
        del image, kwargs

        settings = resolve_settings(self._get_config, is_probe=is_probe_scope(task_id))
        config = container_config or {}
        persistent = _as_bool(config.get("container_persistent", True), True)

        # Import here so selecting a different backend never pays for the
        # environment module, and so a missing SDK surfaces at use time with a
        # clear message rather than at plugin load.
        from .environment import E2BEnvironment

        return E2BEnvironment(
            task_id=task_id,
            settings=settings,
            cwd=cwd or settings.cwd or DEFAULT_CWD,
            timeout=timeout,
            persistent_filesystem=persistent,
        )


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


__all__ = ["E2BTerminalEnvironmentProvider", "E2BSettings"]
