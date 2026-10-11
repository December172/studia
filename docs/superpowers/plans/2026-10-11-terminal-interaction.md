# Terminal interaction implementation plan

> **For agentic workers:** Use executing-plans task-by-task. Claude Code implements; the coordinator reviews, verifies and handles GitHub integration.

**Goal:** Make model testing, concurrency editing, navigation and running progress work as a coherent in-place terminal interface.

**Architecture:** Retain prompt_toolkit and Rich, introducing a focused terminal lifecycle boundary rather than a framework migration. Let one renderer own the visible page and one input owner handle keys at any instant. Reuse atomic SettingsStore persistence and existing completion-driven schedulers.

**Tech Stack:** Python 3.9+, prompt_toolkit, Rich, unittest, POSIX PTY tests (skip on unsupported platforms).

**Spec:** User-approved conversation design and https://github.com/yijunjie07/icve-ai-assistant/issues/3.

## Global constraints

- AI and study independently configurable 1–8, default 8; saved choices honored. Invalid/missing preferences fall back to 8. Do not change low-level library defaults unnecessarily.
- Current run snapshots its setting; next run uses saved changes. Completion-driven refill stays intact.
- Keep secrets redacted. Use synthetic clients and temporary settings; never load real credentials, call live platforms, or submit real answers during tests.
- Python 3.9 and 3.12 CI must pass. No new runtime dependency unless unavoidable.
- Preserve Esc back, Ctrl+C stop, manual answering, non-TTY/scripted fallback and existing cancellation semantics.
- No commits/pushes by implementer; coordinator handles review and integration.

## Review focus

- Testing a non-active/built-in model does not activate it or hit the built-in edit prohibition.
- Cancel/invalid input/save failure in concurrency editing must not partially persist or claim success.
- Runtime keystrokes must not echo or queue into a subsequent default-Enter confirmation; signal/cancel and manual-answer ownership must be restored.
- Repeated page transitions, nested full-screen pickers, resize and exceptions must not leak alternate-screen or cursor state.
- Existing selected courses/papers, page and cursor survive going back; long/CJK content fits terminal width and small terminal heights.

### Task 1: Inline model connection testing

**Files:** model_ui.py, ui.py, app.py; tests/test_model_ui.py, tests/test_model_management.py, tests/test_escape_navigation.py.

**Interfaces:** Extend choose_model/model_picker with an optional selected-profile test callback; test callback returns safe status text. Existing ModelChoice select/add/edit/back behavior stays supported. Application.manage_models resolves profile configuration without activation. Use a background callback during full-screen testing so pending/result render in place; do not nest Rich Live over prompt_toolkit ownership.

- [x] Add failing behavior tests: t and visible test button test highlighted model, builtin accepted, active ID unchanged, pending state prevents duplicate requests, result and elapsed time displayed, cursor retained, errors safely handled.
- [x] Run focused unittest discovery for model UI and management; record expected failures.
- [x] Implement visible use/test/edit buttons and t shortcut, maintain add action and focus navigation. Retain safe save-and-test editing behavior. Remove top-level standalone connection test; answer-summary shortcut may remain.
- [x] Run model tests and navigation tests to green; ensure leaving during pending test does not leave terminal broken or stale callbacks mutating another page.

### Task 2: Concurrency form and execution settings

**Files:** new concurrency_ui.py, settings.py, app.py, ui.py, course_flow.py; tests/test_concurrency_settings.py and relevant flow/UI tests.

**Interfaces:** edit_concurrency(current, *, input=None, output=None) returns a dict with ai/study or None for cancel. TerminalUI.concurrency_editor wraps native and scripted paths. SettingsStore.concurrency returns validated persisted values with fallback 8. Application.study and execute_ai capture respective setting once; confirmation pages receive/show the same intended value.

- [x] Add failing tests for default/invalid/saved settings, arrows/digits, bounds, save/cancel/reset, atomic persistence failure, and propagation of distinct ai/study counts to runners and confirmation pages.
- [x] Run focused settings/flow tests and observe expected failures.
- [x] Add settings menu concurrency entry, form with two rows and Save/Cancel/Reset, 1–8 constraints and next-run hint. Reuse save_concurrency atomically; show validation/save errors without discarding draft. Wire actual execution to saved settings, not hardcoded INTERACTIVE_WORKERS.
- [x] Run relevant tests to green and keep existing eight-worker completion-driven regression tests.

### Task 3: Terminal input and page lifecycle

**Files:** ui.py; focused terminal lifecycle module if useful; tests/test_terminal_lifecycle.py, tests/test_manual_answer_ui.py, tests/test_escape_navigation.py, tests/test_ui.py.

**Interfaces:** TerminalUI.session owns terminal screen lifecycle, activity owns runtime key suppression, ask/manual_answer temporarily own interactive input. Factor context managers only where ownership is explicit and reversible. Existing public view interfaces remain compatible.

- [x] Reproduce in controlled PTY: while activity runs ECHO/ICANON are currently true and three CRs echo three CRLFs. Capture evidence with continuous PTY draining to avoid pipe-buffer deadlock.
- [x] Add failing regression tests: no echo/queued Enter in activity, Ctrl+C interruption, restoration after errors/nesting/manual input, repeated page transitions without accumulating blank screens. Test both native UI and scripted fallback.
- [x] Verify page transition root cause before choosing implementation. Use an alternate-screen/page lifecycle or unified prompt_toolkit rendering with correct nested behavior; do not append huge padding or merely increase refresh rate. Ensure prompt rendering does not push content below viewport and Rich frames cannot wrap beyond terminal width.
- [x] Implement scoped input ownership and screen redraw; discard runtime keys, preserve Ctrl+C, suspend guard during manual answers and safely restore on every exit. Keep progress/log area bounded by actual terminal height.
- [x] Run PTY tests at 120x30 and smaller size, including resize; run manual answer and navigation regressions.

### Task 4: Verification and integration

**Files:** README.md or existing user docs; implementation plan.

- [x] Update current docs describing editable concurrency, model test controls, and navigation; leave historical design records intact.
- [x] Run `/Users/yijunjie/mooc/.venv/bin/python -m unittest discover -s tests -v` and inspect full result.
- [x] Coordinator reviews actual diff and performs independent PTY verification plus independent code review. Resolve findings, rerun affected checks/full suite as warranted.
- [ ] Coordinator commits and pushes branch, creates PR closing #3, attaches PR, waits for Python 3.9/3.12 CI, merges only after checks pass, and updates main cleanly.

## Verification record

- Local Python 3.9.6: 411 tests passed (full unittest discovery).
- Real controlling PTY tests cover nested forms, Enter suppression, later confirmation, resize, Ctrl+C and manual answer handoff.
- Independent review findings resolved: exact connection-success status, settings health-save transaction race, and Windows input ownership. Windows mode boundaries are mocked; native Windows execution was unavailable.
- Synthetic terminal QA at 120x30 and 60x16 verified no queued Enter acceptance and restored terminal modes. No live model/platform requests or submissions were used.
- Claude Code supplied implementation drafts; coordinator completed integration and review corrections.
- Remote CI and merge tracked by the closing PR for issue #3.
