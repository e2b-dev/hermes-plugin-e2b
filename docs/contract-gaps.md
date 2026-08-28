# Contract gaps in the Hermes terminal-backend plugin API

Findings from building this plugin against
`NousResearch/hermes-agent` `main` @ `1bbb6e5bce56e721ab685af4cd87df21bbff4d35`
with `e2b` 2.46.0, re-verified against `main` @
`e60983a69730c058ce772829df3273aee6de3889`.

Every gap below was re-read against that tree rather than re-hashed: none has
been fixed upstream, and the code references are current for it. Consumer
files have moved a lot across the range (~3,500 commits), so line numbers
changed even where behaviour did not.

The *declarative* contract this plugin is built on did not move at all.
`agent/terminal_env_provider.py` (the provider ABC),
`agent/terminal_env_registry.py`, `tools/environments/file_sync.py`, and
`website/docs/developer-guide/terminal-environment-plugin.md` are byte-identical
at `1bbb6e5b` and `e60983a6`. `tools/environments/base.py` is the one contract
file that changed, additively: it gained an `is_local` class attribute that
defaults to `False`, which is already the correct value for a remote backend.

Every item is something the plugin either works around in the open or accepts
as a documented limitation. None of them is patched in Hermes core — per
`CONTRIBUTING.md`, "If your plugin needs a capability the framework doesn't
expose, that's a feature request to **widen the generic plugin surface** (a new
hook or `ctx` method) — never special-case your plugin in core."

Each gap lists the smallest generic seam that would close it.

---

## 1. The documented `execute()` return shape is wrong

**Docs** — `website/docs/developer-guide/terminal-environment-plugin.md:48` and
`:126` show `execute()` returning `{"output": str, "exit_code": int}`.

**Code** — every core consumer reads `returncode`:
`tools/terminal_tool.py:3645`, `tools/file_operations.py:996`,
`tools/process_registry.py:1342`, `tools/tool_result_storage.py:190`,
`tools/code_execution_tool.py:1350`. `exit_code` appears only in the JSON the
terminal tool emits to the model.

**Impact** — a plugin that follows the documented example reports **every
failing command as a success**, silently. This is the highest-severity item
here because it is invisible: the command runs, the output looks right, and the
exit code is always 0.

**Seam** — a documentation fix. `hermes plugins doctor` could also warn when a
returned environment's `execute()` yields a dict with `exit_code` and no
`returncode`.

**What this plugin does** — subclasses `BaseEnvironment`, whose `execute()`
already returns `returncode`. Asserted in
`tests/test_execution.py::test_execute_reports_returncode_not_exit_code`.

---

## 2. `skip_container_guards` is declared but never read

**Docs** — the surface table promises "Dangerous-command approval skipping |
`skip_container_guards`", and the ABC documents it as
"the sandbox is isolated enough that dangerous-command approval prompts are
skipped".

**Code** — `tools/approval.py:3989` `_should_skip_container_guards()` matches a
hardcoded tuple of built-in backend names (`singularity`, `modal`, `daytona`,
`vercel_sandbox`, plus `docker` when it has no host bind-mounts). A
repository-wide search for `provider_flag(..., "skip_container_guards")` still
returns nothing. The predicate has since grown a `has_host_access` parameter to
handle the Docker bind-mount case, which shows the question is being refined —
but only ever for built-in names.

**Impact** — a plugin sandbox is treated as *not* disposable, so the approval
layer stays on for dangerous commands. This is fail-safe, not fail-open, so it
is a UX gap rather than a security hole: users of a plugin backend get approval
prompts that an equivalent built-in backend would not raise.

**Seam** — route `_should_skip_container_guards` through
`terminal_env_registry.provider_flag(env_type, "skip_container_guards", …)`
for non-built-in names, the same way `_is_container_backend` already does.

**What this plugin does** — resolves the flag to `True` (the ABC defaults
`skip_container_guards` to `is_container`, which this provider declares), so
the behaviour arrives for free if core wires it up, and documents that approval
prompts currently still appear. Pinned by `tests/test_plugin_contract.py`.

---

## 3. Persistent scope is profile-scoped for Docker only

**Code** — `tools/terminal_tool.py:1458` `_resolve_container_task_id` scopes
the persistent environment cache key by *profile* only when
`_docker_persistent_profile_scoped()` (`:1407`) is true, which returns early
unless `TERMINAL_ENV == "docker"`. Every other backend — including all the
built-in remote ones — lands on `session:<session_key>`.

