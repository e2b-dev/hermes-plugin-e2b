"""Error mapping between the E2B SDK and Hermes' terminal error contract.

Hermes distinguishes two failure classes:

* a *command* failed — non-zero ``returncode``, handled by the terminal tool;
* the *backend* failed — ``EnvironmentConnectionError``.

How core surfaces the second class depends on where it is raised. An error
during environment *creation* becomes a structured ``status: "degraded"``
result with the retry hint, and the environment is evicted. An error raised
from ``execute()`` on a cached environment — which is where every error from
this plugin lands, because it attaches its sandbox lazily on first use — is
caught by the terminal tool's foreground retry loop instead: the command is
retried up to three times against the same environment (re-entering bring-up
and the state sync, so transient failures self-heal), and only then reported,
as a generic error carrying the reason but not the retry hint, with no
eviction (``tools/terminal_tool.py``, the ``except Exception`` handler in the
foreground loop). Reported upstream as a contract gap.

Everything in this module is about landing E2B failures in the second class
with an actionable reason and no credential material in the message.
"""

from __future__ import annotations

import re
from typing import Any

# Imported unconditionally on purpose. A local fallback class would still
# import, but Hermes would not recognise it, so the terminal tool's
# degraded-backend path would silently stop working. Letting the ImportError
# propagate reaches ``register()``, which reports it as one clear line naming
# the Hermes version requirement.
from tools.environments.base import EnvironmentConnectionError

__all__ = [
    "EnvironmentConnectionError",
    "E2BAuthError",
    "connection_error",
    "is_missing_sandbox",
    "redact",
]


class E2BAuthError(EnvironmentConnectionError):
    """E2B rejected the API key configured for the active Hermes profile."""


_AUTH_RETRY_HINT = (
    "E2B rejected the API key for the active Hermes profile. Set a valid "
    "E2B_API_KEY in ~/.hermes/.env (or the profile's .env) and retry. This is "
    "a configuration problem, not a command failure."
)

_DEFAULT_RETRY_HINT = (
    "This is an infrastructure failure, not a command failure. Check network "
    "reachability of the E2B API and that E2B_API_KEY is valid for the active "
    "Hermes profile, then retry the same command — recovery is automatic once "
    "E2B is reachable again."
)

# Anything that looks like an E2B key or a bearer token. E2B keys are
# ``e2b_<hex>``; the generic patterns catch tokens embedded in SDK error text
# (URLs with query credentials, Authorization headers echoed back by a proxy).
_SECRET_PATTERNS = (
    re.compile(r"e2b_[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"(?i)\b(?:bearer|token|api[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token)=)[^&\s]+"),
)


def _configured_key() -> str | None:
    """The key currently in effect, for exact-match redaction. Never raises."""
    try:
        from .config import get_api_key

        return get_api_key()
    except Exception:
        return None


def redact(text: Any) -> str:
    """Return ``text`` with anything that looks like a credential removed.

    Applied to every message this plugin surfaces to the model, to logs, and
    to ``hermes doctor`` / ``hermes status`` rows. Hermes redacts terminal
    output separately; this covers the *error* path, which does not go through
    that filter.

    Two layers, because either alone is incomplete: an exact match against the
    key actually in effect (which catches any format, present or future), and
    shape patterns (which catch a key that is *not* the configured one — a
    stale value echoed by a proxy, or a second profile's).
    """
    value = str(text)
    configured = _configured_key()
    if configured and len(configured) >= 8 and configured in value:
        value = value.replace(configured, "<redacted>")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            value = pattern.sub(r"\1<redacted>", value)
        else:
            value = pattern.sub("<redacted>", value)
    return value


def _is_auth_failure(exc: BaseException) -> bool:
    """True when *exc* is an E2B authentication rejection.

    ``AuthenticationException`` deliberately inherits from ``Exception`` and
    NOT from ``SandboxException`` in the E2B SDK, so it cannot be caught by a
    blanket ``except SandboxException``. Import it defensively: the SDK is an
    optional dependency and this module is importable without it.
    """
    try:
        from e2b import AuthenticationException
    except Exception:  # pragma: no cover - SDK absent
        return False
    return isinstance(exc, AuthenticationException)


def is_missing_sandbox(exc: BaseException) -> bool:
    """True when E2B says the sandbox no longer exists.

    Used to decide "replace it" rather than "fail the command": a persistent
    sandbox can be reaped server-side (lease expiry with ``on_timeout: kill``,
    an account-level purge, a manual ``e2b sandbox kill``).
    """
    try:
        from e2b import NotFoundException, SandboxNotFoundException
    except Exception:  # pragma: no cover - SDK absent
        return False
    return isinstance(exc, (SandboxNotFoundException, NotFoundException))


def connection_error(action: str, exc: BaseException) -> EnvironmentConnectionError:
    """Wrap *exc* as the Hermes infrastructure-failure type.

    ``action`` is a short human phrase ("sandbox creation", "command launch")
    that names the operation, so the model sees *what* failed rather than a
    bare SDK traceback.
    """
    reason = redact(f"E2B {action} failed: {type(exc).__name__}: {exc}")
    if _is_auth_failure(exc):
        return E2BAuthError(reason, retry_hint=_AUTH_RETRY_HINT)
    return EnvironmentConnectionError(reason, retry_hint=_DEFAULT_RETRY_HINT)
