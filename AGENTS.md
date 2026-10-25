# Instructions for agents

## Git and workspace
- Before every task in this repository, run `git fetch` when a remote exists.
- Create worktrees only when the user explicitly requests one.
- Use Conventional Commits. Never force-push without an explicit request.
- Preserve unrelated user changes and work in progress.

## Access and authorization
- Treat every repository as public, even if marked private.
- Never commit secrets or private information in files or Git metadata. Use synthetic data and portable placeholders instead of personal, financial, or infrastructure details.
- Display secrets or private information only when explicitly requested.
- Use personal accounts and credentials only for explicitly authorized tasks.
- Password-manager CLIs and elevated authentication tools require explicit authorization.
- Use the personal Git identity in personal repositories.

## Files and tooling
- Verify required tools before use. On Windows, use PowerShell 7 (`pwsh`).
- Keep agent scratch files, downloads, one-off scripts, and logs in ignored `temp/`.
- Install project dependencies in `.venv`; standalone user tools belong in `$HOME/Programs`.
- Report installed tools/dependencies and their purpose in the final response.

## Architecture and compatibility
- Keep quantity parsing and unit conversion independent of Telegram and MCP.
- Keep Telegram panel rendering separate from update handlers and database access.
- Reuse the same storage/domain rules from Telegram and MCP; avoid duplicating inventory logic.
- Preserve existing SQLite data with additive, idempotent migrations.
- Check household membership before exposing or modifying its stores and items.
- Preserve old Telegram callback handling when changing navigation.
- Keep Russian user-facing labels concise; show the current home and storage context.
- Keep frequent actions visible, provide a clear way back or cancel, and confirm permanent deletion.
- Do not add automatic messages or reminders unless requested.

## Work and verification
- Run focused tests during implementation and broader checks after integration.
- Store task workflow files in `temp/task-plans/`.
- Test migrations, household isolation, quantity conversion, and changed user flows where relevant.
- Use temporary databases for tests; never test against a user's live inventory.
- Keep external tickets, reviews, and release notes focused on observable changes.
- Distinguish verified facts from assumptions; summarize changes, checks, risks, and manual steps.
- Match the user's task numbering in the final report.

## Collaboration
- Use the user-selected root model and effort.
- Delegate useful independent work, with up to three open child threads.
- Use `sol_worker` for implementation, debugging, and complex or uncertain tasks.
- Use `luna_worker` for mechanical edits, exploration, and routine checks.
- Give each worker its own files and a self-contained task; use `fork_turns = "none"`.
- Workers share the checkout: accommodate other changes and never revert them.
- Root owns architecture, integration, and final acceptance. Perform an independent review only when requested.

<!-- codex-config:bots:begin -->

<!-- codex-config:source bots/AGENTS.md -->

## Navigation
- Use buttons for all user navigation and actions. Keep slash commands out of menus, help and notifications; retain `/start` only for Telegram's Start button.
- Reply keyboard: stable, collapsible main navigation; change it only with interaction mode and restore it on exit. No `is_persistent=True` in ordinary menus.
- Inline buttons: current-message actions and contextual Back within the section. Keep main navigation in the reply keyboard; edit the current message when possible.
## Forms and editors
- Keep cancellation only in the reply keyboard. Use one bound form message: edit it when possible; otherwise remove it before replacement. If removal fails, do not create a duplicate.
## Releases
- Successful production releases with user-visible changes: one polished Russian notification to all eligible users through the existing broadcaster; skip rollbacks. Honor suppression, postponement and customization.
- Deploy requests authorize drafting and sending after approval. After implementation and validation, get approval for the exact final message before deployment unless waived; renew approval for substantial edits. Then deploy → verify the new version is running and reachable → broadcast.
- Explain user benefits with readable spacing, grouped bullets and restrained emoji; technical details only when useful. Send silently when supported or required, idempotently once per version; retry only failed recipients. Report only sent/failed counts to the owner.

<!-- codex-config:bots:end -->
