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

## Scope supplement: uniform spacing and a filled window (user feedback)

After the first preview the user reported the pages looked cramped (太挤了吧) and asked for consistent blank separation between semantic groups plus restrained lines, then refined it to 窗口铺满可以吗，中间可以留空: the window should be filled, with the blank space in the middle. This supersedes the earlier compact preference of keeping a short page's buttons right under its content; that preference only applied before the user saw a rendered frame and is kept here as history.

Every native page therefore shares one frame shape: heading/title and main content at the top, one-row blank separation between semantic groups when the terminal is tall enough, a dim thin rule at the content/action boundary, and the actions/status/help/account footer fixed at the bottom. The surplus height between the content and the action group becomes one flexible blank block instead of leaving unused rows under a compressed stack. On short terminals (40×16, 52×20) the flexible filler and the secondary spacer/rule rows collapse before any control does, so every field, action and selected row stays reachable and no `Window too small` appears; a live resize recomputes all of it. The same helper bounds the row reservations the native list viewports use, so the reservation can never drift from what the frame renders.

Delivery follows the user's established workflow: issue #7 → development branch → Claude Code implementation → PR → CI and independent review → merge. Version update is part of the same release.
