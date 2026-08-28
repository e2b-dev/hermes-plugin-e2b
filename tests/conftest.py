"""Shared fixtures.

The plugin is loaded exactly the way Hermes loads a directory plugin — as a
package rooted at the repo, imported from ``__init__.py`` with
``submodule_search_locations`` — so the tests exercise the real import shape
(relative imports included) rather than a flattened one.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

# Set before anything imports Hermes, and via os.environ rather than
# monkeypatch, so it survives the whole session. Environments torn down late —
# BaseEnvironment.__del__ runs its cleanup, including a state pull, whenever the
# garbage collector gets to it — would otherwise resolve get_hermes_home() after
# a per-test monkeypatch had rolled back, and write into the developer's real
# ~/.hermes.
_SESSION_HERMES_HOME = tempfile.mkdtemp(prefix="hermes-e2b-tests-")
os.environ["HERMES_HOME"] = _SESSION_HERMES_HOME
atexit.register(shutil.rmtree, _SESSION_HERMES_HOME, True)

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = "hermes_plugin_e2b"

# --import-mode=importlib does not put the tests directory on sys.path, and the
# repo root is a Hermes plugin package rather than an importable one, so the
# shared test double is made importable explicitly.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def _load_plugin_package() -> types.ModuleType:
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PACKAGE_NAME
    module.__path__ = [str(REPO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_package()


@pytest.fixture
def plugin_pkg():
    return plugin


@pytest.fixture(autouse=True)
def _clean_terminal_registry():
    """Keep provider registrations from leaking between tests."""
    from agent import terminal_env_registry

    terminal_env_registry._reset_for_tests()
    yield
    terminal_env_registry._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Give every test its own Hermes home.

    Without this the suite reads the developer's real ``~/.hermes`` — the
    environment's bring-up enumerates its credentials, skills, and cache and
    uploads them into the test double — and teardown's pull writes back into
    it, creating ``~/.hermes/.sync.lock`` and potentially overwriting real
    files. It also makes results depend on whatever that directory happens to
    contain. Tests that need specific contents override this with their own
    ``hermes_home`` fixture.
    """
    home = tmp_path / "isolated-hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_retry_sleeps(monkeypatch):
    """Keep Hermes' sync-back retry backoff from dominating the suite.

    ``FileSyncManager`` sleeps 2s, 4s, 8s between sync-back attempts. Several
    tests deliberately make sync-back fail, so the retry *count* matters but
    the wall-clock wait does not.
    """
    from tools.environments import file_sync

    monkeypatch.setattr(file_sync, "_sleep", lambda _seconds: None)


@pytest.fixture
def api_key(monkeypatch):
    """A syntactically plausible key that is not a real credential."""
    key = "e2b_" + "0" * 40
    monkeypatch.setenv("E2B_API_KEY", key)
    return key