**Impact** — with `container_persistent: true`, a gateway or dashboard session
gets its own E2B sandbox rather than sharing one per profile the way persistent
Docker does. CLI runs (no session key) do share one, so CLI persistence works
as expected.

**Seam** — a provider-level declaration, e.g.
`persistent_scope: Literal["task", "profile"] = "task"`, consulted in
`_resolve_container_task_id` alongside the existing
`session_isolated_when_nonpersistent`. That flag already covers the ephemeral
half of the same question; this is the persistent half.

**What this plugin does** — follows core's key exactly rather than inventing a
wider scope. The abandoned in-tree implementation instead added
`if os.getenv("TERMINAL_ENV") == "e2b"` inside `_resolve_container_task_id`,
which a plugin cannot do — and which is what forced it into an
"N environments, one sandbox" ownership problem.

---

## 4. `strip_env_keys` is not applied on the local backend's foreground spawn

**Docs** — "listing it strips it from every spawned subprocess
unconditionally".

**Code** — `tools/environments/local.py:_sanitize_subprocess_env` (terminal /
`execute_code`) and `hermes_subprocess_env` (browser, CLI executors, …) both
apply `_plugin_terminal_env_strip_keys()` (lines 598 and 757). `_make_run_env`
(line 1414), which builds the environment for `_LocalEnvironment._run_bash` at
line 1948, does not: it filters `_HERMES_PROVIDER_ENV_BLOCKLIST` and Hermes'
own internal secrets, and a plugin's declared key is neither.

**Reproduced** against `e60983a6` with this plugin registered:

```
registered strip keys: ['E2B_API_KEY']
sanitize_subprocess_env strips it: True
hermes_subprocess_env strips it: True
_make_run_env strips it: False
```

**Impact** — when `terminal.backend` is `local` while `E2B_API_KEY` is present
in the process environment, a model-authored foreground command can read it.
Selecting the `e2b` backend is unaffected: those commands run inside the
sandbox, and the key is never sent there.

**Seam** — apply `_plugin_terminal_env_strip_keys()` in `_make_run_env`, next
to the existing `_HERMES_PROVIDER_ENV_BLOCKLIST` filter.

**What this plugin does** — declares `strip_env_keys` and documents the
residual local-backend exposure in the README's security section. There is
nothing a plugin can do about a core spawn site.

---

## 5. Core builds an environment for the prompt probe and never reaps it

**Code** — `agent/prompt_builder.py:1167` `_probe_remote_backend` calls
`_create_environment(..., task_id="prompt-backend-probe")` directly (`:1246`),
runs one `uname` command, and drops the object. It is never registered in
`_active_environments`, so the idle reaper never sees it; only CPython
refcounting reaching `BaseEnvironment.__del__` cleans it up.

**Impact** — for a billed cloud sandbox, that is one sandbox creation per
Hermes process at system-prompt build time. It affects every remote backend
equally (Modal, Daytona, Vercel), not just this one.

**Seam** — either register the probe environment so the reaper owns it, or add
a lightweight `provider.probe_environment_description()` that lets a backend
answer without a sandbox. `env_description` already exists as the *fallback*
when the live probe fails, so a "prefer the declaration" switch would be a
small change.

**What this plugin does** — recognises the probe `task_id` and gives it an
ephemeral sandbox with a short, `on_timeout: kill` lease, so it self-destructs
even if `__del__` never runs. If core renames the id the predicate simply stops
matching and the probe falls back to a normal environment.

---

## 6. `FileSyncManager` reports neither success nor failure

**Code** — `tools/environments/file_sync.py` is byte-identical across the whole
verification range, so every reference in this section and in gap 7 is current.
`sync()` and `sync_back()` both return `None`. `sync()` catches every
exception, rolls its state back and logs `"file_sync: sync failed, rolled back
state"` (`file_sync.py:241`); `sync_back()` retries three times and logs
`"sync_back: all N attempts failed"` (`file_sync.py:289`). Neither re-raises.

**Impact** — a backend cannot tell a completed sync from a failed one, so
wrapping the call in `try/except` produces dead code that looks like error
handling. The consequences are not cosmetic: a failed initial upload gives the
agent a sandbox with none of its skills or credentials, and a failed teardown
pull means work done inside the sandbox is silently overwritten by the host on
the next resume. Every in-tree remote backend has the same blind spot.

