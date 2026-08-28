"""Live E2B validation. Creates real, billable sandboxes.

Deselected by default (``-m 'not live'`` in pyproject). Run explicitly:

    E2B_API_KEY=... pytest -m live

Every sandbox created here is registered with the session tracker, force-killed
at the end of the run, and the account is then queried to prove none of this
plugin's sandboxes are left running.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.live


def _require_key() -> str:
    key = os.getenv("E2B_API_KEY", "").strip()
    if not key:
        pytest.skip("E2B_API_KEY is not set")
    return key


@pytest.fixture(scope="session")
def live_key():
    return _require_key()


#: Metadata key stamped on every sandbox a live run creates. Cleanup and the
#: final leak check discover sandboxes BY THIS KEY, not by locally recorded
#: ids, so a sandbox whose creation response was lost (created server-side,
#: never registered locally) is still found, killed, and proven gone.
RUN_METADATA_KEY = "hermes_e2b_live_run"


@pytest.fixture(scope="session")
def tracker(live_key):
    """Reclaims every sandbox this run creates, even untracked ones.

    Every sandbox carries ``{RUN_METADATA_KEY: <run id>}`` (injected by the
    ``make_env`` fixture through ``E2BSettings.metadata``). Teardown kills
    everything discoverable by that metadata — running or paused, recorded or
    not — and then re-queries to prove nothing carrying the run's stamp
    remains. Session-fixture finalization runs after test failures too.
    """
    from e2b import Sandbox, SandboxQuery

    run_id = uuid.uuid4().hex[:12]
    seen: set[str] = set()

    def _discover() -> dict[str, str]:
        found: dict[str, str] = {}
        paginator = Sandbox.list(
            query=SandboxQuery(metadata={RUN_METADATA_KEY: run_id}),
            limit=100,
            api_key=live_key,
        )
        # Sandbox.list returns running AND paused sandboxes by default.
        while paginator.has_next:
            for info in paginator.next_items():
                found[info.sandbox_id] = str(info.state)
        return found

    class _Tracker:
        run_metadata = {RUN_METADATA_KEY: run_id}

        def add(self, sandbox_id):
            if sandbox_id:
                seen.add(sandbox_id)

        def scope_suffix(self, name):
            return f"live-{run_id}-{name}"

        @property
        def ids(self):
            return set(seen)

    yield _Tracker()

    # -- teardown: discover by metadata, destroy, then prove it ---------
    from e2b import SandboxNotFoundException

    discovery_ok = True
    try:
        discovered = _discover()
    except Exception as exc:
        # Discovery failing must not stop cleanup of the ids we do know:
        # kill those, and let the final proof query below fail the run
        # loudly if the account cannot be verified at all.
        discovery_ok = False
        print(f"\n[live] run {run_id}: metadata discovery failed ({type(exc).__name__}: {exc})")
        discovered = {}
    print(f"\n[live] run {run_id}: locally recorded ids: {sorted(seen)}")
    print(f"[live] run {run_id}: discovered by run metadata: {discovered}")
    unrecorded = set(discovered) - seen
    if unrecorded:
        print(f"[live] run {run_id}: NEVER RECORDED LOCALLY (caught by metadata): {unrecorded}")

    # Self-validation of the metadata net: every locally recorded sandbox
    # that still EXISTS must also be discoverable by the run metadata. If one
    # exists but is invisible, the filter is broken and every "empty" proof
    # below would be vacuous — a green run must mean the net works, not that
    # it silently matched nothing.
    net_holes: dict[str, str] = {}
    if discovery_ok:
        for sandbox_id in sorted(seen - set(discovered)):
            try:
                info = Sandbox.get_info(sandbox_id, api_key=live_key)
                net_holes[sandbox_id] = str(info.state)
            except SandboxNotFoundException:
                pass  # truly gone — consistent with not being discovered
            except Exception as exc:
                print(f"[live] get_info {sandbox_id} raised {type(exc).__name__}: {exc}")
        if net_holes:
            print(f"[live] run {run_id}: EXIST BUT INVISIBLE TO DISCOVERY: {net_holes}")

    for sandbox_id in sorted(set(discovered) | seen):
        try:
            # Class-level kill returns False (no raise) when already gone.
            killed = Sandbox.kill(sandbox_id, api_key=live_key)
            print(f"[live] kill {sandbox_id}: {killed}")
        except Exception as exc:
            print(f"[live] kill {sandbox_id} raised {type(exc).__name__}: {exc}")

    remaining = _discover()
    print(f"[live] run {run_id}: remaining with run metadata after cleanup: {remaining}")
    for sandbox_id in sorted(remaining):
        # Reclaim before failing: the assert below reports the leak; it must
        # not also leave the sandbox billing.
        try:
            print(f"[live] late kill {sandbox_id}: {Sandbox.kill(sandbox_id, api_key=live_key)}")
        except Exception as exc:
            print(f"[live] late kill {sandbox_id} raised {type(exc).__name__}: {exc}")
    assert remaining == {}, f"live run leaked sandboxes: {remaining}"
    assert not net_holes, (
        f"sandboxes existed but were invisible to metadata discovery — the "
        f"reclamation net is broken: {net_holes}"
    )


@pytest.fixture
def make_env(tracker, live_key):
    """Build an environment whose sandbox is stamped with the run metadata."""
    from hermes_plugin_e2b.config import E2BSettings
    from hermes_plugin_e2b.environment import E2BEnvironment

    built = []

    def _make(
        name,
        persistent=True,
        lease_seconds=90,
        timeout=60,
        terminal_lifetime_seconds=300,
    ):
        env = E2BEnvironment(
            task_id=tracker.scope_suffix(name),
            settings=E2BSettings(
                lease_seconds=lease_seconds,
                terminal_lifetime_seconds=terminal_lifetime_seconds,
                metadata=dict(tracker.run_metadata),
            ),
            timeout=timeout,
            persistent_filesystem=persistent,
        )
        built.append(env)
        return env

    yield _make

    for env in built:
        try:
            tracker.add(env.sandbox_id)
            env.cleanup()
            env.wait_for_cleanup(timeout=60)
        except Exception as exc:
            print(f"[live] cleanup raised: {type(exc).__name__}: {exc}")
        finally:
            tracker.add(env.sandbox_id)


# ---------------------------------------------------------------------------


def test_commands_really_run_inside_e2b(make_env, tracker):
    env = make_env("exec")
    marker = f"hermes-e2b-origin-{uuid.uuid4().hex}"
    result = env.execute(
        f"uname -a && id -un && pwd && echo ok > /tmp/{marker} && cat /tmp/{marker}",
        timeout=60,
    )
    tracker.add(env.sandbox_id)

    assert result["returncode"] == 0, result
    assert "Linux" in result["output"], result["output"]
    assert "ok" in result["output"], result["output"]
    # The command created a uniquely named file. Its absence on this host
    # proves the command ran somewhere else, whatever OS the host runs.
    assert not os.path.exists(f"/tmp/{marker}"), (
        "the marker file exists on this host — the command ran locally"
    )


def test_the_sandbox_is_discoverable_by_its_metadata(make_env, tracker, live_key):
    from e2b import Sandbox, SandboxQuery
    from hermes_plugin_e2b.sandbox import discovery_filter, scope_id

    env = make_env("discover")
    env.execute("true", timeout=60)
    tracker.add(env.sandbox_id)

    scope = scope_id(env._task_id, env._settings, persistent=env._persistent)
    paginator = Sandbox.list(
        query=SandboxQuery(metadata=discovery_filter(scope)),
        limit=10,
        api_key=live_key,
    )
    found = [info.sandbox_id for info in paginator.next_items()]
    assert env.sandbox_id in found, found


def test_nonzero_exit_and_stderr_come_back(make_env, tracker):
    env = make_env("exit")
    result = env.execute("echo to-stderr >&2; exit 3", timeout=60)
    tracker.add(env.sandbox_id)

    assert result["returncode"] == 3
    assert "to-stderr" in result["output"]


def test_large_output_streams_without_unbounded_host_memory(make_env, tracker):
    env = make_env("stream")
    tracker.add(env.sandbox_id)
    # ~5 MB of output; Hermes' bounded capture keeps a head/tail window.
    result = env.execute(
        "head -c 5000000 /dev/zero | tr '\\0' 'x'", timeout=120, bounded_capture=True
    )

    assert result["returncode"] == 0
    assert len(result["output"]) < 1_000_000, len(result["output"])
    assert result.get("output_total_chars", 0) >= 4_000_000


def test_a_long_command_outlives_the_baseline_lease(make_env, tracker, live_key):
    """The lease must be extended, or E2B's timeout action fires mid-command."""
    from e2b import Sandbox

    env = make_env("lease", lease_seconds=60)
    env.execute("true", timeout=30)
    tracker.add(env.sandbox_id)

    started = time.monotonic()
    result = env.execute("sleep 75 && echo survived", timeout=120)
    elapsed = time.monotonic() - started

    assert result["returncode"] == 0, result
    assert "survived" in result["output"]
    assert elapsed >= 70, elapsed
    assert Sandbox.get_info(env.sandbox_id, api_key=live_key).state


