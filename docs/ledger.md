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

## Work, Not Turn

Work Ledger follows a piece of work, not a single chat reply.

A small request may start and finish in one reply, so it usually does not need a
ledger entry. Larger work can continue across tool calls, subagents, cron
wakeups, process waits, interruptions, and later recovery replies.

The ledger is the handoff note for that larger work. If the current reply is
interrupted, the next recovery reply can read what was requested, what already
happened, what still needs checking, and whether the user already received a
completion report.

Hooks observe what happens inside one reply. Work Ledger keeps the work state on
disk so another reply can continue it later.

## Core Concepts

- **Work entry**: one durable record for a long-running task.
- **Events**: append-only progress, wait, verification, failure, completion, and report-sent records.
- **Recovery packet**: a compact instruction bundle for the main session after stale or unreported work is detected.
- **Visible report**: the final user-facing completion report.
- **Idempotency context**: information used to avoid repeating unsafe side effects.

## Hook And Idempotency Policy

Hooks are best-effort helpers, not the source of truth. Work Ledger must still
recover from durable events and current artifacts when a hook is missed, fired
twice, or arrives out of order.

Idempotency is required only at non-idempotent side-effect boundaries:
external messages, public posts, destructive actions, and Gateway/runtime
changes. Read-only checks, progress updates, verification records, and local
file checkpoints should stay lightweight and must not require idempotency keys.

When an idempotency key is needed, the runtime should own it. Users, prompts,
and LLM-generated payloads should not supply their own keys.

## Expected Lifecycle

1. Start a work entry before meaningful side effects.
2. Record progress as files, tasks, or subagents change.
3. Record waits when blocked on subagents, user input, or external systems.
4. Record verification when checks are running or complete.
5. Mark the work complete only after the requested outcome is handled.
6. Send a visible completion report.
7. Record `report_sent`.

For GoalFlow approval pauses, the visible approval request/update and the
Ledger wait state are both required. GoalFlow uses `waiting_approval` /
`await_owner_approval`; Work Ledger should record the same pause as
`wait --status waiting_user` after the visible update is delivered. This keeps
watchdog recovery from treating a healthy approval wait as a generic stale run.
Approval terminal states that still need the owner to choose a recovery path,
such as `approval_rejected` or `approval_expired`, should also refresh
`waiting_user` after the visible update so the previous approval wait does not
look stale or ambiguous.

## Selective Registration Policy

Do not register every user turn. Work Ledger is for work with durable recovery
value: work where interruption would leave useful state behind, make the next
safe action unclear, or risk a missed completion report.

Use this gate before starting a ledger entry:

1. Will this work take about 10+ minutes, span multiple steps/artifacts, or wait
   on another task, subagent, process, cron run, or external system?
2. If the current reply is interrupted, would the next reply need durable context
   to continue safely?
3. Could blind repetition duplicate external actions, corrupt state, pollute
   files, or produce a misleading completion report?

Create a ledger entry when any answer is strongly yes. Typical examples:

- GoalFlow, Goal, or Ralph-style work where goal state continues across
  multiple steps
- large refactors, major code changes, or implementation spanning multiple
  files/modules
- research, investigation, analysis packages, or report generation with
  multiple outputs or long runtime

Tool use alone is not enough. A quick subagent question, browser lookup, cron
status check, Gateway status check, or external-action draft does not need Work
Ledger. Use Work Ledger only when the tool work becomes long-running, changes
durable state, can be duplicated unsafely, or must be handed off to a later
reply.

Do not create a ledger entry when all of these are true:

- the work is a short explanation, design discussion, or one-shot read-only
  lookup
- any file/code change is small enough to finish, verify, and report in the
  current reply
- there are no background tasks, waits, or multi-step artifacts to recover
- failure can be handled by the user asking again with little lost context
- the answer will be completed in the current reply without background waits

Use `quick-start` presets only after this gate says the work needs recovery
tracking. The helper reduces missed fields; it is not a hook and must not be
used to auto-register every request. Small one-reply edits do not need Work
Ledger.

Example:

```bash
python3 scripts/work_ledger.py quick-start \
  --kind coding \
  --summary "Fix PQ runtime health check" \
  --owner-session-key "agent:main:telegram:direct:test-user" \
  --visible-delivery '{"channel":"telegram","to":"test-user"}' \
  --artifact-paths "pq_platform/backend/main.py"
```

## Recovery Behavior

On recovery, the main session should:

1. Read the ledger entry and latest events.
2. Inspect current artifacts, tasks, and subagents.
3. Avoid repeating external, destructive, or non-idempotent actions without approval.
4. Execute only the next safe action.
5. Verify the result.
6. Send one visible completion report.
7. Record that the report was sent.

