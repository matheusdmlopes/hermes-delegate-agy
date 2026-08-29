# delegate-agy — Implementation and Hardening Plan

Status: ready for implementation
Target release: `0.2.0`
Baseline audited commit: `23aa692a9707143e5b92edc3c5c3639af1ff604b`
Repository: `/home/matheusdm/.hermes/plugins/delegate-agy`

## 1. Mission

Implement the audit findings without changing the plugin's core idea:

```text
Hermes delegate_agy tool
  -> AgyManager
  -> official agy CLI subprocess
  -> native Antigravity authentication, quota, models, and tools
```

The result must retain:

- direct execution of the official `agy` binary;
- no HTTP proxy, bridge, credential extraction, or custom provider client;
- asynchronous and bounded parallel workers;
- model routing by task type plus explicit model override;
- sandbox and unrestricted edit modes;
- conversation continuation and steering;
- POSIX process-group cancellation;
- per-session isolation;
- durable, profile-aware records under Hermes plugin storage;
- a default test suite that consumes no real API quota.

## 2. Instructions for the implementing agent

Read this file completely before editing anything.

Before implementation:

1. Read every repository file listed in section 3.
2. Read `/home/matheusdm/.hermes/hermes-agent/AGENTS.md` before inspecting or
   using Hermes core code.
3. Record `git status --short --untracked-files=all` and the current commit.
4. Preserve unrelated user changes. Do not reset, checkout, or clean them.
5. Do not modify Hermes core. All production changes belong to this plugin.
6. Do not run `tests/integration_smoke.py` unless the user explicitly authorizes
   real Antigravity quota consumption.
7. Do not use `git push`. Do not commit unless explicitly requested.
8. Use `apply_patch` for source edits.

Work phase by phase. A phase is not complete until its tests pass. If a design
decision below proves impossible, stop and report concrete evidence instead of
silently substituting another design.

## 3. Files in scope

Production and packaging:

```text
plugin.yaml
__init__.py
tools.py
schemas.py
README.md
LICENSE
.gitignore
```

Existing tests:

```text
tests/test_unit_mock.py
tests/test_process_groups.py
tests/integration_smoke.py
```

New test files may be added when they keep responsibilities clearer:

```text
tests/test_batch_and_ownership.py
tests/test_persistence_concurrency.py
tests/test_shutdown_lifecycle.py
tests/test_hermes_contract.py
```

## 4. Non-negotiable behavior contracts

### 4.1 Exact agy command contract

For `agy 1.1.22`, command construction must use argument arrays and never a
shell. The two execution modes are:

```text
sandbox:
  --sandbox --dangerously-skip-permissions

accept_edits:
  --dangerously-skip-permissions
  no --sandbox
  no --mode accept-edits
```

`--mode` must not be added in print mode. Preserve support for:

```text
-p
--add-dir
--model
--output-format json
--print-timeout
--conversation
--effort
```

Tests must compare the exact argv list, not only membership with `assertIn`.

### 4.2 Stable ownership contract

The canonical owner is:

```text
session_id, falling back to task_id, otherwise fail closed
```

The same Hermes session must be able to list, inspect, wait for, stop, steer,
and continue its workers even when `task_id` changes between turns. A different
session must never gain access.

### 4.3 Batch default contract

When `tasks` is present, these top-level fields are common defaults:

```text
context
task_type
model
execution_mode
effort
workdir
timeout_seconds
```

An item-level value overrides the common value. Every item still requires its
own `goal`. A top-level `workdir` must never be silently ignored.

### 4.4 Process ownership contract

On POSIX, every `agy` worker is launched with `start_new_session=True`. At
spawn time, capture and validate its actual PGID:

```text
pgid > 0
pgid == process.pid
pgid != os.getpgrp()
```

Never rediscover a PGID during termination. Never signal a group that fails
the captured ownership checks.

### 4.5 Persistence contract

- The in-memory `RunRecord` object remains the canonical object for a locally
  supervised worker.
- Record visibility on disk is atomic.
- Temporary filenames are unique across processes.
- Files use mode `0600`; plugin runtime/runs directories use `0700`.
- A second live Hermes process must never mark another runtime's active worker
  as interrupted.
- Legacy terminal JSON records remain loadable.
- Legacy non-terminal records without supervisor metadata may be marked
  interrupted.

