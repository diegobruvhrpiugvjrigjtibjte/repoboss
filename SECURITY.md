# Security

RepoBoss executes code: your repository's install, build and test commands, and the agents you
configure. Treat every challenge and agent as code running on your machine.

## Threat model and limits

### `local` runner: no isolation
- Commands run as your user, with your network and file access.
- RepoBoss passes only an allowlist of environment variables: `PATH`, toolchain homes such as
  `JAVA_HOME` and `CARGO_HOME`, and system variables. API keys and tokens in your environment are
  not inherited unless an agent's `pass_env` names them.
- A process can still read any file your user can, including the hidden tests and the reference fix
  stored in `.repoboss/`.
- Installed dependency folders (e.g. `node_modules`) are linked into each tree and shared between
  runs.

Use the local runner only for trusted code and agents.

### `docker` runner: isolated mode
**Setup:** dependencies are installed into an image with network access. Afterwards every
non-dependency file is deleted from the image, so neither the tests nor the original sources stay
readable there.

**Each command then runs in a fresh container:**
- created with `--network none` unless enabled per agent;
- `--cap-drop ALL` and `--security-opt no-new-privileges`;
- CPU, memory and PID limits, and a size-limited `/tmp`;
- **no bind mounts**: files are copied in and out as tar streams.

**Copying files back:** RepoBoss validates every entry. Links, devices and paths that escape are
rejected and reported as violations.

**Never available to the container:** your home directory, credentials, the Docker socket and the
original repository.

Containers share the host kernel: **Docker reduces risk but is not a complete security boundary.**
For hostile code, use a dedicated VM.

### The LLM agent
- The loop runs in RepoBoss. The model acts only through file tools that:
  - resolve paths inside the working copy;
  - refuse links, `..` and absolute paths;
  - refuse writes to test files and read-only configuration.
- The `run` tool is off by default. When enabled, commands go through the runner: sandboxed with
  Docker, **not** sandboxed with the local runner.
- API keys are read from the variable named in `api_key_env`. They are never written to the
  database, logs or exports.

### Hidden tests and the reference fix
- They never enter the agent's working copy or container.
- They are stored in the workspace on the host.
- Exported packages contain the fix.

### Transferring changes
- Only regular files within `editable` are copied into the evaluation tree.
- These reject the attempt:
  - symlinks, junctions and special files;
  - oversized files;
  - changes to hidden or protected paths (matched case-insensitively).

### Other limits
- **Imports** are validated: paths, links, hashes, manifest schema and sizes. Nothing from an import
  is executed until you run `repoboss challenge verify ID --trust`.
- **The dashboard** has no authentication and binds to `127.0.0.1` by default.
- **Passing tests** is evidence, not proof, that a fix is correct.

## Reporting a vulnerability

Please report vulnerabilities privately through the repository's security advisory feature
("Report a vulnerability") rather than a public issue. Include steps to reproduce and the affected
version. There is no bug bounty.
