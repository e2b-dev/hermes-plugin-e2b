# hermes-plugin-e2b

Run [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s terminal,
file, and code-execution tools inside an [E2B](https://e2b.dev) cloud sandbox
instead of on the host machine.

Hermes itself keeps running wherever you started it. Every shell command the
agent issues, every file it reads or writes through the file tools, and every
`execute_code` call is dispatched into an E2B sandbox.

The plugin touches no Hermes core files. It registers through the standard
plugin entry point — `register(ctx)` calling
`ctx.register_terminal_environment_provider` — and everything else follows from
the provider's declared metadata: dispatch, the `hermes setup` backend picker,
`hermes doctor` / `hermes status`, the dashboard picker, remote path handling,
and secret stripping.

It is opt-in and never auto-selected.

---

## Install

1. **Install the E2B SDK** into the same interpreter that runs Hermes. Hermes
   never installs plugin dependencies for you.

   ```bash
   pip install 'e2b>=2.46,<3'
   ```

2. **Get an API key** at <https://e2b.dev/dashboard> and put it in your Hermes
   profile's env file:

   ```bash
   echo 'E2B_API_KEY=e2b_...' >> ~/.hermes/.env
   ```

3. **Install and enable the plugin:**

   ```bash
   hermes plugins install <owner>/hermes-plugin-e2b
   hermes plugins enable e2b
   hermes config set terminal.backend e2b
   ```

4. **Check it:**

   ```bash
   hermes doctor
   ```

To pin a version, install with an explicit commit:
`hermes plugins install <owner>/hermes-plugin-e2b --ref <40-char sha>`.

### Uninstall

```bash
hermes config set terminal.backend local
hermes plugins disable e2b
hermes plugins remove e2b
```

Disabling the plugin removes the backend from the registry. Any persistent
sandboxes stay in your E2B account until their lease expires and they pause;
delete them from the E2B dashboard if you want them gone immediately.

---

## Compatibility

| | |
|---|---|
| Hermes Agent | any build with pluggable terminal backends (PR #94400 or later). Verified against `main` @ `4faa721d` (originally built against `1bbb6e5b`; the plugin/provider/terminal contract files are byte-identical between the two). |
| E2B Python SDK | `>=2.46,<3`. Verified against 2.46.0 by reading the installed source. |
| Python | 3.11+ (Hermes' own floor) |

Hermes has no plugin API version handshake — [native plugins are protected by
behaviour, not a version
number](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins).
This plugin uses only documented `PluginContext` and provider-ABC surface, plus
`tools.environments.BaseEnvironment` and `FileSyncManager`, which the terminal
backend guide names explicitly.

The E2B floor is not conservative padding: it is the version whose
`Sandbox.create` / `Sandbox.list` / `lifecycle` behaviour was read from source
and relied on. Older releases may work; they have not been checked.

---

## Configuration

Backend selection uses the standard Hermes keys:

```yaml
terminal:
  backend: e2b
  container_persistent: true   # default
  lifetime_seconds: 300        # default; also the sandbox lease
  timeout: 180                 # default per-command timeout
```

Plugin-specific settings live in the plugin's own config namespace:

```yaml
plugins:
  enabled:
    - e2b
  entries:
    e2b:
      settings:
        template: base              # E2B template name or id
        cwd: /home/user             # starting directory in the sandbox
        lease_seconds: 300          # overrides terminal.lifetime_seconds
        command_grace_seconds: 30   # lease headroom beyond a command's timeout
        allow_internet_access: true # false puts the sandbox behind deny-all egress
        secure: true                # E2B envd access-token protection
        metadata:                   # extra metadata stamped on every sandbox
          team: infra
```

| Setting | Default | Meaning |
|---|---|---|
| `template` | `base` | E2B template name or id. Part of sandbox identity: a sandbox built from a different template is never adopted. |
| `cwd` | `/home/user` | Starting directory. Overridden by the sandbox's real `$HOME` when it differs. |
| `lease_seconds` | `terminal.lifetime_seconds`, else 300 | Baseline sandbox lease. Extended per command; never shortened. |
| `command_grace_seconds` | 30 | Extra lease beyond a command's timeout, so E2B's timeout action cannot fire mid-command. |
| `allow_internet_access` | `true` | `false` denies all egress from the sandbox. |
| `secure` | `true` | Keep E2B's envd access-token protection on. |
| `metadata` | `{}` | Merged into sandbox metadata. Identity keys always win. |

`terminal.image` and the `container_cpu` / `container_memory` / `container_disk`
resource keys are ignored: E2B sizes sandboxes by template, and Hermes does not
forward a per-backend image value to plugin backends (see
[contract gaps](docs/contract-gaps.md#10-smaller-items)).

---

## How it works

### Persistent and ephemeral modes

**`container_persistent: true` (default).** The sandbox is created with E2B's
`lifecycle` set to `{"on_timeout": {"action": "pause", "keep_memory": false}}`.
When its lease runs out, E2B pauses it and preserves the filesystem. The next
session finds it again and resumes it, so installed packages and working files
survive across Hermes restarts.

This plugin never pauses or kills a persistent sandbox. That is deliberate:
two Hermes processes attached to the same sandbox can never destroy it under
each other. If E2B has already paused the sandbox when Hermes begins teardown,
the writer reconnects immediately before every download attempt with a
transfer-sized lease, pulls state, and leaves E2B to pause it again. This
preserves Hermes' own retry policy without introducing a destructive cleanup
decision in either Hermes process.

**Concurrent sessions share one sandbox with a single state synchroniser.**
Two live sessions on the same scope (a second CLI session of the same
profile, or a cron run beside one) both attach, but only one at a time — the
holder of a per-scope writer lease — runs this plugin's state protocol:
resume recovery, the host push, the per-command sync, and the teardown pull.
The other sessions run commands normally as *readers* and take the role over
(with a full recover-then-push hand-off) once it is released, at which point
host-side changes made during their wait propagate. Without this, a second
session's bring-up would push its host snapshot over the sandbox's live
state.

Holding the lease is *not* exclusive write access to the sandbox's
`~/.hermes`. Readers execute commands, and a sandbox may still be running
processes an earlier session started; any of them can write there. What is
serialised is the plugin's own synchronisation.

**The guarantee is at command admission, not for a command's duration.**
Two gates decide whether a reader's command is admitted. A session that has
not yet attached must find the sandbox stamped as prepared by the writer (it
re-stamps after every completed push, and each new writer generation
invalidates the previous stamp before touching state). Every reader, on
every command, must find the scope not marked dirty on the host — the scope
is marked while a writer transition is in flight and after any failed
incremental sync, and cleared once a push or sync has established the host's
state. Either gate failing means the command fails closed with a retry hint
instead of running against a sandbox with no, half-written, or knowingly
stale Hermes state. The marker lives in `~/.hermes/cache/e2b/`, which Hermes
does not mirror into sandboxes, so it still answers when the sandbox
transport is the thing that broke — and if that file cannot be written or
read at all, the plugin says so once at WARNING level, because the
per-command gate is then not working.

Only the writer ever creates a persistent sandbox, and a writer whose
bring-up or attach fails releases the role, so a session that cannot do the
work never freezes the scope.

Caveats:

- a reader's host-state changes reach the sandbox on the writer's cadence
  rather than immediately (bounded by the writer's idle teardown);
- admission is not atomic with launch. A reader can be admitted immediately
  before a writer starts a transition, and a command or background process
  already running inside the sandbox keeps running through later
  synchronisation. Consistency is established at launch, not maintained for
  a command's lifetime — the same model as Hermes' in-tree remote backends,
  whose bind-mounted or synced state also changes underneath a running
  command;
- the lease is a host-side `flock`, so **two different machines** using one
  E2B account with identically-pathed profile homes resolve to the same
  sandbox with no arbitration between them. Concurrent ownership of one
  persistent scope from more than one machine is **not supported**: use
  `container_persistent: false`, or give each machine its own `template`
  (part of the sandbox identity) to split the scope;
- the guarantees above need two host facilities: an exclusive lock for the
  writer lease, and the per-scope marker file. If **either** is unavailable —
  **Windows** (no `flock`), a filesystem whose locks are unsupported (an
  NFS/SMB/FUSE home with no lock manager), or a `~/.hermes/cache/e2b/` that
  cannot be written — then **concurrent same-scope persistent sessions are
  not supported**: they are not protected against overwriting each other's
  in-sandbox Hermes state, or against running against state a peer is
  rebuilding. The plugin logs a warning saying exactly that, once, and acts
  as the sole writer. **A single persistent session is unaffected and fully
  supported.** Failing closed instead would remove persistent mode from
  those platforms entirely over a hazard that only exists when concurrent
  same-scope sessions are actually run.

**`container_persistent: false`.** Every session gets its own fresh sandbox,
never adopted from anywhere, destroyed on teardown, and created with
`on_timeout: kill` so an abandoned one dies on its own. Its lease is never
shorter than `terminal.lifetime_seconds` plus one 60-second Hermes reaper
interval plus E2B 2.46's 60-second control-plane request timeout; normal idle
cleanup therefore has time to renew before pulling state and killing explicitly
ahead of the abandoned-resource backstop. The lifecycle margin is independent of
`command_grace_seconds`, so setting command grace to zero does not recreate the
reaper/kill race. The plugin does not guess an account-specific maximum lease:
if E2B rejects the requested lifetime, Hermes reports the lifecycle operation
and requested duration as a configuration/infrastructure error.

### Identity and subagents

A sandbox belongs to the scope Hermes already resolved for its own environment
cache. Concretely:

- **CLI runs** share one sandbox per profile (`task_id` is `default`), across
  processes and across restarts.
- **Gateway / dashboard sessions** get one sandbox per session
  (`session:<key>`), because that is how Hermes keys non-Docker backends. See
  [contract gap 3](docs/contract-gaps.md#3-persistent-scope-is-profile-scoped-for-docker-only).
- **Delegated subagents share the parent's sandbox.** Hermes collapses a
  child's `task_id` onto the parent's container key, so the child lands in the
  same scope — one shell, one workspace, one set of installed packages.
- **RL / benchmark isolation overrides** (`register_task_env_overrides`) keep
  their own `task_id`, so they keep their own sandbox.

Identity is carried as E2B sandbox **metadata**, not a file on your machine.
The scope value is a SHA-256 digest of (profile home, task id, template,
persistent/ephemeral, internet access, secure) — see the security section for
why the last three are part of identity. Your session ids and home path never
leave the host. There is no local pointer store
to corrupt, and a sandbox whose creation response was lost is still found on
the next run instead of being billed forever with nobody holding its id.

### Timeouts and cancellation

Hermes owns the command deadline. The plugin extends the sandbox lease to
`max(lease_seconds, command timeout + command_grace_seconds)` before every
command, so E2B's timeout action can never fire while a command is running.
Normal ephemeral environments also apply the idle-reaper floor described
above; prompt probes retain their dedicated short kill lease because Hermes
never registers them with the reaper.

Leases are extended with the class-level `Sandbox.connect(id, timeout=…)`,
which the SDK documents as extend-only for a running sandbox. The returned SDK
object is retained because a resumed sandbox may have new envd connection
metadata; the instance method discards that response in E2B 2.46.0.
`set_timeout()` is deliberately unused: it can *shorten* a lease, which on a
shared sandbox would let a short command cut a long one short.

Command reconnects resolve the API key from the current Hermes profile at the
moment of renewal, so key rotation takes effect. Teardown reconnects instead
inherit the already attached sandbox's SDK connection parameters: cleanup may
run after the profile secret scope has disappeared, and it must not consult a
different profile's key. Neither key is logged or persisted by the plugin.

Timeouts and Ctrl-C kill **the command**, by pid. The sandbox survives, and so
does anything else running in it — including background processes Hermes
started through its process registry. (The built-in Daytona and Modal backends
stop the whole sandbox to cancel one command.)

### Output

Output is forwarded chunk by chunk as E2B delivers it, into the pipe Hermes
drains, so its bounded head/tail collector can start evicting immediately and a
verbose command cannot grow host memory without bound. The E2B SDK's own
command handle accumulates every chunk in an uncapped list; the plugin drops
that copy as it goes. If a future SDK removes those internals the plugin logs a
one-time warning and falls back to the SDK's buffering rather than breaking.

### Filesystem and state

**The host owns `~/.hermes`. The sandbox owns its workspace.**

On bring-up and before each command, Hermes' own `FileSyncManager` uploads the
files it syncs to every remote backend — registered credential files, skills,
and the cache directories (uploaded documents, images, screenshots) — to
`$HOME/.hermes` inside the sandbox. On teardown it pulls remote changes back.
This plugin only supplies the transport; the change detection, the transactional
rollback, the credential upload-only rule, the 2 GiB archive cap, and the
cross-process lock are all Hermes'.

Everything else the agent creates lives in the sandbox and stays there, which
in persistent mode means it is still there next session.

**When a sync fails.** Hermes' manager reports neither success nor failure — it
logs and returns — so this plugin recovers the signal itself:

- a failed **initial upload** is fatal. Continuing would hand the agent a
  sandbox with none of its skills or credentials and no clue why its tools
  behave differently;
- a failed **per-command sync** aborts the command. The cycle is transactional
  over uploads *and* deletes and the sync set includes credential files, so the
  pending change may be a credential deletion or a skill update — the command
  must not run against state that is known to be stale. Hermes retries the
  command a few times (each retry re-runs the sync, so transient failures
  self-heal), then reports the failure. It also marks the scope dirty, so any
  reader session sharing the sandbox stops admitting commands too until a sync
  succeeds — otherwise only the session that noticed would fail closed;
- a failed **teardown pull** is logged as an error naming the sandbox; the next
  resume recovers what the sandbox still holds before pushing anything.

**Recovery on resume.** Because a pull can fail — and because a hard crash skips
it entirely — resuming a persistent sandbox pulls agent-authored state *before*
pushing the host snapshot over it. The guarantee covers the `skills/` and
`memories/` trees as recovery's snapshot of the sandbox found them: every file
there is preserved in place, parked where a human can find it, or named in a
warning. **Other synced paths are not recovered** — a sandbox-side change under
`cache/documents/` and the other mounted cache trees is overwritten by the host
copy, which is the intended direction for them (the host owns that content;
the sandbox gets a copy to read).

- a `skills/` or `memories/` file the host does not have is restored in place;
- a file both sides have with different contents is a genuine conflict, since no
  baseline says which is newer. The host copy is left alone and the sandbox copy
  is saved under `~/.hermes/cache/e2b-recovered/<sandbox id>/`, named in a
  warning. Neither version is discarded. So is a sandbox file whose host path
  holds something other than a regular file, and one whose host copy cannot be
  read;
- **recovery restores in place only into a recoverable root's canonical
  location** — the physically resolved `~/.hermes/skills` and
  `~/.hermes/memories`. A file whose destination resolves anywhere else is
  quarantined instead, with a warning: out of the Hermes home, out of its own
  tree (a symlinked directory *inside* `skills/`), or onto a path Hermes maps
  as a credential. Redirecting a whole root — `skills` symlinked to
  `~/.hermes/my-skills` — is also quarantined rather than written through.
  That is deliberate and conservative: Hermes gives an arbitrary in-home
  directory no meaning, so a redirection to a deliberate `my-skills` and one to
  a directory the host owns are indistinguishable. The layout keeps working
  (skills are read and synced through the symlink as always); what it gives up
  is in-place recovery of *sandbox-authored* files. A symlinked `~/.hermes`
  itself is not a redirection and still restores in place;
- quarantine has its own verified boundary — the quarantine tree, not the whole
  home, because a symlink *inside* that tree would otherwise redirect a write
  while staying in the home. The tree itself is held to the same canonical-position
  rule as the recoverable roots: `~/.hermes/cache/e2b-recovered` must resolve to
  exactly itself, so a redirection of it or of any ancestor (`cache/`) is refused
  rather than followed. A destination that lands on host-owned state *nested
  inside* that tree is refused too — Hermes accepts arbitrary registered
  credential paths, so one can be registered under the quarantine directory, and
  parking over it would be the same "remote data replaces a host credential"
  outcome. If a quarantine destination or the root cannot be placed safely,
  recovery fails closed rather than write through it, which also blocks the push
  that would have buried the remote copy;
- credential paths are never restored or created from sandbox data;
- a file this host cannot write at all — a name it cannot represent, a path
  longer than it allows, an ancestor it holds as a regular file — costs that
  one file, not the resume. It stays in the sandbox and is named in a warning.
  Failing bring-up over it would be unfixable: the file lives in the sandbox,
  and bring-up is what would be failing;
- when the sandbox's state **cannot be read at all**, bring-up fails closed
  instead of pushing the host snapshot over state it never verified — the push
  is exactly what would bury the only surviving copy. A resumed sandbox that
  verifiably has no Hermes state simply proceeds. Recovery is retried on the
  next use.

This closes the data-loss window the in-tree remote backends still have, without
a durable sync baseline (which is core surface a plugin cannot add).

**One window it does not close.** A sandbox process — a daemon the agent
started, or a background command from an earlier session — can write to
`~/.hermes` inside the sandbox *after* recovery's snapshot is taken and before
the force-push lands. Nothing can drain those processes, so:

- a write in that window to a path the host **also** has is overwritten by the
  host copy, and is not quarantined. The plugin does not claim otherwise;
- a file **created** in that window survives: a force-push has no deletion
  baseline, so it deletes nothing. It reaches the host on the teardown pull, or
  — if it is in a subdirectory the host does not already sync, which Hermes'
  pull cannot map back — on the next resume's recovery.

Both directions are pinned by a test, so the boundary is a stated contract
rather than an assumption.

### Cleanup

| Trigger | Persistent | Ephemeral |
|---|---|---|
| Session close / idle reaping / `atexit` | reconnect if needed, pull state back, detach; E2B pauses the sandbox at lease expiry | reconnect for the pull window, pull state back, kill the sandbox |
| Cleanup arriving during a command | deferred until the command finishes | deferred until the command finishes |
| Python finalising a dropped reference | releases locally, never pulls state | kills the sandbox, never pulls state |
| Repeated cleanup | no-op | no-op |
| Sandbox already gone | no-op | no-op |

Ordinary reconnect, sync-back, scratch-removal, and kill failures are logged
without skipping mandatory release/reset/destruction. Python process-control
exceptions are different: `KeyboardInterrupt`, `SystemExit`, and
`GeneratorExit` are deferred until that mandatory cleanup has completed and
then re-raised. This preserves Hermes' own sync-back SIGINT contract instead of
silently turning Ctrl-C into a successful cleanup.

---

## Security

- **`E2B_API_KEY` stays on the host.** It authenticates the SDK and nothing
  else. It is never placed in the sandbox environment, never uploaded, never
  written to a command line, and never persisted by this plugin.
- **It is resolved per profile, and fails closed.** The key is read through
  Hermes' profile-aware `get_secret`. Under a multiplexed gateway with no
  profile scope installed, that raises by design — and this plugin reports the
  backend as unconfigured rather than falling back to the process environment,
  which could hold a different profile's key.
- **Sandbox identity includes its security posture.** `allow_internet_access`
  and `secure` are part of the sandbox's identity, so tightening either gives
  you a new sandbox instead of silently re-attaching to one still running under
  the old policy. Changing them leaves the previous sandbox to pause on its own
  lease; delete it from the E2B dashboard if you want it gone sooner.
- **It is stripped from subprocesses.** The provider declares
  `strip_env_keys = {"E2B_API_KEY"}`, so Hermes removes it from the environment
  of processes the agent spawns. One core spawn path does not yet apply this —
  see [contract gap 4](docs/contract-gaps.md#4-strip_env_keys-is-not-applied-on-the-local-backends-foreground-spawn)
  — which matters only when you are running the *local* backend with an E2B key
  in your environment.
- **Errors and logs are redacted.** Anything that looks like an E2B key, a
  bearer token, or a credential in a URL is replaced before a message reaches a
  log, an error, or a `hermes doctor` row.
- **Host credentials are never overwritten by sandbox data.** Hermes marks
  credential files upload-only, and the teardown pull skips them.
- **Sandbox output is untrusted.** The teardown archive is refused once it
  exceeds 2 GiB, mid-transfer, and Hermes extracts it with tar's `data` filter,
  so path traversal and unsafe links are rejected. Resume recovery extracts
  only regular files that pass that filter, one member at a time: an
  agent-created symlink or fifo is simply not recovered, and a member with an
  unsafe path is skipped and named in a warning — neither can abort the resume.
  Every file recovery writes has its destination checked against the canonical
  physical tree it belongs to, and every quarantine write against the verified
  quarantine tree, so no host symlink can turn sandbox-authored data into a
  write outside the Hermes home, into a tree the host owns exclusively, or onto
  a path Hermes maps as a credential. The host-only set is read from Hermes'
  own mapping (`get_credential_file_mounts`, the mirrored cache mounts, the
  profile's `config.yaml` and `.env`, `platforms/`) rather than a list of
  guessed names.
- **Approval prompts still appear** for dangerous commands, even though an E2B
  sandbox is disposable — see
  [contract gap 2](docs/contract-gaps.md#2-skip_container_guards-is-declared-but-never-read).

---

## Troubleshooting

**`hermes doctor` says the backend is not configured.** Check both rows: the
SDK must be importable by the interpreter running Hermes, and `E2B_API_KEY`
must be set for the *active profile* (`~/.hermes/.env`, or the profile's own
`.env` under a multiplexed gateway).

**Commands fail with an infrastructure error.** "E2B … failed" messages mean
the backend, not your command: the E2B API is unreachable, the key is rejected,
the sandbox vanished, or state could not be synced. Hermes retries the command
a few times against the same environment first (transient failures self-heal
because each retry re-runs bring-up and the state sync). Only an error raised
while the environment is being *created* is rendered as a structured
`status: "degraded"` result with a retry hint and evicts the cached
environment — this plugin attaches lazily, so most of its failures surface on
execute and are reported as a plain error instead (a reported contract gap).

**"Sandbox … is not ready: this scope's Hermes state is being rebuilt or is
known to be stale."** Another session sharing this persistent scope is
preparing the sandbox, or its last state sync failed. Retry — the command
proceeds as soon as that session completes a sync, and this session takes the
role over if it is released. If it persists, look in the other session's log
for an E2B sync error: something is stopping state from reaching the sandbox,
and running against it anyway is what this refuses to do.

**The plugin does not appear in `hermes plugins list`.** It must be enabled:
`hermes plugins enable e2b`. Check the log for `Skipping 'e2b' (not in
plugins.enabled)`.

**A new chat gets a fresh sandbox.** Expected under the current core key
resolution — see the identity section above.

**Duplicate sandboxes for one scope.** The plugin logs a warning naming the
count and adopts the newest. Two Hermes processes cold-starting at the exact
same moment can produce this despite the cross-process creation lock (for
example on a filesystem without `flock`). The extras pause on their own leases;
delete them from the E2B dashboard.

**Something ran on the host instead of the sandbox.** Confirm `terminal.backend`
is `e2b` (`hermes status`). Processes that skip Hermes' launchers read the
backend from `config.yaml` via core's own bridge, so a stale `TERMINAL_ENV`
export in your shell can win — unset it.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pip install 'e2b>=2.46,<3'
.venv/bin/pip install -e /path/to/hermes-agent    # for the integration tests
.venv/bin/python -m pytest
```

The suite runs against a behavioural double of the E2B SDK and a real Hermes
installation. It does not touch the network.

Live tests that create real, billable sandboxes are marked `live` and
deselected by default:

```bash
E2B_API_KEY=... .venv/bin/python -m pytest -m live
```

Validate the plugin the way Hermes does:

```bash
hermes plugins doctor . --ci
```

---

## Credits

The E2B terminal backend for Hermes was first proposed and implemented by
**Berkant Ay** ([@berkantay](https://github.com/berkantay)) in
[hermes-agent#18348](https://github.com/NousResearch/hermes-agent/pull/18348).
That work is the origin of this plugin. See [NOTICE](NOTICE) for the full
attribution and licensing detail.

## License

MIT — see [LICENSE](LICENSE).