## 5. Phase 0 — Baseline and regression tests

Add failing tests before production changes.

Required tests:

1. `session_id` stays constant while `task_id` changes; access still succeeds.
2. Different `session_id` is rejected even with a matching/known worker ID.
3. Missing both identifiers fails closed.
4. Batch inherits every common field.
5. Item fields override batch defaults.
6. Exact sandbox argv.
7. Exact accept-edits argv, including absence of `--mode` and `--sandbox`.
8. Clean JSON, compact JSON after logs, multiline JSON after logs, trailing
   logs, malformed JSON, JSON arrays, and misleading JSON log objects.
9. `returncode != 0` fails even when payload status says success.
10. `status: ERROR` prefers the structured `error` field.

Gate:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest \
  tests/test_unit_mock.py \
  tests/test_process_groups.py \
  tests/test_batch_and_ownership.py \
  -v -p no:cacheprovider
```

Expected before fixes: newly added regression tests fail for the intended
reason, while existing tests remain green.

## 6. Phase 1 — Functional correctness

### 6.1 Fix `_owner_id`

Prefer `session_id`, then `task_id`. Keep string normalization and the current
fail-closed behavior.

### 6.2 Fix batch merging

Build a common-default dictionary from the seven fields in section 4.3, then
merge each task as:

```python
effective_task = {**common_defaults, **task}
```

Validate that every task is a mapping before converting it. Do not propagate
top-level `goal` into batch items.

### 6.3 Make spawn versus shutdown atomic

Under one `RLock` critical section:

1. reject `_closed`;
2. add the record;
3. persist its queued state;
4. submit the worker future;
5. add the future to `_futures`.

If submission fails, transition the record to `failed`, set `finished_at`,
persist best-effort, and return a structured error. Never leave a durable
record eternally `queued`.

Gate: all Phase 0 tests pass.

## 7. Phase 2 — Process lifecycle hardening

### 7.1 Introduce one transition helper

Add a lock-required helper such as:

```python
_transition(record, status, *, error=None, finished=False)
```

It must update status, timestamps, error, and persistence consistently. Use it
for terminal transitions wherever practical. Do not perform process waits
while holding `_lock`.

### 7.2 Put cleanup immediately after `Popen`

From the first line after a successful `Popen`, execution must be enclosed by
`try/finally`. The cleanup path must not depend on successful persistence.

If the first post-spawn `_write_record` fails:

1. remove the process from `_processes`;
2. terminate the captured group;
3. reap the direct child;
4. close/drain pipes;
5. mark the in-memory record failed;
6. return `False` without leaking the child.

### 7.3 Rewrite `_terminate_process` around group liveness

Required algorithm:

1. Verify the captured PGID ownership invariants.
2. On POSIX, use `os.killpg(pgid, 0)` to determine group existence even when
   the direct leader has already exited.
3. Send SIGTERM to the whole group.
4. Poll group existence for at most five seconds using `time.monotonic()`.
5. Send SIGKILL if the group remains.
6. Call `process.wait()` with a short bound to reap the direct child.
7. Treat `ProcessLookupError` as already terminated.

The initial `process.poll() is not None` must not cause an early return while a
captured process group still exists.

### 7.4 Make timeout cleanup bounded

After an outer timeout, there must be no unbounded `communicate()` call. Once
the group is terminated and the direct child is reaped, drain/close pipes with
a short bound. If draining still fails, close handles and record a bounded
diagnostic.

### 7.5 Make shutdown deterministic

Shutdown order:

1. acquire lock and set `_closed` once;
2. cancel queued futures individually;
3. transition successfully cancelled queued records to `cancelled`;
4. snapshot active processes and PGIDs;
5. release lock;
6. terminate active groups;
7. wait for worker futures using one global bounded deadline;
8. call executor shutdown with `cancel_futures=True`;
9. close the runtime lease introduced in Phase 3.

Repeated `shutdown()` calls must be harmless.

### 7.6 Process tests

Use local fake executables only. Add cases for:

- normal SIGTERM;
- direct process ignoring SIGTERM, requiring SIGKILL;
- leader exiting while a descendant remains in the group;
- descendant holding inherited stdout/stderr descriptors;
- outer timeout;
- stop before the future starts;
- concurrent stop and natural completion;
- concurrent spawn and shutdown;
- unload with active and queued workers.

For every case, assert the final status and that no tested PID/PGID remains.

Gate:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest tests/test_process_groups.py \
  tests/test_shutdown_lifecycle.py -v -p no:cacheprovider
```

