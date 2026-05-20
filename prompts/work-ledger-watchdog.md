# Work Ledger Watchdog v1 Wake Handler

This is the main-session wake handler for non-clean watchdog results. The clean
path is handled by the deterministic LaunchAgent runner and must not wake the
LLM.

Commands:
```bash
openclaw-ledger watchdog-check --include-cron
```

Procedure:
1. Inspect the precomputed watchdog-check result included in this system event.
   Rerun `watchdog-check --include-cron` only when the included evidence is
   missing, stale, or suspicious.
2. If a rerun returns `status=clean`, reply exactly `HEARTBEAT_OK`. Do not
   send a visible message.
3. If `status=error`, inspect the runner error. Only send a visible message if
   the error is user-relevant or repeated; otherwise record enough ledger
   progress for later diagnosis and reply `HEARTBEAT_OK`.
4. If `wake_reason=recovery`, process each recovery packet from ledger state
   only:
   - inspect ledger events plus referenced artifacts/tasks/subagents;
   - do not use chat-history guesses as truth;
   - do not repeat external, destructive, public, Gateway, browser, email, git
     push, or other risky side effects unless durable clearance is already in
     the packet;
   - execute only the next safe recovery action;
   - verify;
   - send one visible completion report through the packet's `visible_delivery`
     route;
   - make the visible report user-final: do not say that internal bookkeeping
     such as `report-sent` recording or a follow-up `scan` remains; use
     `Remaining: 없음` when only ledger cleanup/verification is left;
   - after the visible report succeeds, record `complete-reported` with the
     packet's `visible_delivery` and the delivered message id. Use plain
     `report-sent` only when the ledger item is already in
     `completed_unreported` or `failed_unreported` and you are intentionally
     recording proof for that existing terminal state.
5. If `wake_reason=referenced_task_reconciliation`, inspect each terminal
   task/subagent referenced by active ledger work:
   - integrate the terminal result into the existing ledger work, or report the
     terminal failure if user-relevant;
   - record `complete`, `fail`, `wait`, `progress`, or `abandon` as
     appropriate before any visible report;
   - if the terminal result was inspected but the user-facing work remains
     active, record `terminal-ref-handled` with the packet's
     `terminal_ref_fingerprints` so the same completed reference does not keep
     waking the main session;
   - do not restart the task, rerun subagents, or repeat risky side effects from
     this signal alone;
   - if the user-facing work is now complete, send one visible completion report
     and record `complete-reported` with the delivered message id. Use
     `report-sent` only for an already-unreported terminal ledger item.
6. If a recovery cannot finish in this turn because it is still waiting or
   blocked, do not send repeated reminders every tick. First record the
   substantive durable outcome (`wait`, `wait-reminder-sent`, or `abandon`) or
   send the visible update if one is required. Do not use silent `progress` to
   refresh a `waiting_user` recovery. Record `wake-delivered` with the packet
   `recovery_fingerprint` only after that durable state transition or visible
   update succeeds.
7. If a packet has context gaps, do not invent the request. Repair the ledger
   context, ask the user only for a real missing decision, or mark
   blocked/abandoned.
8. If `wake_reason=orphan_reconciliation`, reconcile each orphan before any
   user-visible message:
   - refresh live subagent/task state by checking current active handles and
     durable task states, including queued/running and terminal states such as
     succeeded, failed, timed_out, cancelled, lost, shutdown, or notFound;
   - if it is already completed, errored, shutdown, lost, notFound, or otherwise
     terminal/no-live-handle, and the user-facing work is already complete or
     unaffected, record `orphan-handled --orphan-fingerprint
     <orphan_fingerprint> --orphan-fingerprints
     '<orphan_fingerprints_json_array>' --resolution terminal_no_action --note
     "<refresh result and why no user message was needed>"` and send no
     message;
   - if refresh shows it is now referenced by active ledger work, record
     `orphan-handled --orphan-fingerprint <orphan_fingerprint>
     --orphan-fingerprints '<orphan_fingerprints_json_array>' --resolution
     referenced_after_refresh --note "<matched ledger/work reference>"` and
     send no message;
   - if it is clearly not user-relevant but still active or unresolved, do not
     durably suppress it with `orphan-handled`; leave it visible to future
     checks or include it in the aggregated result only when user action/trust
     requires it;
   - if it is still active but fresh, do not warn;
   - if any still-active stale user-relevant orphans remain after reconciliation,
     send at most one aggregated visible result message with the inspected state
     and safest next action; record `orphan-warning-sent` for each reported
     orphan that has an `orphan_fingerprint`;
   - do not send a separate discovery warning before the reconciliation result;
   - do not auto-create ledger entries, restart work, or recover from orphan
     output alone.
9. If no visible recovery report and no user-relevant orphan warning was
   required, reply exactly `HEARTBEAT_OK`.

Scope limits:
- This watchdog may update only work-ledger records and artifacts directly
  required by recovery.
- For orphans, safe cleanup of terminal handles is allowed; risky side effects
  are not. Harmless handled orphans should be durably recorded, not announced.
- It must not restart Gateway, run doctor, send email, use browser, push git, or
  retry risky side effects unless the recovery packet already contains durable
  clearance.

User-visible report style:
- Keep completion reports short and outcome-focused. The user should learn what
  happened, what was fixed or found, the smallest meaningful verification
  result, and whether any action remains.
- Do not expose routine internal checks such as Gmail auth, Codex OAuth, repo
  HEAD, internal task ids, delivery ids, recovery fingerprints, or raw
  smoke-suite lists unless they directly explain a user-relevant failure.
- Prefer one compact Checked line such as:
  - `Checked: 복구 루프가 다시 깨우지 않는 것까지 확인`
  - `Checked: 핵심 회귀 테스트 통과, watchdog-check clean`
- If several tests ran, summarize them as `핵심 회귀 테스트 통과`; keep detailed
  test names in ledger verification, not in the Telegram report.
- Mention a specific internal check only when it failed, blocked completion, or
  changes the user's next decision.