def test_cancellation_kills_the_command_and_keeps_the_sandbox(make_env, tracker):
    env = make_env("cancel")
    env.execute("true", timeout=30)
    tracker.add(env.sandbox_id)

    result = env.execute("sleep 120", timeout=8)
    assert result["returncode"] == 124, result

    after = env.execute("echo still-here", timeout=60)
    assert after["returncode"] == 0
    assert "still-here" in after["output"]


def test_a_persistent_sandbox_is_reused_and_keeps_its_filesystem(make_env, tracker):
    marker = f"live-marker-{uuid.uuid4().hex[:8]}"

    first = make_env("persist")
    first.execute(f"echo {marker} > /home/user/marker.txt", timeout=60)
    first_id = first.sandbox_id
    tracker.add(first_id)
    first.cleanup()
    first.wait_for_cleanup(timeout=60)

    second = make_env("persist")
    result = second.execute("cat /home/user/marker.txt", timeout=60)
    tracker.add(second.sandbox_id)

    assert second.sandbox_id == first_id, "the persistent sandbox was not reused"
    assert marker in result["output"], result["output"]


#: The plugin's minimum lease (``E2BSettings.lease_for`` floors at 60s), so the
#: shortest wait in which E2B's own ``on_timeout`` action can fire.
_MIN_LEASE_SECONDS = 60
#: How long to wait for that to happen, from the last lease renewal. Observed
#: around 54s; this is a bound, not an expectation, so a regression in E2B's
#: timeout handling fails the test instead of hanging it.
_PAUSE_DEADLINE_SECONDS = 180
_PAUSE_POLL_SECONDS = 5