## 8. Phase 3 — Multi-runtime-safe persistence

### 8.1 Extend `RunRecord` compatibly

Add optional/defaulted fields:

```text
runtime_id
supervisor_pid
supervisor_started_at
attempt_index
```

Do not make old JSON records fail construction.

### 8.2 Add a runtime lease

Because supported platforms are Linux and macOS, use `fcntl.flock` on:

```text
<plugin-data>/runtime/<runtime_id>.lock
```

The manager holds an exclusive lock for its lifetime. Store `runtime_id` in
every new record.

During startup recovery:

- terminal record: load normally;
- non-terminal record whose runtime lease is locked by another process: do
  not modify it and do not claim local supervision;
- non-terminal record whose lease can be acquired or is absent: mark
  interrupted;
- legacy non-terminal record with no runtime metadata: mark interrupted.

Do not use PID liveness as the only proof because PIDs can be reused.

### 8.3 Use unique atomic temporary files

Use a same-directory name containing record name, current PID, and UUID. Write,
flush, `fsync`, chmod to `0600`, then `os.replace`. Delete leftovers in
`finally`. Optionally fsync the directory after replace.

The same rule applies to raw stdout/stderr atomic writes.

### 8.4 Avoid stale external snapshots

Do not place live foreign records into `_futures` or `_processes`. If the
implementation chooses to expose them in `list/status`, reload their JSON at
read time and label them as externally supervised. `stop/steer` must fail with
a clear message unless the current manager owns the live process handle.

### 8.5 Multiprocessing tests

With one temporary `HERMES_HOME`, run two real Python processes and assert:

1. manager B does not interrupt manager A's live record;
2. simultaneous atomic writes do not collide;
3. no `.tmp` file remains after success;
4. after manager A exits uncleanly, a new manager marks its record interrupted;
5. terminal records remain readable;
6. legacy records migrate through tolerant loading.

Gate: all persistence concurrency tests pass repeatedly, for example 20 runs.

## 9. Phase 4 — Futures, cache, and retention

### 9.1 Drain futures

Register a done callback that removes a future only when it is still the
registered object for that worker. The worker must finish its record transition
before the callback removes the future.

Wrap `_run_worker` in a supervisor that converts any uncaught exception into a
terminal `failed` record, even if no caller invokes `wait`.

### 9.2 Bound records in memory

Keep all active records and a configurable number of recent terminal records.
Recommended default:

```text
max_records_in_memory = 500
```

When `_owned_record` misses memory, load the exact JSON from disk, validate its
worker ID and owner, and insert it into a small LRU cache. Do not break
continuation of older terminal workers.

### 9.3 Bound raw output and disk retention

Add settings with conservative defaults:

```text
max_raw_output_bytes = 16 MiB per stream
retention_days = 30
max_total_storage_mb = 1024
```

Never delete active records. Retention errors must be logged/recorded but must
not fail an otherwise successful worker.

For steering, use attempt-specific raw files so later continuations do not
overwrite earlier forensic output:

```text
agy_<id>.turn-001.stdout
agy_<id>.turn-001.stderr
agy_<id>.turn-002.stdout
agy_<id>.turn-002.stderr
```

### 9.4 Stress test

Run at least 500 short fake workers and assert after completion:

```text
len(_futures) == 0
len(_processes) == 0
records in memory respect the configured bound
open FD count returns close to baseline on Linux
no fake worker PID remains
disk retention respects configured limits
```

## 10. Phase 5 — JSON parsing and error reporting

Replace line-only fallback parsing with `json.JSONDecoder.raw_decode` over
candidate top-level objects. Accept compact or multiline objects with logs
before/after. Validate candidates by requiring result-contract fields such as
`status`, `response`, `conversation_id`, or `error`; do not accidentally return
a JSON-shaped log entry.

Centralize payload interpretation:

- `returncode != 0` always fails;
- success requires a recognized success status;
- `status: ERROR` always fails;
- structured `payload["error"]` has priority over stderr/stdout;
- `usage` must be a dictionary;
- numeric metrics must reject booleans and non-finite numbers;
- unknown fields must not break forward compatibility.

