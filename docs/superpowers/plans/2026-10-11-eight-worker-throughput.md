# Eight-worker throughput implementation plan

> **For agentic workers:** Use executing-plans for this single implementation task. The user selected Claude Code as implementer and Codex as coordinator/reviewer.

**Goal:** Run AI answers and automatic study with at most eight concurrent tasks, replacing completed tasks without waiting for a batch.

**Architecture:** Keep the existing bounded completion-driven executors. Raise the application's shared execution limit to eight, derive its UI messages from that limit, and retain coordinator-owned validation, persistence and cancellation.

**Tech Stack:** Python 3.9+, unittest, concurrent.futures; no new dependencies.

**Spec:** https://github.com/yijunjie07/icve-ai-assistant/issues/1 and the user's approved instructions: both AI and study use up to eight workers, completed tasks immediately yield to the next; issue → branch → PR → CI → merge.

## Global constraints

- Maximum eight active tasks; no fixed delay or batch barrier on successful completion.
- Keep courses/papers sequential and answers/submissions in original question order.
- Preserve manual-input draining, cancellation, Retry-After cooldown, validation and submission protection.
- Do not use real account settings, call live answer/study APIs, change models or change release versions.
- Keep the simplified settings menu; historical concurrency settings do not override the execution limit.

## Review focus

- Slow first task must not block replacement work in either AI or study.
- Multiple completed answers must be inspected before dispatch if they contain a fatal error or need manual input.
- Fewer than eight tasks must not create duplicate work.
- Cancellation/authentication failure/rate limits must stop new work and keep completed results accurate.
- Higher concurrency must preserve question identity, ordering and single submission.

## Task 1: Restore eight-worker execution and prove rolling dispatch

**Files:** base/concurrency.py, app.py, tests/test_concurrency_settings.py, tests/test_ai_concurrency.py, tests/test_study_concurrency.py, README.md, docs/USAGE.md, docs/DEVELOPMENT.md.

**Interfaces:** Application.execute_ai and Application.study continue using INTERACTIVE_WORKERS. Existing ParallelAnswers and run_resources remain the scheduling boundary.

- [ ] Update the app execution regression test to expect eight AI/study workers despite historical settings; run it and capture the expected 2-versus-8 failure.
- [ ] Add/extend deterministic event-driven tests proving a ninth task starts while an earlier task is blocked, for AI and both study handlers. Assert the eight-worker bound, final results/order and no duplicate work. Reuse existing cancellation/cooldown tests.
- [ ] Set INTERACTIVE_WORKERS to eight and derive startup copy from the effective limit. Only change scheduling if the rolling-dispatch tests expose a defect.
- [ ] Update current documentation to describe eight-worker completion-driven execution and remove obsolete configurable/batch descriptions.
- [ ] Run focused tests, then `python -m unittest discover -s tests -v`; review the diff and commit.
- [ ] Push the branch, open a PR closing issue #1, independently review the change, wait for Python 3.9/3.12 CI, then merge and synchronize the main checkout.

## Existing CI failure discovered during verification

The base commit's Python 3.12 job fails `test_native_menu_and_summary_share_escape_input`: ANSI styling separates `Esc` from its label, so a raw-string assertion misses visible text. Reproduced locally with `TERM=xterm-256color python -m unittest tests.test_escape_navigation.EscapeNavigationTests.test_native_menu_and_summary_share_escape_input`. Decode ANSI before the existing visible-text assertions in this test, retaining its real keyboard navigation checks. Run the full suite with that terminal setting before the PR; do not disable color in production or weaken CI.