def _wait_for_natural_pause(sandbox_id: str, live_key: str) -> list[str]:
    """Wait for E2B's on-timeout lifecycle and return the observed states."""
    from e2b import Sandbox, SandboxNotFoundException

    started = time.monotonic()
    observed: list[str] = []
    deadline = started + _PAUSE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - started
        try:
            state = str(
                getattr(Sandbox.get_info(sandbox_id, api_key=live_key), "state", "")
            ).lower()
        except SandboxNotFoundException:
            observed.append(f"{elapsed:.0f}s gone")
            break
        if not observed or not observed[-1].endswith(state):
            observed.append(f"{elapsed:.0f}s {state}")
        if "paused" in state:
            return observed
        time.sleep(_PAUSE_POLL_SECONDS)

    pytest.fail(
        f"sandbox {sandbox_id} did not reach 'paused' within "
        f"{_PAUSE_DEADLINE_SECONDS}s of a {_MIN_LEASE_SECONDS}s lease. Observed "
        f"state sequence: {', '.join(observed) or '(no state ever read)'}. The "
        "sandbox was created with on_timeout={'action': 'pause', "
        "'keep_memory': False}, so either that lifecycle was not applied or the "
        "lease was extended past the last command."
    )


def _wait_until_gone(sandbox_id: str, live_key: str, *, timeout: int = 240) -> list[str]:
    """Wait until E2B's kill lifecycle removes a sandbox."""
    from e2b import Sandbox, SandboxNotFoundException

    started = time.monotonic()
    observed: list[str] = []
    deadline = started + timeout
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - started
        try:
            state = str(
                getattr(Sandbox.get_info(sandbox_id, api_key=live_key), "state", "")
            ).lower()
        except SandboxNotFoundException:
            observed.append(f"{elapsed:.0f}s gone")
            return observed
        if not observed or not observed[-1].endswith(state):
            observed.append(f"{elapsed:.0f}s {state}")
        time.sleep(_PAUSE_POLL_SECONDS)

    pytest.fail(
        f"sandbox {sandbox_id} still existed after {timeout}s. Observed state "
        f"sequence: {', '.join(observed) or '(no state ever read)'}"
    )


