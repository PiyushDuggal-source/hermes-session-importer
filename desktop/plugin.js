/**
 * session-importer — browse, view, and import Claude Code / OpenCode
 * sessions into Hermes as resumable plain-text sessions.
 *
 * v2: search, date-grouped list, cwd filter chips, persisted prefs,
 * bulk import-all-new, relative timestamps, keybinds + palette commands.
 * Backend: dashboard/plugin_api.py via ctx.rest.
 */

import {
  KEYBINDS_AREA,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  SearchField,
  Skeleton,
  Streamdown,
  Tip,
  cn,
  haptic,
  host,
  queryClient,
  useMutation,
  useQuery,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useMemo, useState } from 'react'

const ID = 'session-importer'
const PATH = '/foreign-sessions'

const PROVIDERS = [
  { key: 'claude', label: 'Claude Code', codicon: 'sparkle' },
  { key: 'opencode', label: 'OpenCode', codicon: 'terminal' },
]

function providerMeta(key) {
  return PROVIDERS.find((p) => p.key === key) || { label: key, codicon: 'puzzle' }
}

function relTime(ms) {
  if (!ms) return ''
  const diff = Date.now() - ms
  const min = Math.floor(diff / 60000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  const d = Math.floor(hr / 24)
  if (d < 7) return `${d}d ago`
  return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function dayBucket(ms) {
  if (!ms) return 'older'
  const d = new Date(ms)
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const that = new Date(d); that.setHours(0, 0, 0, 0)
  const days = Math.round((today - that) / 86400000)
  if (days === 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return 'This week'
  return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}

function fmtCwd(cwd) {
  if (!cwd) return ''
  const parts = cwd.replace(/\/+$/, '').split('/')
  return parts[parts.length - 1] || cwd
}

// ── persisted prefs ──────────────────────────────────────────────────────

let _storage = null
function prefs() {
  if (!_storage) throw new Error('ctx.storage not captured')
  return _storage
}
function loadPref(key, fallback) {
  try { return prefs().get(key, fallback) } catch { return fallback }
}
function savePref(key, value) {
  try { prefs().set(key, value) } catch { /* storage optional */ }
}

// ── data ─────────────────────────────────────────────────────────────────

function useForeignList(rest, provider) {
  return useQuery({
    queryKey: [ID, 'list', provider],
    queryFn: () => rest(`/list?source=${provider}`),
    refetchInterval: 5000,
  })
}

function refreshAll(provider) {
  queryClient.invalidateQueries({ queryKey: [ID, 'list'] })
}

// ── components ───────────────────────────────────────────────────────────

function ProviderPicker({ onPick }) {
  return jsx('div', {
    className: 'flex h-full flex-col items-center justify-center gap-3 p-6',
    children: [
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
            jsx('div', { className: 'flex-1 text-sm font-medium', children: p.label }),
            jsx(Codicon, { name: 'chevron-right', size: 14 }),
          ],
        }, p.key))
      }),
    ],
  })
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
              jsx('span', { children: relTime(s.mtime_ms) }),
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
        jsx(Button, { variant: 'ghost', size: 'sm', onClick: () => onView(s), children: 'View' }),
        jsx(Button, { variant: 'secondary', size: 'sm', onClick: () => onImport(s), children: 'Import' }),
      ]}),
    ],
  })
}

function DayHeader({ label, count }) {
  return jsx('div', {
    className: 'sticky top-0 z-10 bg-(--ui-card) px-4 py-1 text-xs font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
    children: `${label} (${count})`,
  })
}

function ToolFooter({ tools }) {
  const entries = Object.entries(tools).sort((a, b) => b[1] - a[1])
  const label = entries.map(([name, n]) => `${n}× ${name}`).join(' · ')
  return jsxs('div', {
    className: 'mt-1 flex items-center gap-1.5 px-3 text-xs text-(--ui-text-tertiary)',
    children: [
      jsx(Codicon, { name: 'tools', size: 12 }),
      jsx('span', {
        className: 'truncate',
        title: `This message made ${entries.reduce((a, [, n]) => a + n, 0)} tool calls: ${label}`,
        children: label,
      }),
    ],
  })
}

function Transcript({ rest, meta, onClose }) {
  const viewQuery = useQuery({
    queryKey: [ID, 'view', meta.source, meta.path],
    queryFn: () => rest(`/view?source=${meta.source}&path=${encodeURIComponent(meta.path)}`),
    enabled: !!meta,
    staleTime: Infinity,
  })

  if (viewQuery.isLoading) {
    return jsxs('div', { className: 'flex h-full flex-col gap-2 p-4', children: [
      jsx(Skeleton, {}), jsx(Skeleton, {}), jsx(Skeleton, {}),
    ]})
  }
  if (viewQuery.isError || !viewQuery.data?.ok) {
    const err = viewQuery.error || new Error(viewQuery.data?.error || 'View failed')
    return jsx(ErrorState, { title: 'Could not load transcript', error: err })
  }

  const data = viewQuery.data
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
            'rounded-md px-3 py-2 text-sm [&_p]:my-1 [&_pre]:overflow-x-auto [&_code]:text-xs',
            t.role === 'user' ? 'bg-(--chrome-action-hover)' : ''
          ),
          children: jsx(Streamdown, { children: t.content }),
        }),
        t.tools
          ? jsx(ToolFooter, { tools: t.tools })
          : null,
      ]}, i))
    }),
  ]})
}

