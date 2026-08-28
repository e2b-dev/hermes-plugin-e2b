# E2B terminal backend — next steps

1. **Install the SDK** into the interpreter that runs Hermes:

   ```
   pip install 'e2b>=2.46,<3'
   ```

2. **Set your API key** for this profile (get one at https://e2b.dev/dashboard):

   ```
   echo 'E2B_API_KEY=e2b_...' >> ~/.hermes/.env
   ```

3. **Enable and select the backend:**

   ```
   hermes plugins enable e2b
   hermes config set terminal.backend e2b
   hermes doctor
   ```

From then on the agent's terminal, file, and code-execution tools run inside an
E2B sandbox instead of on this machine.

`terminal.container_persistent: true` (the default) keeps one sandbox per
session scope and preserves its filesystem across restarts. Set it to `false`
for a throwaway sandbox per session.

Optional settings live under `plugins.entries.e2b.settings` — see
`config.yaml.example` and the README.
