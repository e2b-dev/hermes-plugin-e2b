# Behavior contract

This document summarizes the behavior users can rely on from the E2B terminal backend. Implementation details and known Hermes API gaps are documented separately in [contract-gaps.md](contract-gaps.md).

## Sandbox identity

- Persistent sessions reuse the sandbox for the scope resolved by Hermes.
- Ephemeral sessions receive separate sandboxes and cannot tear down each other's environments.
- A delegated subagent shares its parent's sandbox in both modes.
- Explicit benchmark or environment-isolation overrides receive their own sandbox.

## Lifecycle

- Sandboxes are created or attached lazily, on first use.
- A persistent sandbox is never paused or killed by plugin cleanup. E2B pauses it when its lease expires, and the plugin reconnects when it is needed again.
- An ephemeral sandbox is pulled back and killed during normal cleanup; `on_timeout: kill` reclaims it if Hermes exits without cleanup.
- Command and cleanup leases are extended before the corresponding operation and are never shortened by a later, shorter command.
- Cleanup never creates a replacement for a sandbox that is already gone.

## State and concurrency

- The host owns `~/.hermes`; the sandbox owns its workspace.
- Only one same-host writer performs recovery, upload, incremental sync, and teardown pull for a persistent scope. Other attached sessions may execute as readers after the state-admission checks pass.
- State consistency is established when a command is admitted. It is not a transaction covering the command's entire lifetime.
- On resume, sandbox-authored files under `skills/` and `memories/` are recovered before host state is uploaded. Conflicts are preserved in a safe quarantine rather than silently overwritten.
- Credential files are host-owned and never restored from sandbox data. Host deletions are propagated before a command runs.
- A failed required sync blocks command execution instead of running against state known to be stale. Cleanup failures are reported without skipping mandatory release or ephemeral destruction.

## Commands and security

- Terminal, file, and code-execution operations run inside the same selected E2B sandbox.
- A timeout or cancellation stops the command, not unrelated processes or the persistent sandbox.
- Command output streams incrementally into Hermes' bounded collector, and bulk file transfers enforce their configured size limit while downloading.
- `E2B_API_KEY` remains on the host and is never uploaded to the sandbox or persisted by the plugin.
- Sandbox identity includes settings that change its security posture, so a sandbox is not reused after those settings change.

## Supported concurrency boundary

Concurrent ownership is supported only on one host with a working filesystem lock and writable state markers. Cross-machine ownership of the same persistent scope, and concurrent same-scope use on filesystems without working locks, are not supported. A single session remains supported in those environments.
