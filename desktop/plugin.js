/**
 * session-importer — list Claude Code / OpenCode sessions, view them
 * read-only, and import into Hermes as resumable plain-text sessions.
 *
 * v1.1 surface: provider stepper (pick provider → list) with per-session
 * View (read-only transcript) and Import actions.
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
import { useState } from 'react'

const ID = 'session-importer'
const PATH = '/foreign-sessions'

const PROVIDERS = [
  { key: 'claude', label: 'Claude Code', codicon: 'sparkle', hint: '~/.claude/projects' },
  { key: 'opencode', label: 'OpenCode', codicon: 'terminal', hint: 'opencode.db' },
]

function providerMeta(key) {
  return PROVIDERS.find((p) => p.key === key) || { label: key, codicon: 'puzzle' }
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

function useForeignList(rest, provider) {
  return useQuery({
    queryKey: [ID, 'list', provider],
    queryFn: () => rest(`/list?source=${provider}`),
    refetchInterval: 5000,
  })
}

function ProviderPicker({ onPick }) {
  return jsxs('div', { className: 'flex h-full flex-col items-center justify-center gap-3 p-6', children: [
    jsx('h2', { className: 'text-base font-semibold', children: 'Choose a provider' }),
    jsx('div', { className: 'flex w-full max-w-sm flex-col gap-2', children:
      PROVIDERS.map((p) => jsx('button', {
        type: 'button',
        className: cn(
          'flex w-full items-center gap-3 rounded-lg border border-(--ui-stroke-secondary) px-4 py-3 text-left transition-colors',
          'hover:bg-(--chrome-action-hover)'
        ),
        onClick: () => { haptic('tap'); onPick(p.key) },
        children: [
          jsx(Codicon, { name: p.codicon, size: 18 }),
          jsxs('div', { className: 'flex-1', children: [
            jsx('div', { className: 'text-sm font-medium', children: p.label }),
            jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: p.hint }),
          ]}),
          jsx(Codicon, { name: 'chevron-right', size: 14 }),
        ],
      }, p.key))
    }),
  ]})
}

function SessionRow({ s, onView, onImport }) {
  return jsxs('div', {
    className: cn(
      'group flex w-full items-start gap-2 rounded-md px-3 py-2 text-sm transition-colors',
      'hover:bg-(--chrome-action-hover)'
    ),
    children: [
      jsxs('button', {
        type: 'button',
        className: 'min-w-0 flex-1 text-left',
        onClick: () => onView(s),
        children: [
          jsx('div', { className: 'truncate font-medium', children: s.title || s.session_id || s.path }),
          jsxs('div', {
            className: 'flex items-center gap-2 text-xs text-(--ui-text-tertiary)',
            children: [
              jsx('span', { children: fmtTime(s.mtime_ms) }),
              s.cwd ? jsx('span', { children: fmtCwd(s.cwd) }) : null,
              jsx('span', { children: `${s.turn_count} turns` }),
            ],
          }),
        ],
      }),
      jsxs('div', { className: 'flex shrink-0 items-center gap-1 self-center', children: [
        s.imported
          ? jsx(Tip, {
              label: `Already imported — opens ${s.imported_hermes_id}`,
              children: jsx(Badge, { variant: 'secondary', children: 'imported' }),
            })
          : null,
        jsx(Button, {
          variant: 'ghost', size: 'sm',
          onClick: () => onView(s),
          children: 'View',
        }),
        jsx(Button, {
          variant: 'secondary', size: 'sm',
          onClick: () => onImport(s),
          children: 'Import',
        }),
      ]}),
    ],
  })
}

function Transcript({ data, onClose }) {
  return jsxs('div', { className: 'flex h-full flex-col', children: [
    jsxs('div', { className: 'flex items-center justify-between px-4 pt-4 pb-2', children: [
      jsxs('div', { className: 'min-w-0', children: [
        jsx('div', { className: 'truncate text-sm font-semibold', children: data.title || 'Untitled session' }),
        data.cwd ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: data.cwd }) : null,
      ]}),
      jsx(Button, { variant: 'ghost', size: 'sm', onClick: onClose, children: '← Back' }),
    ]}),
    jsx('div', { className: 'flex-1 overflow-y-auto px-4 pb-4', children:
      (data.turns || []).map((t, i) => jsxs('div', { className: 'mb-3', children: [
        jsx('div', {
          className: cn(
            'mb-1 text-xs font-medium uppercase tracking-wide',
            t.role === 'user' ? 'text-(--ui-accent)' : 'text-(--ui-text-quaternary)'
          ),
          children: t.role,
        }),
        jsx('div', {
          className: cn(
            'whitespace-pre-wrap rounded-md px-3 py-2 text-sm',
            t.role === 'user' ? 'bg-(--chrome-action-hover)' : ''
          ),
          children: t.content,
        }),
      ]}, i))
    }),
  ]})
}

function ProviderList({ rest, provider, onBack }) {
  const listQuery = useForeignList(rest, provider)
  const [viewing, setViewing] = useState(null)

  const viewMut = useMutation({
    mutationFn: (s) =>
      rest(`/view?source=${s.source}&path=${encodeURIComponent(s.path)}`),
    onSuccess: (res) => {
      if (res && res.ok) setViewing(res)
      else if (res && res.error) host.notifyError(new Error(res.error), 'View failed')
    },
    onError: (err) => host.notifyError(err, 'View failed'),
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

  if (viewing) {
    return jsx(Transcript, { data: viewing, onClose: () => setViewing(null) })
  }

  if (listQuery.isLoading) {
    return jsxs('div', { className: 'flex h-full flex-col gap-2 p-4', children: [
      jsx(Skeleton, {}), jsx(Skeleton, {}), jsx(Skeleton, {}),
    ]})
  }
  if (listQuery.isError) {
    const status = listQuery.error && (listQuery.error.status || listQuery.error.statusCode)
    const staleHint = status === 404 || status === undefined
      ? ' If you just enabled this plugin, fully quit Hermes (⌘Q) and relaunch — plugin API routes mount only at server startup.'
      : ''
    return jsx(ErrorState, {
      title: `Could not load sessions${status ? ` (HTTP ${status})` : ''}`,
      error: listQuery.error,
      description: `Backend /api/plugins/${ID}/list failed.${staleHint}`,
    })
  }

  const sessions = (listQuery.data && listQuery.data.sessions) || []
  const errors = (listQuery.data && listQuery.data.errors) || []
  const meta = providerMeta(provider)

  return jsxs('div', { className: 'flex h-full flex-col', children: [
    jsxs('div', { className: 'flex items-center justify-between px-4 pt-4 pb-2', children: [
      jsxs('div', { className: 'flex items-center gap-2', children: [
        jsx(Button, { variant: 'ghost', size: 'sm', onClick: onBack, children: '←' }),
        jsxs('h2', { className: 'text-base font-semibold', children: [
          meta.label, ` (${sessions.length})`,
        ]}),
      ]}),
      jsx(Button, {
        variant: 'ghost', size: 'sm',
        onClick: () => listQuery.refetch(),
        disabled: listQuery.isFetching,
        children: listQuery.isFetching ? 'refreshing…' : 'Refresh',
      }),
    ]}),
    errors.length > 0
      ? jsxs('div', { className: 'px-4 pb-2 text-xs text-(--ui-text-tertiary)', children: [
          jsx('div', { className: 'font-medium', children: 'Some sessions could not be read:' }),
          errors.slice(0, 3).map((e, i) => jsx('div', { key: i, className: 'truncate', children: e })),
        ]})
      : null,
    jsx('div', { className: 'flex-1 overflow-y-auto pb-4', children:
      sessions.length === 0
        ? jsx(EmptyState, {
            icon: 'history',
            title: `No ${meta.label} sessions found`,
            description: 'Sessions will appear here as you use the tool.',
          })
        : sessions.map((s) =>
            jsx(SessionRow, {
              s,
              onView: (x) => viewMut.mutate(x),
              onImport: (x) => importMut.mutate(x),
            }, s.session_id || s.path)
          ),
    }),
    importMut.isPending || viewMut.isPending
      ? jsx('div', {
          className: 'px-4 py-2 text-xs text-(--ui-text-tertiary)',
          children: importMut.isPending ? 'Importing…' : 'Loading transcript…',
        })
      : null,
  ]})
}

function ForeignSessionsPage({ rest }) {
  const [provider, setProvider] = useState(null)
  if (!provider) return jsx(ProviderPicker, { onPick: setProvider })
  return jsx(ProviderList, {
    rest, provider,
    onBack: () => setProvider(null),
  })
}

// useState must come from the app's React; imported above from 'react'.

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
