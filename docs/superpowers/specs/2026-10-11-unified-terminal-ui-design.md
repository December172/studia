# Unified Terminal Interaction

The user reports that Tab makes model-picker button text disappear and that interaction across the application feels inconsistent. The concurrency form is the reference: clear keyboard selection, editable values, visible actions and cancellation without saving. The user explicitly requests a version update; this release will be **4.2.0** (coordinator's versioning decision).

## Outcome

Interactive terminals use a consistent keyboard UI for course/function/settings menus, confirmations, model management, execution summaries and result navigation. Text entry remains for actual text such as search, provider names, model IDs and keys. Existing non-TTY/injected-input callers retain their scripted fallback and public business return values.

- Up/down choose list rows; Space toggles multi-selection; Enter confirms the current choice or activates the focused button.
- Tab/Shift+Tab move between controls. Forms move Enter to the next field or action instead of saving implicitly. Esc returns/cancels the current view, preserving selection and discarding unsaved form changes. q remains a menu exit shortcut, and remains ordinary text inside fields/search.
- Menus offer explicit primary and return actions. Execution summaries retain Enter-to-start with visible model/settings/test actions. Results offer return-to-course and switch-course actions. Course search, paging/scrolling, refresh/platform/settings shortcuts and selection state remain available.
- Focused button text, arrows and padding have explicit contrast at the actual rendered-cell level. Focus, selected row, active model and unavailable item remain distinguishable; a list should not look focused when Tab has moved to its buttons.
- Model actions reflect the selected row: the add row offers adding, not misleading use/test/edit actions. Built-in models cannot be edited. Async connection testing still prevents duplicate actions, preserves selection and safely ignores completion after leaving.
- The layout follows a small shared frame: heading/title, main content, visible actions, concise navigation help and inline status. Use terminal-cell widths, sensible spacing and responsive buttons. Preserve the working concurrency editing behavior.
- All touched native views share styling and focus/navigation helpers; avoid independent copies drifting apart.

## Scope and invariants

This is a presentation/navigation change. Keep AI answers, auto-submit defaults, platform calls, retry/cancellation, task availability, credentials, settings storage and export semantics unchanged. Do not contact actual platform/AI accounts to verify it. Keys remain masked; terminal screen/input ownership must restore correctly. Untrusted labels are literal, not markup or terminal commands.

No new dependencies; Python 3.9+ remains supported. At minimum validate 120×30, 52×20 and a narrow/short terminal and resize. All actionable labels must remain readable and all controls reachable, using scrolling or stacked actions when necessary.

Update all active version sources and user-facing release references to 4.2.0, including locked project metadata if present. Leave historical research/plans as history.

## Implementation choices delegated to Claude Code

Use prompt_toolkit already in the repository. Introduce a small reusable terminal widget module for style, responsive actions/frame and native option/item/action selection. Keep TerminalUI as the adapter and retain injected-input fallback paths. Exact module names and visually compact layout may be adjusted if this preserves the above behavior; avoid rewriting business workflows or adding new settings.

## Acceptance evidence

The original failure was reproduced through the actual model-picker renderer: the focused `使` cell had `fg=ansicyan`, `bg=ansicyan`, style `class:button.focused class:button.text class:last-line`. Cover real focused text/arrow cells in regression tests, rather than just checking that a label occurs in output. Cover keyboard actions, input-mode isolation, empty/search/disabled lists, default false/zero choices, cancelled drafts, model async testing, screen restoration and production adapter routing. Visually inspect realistic offline previews before committing.

Delivery follows the user's established workflow: issue #7 → development branch → Claude Code implementation → PR → CI and independent review → merge. Version update is part of the same release.