**Seam** — return a bool (or raise) from both. `sync()` already distinguishes
the outcomes internally; only the return value is missing.

**What this plugin does** — recovers the signal from two sides: its own
transport callbacks record the real exception on the way past (attributed to
the innermost in-flight call on the *emitting thread*, via a permanently
installed log handler and a thread-local watch stack), and records from the
manager's logger are classified against the manager's known messages — only
`"file_sync: sync failed, rolled back state"`, `"sync_back: all N attempts
failed"`, and the tar-cap skip are terminal; per-attempt retry warnings and
last-write-wins conflict notices are part of a successful call. Verdicts are
per call type: `sync()` has no internal retries, so any transport error is
final; `sync_back()` retries internally, so it failed only when the manager
said so or every attempt in its retry budget failed in transport. A failed
initial upload is fatal, a failed incremental sync aborts the command
(fail-closed — the pending change can be a credential deletion), a failed pull
is an error, and the next resume recovers what the sandbox still holds.

**Precise constraints of this detection** — it depends on (a) the manager's
log message wording (matched by narrow prefixes, not broad severity), and
(b) the manager's log records being *created* at all. For (b), the plugin
floors the `tools.environments.file_sync` logger's level to WARNING for the
duration of each watched call (restoring the previous level afterwards), so
a deployment that raises that logger's or the root's level cannot blind the
detection. What it cannot counteract is a process-global
`logging.disable(logging.WARNING)` or higher — record creation is then
suppressed before any level is consulted. Transport-callback errors remain
the text- and level-independent signal, and they cover every transport
failure; only host-side post-download failures (tar extraction, applying
files) are log-only. All of these constraints disappear the moment
`sync()`/`sync_back()` return a result.

---

## 7. `sync_back()`'s cross-process lock is not reentrant, and `__del__` can re-enter it

**Code** — `_sync_back_locked` (`file_sync.py:336`) takes an exclusive `flock`
on `~/.hermes/.sync.lock`. `BaseEnvironment.__del__` calls `cleanup()`
(`base.py:1540`), and every remote backend's `cleanup()` calls `sync_back()`.

**Impact** — when the garbage collector finalises an unreferenced environment
while another environment's pull is in flight on the same thread, the nested
pull opens a second descriptor for the lock file and blocks on a lock that same
thread already holds. `flock` is per-descriptor, so it never resolves: the
process deadlocks. Reproduced here from an ordinary test run. It applies equally
to the in-tree Daytona, Modal, SSH, and Vercel backends — environments become
unreferenced routinely (idle reaping, and the prompt probe's environment is
dropped immediately after its one command).

**Seam** — do not do lock-taking, network-heavy work from `__del__`, or make
the re-entrant case a no-op.

**What this plugin does** — finalisation releases resources and never syncs,
backed by a thread-local guard that turns any nested pull into a no-op.

---

## 8. No streaming `ProcessHandle` is exported

**Code** — `tools/environments/base._ThreadedProcessHandle` (`base.py:488`)
accepts only a blocking `exec_fn() -> (output, exit_code)` and writes the whole
result into the pipe after the command finishes. A streaming variant (`stream_exec_fn`)
was written for the in-tree E2B work but is a core change and never landed.

**Impact** — every plugin backend whose SDK delivers output incrementally has
to reimplement the `ProcessHandle` duck type to get live output and bounded
memory.

**Seam** — the `stream_exec_fn` parameter on `_ThreadedProcessHandle`, or a
public `tools.environments.StreamingProcessHandle`.

**What this plugin does** — ships its own (`process.py`).

---

## 9. Execute-time `EnvironmentConnectionError` is not rendered as degraded and does not evict

**Code** — the terminal tool's degraded handler
(`tools/terminal_tool.py:3854`, `except EnvironmentConnectionError` after the
tool body) renders `status: "degraded"` with the retry hint and evicts the
cached environment via `_evict_environment_for_task` (`:3908`). But it is only
reachable for errors raised *outside* the foreground execution loop — in
practice, from `_create_environment`. An `EnvironmentConnectionError` raised by
`env.execute()` on a cached environment is caught first by the loop's blanket
`except Exception` (`:3591`): the command is retried up to three times (2s/4s/8s
backoff) against the same environment object, then reported as a generic
`{"exit_code": -1, "error": "Command execution failed: …"}` (`:3615`) — no
`status` field, no `retry_hint`, no eviction. If the error text happens to
contain "timeout" (an SDK `ReadTimeout`, say), the same handler misreports it as
a command timeout with exit code 124.

