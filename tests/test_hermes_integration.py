"""End-to-end through a real Hermes installation.

Each case runs in a fresh subprocess against a throwaway ``HERMES_HOME`` with
the plugin installed the way a user installs it — a directory under
``~/.hermes/plugins/`` plus ``plugins.enabled`` — so the assertions cover
Hermes' own discovery, manifest parsing, namespaced import, and terminal
dispatch rather than a hand-built registry.

A subprocess per case is deliberate: plugin discovery, the terminal-environment
registry, and the terminal tool's environment cache are all process-global, so
in-process cases would contaminate each other.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hermes_install(tmp_path):
    """A Hermes home with this plugin installed and enabled."""
    home = tmp_path / "hermes"
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    target = plugins / "e2b"
    shutil.copytree(
        REPO_ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "tests", "__pycache__", "*.egg-info", ".pytest_cache"
        ),
    )
    (home / "config.yaml").write_text(
        textwrap.dedent(
            """
            plugins:
              enabled:
                - e2b
            terminal:
              backend: e2b
              container_persistent: true
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return home


def run_in_hermes(home: Path, body: str, env_extra=None) -> dict:
    """Run *body* in a subprocess with HERMES_HOME set; return its JSON result."""
    indented = textwrap.indent(textwrap.dedent(body).strip(), " " * 8)
    script = (
        "import json, os, sys\n"
        "result = {}\n"
        "try:\n" + indented + "\n"
        "except BaseException:\n"
        "    import traceback\n"
        "    result['__error__'] = traceback.format_exc()\n"
        "sys.stdout.write('---RESULT---' + json.dumps(result, default=str))\n"
    )

    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env.pop("TERMINAL_ENV", None)
    env.update(env_extra or {})

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(home),
    )
    marker = "---RESULT---"
    assert marker in proc.stdout, (
        f"subprocess produced no result\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    payload = json.loads(proc.stdout.split(marker, 1)[1])
    assert "__error__" not in payload, payload["__error__"]
    return payload


# ---------------------------------------------------------------------------
# Discovery and registration
# ---------------------------------------------------------------------------


def test_hermes_discovers_and_registers_the_backend(hermes_install):
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        from agent import terminal_env_registry as reg

        discover_plugins()
        manager = get_plugin_manager()
        loaded = manager._plugins.get("e2b")
        result["loaded"] = bool(loaded and loaded.enabled)
        result["load_error"] = getattr(loaded, "error", None)
        result["backends"] = reg.plugin_backend_names()
        provider = reg.get_provider("e2b")
        result["display_name"] = provider.display_name
        result["description"] = provider.description
        """,
    )
    assert out["loaded"] is True, out["load_error"]
    assert out["backends"] == ["e2b"]
    assert out["display_name"] == "E2B"
    assert "E2B" in out["description"]


def test_a_disabled_plugin_registers_nothing(hermes_install):
    (hermes_install / "config.yaml").write_text(
        "plugins:\n  enabled: []\nterminal:\n  backend: local\n", encoding="utf-8"
    )
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins
        from agent import terminal_env_registry as reg

        discover_plugins()
        result["backends"] = reg.plugin_backend_names()
        """,
    )
    assert out["backends"] == []


