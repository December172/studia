# Unified Terminal UI Implementation Plan

> **For agentic workers:** Use executing-plans and test-driven-development. The user chose Claude Code implementation and authorized issue/branch/PR/CI/review/merge. Implement the concrete scope in the adjacent design; do not pause for a second authorization.

**Goal:** Fix invisible Tab-focused text, unify native terminal interaction around the concurrency form, and release 4.2.0.

**Architecture:** Small shared prompt_toolkit widgets own focus styles, responsive frames/actions and option/item/action navigation. Existing native forms consume those helpers; TerminalUI routes actual TTY callers to the native controls while preserving scripted fallback and business return values.

**Tech Stack:** Python 3.9+, prompt_toolkit, Rich, unittest; no new dependencies.

**Spec:** docs/superpowers/specs/2026-10-11-unified-terminal-ui-design.md and issue #7.

## Global constraints

- Actual focused text/arrow cells must have contrasting foreground/background in all native pages.
- ↑↓ select, Space multiselect, Enter confirm/activate, Tab/Shift+Tab focus, Esc back/cancel. Typed q/b/shortcut letters are text inside fields and search.
- Keep domain behavior, default AI auto-submit and non-TTY/injected-input interfaces unchanged.
- Preserve drafts/selection, task availability, masked keys, async test cancellation and terminal restoration.
- Layouts must remain useful at 120×30 and 52×20 and narrower/shorter sizes; stack/scroll where needed.
- Active version sources must be 4.2.0. Baseline is clean 2bce7db with 419 tests passing.

## Review focus

- Button fragment inheritance can overwrite focused color after the window style.
- Global arrow/letter bindings must not alter another control or exit a text field.
- Defaults with values 0/False, empty searches and unavailable rows must not be confused with cancel/no selection.
- Adding/built-in model rows must expose only appropriate reachable actions; pending async work must not duplicate or overwrite after exit.
- Native adapters must be exercised through TerminalUI and a real PTY, not only helper tests; scripted fallback must remain green.

### Task 1: Shared focus/frame helpers and native forms

**Files:** new small terminal widget module; model_ui.py, concurrency_ui.py, checklist.py; tests/test_model_ui.py, tests/test_concurrency_ui.py, checklist tests and new shared-widget tests.

**Interfaces:** Existing choose_model/edit_model/edit_concurrency/choose_many return contracts stay unchanged. Shared helpers provide one style, responsive actions and focused control behavior without storage/network operations.

- [x] Create a failing real-render regression for same-color focused button text/arrow cells; include model picker, editor and concurrency actions.
      Evidence: `tests/test_native_focus.py` reads real rendered cells (char + style + resolved attrs from `app.renderer._attrs_for_style`), asserting focused button glyphs never end at fg == bg and carry `class:button.focused class:button.text` / `.arrow`.
- [x] Add regressions for Tab/Shift+Tab, inactive list highlight, scoped arrow handling and context-appropriate model actions.
      Evidence: `test_model_picker_highlight_is_not_a_focused_control`, `test_concurrency_row_is_not_focused_while_a_button_owns_focus`, `test_checklist_row_drops_the_cursor_background_when_a_button_is_focused` (a Tab-focused 确认 button leaves no checklist row with cursor-bg while the highlighted row keeps its `>` marker and the checked task stays selected), `ScopedBindingTests`, `test_context_help_matches_the_visible_actions`, `test_custom_row_exposes_a_visible_back_button`; `tests/test_checklist.py` Tab/Shift+Tab tests.
- [x] Run tests to capture failures, then implement shared style/frame helpers and migrate native pages.
      Evidence: new `terminal_widgets.py` (198 lines: STYLE_DICT, `field`, `responsive_actions`, `action_rows`, `status_window`, `wrap_help`/`help_window`, `name_fragments`, `focus_targets`/`cycle_focus`, `frame`); model_ui/concurrency_ui/checklist/native_ui all consume it. Fixed bugs: Button-vs-Window focus comparison (every Tab snapped to the first button) and the focused-label fg == bg rule.
- [x] Preserve async tests, form drafts, password masking, multiselect/disabled items and CJK sizing.
      Evidence: `test_passwords_are_masked_in_real_terminal_rendering` (40/52/80 columns, old and new key never rendered), pending-test cancellation test, draft-reuse tests, disabled attachment rows still unselectable, `fit`/`get_cwidth` column budget.
