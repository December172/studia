# Fully AI Answering Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan. The user explicitly chose Claude Code implementation and authorized issue → branch → PR → CI/review → merge.

**Goal:** Simplify the terminal UI and complete supported papers entirely through AI without confidence gates or manual-answer prompts.

**Architecture:** Keep bounded AI generation, local checkpoints and final validated submission. Remove confidence policy and manual interaction from the answering workflow. Confidence may remain model metadata but must not decide acceptance.

**Tech Stack:** Python 3.9+, unittest, Rich, prompt_toolkit.

**Spec:** User request dated 2026-10-11 and the GitHub issue created for this branch.

## Global Constraints

- Follow the existing Python 3.9 floor; no new dependencies.
- Remove runtime footer `Ctrl+C 停止 · 运行中无需按回车` and AI startup concurrency/save explanatory log.
- Remove confidence UI/settings and verbose concurrency explanation from AI execution summary; retain necessary model, connection and execution-mode information.
- Accept valid AI answers regardless of self-reported confidence; ignore legacy confidence settings.
- No human-answer callback in the production workflow. Failed or unsupported questions must be recorded automatically without waiting for input.
- Preserve format/option validation, supported-question checks, bounded retry, concurrency replenishment, deadlines, cancellation and submission confirmation. Never submit incomplete papers or retry an ambiguous submission.
- All verification offline. Do not log credentials or use real platform accounts/attempts.

## Review Focus

- Low confidence and legacy high confidence configuration cannot block a valid answer.
- Invalid answers and unsupported/media questions cannot bypass structural validation.
- Retry exhaustion and fatal authentication/configuration failures cannot open manual input or submit incomplete work.
- Concurrent late results, cancellation and expired deadlines cannot submit after a terminal failure.
- Both course-first and legacy app entry points have no confidence prompt or manual-answer wiring; terminal input restoration remains correct without the footer.

### Task 1: Fully automatic AI answering

**Files:** ai/runner.py, ai/concurrency.py, app.py, course_flow.py, relevant runner/concurrency/app tests.

**Interfaces:** Keep AnswerRunner and app execution consistent across both UI entry points; remove obsolete confidence/manual parameters if all callers and tests are migrated, or preserve explicitly inert compatibility only where needed.

- [x] Write regression tests for valid low-confidence auto-submit, ignored legacy confidence, invalid answers, retry exhaustion and no human input in sequential/concurrent execution.
      Evidence: new `tests/test_fully_ai_answers.py` (19 tests) covers low/zero/missing confidence accepted, legacy `confidence` preference inert with AI-only report files, invalid answers blocking submission, automatic retry-exhaustion failure, fatal service stop, deadline expiry, and absence of `manual_answer`/`min_confidence` on `AnswerRunner`.
- [x] Run focused tests and capture their expected failures before implementation.
      Evidence: before implementation `tests/test_fully_ai_answers.py` showed 7 failures + 5 errors (confidence gate blocked auto-submit; `manual_answer`/`min_confidence` still present; `execute_ai` signature) and `tests/test_ai_summary_ui.py` showed 8 failures.
- [x] Remove confidence acceptance gates and manual-answer workflow; keep checkpoint and submission invariants.
      Evidence: `ai/runner.py` drops `min_confidence`/`manual_answer`/`math` and the pause branch; `ai/concurrency.py` drops `allow_manual` and the now-unused `drain()`; `app.py`/`course_flow.py`/`ui.py` drop threshold plumbing. Checkpoints, `.lock`, `attempt_not_draft`, `submitting`, `submission_unknown`, bounded workers and cooldown logic are unchanged.
- [x] Replace obsolete manual workflow tests with meaningful automatic failure/cancellation tests; preserve unrelated safety assertions.
      Evidence: `tests/test_ai_concurrency.py` (15) and `tests/test_ai_retry.py` (10) keep bounded-overlap, out-of-order identity, fatal-stop and cooldown coverage while asserting low-confidence answers are used; `tests/test_terminal_lifecycle.py` replaces the manual prompt with a runtime-input handoff check.
- [x] Run focused runner, retry, concurrency and entry-point tests.
      Evidence: `tests.test_fully_ai_answers tests.test_ai_summary_ui` 28 OK; `tests.test_ai_concurrency` 15 OK; `tests.test_ai_retry` 10 OK; `tests.test_course_flow tests.test_ai` 65 OK; `tests.test_ui` 15 OK; `tests.test_model_management` 18 OK; `tests.test_terminal_lifecycle` 3 OK.

### Task 2: Compact terminal UI and documentation

**Files:** ui.py, tools/ui_demo.py, README.md, docs/USAGE.md, docs/EXAM_PROTOCOL.md, relevant UI and terminal tests.

**Interfaces:** Execution settings expose only execution mode; confidence is no longer requested, displayed or persisted by UI flows.

- [x] Add focused regressions proving compact summary, absence of runtime footer/startup explanation and confidence/manual prompts.
      Evidence: `tests/test_ai_summary_ui.py` asserts the summary keeps 供应商/模型/连接状态/执行方式 with no 最低置信度/答题并发/置信度/完成即 rows, no `threshold` key, no `confidence`/`manual_answer` methods on `TerminalUI`, and no footer/`同时处理` text at 120 and 52 columns.
- [x] Remove obsolete input methods and labels, simplify summary and settings, preserve navigation and runtime cancellation.
      Evidence: `ui.py` deletes `manual_answer()` and `confidence()`, drops the footer `Ctrl+C 停止 · 运行中无需按回车`, the startup concurrency note and the 学习并发 note, and maps `needs_review` to 未完成; `review_ai` returns only the execution mode. Runtime input ownership and Ctrl+C stay covered by `tests/test_terminal_lifecycle.py`.
- [x] Update active documentation; leave historical plans/specs as historical records.
      Evidence: README.md, docs/USAGE.md, docs/EXAM_PROTOCOL.md and docs/DEVELOPMENT.md no longer describe confidence thresholds, manual补答 or `paused`; historical plans and research notes under docs/superpowers/ and docs/research/ are untouched.
- [x] Run full offline suite: `/Users/yijunjie/mooc/.venv/bin/python -m unittest discover -s tests -q`.
      Evidence: `Ran 414 tests ... OK` (baseline was 411; the two renamed manual-answer modules were replaced by 28 focused tests).
- [x] Run compileall and `git diff --check`; inspect rendered terminal output at 120×30 and a small terminal.
      Evidence: `python -m compileall -q .` is clean, `git diff --check` reports nothing; the runtime frame, AI summary and result page were rendered offline at 120×30 and 52 columns with no footer and no line exceeding the terminal width.
- [x] Commit tested work. Coordinator creates PR, obtains CI and independent review, resolves findings, then merges.
      Evidence: commit cdea17b; independent review found the `AIClient` response parser still required a well-formed `confidence` field, fixed in a follow-up commit.