def test_a_sandbox_paused_by_its_own_lease_is_resumed_with_its_filesystem_intact(
    make_env, tracker, live_key
):
    """The core persistence claim, through the lifecycle the plugin actually uses.

    Production never pauses a sandbox: the plugin leaves persistent sandboxes
    alive and relies on the ``lifecycle={"on_timeout": {"action": "pause",
    "keep_memory": False}}`` it created them with. So this waits out a real
    lease rather than calling ``Sandbox.pause()`` — which was what this test did
    before, and which was found to report a pause as accepted while the sandbox
    stayed running for over 45s. That is E2B's explicit-pause path, not the
    plugin's, and asserting through it tested a contract the plugin does not
    depend on.

    The lease is the plugin's 60s minimum and the command is short enough not to
    extend it: ``lease_for`` raises the lease to ``timeout + grace`` when that
    exceeds the baseline, so a 60s command would have pushed the lease to 90s.
    """
    marker = f"lease-marker-{uuid.uuid4().hex[:8]}"

    first = make_env("resume", lease_seconds=_MIN_LEASE_SECONDS, timeout=20)
    first.execute(f"echo {marker} > /home/user/paused.txt", timeout=20)
    sandbox_id = first.sandbox_id
    tracker.add(sandbox_id)

    # Wait for E2B to pause it on its own, recording every state change so a
    # timeout says what actually happened instead of just "not paused". Keep
    # the first environment attached: teardown now reconnects before its state
    # pull by design, so cleaning it first would correctly renew the lease.
    observed = _wait_for_natural_pause(sandbox_id, live_key)

    print(f"[live] sandbox {sandbox_id} state sequence: {', '.join(observed)}")

    # A distinct environment object takes the real discovery/class-connect
    # path while the first still owns the state-sync lease. It resumes the
    # paused sandbox as a reader and must retain the fresh SDK object returned
    # by E2B before touching the data plane.
    second = make_env("resume", lease_seconds=_MIN_LEASE_SECONDS, timeout=120)
    result = second.execute("cat /home/user/paused.txt", timeout=120)
    tracker.add(second.sandbox_id)

    assert second.sandbox_id == sandbox_id, "the paused sandbox was not resumed"
    assert marker in result["output"], result["output"]


def test_the_same_environment_replaces_its_sdk_client_after_a_natural_pause(
    make_env, tracker, live_key
):
    """Acceptance: same environment, same sandbox/filesystem, fresh SDK client."""
    marker = f"same-object-marker-{uuid.uuid4().hex[:8]}"
    env = make_env("same-object-resume", lease_seconds=_MIN_LEASE_SECONDS, timeout=20)
    env.execute(f"echo {marker} > /home/user/same-object.txt", timeout=20)
    sandbox_id = env.sandbox_id
    stale_client = env._sandbox
    tracker.add(sandbox_id)

    observed = _wait_for_natural_pause(sandbox_id, live_key)
    print(f"[live] sandbox {sandbox_id} same-object sequence: {', '.join(observed)}")

    result = env.execute("cat /home/user/same-object.txt", timeout=20)

    assert env.sandbox_id == sandbox_id
    assert env._sandbox is not stale_client, "resume retained stale envd connection metadata"
    assert marker in result["output"], result["output"]


