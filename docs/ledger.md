# Work Ledger

Work Ledger is a small recovery system for long-running OpenClaw work. It records enough state for the main session to recover safely after interruption, stale execution, or a missed completion report.

## Purpose

Work Ledger exists to answer:

- What work was requested?
- What side effects were already attempted?
- What artifacts should exist?
- What is the next safe recovery action?
- Was the user already sent a visible completion report?

It does not perform project-specific completion judgment. For example, it does not decide whether a GoalFlow goal is complete.

## Core Concepts

- **Work entry**: one durable record for a long-running task.
- **Events**: append-only progress, wait, verification, failure, completion, and report-sent records.
- **Recovery packet**: a compact instruction bundle for the main session after stale or unreported work is detected.
- **Visible report**: the final user-facing completion report.
- **Idempotency context**: information used to avoid repeating unsafe side effects.

## Expected Lifecycle

1. Start a work entry before meaningful side effects.
2. Record progress as files, tasks, or subagents change.
3. Record waits when blocked on subagents, user input, or external systems.
4. Record verification when checks are running or complete.
5. Mark the work complete only after the requested outcome is handled.
6. Send a visible completion report.
7. Record `report_sent`.

## Recovery Behavior

On recovery, the main session should:

1. Read the ledger entry and latest events.
2. Inspect current artifacts, tasks, and subagents.
3. Avoid repeating external, destructive, or non-idempotent actions without approval.
4. Execute only the next safe action.
5. Verify the result.
6. Send one visible completion report.
7. Record that the report was sent.

## Boundaries

Work Ledger is independent from GoalFlow. It can recover any long-running work, not only goals.

It should not:

- infer missing source context from chat history
- invent expected outputs
- mutate project files as part of heartbeat checks
- retry destructive side effects automatically
- replace the final visible user report

## CLI

The main entry point is:

```bash
python3 src/work_ledger.py --help
```

The smoke test is:

```bash
python3 tests/smoke/work_ledger_smoke.py
```
