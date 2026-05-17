# Work Ledger

Work Ledger is a small recovery system for long-running OpenClaw work. It records enough state for a watchdog to detect stale or completed-but-unreported work, wake the main session, and let that session recover safely after interruption or a missed completion report.

## Purpose

Work Ledger exists to answer:

- What work was requested?
- What side effects were already attempted?
- What artifacts should exist?
- What is the next safe recovery action?
- Was the user already sent a visible completion report?
- Should the watchdog wake the main session for recovery?

It does not perform project-specific completion judgment. It records recovery state; the caller decides whether the original task is actually complete.

## Core Concepts

- **Work entry**: one durable record for a long-running task.
- **Events**: append-only progress, wait, verification, failure, completion, and report-sent records.
- **Watchdog scan**: a periodic check for stale, interrupted, or completed-but-unreported work.
- **Recovery packet**: a compact instruction bundle used to wake and guide the main session after stale or unreported work is detected.
- **Visible report**: the final user-facing completion report.
- **Idempotency context**: information used to avoid repeating unsafe side effects.

## Expected Lifecycle

1. Start a work entry before meaningful side effects.
2. Record progress as files, tasks, or subagents change.
3. Record waits when blocked on subagents, user input, or external systems.
4. Record verification when checks are running or complete.
5. Mark the work complete only after the requested outcome is handled.
6. If work becomes stale or completed-but-unreported, the watchdog wakes the main session with a recovery packet.
7. Send a visible completion report.
8. Record `report_sent`.

## Recovery Behavior

On watchdog recovery, the main session should:

1. Read the ledger entry and latest events.
2. Inspect current artifacts, tasks, and subagents.
3. Avoid repeating external, destructive, or non-idempotent actions without approval.
4. Execute only the next safe action.
5. Verify the result.
6. Send one visible completion report.
7. Record that the report was sent.

## Boundaries

Work Ledger is project-agnostic. It can recover any long-running work when the caller records enough context.

It should not:

- infer missing source context from chat history
- invent expected outputs
- mutate project files as part of heartbeat checks
- retry destructive side effects automatically
- replace the final visible user report

## CLI

The repository entry point is:

```bash
python3 src/work_ledger.py --help
```

After installation, use:

```bash
~/.openclaw/bin/openclaw-ledger --help
~/.openclaw/bin/openclaw-ledger scan
```

Most users do not need these commands directly. They are mainly for orchestrator integrations, smoke tests, and custom recovery wiring.

Example command sequence:

```bash
python3 src/work_ledger.py start --work-id example-work --request-summary "Implement and verify the requested change" --owner-session-key agent:main:example --visible-delivery '{"session_key":"agent:main:example"}'
python3 src/work_ledger.py progress --work-id example-work --note "Implementation started"
python3 src/work_ledger.py verify --work-id example-work --verification '{"tests":"passed"}'
python3 src/work_ledger.py complete --work-id example-work --note "Work completed"
python3 src/work_ledger.py scan
```

The smoke test is:

```bash
python3 tests/smoke/work_ledger_smoke.py
```
