"""E2B terminal backend for Hermes Agent.

Registers ``terminal.backend: e2b`` so Hermes can run outside E2B while
delegating terminal, file, and code execution into an E2B cloud sandbox.

The plugin touches no Hermes core files. It registers through the standard
plugin entry point — ``register(ctx)`` calling
``ctx.register_terminal_environment_provider`` — and everything else
(dispatch, the setup wizard, doctor/status, the dashboard picker, approval
policy, remote path handling, secret stripping) follows from the provider's
declared metadata.

See NOTICE for attribution to the original in-tree implementation.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin entry point. Called once by Hermes at plugin load."""
    # Imported inside register() so a Hermes version without the terminal
    # provider ABC fails with one clear log line instead of an import error
    # during plugin discovery.
    try:
        from .provider import E2BTerminalEnvironmentProvider
    except ImportError as exc:
        logger.error(
            "The e2b plugin needs a Hermes build with pluggable terminal "
            "backends (hermes-agent with agent/terminal_env_provider.py, "
            "PR #94400 or later): %s",
            exc,
        )
        return

    # ctx.get_config reads plugins.entries.e2b.settings.<key> from config.yaml.
    # Bound rather than snapshotted so a config edit takes effect on the next
    # environment without reloading the plugin. Fetched defensively: the
    # provider falls back to defaults when the host context predates it.
    provider = E2BTerminalEnvironmentProvider(get_config=getattr(ctx, "get_config", None))
    ctx.register_terminal_environment_provider(provider)


__all__ = ["register", "__version__"]
