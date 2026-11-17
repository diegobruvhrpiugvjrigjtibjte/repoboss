# Agents

Agents are configured in `.repoboss/agents.toml` and run with:

```bash
repoboss run CHALLENGE_ID --agent NAME --attempts 5
```

Every attempt:
- starts from a clean copy of the challenge;
- gets a time limit;
- is evaluated only through the protected tests.

## Built-in demo adapters

| Name | Behaviour | Purpose |
|---|---|---|
| `demo-oracle` | Applies the known reference fix | Shows the pipeline passes a correct fix |
| `demo-noop` | Changes nothing | Shows the pipeline fails an unfixed bug |

Their runs carry `agent_is_demo = 1` and are labelled **DEMO** everywhere. They do not measure any
model.

## `type = "llm"`: a real AI coding agent

RepoBoss contains a complete tool-using agent. A language model drives it; the loop runs inside
RepoBoss.

```toml
[agents.qwen]                    # local model through Ollama, no account needed
type = "llm"
provider = "ollama"
model = "qwen3:14b"
base_url = "http://127.0.0.1:11434"
max_steps = 25
num_ctx = 16384                  # context window requested from Ollama
think = false                    # qwen3 "thinking" off: much faster, usually as good for small fixes
timeout_s = 900

[agents.gpt]                     # any OpenAI-compatible API
type = "llm"
provider = "openai"
base_url = "https://api.openai.com/v1"     # or OpenRouter, Groq, LM Studio, vLLM, Ollama's /v1 ...
model = "gpt-4.1-mini"
api_key_env = "OPENAI_API_KEY"   # the variable's name; the key is read from your environment
price_input_per_mtok = 0.40      # optional: with both prices, cost is computed from measured tokens
price_output_per_mtok = 1.60
temperature = 0.2
allow_run = false                # true lets the model run commands (sandboxed with Docker)
```

**What the model receives:**
- the task instructions;
- the names of the failing tests (the files stay hidden);
- the test command;
- the repository's file list.

**Tools it can call:**

| Tool | Effect |
|---|---|
| `list_files(path?)` | Files of the working copy (dependency directories excluded) |
| `read_file(path, start_line?, end_line?)` | Numbered lines, up to 400 per call |
| `search(pattern, path?)` | Regular-expression search, up to 80 matches |
| `replace_in_file(path, old, new)` | Exact replacement; `old` must occur exactly once |
| `write_file(path, content)` | Create or overwrite a file (max 256 KB) |
| `run(command)` | Only with `allow_run = true`. Runs an argv list through the runner, without network |
| `finish(summary)` | Ends the attempt |

**Safety of the tools:**
- Paths are resolved inside the working copy: no `..`, no absolute paths, no links.
- Test files and read-only configuration cannot be written.
- The evaluation re-checks every change regardless.

**What is recorded for each run:**

| Field | Source |
|---|---|
| Version | Ollama: the model digest (`qwen3:14b@bdbd181c33f2`, "detected"). OpenAI-compatible: the model name the API reports |
| Tokens | Summed from the API responses: Ollama's `prompt_eval_count` and `eval_count`, OpenAI's `usage` |
| Cost | Only when you configure prices, labelled "computed from configured prices". Otherwise `n/a` |
| Transcript | Every step in `runs/<id>/agent-out/transcript.jsonl` |
| Log | A readable step log in `agent.log` |

**Verified providers:**
- `ollama` with `qwen3:14b` on Ollama 0.35 (real runs).
- `openai` through Ollama's OpenAI-compatible `/v1` endpoint (real run). Hosted OpenAI-compatible
  services use the same protocol but have not been tested with a key.

There is no native Anthropic API provider and no Claude Code integration. Both can be added once
their interface is verified with real credentials; until then, use a command agent.

## `type = "command"` (default): your own tool

```toml
[agents.custom]
command = ["python", "{agent_dir}/run.py", "--task", "{task_file}"]
agent_dir = "/abs/or/relative/path"     # relative to agents.toml
version = "1.4.0"                         # stored with version_source = "declared"
pass_env = ["MY_API_KEY"]                 # forwarded by name; nothing else from your environment
network = false                           # Docker runner: network during the attempt
timeout_s = 600
```

**How the command runs:**
- It is an argv list, executed without a shell.
- Only the placeholders `{workspace}`, `{output_dir}`, `{task_file}` and `{agent_dir}` are
  substituted. In Docker they are `/workspace`, `/repoboss/out`, `/repoboss/out/task.json` and
  `/agent`.
- A leading `python` is the environment's interpreter.
- The command runs inside the challenge environment. In Docker that is the project's image, so the
  agent must be self-contained.

**Environment variables:**

| Variable | Content |
|---|---|
| `REPOBOSS_WORKSPACE` | Working copy (current directory); test files are absent |
| `REPOBOSS_TASK_FILE` | JSON task: instructions, `failing_tests`, `test_command`, `editable`, `hidden`, `protected`, `network` |
| `REPOBOSS_OUTPUT_DIR` | Writable folder for `usage.json` and anything you want to keep |
| `REPOBOSS_AGENT_DIR` | Your `agent_dir` |
| `REPOBOSS_CHALLENGE_ID`, `REPOBOSS_NETWORK` | Id and `1`/`0` |

**Exit status:**

| Result | Recorded as |
|---|---|
| Exit `0` | `completed` |
| Any other exit code | `agent_error` |
| Over the time limit | `timeout`, and the process tree is killed |

Only the evaluation decides `passed` or `failed`.

**Usage (optional).** Write `usage.json` only when the numbers come from a measurable source, such as
an API response:

```json
{"input_tokens": 1234, "output_tokens": 567, "cost_usd": 0.0123, "source": "api usage field"}
```

It is stored as **agent-reported**. Missing values stay `n/a`, never zero.

[examples/agents/skeleton_agent.py](../examples/agents/skeleton_agent.py) is a minimal starting point.