### 2026-05-18 Watchdog Smoke Results

The main-session watchdog route was tested with two controlled recovery cases:

- \`completed_unreported\`: the watchdog detected an already-completed ledger item
  missing a visible report, sent the completion report, recorded \`report-sent\`,
  and the next scan was clean.
- \`stale running\`: the watchdog detected a stale running ledger item, executed
  only the packet's safe local marker-file action, verified the marker file,
  recorded verify/complete/report-sent, and the next scan was clean.

User-facing recovery reports must be final reports. Internal bookkeeping such
as \`report-sent\` recording or a follow-up clean scan should not be described as
\`Remaining\` unless the user must act or the recovery is genuinely incomplete.

## Orphan Active Work Reconciliation

`openclaw-ledger orphans` is a read-only reconciliation check. It compares active
OpenClaw tasks with active ledger task/subagent references and reports active
tasks that are not referenced by a ledger entry. Fresh tasks are ignored by
default so short subagent/tool work does not create pressure to ledger every
request.

The output is not a request to immediately warn the user:

- do not auto-create ledger entries from orphan output
- do not recover or retry work from orphan output alone
- inspect the task first
- if the orphan is terminal/no-live-handle and user-facing work is complete or
  unaffected, record `orphan-handled --orphan-fingerprint <orphan_fingerprint>
  --orphan-fingerprints '<orphan_fingerprints_json_array>' --resolution
  terminal_no_action` with a note explaining the refreshed terminal/no-impact
  evidence, then send no message
- if refresh shows the task is now referenced by active ledger work, record
  `orphan-handled --orphan-fingerprint <orphan_fingerprint>
  --orphan-fingerprints '<orphan_fingerprints_json_array>' --resolution
  referenced_after_refresh` and send no message
- if it is clearly not user-relevant but still active or unresolved, do not
  durably suppress it with `orphan-handled`; either leave it visible to future
  checks or include it in the aggregated result when user action/trust requires
  it
- warn the user only when a stale active user-relevant orphan remains after
  reconciliation; send at most one aggregated result message, not a discovery
  warning followed by a second outcome message
- handled orphan suppression is durable and does not expire like visible warning
  suppression; visible `orphan-warning-sent` records still require the visible
  delivery route plus delivery message id and suppress repeat warnings for 24
  hours
- orphans without a stable identity fingerprint are not silently suppressible;
  inspect them and include them only in the aggregated result message when they
  remain user-relevant
- keep the default age threshold unless you are explicitly debugging fresh
  tasks; tool use alone still does not justify a ledger entry
- run the clean path outside the LLM; the 10-minute check should be a
  deterministic local runner that wakes the main session only for non-clean
  results

## Watchdog Check Contract

`openclaw-ledger watchdog-check --include-cron` is the deterministic watchdog
triage contract. The LaunchAgent runner executes it every 10 minutes. It runs
recovery scan, referenced task terminal-state reconciliation, and orphan
reconciliation input gathering, then returns one of:

- `clean`: no LLM/user-visible work is needed.
- `needs_wake` with `wake_reason=recovery`: process recovery packets, verify,
  send one visible completion report, then record `report-sent`.
- `needs_wake` with `wake_reason=referenced_task_reconciliation`: inspect
  ledger-referenced terminal tasks/subagents, integrate their result or report
  failure, and do not restart or repeat side effects from this signal alone.
- `needs_wake` with `wake_reason=orphan_reconciliation`: refresh/reconcile
  orphan state before any warning, then either record `orphan-handled` silently
  or send one aggregated result and record `orphan-warning-sent`.
- `error`: inspect runner errors before deciding whether a visible message is
  warranted.

The check is intentionally not a recovery engine. It must not restart work,
repeat risky side effects, or send user-visible messages from scan/orphan output
alone. The clean result must not wake the main session.

## Boundaries

Work Ledger is independent from GoalFlow. It can recover any long-running work, not only goals.

It should not:

- infer missing source context from chat history
- invent expected outputs
- mutate project files as part of heartbeat checks
- retry destructive side effects automatically
- replace the final visible user report

## Implementation Readiness

Before adding hook integration, confirm:

- the existing ledger path works without hooks
- hook failures do not block normal work
- duplicate hook delivery cannot duplicate external side effects
- recovery still reconciles artifacts, tasks, and visible reports before acting
- tests cover missed hook, duplicate hook, and unsafe retry cases

## CLI

The main entry point is:

```bash
/Users/moon/.openclaw/bin/openclaw-ledger --help
```

For development, the deployed command should stay in sync with the workspace
script:

```bash
python3 scripts/work_ledger.py --help
```

The smoke test is:

```bash
python3 tests/smoke/work_ledger_smoke.py
```
