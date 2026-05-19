# OpenClaw Ledger

OpenClaw Ledger is a recovery safety net for OpenClaw automation. It records progress, waits, verification, failures, and visible report delivery, then lets a watchdog detect unfinished work and wake the main session with a recovery packet.

Why it exists: long-running agent work can be interrupted by a session stop, Gateway restart, model failure, network issue, or missed visible delivery. Without a ledger, recovery has to infer what happened from chat history and logs. That can lead to duplicate work, repeated side effects, or a task that finished internally but was never reported to the user.

Most users do not run Ledger commands by hand. Install or wire it into your OpenClaw workflow, then let the orchestrator record work before side effects while a watchdog checks for stale or completed-but-unreported work. When recovery is needed, the watchdog wakes the main session so it can inspect the current state, continue safely, and send one visible completion report.

Think of Ledger as a watchdog for unfinished work. It does not do the work itself; it watches the work records and wakes the main OpenClaw session when something needs recovery.

## When To Use It

Use Ledger for work where interruption would make recovery unsafe or confusing:

- file edits, commits, pushes, or generated artifacts
- subagents, browser automation, cron, Gateway, or external systems
- user approval gates or waiting states
- verification-heavy work where completion must be reported
- any task where repeating the same action could cause duplicate or risky side effects

Simple one-shot answers usually do not need Ledger.

## Flow

| Stage | What Happens |
|---|---|
| **1. Record** | Start work entry -> record progress -> record wait or verification |
| **2. Detect** | Watchdog check -> find stale or completed-but-unreported work -> create recovery packet |
| **3. Recover** | Wake main session -> reconcile state -> send visible report -> mark reported |

## What It Does

- Starts a durable work record before meaningful side effects.
- Records progress, waits, verification, failures, and report delivery.
- Detects stale or completed-but-unreported work through watchdog checks.
- Wakes the main session with recovery packets that contain enough context for safe reconciliation.
- Requires visible completion reporting before work is marked reported.

## Integration

Ledger stores entries under the OpenClaw workspace by default. It uses `$OPENCLAW_WORKSPACE` when set, otherwise `~/.openclaw/workspace`.

A typical integration has three parts:

1. The orchestrator records the work before meaningful side effects.
2. A watchdog periodically runs `~/.openclaw/bin/openclaw-ledger watchdog-check --include-cron`.
3. If unfinished work is found, the watchdog wakes the main session with a recovery packet. It does not send raw discovery warnings by itself.

Here, "main session" means the user-facing OpenClaw conversation that owns the work. "Visible report" means the final message the user actually receives, not just an internal tool result.

## Install

The default install is the full recovery package. It assumes OpenClaw is already
installed and Telegram is connected. The installer configures:

- `openclaw-ledger` CLI
- `hook_event_contract.py`
- deterministic watchdog runner
- packaged recovery prompt
- `~/.openclaw/ledger/config.json`
- a macOS LaunchAgent that checks every 10 minutes

~~~bash
curl -fsSL https://raw.githubusercontent.com/moonhwilee/openclaw-ledger/main/install.sh | bash
~~~

What the installer does:

1. Downloads the Ledger CLI, hook contract, watchdog runner, and recovery prompt.
2. Verifies Python syntax and Ledger CLI startup before replacing live files.
3. Detects a single recent main Telegram direct session, or uses `--session-key`.
4. Writes private local config under `~/.openclaw/ledger/config.json`.
5. Installs a macOS LaunchAgent that runs the deterministic watchdog every 10 minutes.

If there is more than one possible Telegram direct session, pass the owner
session explicitly:

~~~bash
curl -fsSL https://raw.githubusercontent.com/moonhwilee/openclaw-ledger/main/install.sh | bash -s -- --session-key agent:main:telegram:direct:<target>
~~~

CLI-only install is advanced mode. It does not install automatic recovery
wakeups:

~~~bash
curl -fsSL https://raw.githubusercontent.com/moonhwilee/openclaw-ledger/main/install.sh | bash -s -- --cli-only
~~~

Verify:

~~~bash
~/.openclaw/bin/openclaw-ledger --help
~/.openclaw/bin/openclaw-ledger watchdog-check --include-cron
launchctl print gui/$UID/com.openclaw.ledger.watchdog
~~~