def test_uninstalling_the_directory_removes_the_backend(hermes_install):
    shutil.rmtree(hermes_install / "plugins" / "e2b")
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins
        from agent import terminal_env_registry as reg

        discover_plugins()
        result["backends"] = reg.plugin_backend_names()
        """,
    )
    assert out["backends"] == []


# ---------------------------------------------------------------------------
# Classification reaching the core policy sites
# ---------------------------------------------------------------------------


def test_core_policy_sites_classify_the_backend(hermes_install):
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins
        discover_plugins()

        import os
        os.environ["TERMINAL_ENV"] = "e2b"

        from tools import terminal_tool
        from tools import file_tools, env_probe, skills_tool
        from tools.environments import local as local_env
        from agent import prompt_builder

        result["is_container"] = terminal_tool._is_container_backend("e2b")
        result["file_tools_container_paths"] = file_tools._uses_container_paths("default")
        result["env_probe_remote"] = env_probe._plugin_backend_is_remote("e2b")
        result["skills_remote"] = skills_tool._is_remote_env_backend("e2b")
        result["prompt_remote"] = prompt_builder._plugin_backend_is_remote("e2b")
        result["prompt_description"] = prompt_builder._plugin_backend_description("e2b")
        result["stripped"] = sorted(local_env._plugin_terminal_env_strip_keys())
        # The terminal / execute_code spawn path is the one that matters: it is
        # what a model-authored command runs through.
        sanitized = local_env._sanitize_subprocess_env(
            {"E2B_API_KEY": "e2b_secret", "PATH": "/usr/bin"}
        )
        result["sanitized_keys"] = sorted(sanitized)
        """,
    )
    assert out["is_container"] is True
    assert out["file_tools_container_paths"] is True
    assert out["env_probe_remote"] is True
    assert out["skills_remote"] is True
    assert out["prompt_remote"] is True
    assert out["prompt_description"] == "an E2B sandbox (Linux)"
    assert "E2B_API_KEY" in out["stripped"]
    assert "E2B_API_KEY" not in out["sanitized_keys"]
    assert "PATH" in out["sanitized_keys"], "the sanitizer dropped everything"


def test_cache_paths_are_translated_into_the_sandbox(hermes_install):
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins
        discover_plugins()

        import os
        os.environ["TERMINAL_ENV"] = "e2b"

        from hermes_constants import get_hermes_home
        from tools.credential_files import to_agent_visible_cache_path

        host = str(get_hermes_home() / "cache" / "documents" / "report.pdf")
        os.makedirs(os.path.dirname(host), exist_ok=True)
        open(host, "w").close()
        result["mapped"] = to_agent_visible_cache_path(host)
        """,
    )
    assert out["mapped"].startswith("~/.hermes/") or out["mapped"].startswith("/"), out
    assert "cache/documents/report.pdf" in out["mapped"]


def test_requirements_check_fails_loudly_without_a_key(hermes_install):
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins
        discover_plugins()

        import os
        os.environ["TERMINAL_ENV"] = "e2b"
        os.environ.pop("E2B_API_KEY", None)

        from tools.terminal_tool import check_terminal_requirements
        result["ok"] = check_terminal_requirements()
        """,
        env_extra={"E2B_API_KEY": ""},
    )
    assert out["ok"] is False


def test_requirements_check_passes_with_sdk_and_key(hermes_install):
    out = run_in_hermes(
        hermes_install,
        """
        from hermes_cli.plugins import discover_plugins
        discover_plugins()

        import os
        os.environ["TERMINAL_ENV"] = "e2b"

        from tools.terminal_tool import check_terminal_requirements
        result["ok"] = check_terminal_requirements()
        """,
        env_extra={"E2B_API_KEY": "e2b_" + "0" * 40},
    )
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_FAKE_SDK_PREAMBLE = """
import sys
sys.path.insert(0, {tests_dir!r})
from hermes_cli.plugins import discover_plugins
discover_plugins()

import os
os.environ["TERMINAL_ENV"] = "e2b"

# Point the plugin's SDK boundary at the double, exactly as the unit suite does.
import hermes_plugins.e2b as plugin
sys.modules.setdefault("hermes_plugin_e2b", plugin)
sys.modules["hermes_plugin_e2b.sandbox"] = sys.modules["hermes_plugins.e2b.sandbox"]
sys.modules["hermes_plugin_e2b.config"] = sys.modules["hermes_plugins.e2b.config"]
sys.modules["hermes_plugin_e2b.errors"] = sys.modules["hermes_plugins.e2b.errors"]

from fake_e2b import FakeE2B
import hermes_plugins.e2b.sandbox as sandbox_api

fake = FakeE2B()


class _MP:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


import fake_e2b
fake_e2b.install(_MP(), fake)
"""


