/**
 * session-importer — list Claude Code / OpenCode sessions, click to import
 * into Hermes as a resumable plain-text session and open it.
 *
 * Surface: sidebar nav row → full page route (/foreign-sessions).
 * Backend: dashboard/plugin_api.py via ctx.rest.
 */

import {
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Badge,
  Button,
  Codicon,
  EmptyState,
  ErrorState,
  Skeleton,
  Tip,
  cn,
  haptic,
  host,
  queryClient,
  useMutation,
  useQuery,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'session-importer'
const PATH = '/foreign-sessions'

const PROVIDERS = {
  claude: { label: 'Claude Code', codicon: 'sparkle' },
  opencode: { label: 'OpenCode', codicon: 'terminal' },
}

function fmtTime(ms) {
  if (!ms) return ''
  return new Date(ms).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function fmtCwd(cwd) {
  if (!cwd) return ''
  const parts = cwd.replace(/\/+$/, '').split('/')
  return parts[parts.length - 1] || cwd
}

function SessionRow({ s, onImport }) {
  const prov = PROVIDERS[s.source] || { label: s.source }
  return jsxs('button', {
    type: 'button',
    className: cn(
      'group flex w-full items-start gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors',
      'hover:bg-(--chrome-action-hover)'
    ),
    onClick: () => onImport(s),
    children: [
      jsx('div', {
        className: 'mt-0.5 shrink-0',
        children: jsx(Codicon, { name: prov.codicon, size: 14 }),
      }),
      jsxs('div', { className: 'min-w-0 flex-1', children: [
        jsx('div', {
          className: 'truncate font-medium',
          children: s.title || s.session_id || s.path,
        }),
        jsxs('div', {
          className: 'flex items-center gap-2 text-xs text-(--ui-text-tertiary)',
          children: [
            jsx('span', { children: fmtTime(s.mtime_ms) }),
            s.cwd ? jsx('span', { children: fmtCwd(s.cwd) }) : null,
            jsx('span', { children: `${s.turn_count} turns` }),
          ],
        }),
      ]}),
      jsx('div', {
        className: 'shrink-0 self-center',
        children: s.imported
          ? jsx(Tip, {
              label: `Already imported — opens ${s.imported_hermes_id}`,
              children: jsx(Badge, { variant: 'secondary', children: 'imported' }),
            })
          : jsx('span', {
              className:
                'text-xs text-(--ui-text-quaternary) opacity-0 group-hover:opacity-100',
              children: 'click to import →',
            }),
      }),
    ],
  })
}

function ForeignSessionsPage({ rest }) {
  const listQuery = useQuery({
    queryKey: [ID, 'list'],
    queryFn: () => rest('/list'),
    refetchInterval: 5000,
  })

  const importMut = useMutation({
    mutationFn: (s) =>
      rest('/import', { method: 'POST', body: { source: s.source, path: s.path } }),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: [ID, 'list'] })
      if (res && res.ok && res.session_id) {
        haptic('tap')
        if (typeof host.openSession === 'function') {
          host.openSession(res.session_id, { intent: 'tab' })
        } else {
          host.navigate('/chat/' + res.session_id)
        }
      } else if (res && res.error) {
        host.notifyError(new Error(res.error), `Import failed: ${res.error}`)
      }
    },
    onError: (err) => host.notifyError(err, 'Import failed'),
  })

  const doImport = (s) => {
    // Already-imported sessions open directly instead of re-importing.
    if (s.imported && s.imported_hermes_id) {
      host.openSession(s.imported_hermes_id, { intent: 'tab' })
      return
    }
    importMut.mutate(s)
  }

  if (listQuery.isLoading) {
    return jsxs('div', { className: 'flex h-full flex-col gap-2 p-4', children: [
      jsx(Skeleton, {}),
      jsx(Skeleton, {}),
      jsx(Skeleton, {}),
    ]})
  }
  if (listQuery.isError) {
    const status = listQuery.error && (listQuery.error.status || listQuery.error.statusCode)
    const staleHint = status === 404 || status === undefined
      ? ' If you just enabled this plugin, fully quit Hermes (⌘Q) and relaunch — plugin API routes mount only at server startup.'
      : ''
    return jsx(ErrorState, {
      title: `Could not load foreign sessions${status ? ` (HTTP ${status})` : ''}`,
      error: listQuery.error,
      description: `Backend /api/plugins/session-importer/list failed.${staleHint}`,
    })
  }

  const sessions = (listQuery.data && listQuery.data.sessions) || []
  const errors = (listQuery.data && listQuery.data.errors) || []

  const groups = Object.keys(PROVIDERS)
    .map((key) => ({
      key,
      label: PROVIDERS[key].label,
      items: sessions.filter((s) => s.source === key),
    }))
    .filter((g) => g.items.length > 0)

  return jsxs('div', { className: 'flex h-full flex-col', children: [
    jsxs('div', {
      className: 'flex items-center justify-between px-4 pt-4 pb-2',
      children: [
        jsx('h2', { className: 'text-base font-semibold', children: 'Foreign Sessions' }),
        jsx(Button, {
          variant: 'ghost',
          size: 'sm',
          onClick: () => listQuery.refetch(),
          disabled: listQuery.isFetching,
          children: listQuery.isFetching ? 'refreshing…' : 'Refresh',
        }),
      ],
    }),
    errors.length > 0
      ? jsx('div', {
          className: 'px-4 pb-2 text-xs text-(--ui-text-tertiary)',
          children: errors.join(' · '),
        })
      : null,
    jsx('div', { className: 'flex-1 overflow-y-auto pb-4', children:
      groups.length === 0
        ? jsx(EmptyState, {
            icon: 'history',
            title: 'No foreign sessions found',
            description:
              'Sessions from Claude Code (~/.claude/projects) and OpenCode will appear here.',
          })
        : groups.map((g) =>
            jsxs('div', { className: 'mb-4', children: [
              jsx('div', {
                className:
                  'px-4 py-1 text-xs font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
                children: `${g.label} (${g.items.length})`,
              }),
              g.items.map((s) =>
                jsx(SessionRow, { s, onImport: doImport }, `${s.source}:${s.session_id}`)
              ),
            ]}, g.key)
          ),
    }),
    importMut.isPending
      ? jsx('div', {
          className: 'px-4 py-2 text-xs text-(--ui-text-tertiary)',
          children: 'Importing…',
        })
      : null,
  ]})
}

export default {
  id: ID,
  name: 'Session Importer',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PATH },
        render: () => jsx(ForeignSessionsPage, { rest: ctx.rest.bind(ctx) }),
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PATH, label: 'Foreign Sessions', codicon: 'history' },
      },
      {
        id: 'palette',
        area: PALETTE_AREA,
        data: {
          id: 'session-importer.open',
          label: 'Open Foreign Sessions',
          keywords: ['import', 'claude', 'opencode', 'sessions'],
          run: () => host.navigate(PATH),
        },
      },
    ])
  },
}
