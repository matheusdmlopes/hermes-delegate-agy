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
    (--model, optional --sandbox, --dangerously-skip-permissions, JSON output)
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

Enable the plugin, then start or restart Hermes:

```bash
hermes plugins enable delegate-agy
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

- **`sandbox` (Default)**: Runs `agy` with `--sandbox --dangerously-skip-permissions`. Constrains terminal execution for read-only / diagnostic tasks while avoiding interactive prompt deadlocks.
- **`accept_edits`**: Runs `agy` in full YOLO mode (`--dangerously-skip-permissions` without sandbox). Allows full file creation, editing, patching, and unrestricted terminal commands.

`accept_edits` is enabled by default for 0.2.0 compatibility. To prohibit unrestricted edit workers, set `allow_accept_edits: false`; requests for that mode then return a structured error before any process starts.

## ⚙️ Plugin Settings

Settings live under the plugin entry in `config.yaml`:

```yaml
plugins:
  entries:
    delegate-agy:
      settings:
        max_concurrent: 3
        default_timeout_seconds: 600
        max_result_chars: 24000
        max_records_in_memory: 500
        max_raw_output_bytes: 16777216
        retention_days: 30
        max_total_storage_mb: 1024
        allow_accept_edits: true
```

- `max_concurrent` bounds parallel workers from 1 to 10.
- `default_timeout_seconds` bounds a print run when the call omits a timeout.
- `max_result_chars` limits the response returned to Hermes; the raw artifact path remains available.
- `max_records_in_memory` limits recent terminal records while active records are always retained.
- `max_raw_output_bytes` limits each stdout or stderr artifact.
- `retention_days` and `max_total_storage_mb` bound terminal artifacts on disk. Active records are never deleted.
- `allow_accept_edits` controls whether unrestricted edit mode may start.

## 💾 Storage and Recovery

Records are stored in the active Hermes profile under `plugin-data/delegate-agy/runs/`; runtime leases live under `plugin-data/delegate-agy/runtime/`. Record publication is atomic, files are private (`0600`), and directories use `0700`.

Each Hermes runtime holds an exclusive lease. A second live runtime does not interrupt or claim its workers. After an unclean exit releases that lease, the next runtime marks unfinished records as `interrupted`. Legacy terminal records remain readable. Steering writes attempt-specific artifacts such as `agy_<id>.turn-001.stdout` and `agy_<id>.turn-002.stdout`.

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

Run the complete zero-quota fake/mock suite:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest \
  tests/test_unit_mock.py \
  tests/test_process_groups.py \
  tests/test_batch_and_ownership.py \
  tests/test_persistence_concurrency.py \
  tests/test_shutdown_lifecycle.py \
  tests/test_hermes_contract.py \
  -v -p no:cacheprovider
```

`tests/integration_smoke.py` invokes the real authenticated `agy` service and consumes Antigravity quota. Run it only when live quota consumption is explicitly intended.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