**Impact** — a backend that attaches lazily (this plugin: the sandbox is
created/resumed on first `execute()`) never gets the degraded rendering or the
eviction for connection failures; the model sees a generic error without the
retry hint. The blind retries are actually helpful for transient bring-up and
sync failures — each retry re-enters `_before_execute` — but a permanently
broken environment stays cached until the idle reaper.

**Seam** — catch `EnvironmentConnectionError` explicitly inside the foreground
loop and route it to the existing degraded handler (or re-raise past the
blanket handler).

**What this plugin does** — documents the real behaviour (errors.py module
docstring, README troubleshooting) and keeps its fail-closed raises: the
command still never runs against a broken or unverified backend, which is the
property that matters.

---

## 10. Smaller items

| Gap | Evidence | Effect here |
|---|---|---|
| `image`, `ssh_config`, `local_config`, `host_cwd` are not forwarded to plugin factories | `tools/terminal_tool.py:2137` passes only `cwd`, `timeout`, `task_id`, `image`, `container_config`, and `image` is always `""` for plugin backends (`:2308`). `_create_environment` itself now also takes `host_cwd`, but does not pass it on | The E2B template comes from `plugins.entries.e2b.settings.template` instead of a `terminal.*` key |
| `terminal.lifetime_seconds` is not in `container_config` | `_container_config_from_config` (`:1921`) omits it | Read from the `TERMINAL_LIFETIME_SECONDS` env var Hermes bridges |
| Persistence is read off a private attribute | `is_persistent_env` reads `env._persistent` (`:2380`) | The environment sets `_persistent` |
| `code_execution_tool.check_sandbox_requirements` never calls `provider.check_requirements()` | `tools/code_execution_tool.py:356` — it special-cases `vercel_sandbox` only | `execute_code` can report available when the backend is not configured |
| `hermes plugins install` rejects `manifest_version > 1` while the loader supports 2 | `hermes_cli/plugins_cmd.py:145` (rejected at `:817`) vs `hermes_cli/plugins.py:728` | This plugin ships a v1 manifest; `config_schema` and `python_dependencies` are unusable via the installer |
| `file_tools._terminal_env_type_for_task` sniffs class names before reading `_hermes_backend_name` | `tools/file_tools.py:175` | The environment class must avoid the substrings `local`, `ssh`, `docker`, `singularity`, `modal`, `daytona` |
| A plugin whose `__init__.py` mentions `MemoryProvider` in its first 8 KB is classified `exclusive` and never loads | `hermes_cli/plugins.py:997` | Only applies when `kind` is absent; this manifest sets `kind: backend` |
| `sync_back()` cannot map a remote file in a directory the host does not already sync | `_infer_host_path` (`file_sync.py:454`) infers a host path only by matching the *parent* of an existing mapping entry, so a file the agent creates in a brand-new subdirectory is logged as "no host mapping" and dropped | Sandbox-authored files in new subdirectories of `skills/`/`memories/` reach the host through this plugin's resume recovery rather than the teardown pull. Pinned by a characterisation test |

---

## Non-gaps worth recording

These looked like gaps and are not:

- **`cleanup_vm(task_id)` uses the raw id.** `run_agent.py:4645` passes the raw
  session id while the environment is cached under the resolved container key,
  so session-close teardown can miss. It does not matter here: persistent
  teardown is a no-op by design, and ephemeral environments are reclaimed by
  the idle reaper and `atexit`.
- **File tools need filesystem methods on the environment.** They do not.
  `ShellFileOperations` drives everything through `execute()` as POSIX shell,
  so a backend that can run `bash` needs no `read_file`/`write_file` hooks.
- **`e2b` is a reserved backend name.** It is not, on `main`:
  `BUILTIN_BACKEND_NAMES` (`agent/terminal_env_registry.py:40`) holds `local`,
  `docker`, `singularity`, `modal`, `managed_modal`, `daytona`,
  `vercel_sandbox`, and `ssh`. The abandoned
  in-tree branch's final commit adds it to `BUILTIN_BACKEND_NAMES`; if that ever
  lands, `register_provider` raises and `PluginContext` swallows the error as a
  warning, so the plugin would silently not register. Worth watching.
