# v2 Research & Plan

## Verified facts (from live code, not guesses)

- `ctx.rest` supports query strings on paths (kanban's own `api.ts` builds
  `path?${qs}` via URLSearchParams). The v1.1 "View 404" was a stale server:
  the screenshot (06:30:28) predates the `/view` mount (06:31:59). `/view`
  verified working on the live server via direct HTTP.
- Plugin API routes mount **once at web-server start**; only `plugin.js`
  hot-reloads. Any backend change needs a full app restart.
- `ctx.socket` is a no-op on OAuth remotes — polling fallback required.
- SDK UI kit available: SearchField, Badge, EmptyState/ErrorState, Skeleton,
  ScrollArea, Tip, Button, Codicon, CopyButton; formatters like relativeTime.
- `ctx.storage` = plugin-scoped persisted prefs (`get/set/remove`).
- Palette commands + keybinds with defaults (`mod+shift+r` style).
- Statusbar chips (`STATUSBAR_AREAS.right`), titlebar tools.
- `host.notify` for in-app toasts; `ctx.os.notify` for OS notifications.

## v2 feature set

1. Search box across title + cwd + first message (client-side filter).
2. Date-grouped list with sticky day headers ("Today", "Yesterday", dates).
3. cwd/project filter chips derived from the loaded sessions.
4. Persisted view prefs via `ctx.storage`: last provider, search text,
   cwd filter.
5. Import-all-new bulk action with ConfirmDialog and per-item results;
   `host.notify` toast summary on completion.
6. Relative timestamps in rows.
7. Keybinds: refresh (`mod+shift+r`), focus search.
8. Palette commands: open page, import-all-new, jump to provider.
9. Error card keeps HTTP status + restart hint (from v1.1).
10. Polling stays the source of truth (5s); socket invalidation skipped for
    now since our backend has no push channel — noted as future work.

## Non-goals (v2)

- Tool-call fidelity replay (contract remains flattened-text).
- Theming/accent override.
- Delete/archive of imports (needs gateway session lifecycle care).