def test_naturally_paused_writer_reconnects_and_pulls_state_on_teardown(
    make_env, tracker, live_key, tmp_path, monkeypatch
):
    """Acceptance: teardown resumes a paused writer before its final pull."""
    from e2b import Sandbox

    home = tmp_path / "hermes-home"
    skill = home / "skills" / "live" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# host baseline\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env("paused-teardown", lease_seconds=_MIN_LEASE_SECONDS, timeout=20)
    env.execute(
        f"printf '# authored in paused sandbox\\n' > {env.remote_hermes_home}/skills/live/SKILL.md",
        timeout=20,
    )
    sandbox_id = env.sandbox_id
    tracker.add(sandbox_id)

    observed = _wait_for_natural_pause(sandbox_id, live_key)
    print(f"[live] sandbox {sandbox_id} teardown sequence: {', '.join(observed)}")

    env.cleanup()
    assert env.wait_for_cleanup(timeout=120) is True

    assert skill.read_text(encoding="utf-8") == "# authored in paused sandbox\n"
    assert Sandbox.get_info(sandbox_id, api_key=live_key).sandbox_id == sandbox_id


def test_ephemeral_reaper_window_precedes_cleanup_and_kill_backstops(
    make_env, tracker, live_key, tmp_path, monkeypatch
):
    """Acceptance: normal reaping wins, while an abandoned sandbox self-destructs."""
    from e2b import Sandbox, SandboxNotFoundException

    home = tmp_path / "hermes-home"
    skill = home / "skills" / "live" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# host baseline\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env(
        "ephemeral-reaper",
        persistent=False,
        lease_seconds=_MIN_LEASE_SECONDS,
        timeout=20,
        terminal_lifetime_seconds=0,
    )
    env.execute(
        f"printf '# pulled before explicit kill\\n' > {env.remote_hermes_home}/skills/live/SKILL.md",
        timeout=20,
    )
    sandbox_id = env.sandbox_id
    tracker.add(sandbox_id)
    assert env._desired_lease(None) == 120

    # Simulate Hermes' worst-case idle-reaper admission: lifetime is zero and
    # the reaper checks once per minute. The sandbox's 120s kill backstop must
    # still leave enough time for cleanup to renew, pull, and explicitly kill.
    time.sleep(65)
    env.cleanup()
    assert env.wait_for_cleanup(timeout=120) is True
    assert skill.read_text(encoding="utf-8") == "# pulled before explicit kill\n"
    with pytest.raises(SandboxNotFoundException):
        Sandbox.get_info(sandbox_id, api_key=live_key)

    abandoned = make_env(
        "ephemeral-abandoned",
        persistent=False,
        lease_seconds=_MIN_LEASE_SECONDS,
        timeout=20,
        terminal_lifetime_seconds=0,
    )
    abandoned._ensure_ready()
    abandoned_id = abandoned.sandbox_id
    tracker.add(abandoned_id)
    assert abandoned._desired_lease(None) == 120

    observed = _wait_until_gone(abandoned_id, live_key)
    print(f"[live] sandbox {abandoned_id} abandoned sequence: {', '.join(observed)}")


def test_persistent_cleanup_leaves_the_sandbox_alive(make_env, tracker, live_key):
    from e2b import Sandbox

    env = make_env("nodestroy")
    env.execute("true", timeout=60)
    sandbox_id = env.sandbox_id
    tracker.add(sandbox_id)

    env.cleanup()
    env.wait_for_cleanup(timeout=60)

    info = Sandbox.get_info(sandbox_id, api_key=live_key)
    assert str(info.state) in {"running", "SandboxState.RUNNING"}, info.state


def test_ephemeral_sessions_are_isolated_and_destroyed(make_env, tracker, live_key):
    from e2b import Sandbox, SandboxNotFoundException

    one = make_env("ephem", persistent=False)
    two = make_env("ephem", persistent=False)
    one.execute("echo one > /home/user/only-mine.txt", timeout=60)
    two.execute("true", timeout=60)
    tracker.add(one.sandbox_id)
    tracker.add(two.sandbox_id)

    assert one.sandbox_id != two.sandbox_id

    missing = two.execute("cat /home/user/only-mine.txt", timeout=60)
    assert missing["returncode"] != 0, "ephemeral sandboxes shared a filesystem"

    killed_id = one.sandbox_id
    one.cleanup()
    one.wait_for_cleanup(timeout=60)

    # The exact SDK contract for a killed sandbox: the API answers 404 and the
    # SDK raises SandboxNotFoundException (e2b 2.46.0 sandbox_sync/sandbox_api.py
    # — _cls_get_info). Anything broader would let auth, network, and SDK
    # failures satisfy this assertion.
    with pytest.raises(SandboxNotFoundException):
        Sandbox.get_info(killed_id, api_key=live_key)

    assert two.execute("echo alive", timeout=60)["returncode"] == 0