def test_terminal_dispatch_routes_a_command_into_the_sandbox(hermes_install):
    tests_dir = str(Path(__file__).resolve().parent)
    out = run_in_hermes(
        hermes_install,
        _FAKE_SDK_PREAMBLE.format(tests_dir=tests_dir)
        + """
from tools import terminal_tool

payload = terminal_tool.terminal_tool(command="echo hello-from-e2b")
import json as _json
data = _json.loads(payload)
result["exit_code"] = data.get("exit_code")
result["output"] = data.get("output", "")
result["error"] = data.get("error")
result["sandboxes_created"] = len(fake.create_calls)
result["commands"] = [c.cmd for c in fake.only().commands_run]
result["backend_stamp"] = getattr(
    terminal_tool.get_active_env("default"), "_hermes_backend_name", None
)
""",
        env_extra={"E2B_API_KEY": "e2b_" + "0" * 40},
    )
    assert out["error"] in (None, ""), out
    assert out["exit_code"] == 0
    assert out["sandboxes_created"] == 1
    assert any("echo hello-from-e2b" in cmd for cmd in out["commands"])
    assert out["backend_stamp"] == "e2b"


def test_ephemeral_delegate_alias_shares_the_parent_sandbox_end_to_end(hermes_install):
    """Exercise Hermes' real alias, cache, provider, and E2B identity chain."""
    (hermes_install / "config.yaml").write_text(
        textwrap.dedent(
            """
            plugins:
              enabled:
                - e2b
            terminal:
              backend: e2b
              container_persistent: false
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    tests_dir = str(Path(__file__).resolve().parent)
    out = run_in_hermes(
        hermes_install,
        _FAKE_SDK_PREAMBLE.format(tests_dir=tests_dir)
        + """
from tools import terminal_tool
import json as _json

parent = "session:parent"
child = "subagent:child"
other = "session:unrelated"
terminal_tool.register_container_alias(child, parent)

parent_result = _json.loads(terminal_tool.terminal_tool(command="echo parent", task_id=parent))
child_result = _json.loads(terminal_tool.terminal_tool(command="echo child", task_id=child))
parent_env = terminal_tool.get_active_env(parent)
child_env = terminal_tool.get_active_env(child)

other_result = _json.loads(terminal_tool.terminal_tool(command="echo other", task_id=other))
other_env = terminal_tool.get_active_env(other)

result["exit_codes"] = [
    parent_result.get("exit_code"),
    child_result.get("exit_code"),
    other_result.get("exit_code"),
]
result["parent_child_same_env"] = parent_env is child_env
result["parent_child_same_sandbox"] = parent_env.sandbox_id == child_env.sandbox_id
result["other_isolated"] = other_env.sandbox_id != parent_env.sandbox_id
result["sandboxes_created"] = len(fake.create_calls)
""",
        env_extra={"E2B_API_KEY": "e2b_" + "0" * 40},
    )

    assert out["exit_codes"] == [0, 0, 0]
    assert out["parent_child_same_env"] is True
    assert out["parent_child_same_sandbox"] is True
    assert out["other_isolated"] is True
    assert out["sandboxes_created"] == 2


def test_the_factory_produces_the_plugin_environment(hermes_install):
    tests_dir = str(Path(__file__).resolve().parent)
    out = run_in_hermes(
        hermes_install,
        _FAKE_SDK_PREAMBLE.format(tests_dir=tests_dir)
        + """
from tools.terminal_tool import _create_environment, _get_env_config

config = _get_env_config()
env = _create_environment(
    env_type="e2b",
    image="",
    cwd=config["cwd"],
    timeout=config["timeout"],
    container_config={"container_persistent": True},
    task_id="default",
)
result["class_name"] = type(env).__name__
result["backend_stamp"] = env._hermes_backend_name
result["persistent"] = env._persistent
result["created_eagerly"] = len(fake.create_calls)
env.cleanup()
""",
        env_extra={"E2B_API_KEY": "e2b_" + "0" * 40},
    )
    assert out["class_name"] == "E2BEnvironment"
    assert out["backend_stamp"] == "e2b"
    assert out["persistent"] is True
    assert out["created_eagerly"] == 0, "the factory must not create a sandbox eagerly"


def test_hermes_core_is_unmodified(hermes_install):
    """The plugin must work against a stock Hermes checkout."""
    import subprocess as sp

    hermes_repo = Path(
        sp.run(
            [
                sys.executable,
                "-c",
                "import hermes_constants, os; print(os.path.dirname(hermes_constants.__file__))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    status = sp.run(
        ["git", "-C", str(hermes_repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        pytest.skip("Hermes is not installed from a git checkout")
    assert status.stdout.strip() == "", (
        "the Hermes checkout used for these tests has local modifications:\n" + status.stdout
    )
