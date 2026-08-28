# delegate-agy

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**delegate-agy** is a native [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool plugin that delegates complex sub-tasks, deep research, code implementation, and multi-model reviews to local [Antigravity CLI (`agy`)](https://github.com/google/antigravity) workers.

It enables the primary Hermes agent to spawn asynchronous or blocking background workers, route tasks across different models, control process execution groups, resume conversations, and queue steering follow-ups—all without running external HTTP proxies or custom bridges.

---

## 🏛️ Architecture

```text
    Hermes Agent Core
           │
           ▼
    delegate_agy (Tool Handler)
           │
           ▼
       AgyManager
    (Concurrency, Process Groups, JSON Streams, Disk Persistence)
           │
           ▼
    Official `agy` CLI Binary Subprocess
    (--model, --sandbox / --mode accept-edits, --output-format json)
           │
           ▼
    Antigravity Native Quota / Auth / Providers
```

- **Zero API Key Extraction**: The `agy` CLI binary manages its own credentials and quota.
- **Zero Daemon / Bridge Required**: Uses direct standard POSIX subprocess management (`start_new_session=True`, `os.killpg`).
- **Dynamic Binary Gating**: Automatically hides the `delegate_agy` tool from the model schema if `agy` is not found in `$PATH`.

---

## 📦 Installation

Clone this repository into your local Hermes plugins directory:

```bash
git clone https://github.com/matheusdmlopes/hermes-delegate-agy.git ~/.hermes/plugins/delegate-agy
```

Ensure the official `agy` CLI is installed and authenticated:

```bash
which agy
agy models
```

Start or restart Hermes:

```bash
hermes
```

---

## 🧭 Model Routing Table

When delegating, Hermes selects a `task_type` which routes to an appropriate model:

| `task_type` | Default Model | Best For |
|---|---|---|
| `simple` | `gemini-3.7-flash-low` | Fact extraction, quick classification, reading files |
| `investigation` | `gemini-3.7-flash-medium` | Diagnosis, debugging, logs inspection (Default) |
| `implementation` | `gemini-3.7-flash-high` | Writing and modifying code |
| `hard` | `gemini-3.1-pro-high` | Difficult reasoning, large-scale codebase analysis |
| `review` | `claude-sonnet-4-6` | Independent adversarial code review |
| `exceptional` | `claude-opus-4-6-thinking` | Complex architecture & deep multi-step planning |
| `cheap_second_opinion` | `gpt-oss-120b-medium` | Low-cost cross-checks and validation |

> **Note**: An explicit `model` argument (e.g. `model="gemini-3.7-flash-high"`) completely overrides `task_type` routing.

---

## ⚙️ Execution Modes

- **`sandbox` (Default)**: Runs `agy` with `--sandbox --dangerously-skip-permissions`. Enforces terminal restrictions while avoiding interactive prompt deadlocks in headless subshells.
- **`accept_edits`**: Runs `agy` with `--mode accept-edits --dangerously-skip-permissions`. Selected when the user explicitly requests file edits in the target workspace.

---

## 🛠️ Actions Reference

- **`spawn`**: Launch a single task or up to 10 parallel tasks (`tasks: [...]`, `wait: true|false`).
- **`list`**: List all worker processes owned by the current session.
- **`status`**: Get live progress, PID, PGID, and execution status.
- **`result`**: Retrieve the formatted result, structured usage, and output paths.
- **`wait`**: Block until target workers complete or timeout expires.
- **`stop`**: Gracefully terminate worker process groups via `SIGTERM` / `SIGKILL`.
- **`continue`**: Resume a completed conversation by its Antigravity `conversation_id`.
- **`steer`**: Queue a follow-up instruction to run sequentially after the active print turn finishes.

---

## 🧪 Testing

Run the zero-quota unit mock test suite:

```bash
python3 -m pytest tests/test_unit_mock.py -v
python3 -m pytest tests/test_process_groups.py -v
```

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