def test_the_api_key_is_not_visible_inside_the_sandbox(make_env, tracker, live_key):
    env = make_env("secret")
    tracker.add(env.sandbox_id)

    result = env.execute(
        "env | grep -c E2B_API_KEY; grep -rl E2B_API_KEY /home/user 2>/dev/null | head",
        timeout=60,
    )
    assert live_key not in result["output"]
    assert result["output"].strip().splitlines()[0].strip() == "0", result["output"]


def test_host_state_is_readable_inside_the_sandbox(make_env, tracker, tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "skills" / "live").mkdir(parents=True)
    (home / "skills" / "live" / "SKILL.md").write_text("# live skill\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env("sync")
    tracker.add(env.sandbox_id)
    result = env.execute(f"cat {env.remote_hermes_home}/skills/live/SKILL.md", timeout=60)
    assert "live skill" in result["output"], result["output"]


def test_sandbox_authored_state_is_pulled_back_on_teardown(
    make_env, tracker, tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    (home / "skills" / "live").mkdir(parents=True)
    (home / "skills" / "live" / "SKILL.md").write_text("# from host\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env("syncback", persistent=False)
    tracker.add(env.sandbox_id)
    env.execute(
        f"printf '# authored in the sandbox\\n' > {env.remote_hermes_home}/skills/live/SKILL.md",
        timeout=60,
    )
    env.cleanup()
    env.wait_for_cleanup(timeout=120)

    assert "authored in the sandbox" in (home / "skills" / "live" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_stdin_reaches_the_command_over_the_stdin_channel(make_env, tracker):
    """The payload must arrive without ever appearing in the command line."""
    env = make_env("stdin")
    tracker.add(env.sandbox_id)

    secret_ish = "correct-horse-battery-staple"
    result = env.execute("cat", timeout=60, stdin_data=f"{secret_ish}\n")

    assert result["returncode"] == 0, result
    assert secret_ish in result["output"], result["output"]
    assert "stdin_error" not in result, result

    # Nothing wrote the payload into a process argument list.
    leaked = env.execute(f"grep -rl {secret_ish} /proc/*/cmdline 2>/dev/null | head -1", timeout=60)
    assert secret_ish not in leaked["output"], leaked["output"]


def test_output_produced_after_a_timeout_cannot_wedge_the_environment(make_env, tracker):
    """A killed command that keeps streaming must not park the worker thread.

    The worker is what releases the environment's in-flight count, so a parked
    one would defer cleanup forever and leak the sandbox.
    """
    env = make_env("chatty", persistent=False)
    env.execute("true", timeout=30)
    tracker.add(env.sandbox_id)

    result = env.execute(
        "yes 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'", timeout=6, bounded_capture=True
    )
    assert result["returncode"] == 124, result

    env.cleanup()
    assert env.wait_for_cleanup(timeout=90) is True, "teardown never settled"


def test_repeated_cleanup_is_safe(make_env, tracker):
    env = make_env("idempotent", persistent=False)
    env.execute("true", timeout=60)
    tracker.add(env.sandbox_id)

    env.cleanup()
    env.cleanup()
    assert env.wait_for_cleanup(timeout=60) is True


def test_cleanup_during_a_live_command_defers(make_env, tracker):
    env = make_env("defer", persistent=False)
    env.execute("true", timeout=30)
    tracker.add(env.sandbox_id)

    outcome = {}

    def _run():
        outcome["result"] = env.execute("sleep 6 && echo finished", timeout=60)

    worker = threading.Thread(target=_run)
    worker.start()
    time.sleep(2)
    env.cleanup()
    worker.join(timeout=90)

    assert outcome["result"]["returncode"] == 0, outcome
    assert "finished" in outcome["result"]["output"]
    assert env.wait_for_cleanup(timeout=90) is True