Preserve the latest response in the existing top-level fields for compatibility.

## 11. Phase 6 — Hermes integration, packaging, and docs

### 11.1 Manifest

Change the plugin kind to the semantically correct value:

```yaml
kind: standalone
```

Add metadata where supported:

```yaml
license: MIT
homepage: https://github.com/matheusdmlopes/hermes-delegate-agy
external_dependencies:
  - agy
```

Bump the plugin version to `0.2.0` after all behavioral and persistence tests
pass.

### 11.2 Registration lifecycle

Keep `ctx.on_unload(manager.shutdown)` and `ctx.register_tool(...)`. If tool
registration is rejected or raises after manager construction, immediately
shutdown the manager so no unused executor/lease remains.

Keep `check_fn` passive and side-effect-free: it should only test whether `agy`
is resolvable.

### 11.3 Import fallback

Replace the broad `except ImportError` fallback with explicit package-context
logic. An ImportError inside `tools.py` must surface as the real error rather
than being misdiagnosed as a relative-import problem.

### 11.4 Real Hermes contract test without quota

Against a temporary `HERMES_HOME` and a fake `agy` in PATH, exercise the real
Hermes plugin discovery/registry path:

- plugin enabled;
- manifest parsed;
- toolset exposed;
- handler receives task/session context;
- check function works;
- unload removes the tool and shuts down manager resources.

Do not modify Hermes core and do not call a real model.

### 11.5 README

Document the required activation command:

```bash
hermes plugins enable delegate-agy
```

Remove the stale `--mode accept-edits` reference. Document sandbox versus
direct YOLO flags, plugin settings, storage/retention, recovery semantics, and
that live smoke tests consume quota.

Add an optional safety setting without breaking the plugin's central editing
capability:

```text
allow_accept_edits = true by default for 0.2.0 compatibility
```

When false, reject `execution_mode=accept_edits` with a structured error. A
future breaking release may reconsider the default, but this hardening release
must not silently remove the plugin's implementation use case.

## 12. Complete zero-quota verification command

Run only fake/mocked tests by default:

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

If file names differ, run the equivalent complete zero-quota suite and list the
exact files in the final report.

Do not run this unless the user explicitly authorizes real quota:

```bash
python tests/integration_smoke.py
```

## 13. Definition of done

Implementation is complete only when all conditions hold:

- exact `agy 1.1.22` argv tests pass for both modes;
- batch defaults and item overrides behave correctly;
- same-session access survives changing task IDs;
- cross-session access fails closed;
- no process or process group survives stop, timeout, or unload tests;
- persistence failure after spawn cannot leak a child or FD;
- concurrent runtimes do not corrupt or interrupt each other's live records;
- legacy records remain readable according to section 8;
- `_futures` and `_processes` return to zero after stress tests;
- in-memory and on-disk retention limits are enforced;
- noisy compact/multiline JSON parsing tests pass;
- real Hermes registration/unload test passes with fake `agy`;
- README installation instructions actually enable the plugin;
- the complete zero-quota test suite is green at least three consecutive times;
- `git diff --check` passes;
- `git status` contains only intentional implementation files;
- no live API or Antigravity quota was consumed during default verification.

## 14. Recommended commit sequence

If commits are authorized, keep them reviewable:

1. `test: reproduce ownership batch and parser regressions`
2. `fix: stabilize session ownership and batch defaults`
3. `test: cover descendant process groups and shutdown races`
4. `fix: harden agy subprocess lifecycle`
5. `test: cover multi-runtime record persistence`
6. `fix: make record persistence multi-runtime safe`
7. `fix: drain futures and bound retained state`
8. `fix: parse noisy multiline agy JSON output`
9. `test: add real Hermes plugin contract coverage`
10. `docs: correct installation execution modes and security settings`
11. `release: delegate-agy 0.2.0`

## 15. Final handoff report required from the implementing agent

The final response must include:

1. concise summary of behavior changed;
2. files changed;
3. exact tests run and results;
4. confirmation that no live integration/quota test ran, unless authorized;
5. remaining risks or intentionally deferred work;
6. `git status --short` result;
7. explicit confirmation that no push occurred.