Runner diagnostics:

~~~bash
~/.openclaw/ledger/work_ledger_watchdog_runner.py --help
OPENCLAW_LEDGER_CONFIG=~/.openclaw/ledger/config.json ~/.openclaw/ledger/work_ledger_watchdog_runner.py
~~~

macOS LaunchAgents do not wake a sleeping Mac. Checks resume after login/wake.

Public builds must not ship maintainer usernames, absolute home paths, chat IDs,
or personal session keys as defaults.

## How It Is Used

Typical flow:

1. A task with meaningful state or side effects starts.
2. The orchestrator creates a Ledger entry.
3. Progress, waits, verification, and failures are appended as events.
4. If the session stops responding or completion was never reported, the watchdog check produces a recovery packet.
5. The watchdog wakes the main session.
6. The recovered session inspects the current state, continues safely, sends one visible completion report, and records that the report was sent.

For command details:

~~~bash
~/.openclaw/bin/openclaw-ledger --help
~/.openclaw/bin/openclaw-ledger watchdog-check --include-cron
~~~

If `~/.openclaw/bin` is on your PATH, the shorter `openclaw-ledger` command works too.

## Repository Layout

- src/work_ledger.py - CLI implementation.
- src/hook_event_contract.py - hook observation/guardrail classifier used by Ledger recovery safety checks.
- tests/smoke/install_smoke.sh - installer smoke tests for CLI-only and full no-LaunchAgent setup.
- tests/smoke/work_ledger_smoke.py - behavior smoke tests.
- tests/smoke/work_ledger_hook_smoke.py and tests/smoke/hook_* - hook contract and guardrail smoke tests.
- tests/smoke/work_ledger_watchdog_runner_smoke.py - deterministic watchdog runner smoke tests.
- docs/ledger.md - current behavior, recovery policy, and command reference.

## Local Tests

~~~bash
python3 -m py_compile src/hook_event_contract.py src/work_ledger.py scripts/work_ledger_watchdog_runner.py tests/smoke/work_ledger_smoke.py tests/smoke/work_ledger_hook_smoke.py tests/smoke/hook_event_contract_smoke.py tests/smoke/hook_action_classifier_golden_smoke.py tests/smoke/work_ledger_watchdog_runner_smoke.py
tests/smoke/install_smoke.sh
python3 tests/smoke/work_ledger_smoke.py
python3 tests/smoke/work_ledger_hook_smoke.py
python3 tests/smoke/hook_event_contract_smoke.py
python3 tests/smoke/hook_action_classifier_golden_smoke.py
python3 tests/smoke/work_ledger_watchdog_runner_smoke.py
~~~

## Maintainer Workflow

Use this repository as the source of truth for Ledger changes.

~~~bash
# edit and test inside this repo
python3 tests/smoke/work_ledger_smoke.py
python3 tests/smoke/work_ledger_hook_smoke.py
python3 tests/smoke/hook_event_contract_smoke.py
python3 tests/smoke/hook_action_classifier_golden_smoke.py
python3 tests/smoke/work_ledger_watchdog_runner_smoke.py
tests/smoke/install_smoke.sh

# deploy the verified repo version to the local OpenClaw server
scripts/deploy-local.sh

# publish with the normal GitHub flow
git checkout -b codex/my-ledger-change
git add .
git commit -m "Describe Ledger change"
git push -u origin HEAD
gh pr create --draft --base main --head "$(git branch --show-current)"
~~~

Do not edit the local installed files under `~/.openclaw/bin` as the source of truth. They are deploy outputs from this repository.

## Release Policy

GitHub Releases are the public version source for Ledger. The release shown on the repository sidebar, for example `openclaw-ledger v0.3.0`, is created from the matching Git tag `v0.3.0`.

Ledger does not keep a separate in-repo version file. For a release:

1. Merge the verified PR to `main`.
2. Create and push an annotated tag such as `v0.3.0` on the merged main commit.
3. Create the GitHub Release from that tag with the title `openclaw-ledger v0.3.0`.

Do not treat ad-hoc text files as release versions; they drift from GitHub Releases too easily.

## License

MIT