function ProviderList({ rest, provider, onBack }) {
  const listQuery = useForeignList(rest, provider)
  const [viewing, setViewing] = useState(null)
  const [search, setSearch] = useState(() => loadPref('search:' + provider, ''))
  const [cwdFilter, setCwdFilter] = useState(() => loadPref('cwd:' + provider, ''))
  const [confirmBulk, setConfirmBulk] = useState(false)

  const importOne = useMutation({
    mutationFn: (s) =>
      rest('/import', { method: 'POST', body: { source: s.source, path: s.path } }),
    onSuccess: (res) => {
      refreshAll()
      if (res?.ok && res.session_id && typeof host.openSession === 'function') {
        host.openSession(res.session_id, { intent: 'tab' })
      }
    },
    onError: (err) => host.notifyError(err, 'Import failed'),
  })

  const bulk = useMutation({
    mutationFn: async (targets) => {
      let ok = 0, failed = 0
      for (const s of targets) {
        try {
          const res = await rest('/import', {
            method: 'POST', body: { source: s.source, path: s.path },
          })
          if (res?.ok) ok += 1; else failed += 1
        } catch { failed += 1 }
      }
      return { ok, failed }
    },
    onSuccess: ({ ok, failed }) => {
      refreshAll()
      setConfirmBulk(false)
      haptic('tap')
      host.notify({
        kind: failed ? 'warning' : 'success',
        message: `Imported ${ok} session${ok === 1 ? '' : 's'}` +
                 (failed ? `, ${failed} failed` : ''),
      })
    },
    onError: (err) => { setConfirmBulk(false); host.notifyError(err, 'Bulk import failed') },
  })

  if (viewing) {
    return jsx(Transcript, { rest, meta: viewing, onClose: () => setViewing(null) })
  }

  if (listQuery.isLoading) {
    return jsx('div', { className: 'flex h-full flex-col gap-2 p-4', children: [
      jsx(Skeleton, {}), jsx(Skeleton, {}), jsx(Skeleton, {}),
    ]})
  }
  if (listQuery.isError) {
    const status = listQuery.error && (listQuery.error.status || listQuery.error.statusCode)
    const staleHint = status === 404 || status === undefined
      ? ' If you just changed the plugin backend, fully quit Hermes (⌘Q) and relaunch — plugin API routes mount only at server startup.'
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

  // filters
  const q = search.trim().toLowerCase()
  const cwds = [...new Set(sessions.map((s) => s.cwd).filter(Boolean))].sort()
  const filtered = sessions.filter((s) => {
    if (cwdFilter && s.cwd !== cwdFilter) return false
    if (!q) return true
    return (
      (s.title || '').toLowerCase().includes(q) ||
      (s.cwd || '').toLowerCase().includes(q) ||
      (s.session_id || '').toLowerCase().includes(q)
    )
  })

  // date groups in stable order
  const groups = []
  let lastLabel = null
  for (const s of filtered) {
    const label = dayBucket(s.mtime_ms)
    if (label !== lastLabel) { groups.push({ label, items: [] }); lastLabel = label }
    groups[groups.length - 1].items.push(s)
  }

  const newCount = sessions.filter((s) => !s.imported).length

  const setSearchP = (v) => { setSearch(v); savePref('search:' + provider, v) }
  const setCwdP = (v) => { setCwdFilter(v); savePref('cwd:' + provider, v) }

  const total = (listQuery.data && listQuery.data.total) || sessions.length
  const truncated = total > sessions.length

  return jsxs('div', { className: 'flex h-full flex-col', children: [
    jsxs('div', { className: 'flex items-center justify-between gap-2 px-4 pt-4 pb-2', children: [
      jsxs('div', { className: 'flex min-w-0 items-center gap-2', children: [
        jsx(Button, { variant: 'ghost', size: 'sm', onClick: onBack, children: '←' }),
        jsx('h2', { className: 'truncate text-base font-semibold', children: meta.label }),
      ]}),
      jsxs('div', { className: 'flex shrink-0 items-center gap-1', children: [
        jsx(Tip, { label: 'Import every not-yet-imported session', children:
          jsx(Button, {
            variant: 'secondary', size: 'sm',
            disabled: newCount === 0 || bulk.isPending,
            onClick: () => setConfirmBulk(true),
            children: `Import all new${newCount ? ` (${newCount})` : ''}`,
          }),
        }),
        jsx(Button, {
          variant: 'ghost', size: 'sm',
          onClick: () => refreshAll(),
          disabled: listQuery.isFetching,
          children: listQuery.isFetching ? '…' : 'Refresh',
        }),
      ]}),
    ]}),
    jsx('div', { className: 'px-4 pb-2', children:
      jsx(SearchField, {
        placeholder: `Search ${meta.label} sessions…`,
        value: search,
        onChange: setSearchP,
      })
    }),
    cwds.length > 1
      ? jsx('div', { className: 'flex flex-wrap gap-1 px-4 pb-2', children:
          [jsx('button', {
            key: '__all__', type: 'button',
            onClick: () => setCwdP(''),
            className: cn(
              'rounded-full border px-2 py-0.5 text-xs transition-colors',
              !cwdFilter
                ? 'border-(--ui-accent) text-(--ui-accent)'
                : 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
            ),
            children: 'all projects',
          })].concat(cwds.map((c) => jsx('button', {
            type: 'button',
            onClick: () => setCwdP(cwdFilter === c ? '' : c),
            className: cn(
              'max-w-[180px] truncate rounded-full border px-2 py-0.5 text-xs transition-colors',
              cwdFilter === c
                ? 'border-(--ui-accent) text-(--ui-accent)'
                : 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
            ),
            children: fmtCwd(c),
          }, c)))
        })
      : null,
    truncated
      ? jsx('div', {
          className: 'px-4 pb-1 text-xs text-(--ui-text-quaternary)',
          children: `Showing ${sessions.length} of ${total} sessions — oldest hidden. Import or search to reach older ones; raise the cap in the plugin backend if needed.`,
        })
      : null,
    errors.length > 0
      ? jsxs('div', { className: 'px-4 pb-2 text-xs text-(--ui-text-tertiary)', children: [
          jsx('div', { className: 'font-medium', children: 'Some sessions could not be read:' }),
          errors.slice(0, 3).map((e, i) => jsx('div', { key: i, className: 'truncate', children: e })),
        ]})
      : null,
    jsx('div', { className: 'flex-1 overflow-y-auto pb-4', children:
      filtered.length === 0
        ? jsx(EmptyState, {
            icon: 'search',
            title: q || cwdFilter ? 'No matching sessions' : `No ${meta.label} sessions found`,
            description: q || cwdFilter ? 'Try a different search or filter.' : 'Sessions will appear here as you use the tool.',
          })
        : groups.map((g) => jsxs('div', { children: [
            jsx(DayHeader, { label: g.label, count: g.items.length }),
            g.items.map((s) =>
              jsx(SessionRow, {
                s,
                onView: setViewing,
                onImport: (x) => importOne.mutate(x),
              }, s.session_id || s.path)
            ),
          ]}, g.label))
    }),
    confirmBulk
      ? jsx(ConfirmDialog, {
          title: `Import ${newCount} new session${newCount === 1 ? '' : 's'}?`,
          description: 'Each becomes a resumable plain-text Hermes session. This cannot be undone in bulk.',
          confirmLabel: 'Import all',
          busy: bulk.isPending,
          onCancel: () => setConfirmBulk(false),
          onConfirm: () => bulk.mutate(sessions.filter((s) => !s.imported)),
        })
      : null,
    (bulk.isPending || importOne.isPending)
      ? jsx('div', {
          className: 'px-4 py-2 text-xs text-(--ui-text-tertiary)',
          children: bulk.isPending ? 'Importing all…' : 'Importing…',
        })
      : null,
  ]})
}

function ForeignSessionsPage({ rest }) {
  const [provider, setProviderRaw] = useState(() => loadPref('provider', null))
  const setProvider = (p) => { setProviderRaw(p); savePref('provider', p) }
  if (!provider) return jsx(ProviderPicker, { onPick: setProvider })
  return jsx(ProviderList, { rest, provider, onBack: () => setProvider(null) })
}

// ── statusbar chip: unimported count across both providers ───────────────

function ImportCountChip({ rest }) {
  // Count-only endpoint: no session rows, no double /list load.
  const q = useQuery({
    queryKey: [ID, 'counts'],
    queryFn: () => rest('/counts'),
    refetchInterval: 30000,
  })
  if (!q.data || !q.data.total) return null
  return jsx(Tip, {
    label: `${q.data.fresh} not yet imported of ${q.data.total} foreign sessions`,
    children: jsx('button', {
      type: 'button',
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      onClick: () => { haptic('tap'); host.navigate(PATH) },
      children: `${q.data.fresh} new`,
    }),
  })
}

export default {
  id: ID,
  name: 'Session Importer',
  register(ctx) {
    const rest = ctx.rest.bind(ctx)
    _storage = ctx.storage

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PATH },
        render: () => jsx(ForeignSessionsPage, { rest }),
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PATH, label: 'Foreign Sessions', codicon: 'history' },
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 140,
        render: () => jsx(ImportCountChip, { rest }),
      },
      {
        id: 'palette-open',
        area: PALETTE_AREA,
        data: {
          id: 'session-importer.open',
          label: 'Open Foreign Sessions',
          keywords: ['import', 'claude', 'opencode', 'sessions'],
          run: () => host.navigate(PATH),
        },
      },
      {
        id: 'keybind-refresh',
        area: KEYBINDS_AREA,
        data: {
          id: 'session-importer.refresh',
          label: 'Refresh foreign sessions',
          // No hard default: mod+shift+r collides with Chromium reload
          // conventions — users bind it themselves in Settings.
          defaults: [],
          run: () => refreshAll(),
        },
      },
    ])
  },
}