- [x] Run focused native UI tests and inspect offline frames at large/small sizes.
      Evidence: model/concurrency/menu/course/summary/checklist rendered at 120×30, 52×20 and 40×16; below 52 columns the checklist drops the Type column so name/status/score/remaining keep real widths (`test_narrow_width_hides_type_column_and_keeps_metrics_useful`, `test_wide_width_keeps_the_type_column`).

### Task 2: Native menu/list/action adapters

**Files:** shared native option/item/action helpers; ui.py; tests/test_ui.py, tests/test_course_flow.py, tests/test_terminal_lifecycle.py, new adapter/keyboard tests.

**Interfaces:** TerminalUI.choice/select(single)/confirm keep original scalar/list/bool return contracts and Back/Cancelled exceptions. Native summaries/results preserve their existing return values and callbacks. Helpers return typed results indicating selection versus back/quit/action; never conflate false/zero values with cancellation.

- [x] Add failing native routing and keyboard tests for option menus, course single selection/search/actions and yes/no confirmation.
      Evidence: `tests/test_native_controls.py` (400 lines) drives menus, search typing inside the field, default `False` confirmation, resize and Esc/q through a real pipe.
- [x] Implement reusable native controls and actual-TTY routing; retain existing line-based fallback.
      Evidence: `native_ui.py` (675 lines: `option_menu`, `single_picker`, `action_page`, `confirm_prompt`) routed from `TerminalUI.choice/select(multiple=False)/confirm` only when `input_fn is None and console.is_terminal and sys.stdin.isatty()`; `test_injected_input_never_calls_native_pages` covers the fallback.
- [x] Migrate study/export/AI summaries and result navigation to visible actions, keeping existing start/settings/model/test callbacks and return choices.
      Evidence: `tests/test_native_summaries.py::RoutingTests` asserts export `start`/`s`/back, AI start with auto-submit, `m`/`t` callbacks, no-model reopen, study Back button (`PageResult('action','b')` now raises `Back`), and `operation_result` returning 1 / 2 / `Cancelled`.
- [x] Verify searches/empty results, retained selection, Esc hierarchy, shortcut letters typed as text, defaults and disabled controls.
      Evidence: search `/bqt` selects the filtered course; space/enter on an empty checklist refuses with a message; selections stay retained across refresh; typed `/` prefix inside the field is literal; `0`/`False` defaults return as values; action pages bind only their explicit keys (`test_unbound_digit_is_ignored_instead_of_starting_by_ordinal`, `test_explicit_result_key_binds_to_its_own_action`).
- [x] Run real PTY navigation/resize/screen restoration checks and related course/UI tests.
      Evidence: new `tests/test_terminal_lifecycle.py::test_native_workflow_menu_to_results` reuses the file's launcher/drain/until and drives menu → course `/`search → confirm → study → AI → export → results at 20×52, asserting one alternate-screen enter/leave pair, echo restored, and no `Window too small`; 4 PTY tests pass in 3.063s.

### Task 3: Release and verification

**Files:** active version sources (discover with rg), lock metadata if present, README.md, docs/USAGE.md, tools/ui_demo.py or an offline native preview helper, plan evidence.

- [x] Set version 4.2.0 consistently and update active interaction docs; leave historical records unchanged.
      Evidence: `pyproject.toml` 4.2.0 and only the `icve-ai-assistant` entry in `uv.lock` (the unrelated markdown-it-py 4.2.0 entry untouched); `StartWork.APP_VERSION == '4.2.0'`; README/USAGE/DEVELOPMENT updated for the final native behavior plus the narrow-terminal column rule; `docs/legacy/` untouched.
- [x] Provide realistic offline previews for model, concurrency, menu, course list and execution summary; capture and visually inspect focused states.
      Evidence: `python -m tools.ui_demo --native` delegates to the new `tools/native_demo.py` (115 lines) which walks the real pages — platform menu, course picker, single/multi task selection, confirm, model picker, concurrency, AI/export summaries and the result page — with synthetic courses/tasks/models and no platform, AI or settings access; the static HTML snapshot mode is unchanged. Frames were rendered at 120×30, 52×20 and 40×16.
- [x] Run full offline suite once after final source changes, compileall, git diff --check; record exact outcomes.
      Evidence: `.venv/bin/python -m unittest discover -s tests` → `Ran 502 tests in 70.744s`, `OK`, exit status 0; `compileall` OK for every touched module; `git diff --check` clean. Focused native suites: 120 tests OK.
- [x] Mark plan checkboxes with evidence and commit tested implementation. Do not push/PR/merge; coordinator handles CI and independent review.
      Evidence: release commit on `codex/unified-terminal-ui` referencing issue #7; no tag, build, dependency or push.
