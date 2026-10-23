/**
 * Quota Gauge — desktop half.
 *
 * A pane with one card per subscription (Anthropic, Command Code, xAI): each window drains from
 * 100% remaining toward 0%, and the header shows tokens tracked since day one (hover for the exact
 * count). Below, stacked token bars for Today / 7d / All time, every provider combined.
 *
 * Data comes from this plugin's backend (dashboard/plugin_api.py) through ctx.rest. Loaded
 * uncompiled: UI is jsx() calls, imports are limited to the SDK and React.
 */

import { PALETTE_AREA, SegmentedControl, Tip, host, icons, queryClient, useQuery } from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'quota-gauge'
const REFRESH_MS = 60_000
const QUOTA_KEY = [ID, 'quota']
const PANE_ID = `${ID}:pane`

let rest = null
let storage = null

// ---------------------------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------------------------

const EXACT = new Intl.NumberFormat('en-US')

/** 999 → "999", 15500 → "15.5k", 100000 → "100k", 1234567 → "1.2M", 1e9 → "1B". */
function abbreviate(value) {
  const v = Math.max(0, Math.round(Number(value) || 0))
  if (v < 1000) return String(v)
  const units = ['k', 'M', 'B', 'T']
  let x = v
  let i = -1
  while (x >= 1000 && i < units.length - 1) {
    x /= 1000
    i += 1
  }
  let s = x >= 100 ? String(Math.round(x)) : String(Math.round(x * 10) / 10)
  if (s === '1000' && i < units.length - 1) {
    s = '1'
    i += 1
  }
  return s + units[i]
}

const exact = value => EXACT.format(Math.max(0, Math.round(Number(value) || 0)))

function pct(value) {
  const v = Math.max(0, Math.min(100, Number(value) || 0))
  if (v > 0 && v < 1) return '<1'
  return String(Math.round(v))
}

function resetText(epoch, nowMs) {
  if (!epoch) return null
  const s = Math.max(0, Math.round(epoch - nowMs / 1000))
  if (s < 60) return 'resets now'
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  const d = Math.floor(h / 24)
  if (d >= 1) return `resets in ${d}d ${h % 24}h`
  if (h >= 1) return `resets in ${h}h ${m % 60}m`
  return `resets in ${m}m`
}

function agoText(epoch, nowMs) {
  if (!epoch) return ''
  const s = Math.max(0, Math.round(nowMs / 1000 - epoch))
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

const fmtDate = (epoch, opts) => new Date(epoch * 1000).toLocaleDateString(undefined, opts)
const fmtHour = epoch => String(new Date(epoch * 1000).getHours()).padStart(2, '0')

function localDayKey(nowMs) {
  const d = new Date(nowMs)
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

/** Remaining quota → colour: green, then yellow, orange, red as it drains. */
function levelColor(remaining) {
  if (remaining >= 50) return 'var(--ui-green)'
  if (remaining >= 25) return 'var(--ui-yellow)'
  if (remaining >= 10) return 'var(--ui-orange)'
  return 'var(--ui-red)'
}

// ---------------------------------------------------------------------------------------------
// Token kinds and provider marks
// ---------------------------------------------------------------------------------------------

const KINDS = [
  { key: 'input', label: 'Input', color: 'var(--qg-input)', Icon: icons.LogIn },
  { key: 'output', label: 'Output', color: 'var(--qg-output)', Icon: icons.Send },
  { key: 'cache_read', label: 'Cache read', color: 'var(--qg-cache-read)', Icon: icons.Zap },
  { key: 'cache_write', label: 'Cache write', color: 'var(--qg-cache-write)', Icon: icons.Save }
]

const sumKinds = b => (b ? KINDS.reduce((acc, k) => acc + (Number(b[k.key]) || 0), 0) : 0)

function svg(viewBox, children, extra = {}) {
  return jsx('svg', { viewBox, 'aria-hidden': true, focusable: 'false', ...extra, children })
}

// Anthropic and xAI marks: @lobehub/icons (MIT). Command Code: the glyph of its official symbol
// (commandcode.ai/brand), cropped to the glyph so all three sit on the same kind of tile.
const MARKS = {
  anthropic: () =>
    svg(
      '0 0 24 24',
      jsx('path', {
        fill: 'currentColor',
        fillRule: 'evenodd',
        d: 'M13.827 3.52h3.603L24 20h-3.603l-6.57-16.48zm-7.258 0h3.767L16.906 20h-3.674l-1.343-3.461H5.017l-1.344 3.46H0L6.57 3.522zm4.132 9.959L8.453 7.687 6.205 13.48H10.7z'
      })
    ),
  xai: () =>
    svg(
      '0 0 24 24',
      jsx('path', {
        fill: 'currentColor',
        d: 'M6.469 8.776L16.512 23h-4.464L2.005 8.776H6.47zm-.004 7.9l2.233 3.164L6.467 23H2l4.465-6.324zM22 2.582V23h-3.659V7.764L22 2.582zM22 1l-9.952 14.095-2.233-3.163L17.533 1H22z'
      })
    ),
  commandcode: () => svg('24 24 88 88', jsx('path', { fill: 'currentColor', d: CC_GLYPH }))
}

const PROVIDERS = [
  { id: 'anthropic', name: 'Anthropic', tile: 'var(--ui-orange)' },
  { id: 'commandcode', name: 'Command Code', tile: 'var(--qg-cc)' },
  { id: 'xai', name: 'xAI', tile: 'var(--ui-text-primary)' }
]

const CC_GLYPH = 'm93.6604 26.1784c-8.982 0-16.2887 7.3067-16.2887 16.2888v6.9809h-18.6158v-6.9809c0-8.9821-7.3067-16.2888-16.2887-16.2888-8.9821 0-16.2888 7.3067-16.2888 16.2888s7.3067 16.2887 16.2888 16.2887h6.9809v18.6158h-6.9809c-8.9821 0-16.2888 7.3067-16.2888 16.2888 0 8.9825 7.3067 16.2885 16.2888 16.2885 8.982 0 16.2887-7.306 16.2887-16.2885v-6.981h18.6158v6.981c0 8.9825 7.3067 16.2885 16.2887 16.2885 8.9826 0 16.2886-7.306 16.2886-16.2885 0-8.9821-7.306-16.2888-16.2886-16.2888h-6.9809v-18.6158h6.9809c8.9826 0 16.2886-7.3066 16.2886-16.2887s-7.306-16.2888-16.2886-16.2888zm-6.9809 23.2697v-6.9809c0-3.8628 3.1182-6.9809 6.9809-6.9809 3.8628 0 6.9806 3.1181 6.9806 6.9809 0 3.8627-3.1178 6.9809-6.9806 6.9809zm-44.2123 0c-3.8628 0-6.9809-3.1182-6.9809-6.9809 0-3.8628 3.1181-6.9809 6.9809-6.9809 3.8627 0 6.9809 3.1181 6.9809 6.9809v6.9809zm16.2887 27.9236v-18.6158h18.6158v18.6158zm34.9045 23.2693c-3.8627 0-6.9809-3.1178-6.9809-6.9805v-6.981h6.9809c3.8628 0 6.9806 3.1182 6.9806 6.981 0 3.8627-3.1178 6.9805-6.9806 6.9805zm-51.1932 0c-3.8628 0-6.9809-3.1178-6.9809-6.9805 0-3.8628 3.1181-6.981 6.9809-6.981h6.9809v6.981c0 3.8627-3.1182 6.9805-6.9809 6.9805z'

// ---------------------------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------------------------

function useNow(intervalMs) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

function revealPane() {
  if (typeof host?.revealPane === 'function') host.revealPane(PANE_ID)
}

const quotaQuery = () => ({
  queryKey: QUOTA_KEY,
  queryFn: () => call('/quota', { timeoutMs: 45_000 }),
  refetchInterval: REFRESH_MS,
  staleTime: 20_000,
  retry: 1
})

function call(path, opts) {
  if (!rest) return Promise.reject(new Error('Quota Gauge is not loaded'))
  return rest(path, opts)
}

function backendMissing(error) {
  const text = String(error?.message || error || '')
  return /\b404\b|not found|ECONNREFUSED|plugin.*(disabled|not enabled)/i.test(text)
}

// ---------------------------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------------------------

function Logo({ id }) {
  const meta = PROVIDERS.find(p => p.id === id)
  const Mark = MARKS[id]
  return jsx('span', {
    className: 'qg-logo',
    style: { color: meta.tile, background: `color-mix(in srgb, ${meta.tile} 13%, transparent)` },
    children: Mark()
  })
}

function KindIcon({ kind, size = 12 }) {
  const Icon = kind.Icon
  return Icon
    ? jsx(Icon, { size, stroke: 2, style: { color: `color-mix(in srgb, ${kind.color} 70%, var(--ui-text-primary))`, flex: 'none' }, 'aria-label': kind.label })
    : null
}

function TokenTotal({ totals, since }) {
  const total = totals?.total || 0
  const label = jsxs('div', {
    className: 'qg-tip',
    children: [
      jsx('div', { className: 'qg-tip-strong', children: `${exact(total)} tokens` }),
      jsx('div', { className: 'qg-tip-muted', children: since ? `tracked since ${fmtDate(since, { month: 'short', day: 'numeric', year: 'numeric' })}` : 'tracking starts with your next request' }),
      jsx('div', {
        className: 'qg-tip-grid',
        children: KINDS.map(k =>
          jsxs('div', { className: 'qg-tip-row', children: [jsx(KindIcon, { kind: k }), jsx('span', { children: exact(totals?.[k.key] || 0) })] }, k.key)
        )
      })
    ]
  })
  return jsx(Tip, {
    label,
    side: 'bottom',
    children: jsxs('span', {
      className: 'qg-total',
      tabIndex: 0,
      children: [jsx('b', { children: abbreviate(total) }), jsx('span', { children: 'tokens' })]
    })
  })
}

function QuotaRow({ w, now }) {
  const remaining = Math.max(0, Math.min(100, Number(w.remaining_percent) || 0))
  const color = levelColor(remaining)
  const reset = resetText(w.resets_at, now)
  const tip = [w.detail, `${pct(100 - remaining)}% used`, reset].filter(Boolean).join(' · ')
  return jsxs('div', {
    className: 'qg-row',
    children: [
      jsxs('div', {
        className: 'qg-row-top',
        children: [
          jsx('span', { className: 'qg-wlabel', children: w.label }),
          reset ? jsx('span', { className: 'qg-reset', children: reset }) : null,
          jsx('span', { className: 'qg-spacer' }),
          jsx('span', { className: 'qg-pct', style: { color: `color-mix(in srgb, ${color} 78%, var(--ui-text-primary))` }, children: `${pct(remaining)}%` }),
          jsx('span', { className: 'qg-rem', children: 'remaining' })
        ]
      }),
      jsx(Tip, {
        label: tip,
        children: jsx('div', {
          className: 'qg-track',
          role: 'meter',
          'aria-valuenow': Math.round(remaining),
          'aria-valuemin': 0,
          'aria-valuemax': 100,
          'aria-label': `${w.label}: ${pct(remaining)}% remaining`,
          children: jsx('div', {
            className: 'qg-fill',
            style: {
              width: `${remaining}%`,
              background: `linear-gradient(90deg, color-mix(in srgb, ${color} 62%, transparent), ${color})`,
              boxShadow: `0 0 10px -2px color-mix(in srgb, ${color} 55%, transparent)`
            }
          })
        })
      })
    ]
  })
}

function ProviderCard({ meta, data, totals, since, now }) {
  const ok = data?.status === 'ok'
  const windows = ok ? data.windows || [] : []
  return jsxs('section', {
    className: 'qg-card',
    children: [
      jsxs('div', {
        className: 'qg-card-head',
        children: [
          jsx(Logo, { id: meta.id }),
          jsx('span', { className: 'qg-name', children: meta.name }),
          data?.plan ? jsx('span', { className: 'qg-badge', children: data.plan }) : null,
          ok && data.warning ? jsx(Tip, { label: data.warning.detail || data.warning.label, children: jsx('span', { className: `qg-flag qg-flag-${data.warning.level || 'warn'}`, children: data.warning.label }) }) : null,
          data?.stale
            ? jsx(Tip, { label: data.error || 'Showing the last good reading', children: jsx('span', { className: 'qg-stale', children: jsx(icons.AlertTriangle, { size: 12 }) }) })
            : null,
          jsx('span', { className: 'qg-spacer' }),
          jsx(TokenTotal, { totals, since: totals?.first_at || since })
        ]
      }),
      windows.length
        ? windows.map(w => jsx(QuotaRow, { w, now }, w.id))
        : jsxs('div', {
            className: 'qg-msg',
            children: [
              data?.status === 'loading' ? jsx('span', { className: 'qg-spin', children: jsx(icons.RefreshCw, { size: 12 }) }) : jsx(icons.Info, { size: 13, style: { flex: 'none', marginTop: 1 } }),
              jsx('span', { children: data?.message || 'Loading…' })
            ]
          }),
      ok && data.details?.length ? jsx('div', { className: 'qg-detail', children: data.details.join(' · ') }) : null
    ]
  })
}

// Usage chart --------------------------------------------------------------------------------

const RANGES = [
  { id: 'today', label: 'Today' },
  { id: '7d', label: '7d' },
  { id: 'all', label: 'All time' }
]

function barTitle(bar, unit) {
  if (!bar) return ''
  if (unit === 'hour') return `${fmtHour(bar.start)}:00 – ${fmtHour(bar.end)}:00`
  if (unit === 'day') return fmtDate(bar.start, { weekday: 'short', month: 'short', day: 'numeric' })
  if (unit === 'week') return `Week of ${fmtDate(bar.start, { month: 'short', day: 'numeric' })}`
  return fmtDate(bar.start, { month: 'long', year: 'numeric' })
}

function axisLabel(bar, i, n, unit, range) {
  if (range === '7d') return fmtDate(bar.start, { weekday: 'short' })
  const every = Math.max(1, Math.ceil(n / 4))
  if (i !== 0 && i !== n - 1 && i % every !== 0) return ''
  if (i !== n - 1 && n - 1 - i < every / 2 && i !== 0) return ''
  if (unit === 'month') return fmtDate(bar.start, { month: 'short' })
  return fmtDate(bar.start, { month: 'short', day: 'numeric' })
}

function rangeCaption(range, usage) {
  if (range === 'today') return 'today, all providers'
  if (range === '7d') return 'last 7 days, all providers'
  const since = usage?.tracking_since
  return since ? `since ${fmtDate(since, { month: 'short', day: 'numeric', year: 'numeric' })}` : 'all time'
}

function Bars({ bars, unit, range, hover, setHover, nowMs }) {
  const totals = bars.map(sumKinds)
  const max = Math.max(1, ...totals)
  const n = bars.length
  const gap = n > 40 ? 1 : n > 20 ? 2 : n > 10 ? 4 : 8
  const nowS = nowMs / 1000
  const empty = totals.every(t => t === 0)
  return jsxs('div', {
    className: 'qg-chart-wrap',
    style: { '--qg-gap': `${gap}px` },
    onMouseLeave: () => setHover(null),
    children: [
      jsxs('div', {
        className: 'qg-chart',
        children: [
          !empty ? jsx('div', { className: 'qg-grid', children: jsx('span', { children: abbreviate(max) }) }) : null,
          empty ? jsx('div', { className: 'qg-empty', children: range === 'today' ? 'No tokens yet today' : 'No tokens in this range' }) : null,
          jsx('div', {
            className: 'qg-cols',
            children: bars.map((bar, i) => {
              const total = totals[i]
              const future = bar.start > nowS
              const current = bar.start <= nowS && nowS < bar.end
              const height = total > 0 ? Math.max(2.5, (total / max) * 100) : 0
              const cls = ['qg-col', hover !== null && hover !== i ? 'dim' : '', hover === i ? 'on' : '', current ? 'now' : '', future ? 'future' : '']
              const centre = (i + 0.5) / n
              const pillPos = n <= 12 ? 'mid' : centre < 0.08 ? 'start' : centre > 0.92 ? 'end' : 'mid'
              return jsxs(
                'div',
                {
                  className: cls.filter(Boolean).join(' '),
                  onMouseEnter: () => setHover(i),
                  onFocus: () => setHover(i),
                  tabIndex: future ? -1 : 0,
                  'aria-label': `${barTitle(bar, unit)}: ${exact(total)} tokens`,
                  children: [
                    hover === i && total > 0
                      ? jsx('span', { className: `qg-pill qg-pill-${pillPos}`, style: { bottom: `calc(${height}% + 5px)` }, children: abbreviate(total) })
                      : null,
                    total > 0
                      ? jsx('div', {
                          className: 'qg-stack',
                          style: { height: `${height}%` },
                          children: KINDS.map(k => {
                            const v = Number(bar[k.key]) || 0
                            return v > 0 ? jsx('div', { className: 'qg-seg', style: { flexGrow: v, background: k.color } }, k.key) : null
                          })
                        })
                      : jsx('div', { className: 'qg-ghost' })
                  ]
                },
                bar.start
              )
            })
          })
        ]
      }),
      unit === 'hour'
        ? jsx('div', {
            className: 'qg-axis qg-axis-abs',
            children: [0, 6, 12, 18, 24].map(h =>
              jsx('span', { style: { left: `${(h / 24) * 100}%` }, className: h === 0 ? 'first' : h === 24 ? 'last' : '', children: String(h).padStart(2, '0') }, h)
            )
          })
        : jsx('div', {
            className: 'qg-axis',
            children: bars.map((bar, i) => jsx('span', { className: hover === i ? 'on' : '', children: axisLabel(bar, i, n, unit, range) }, bar.start))
          })
    ]
  })
}

function UsageCard({ usage, nowMs, loading }) {
  const [range, setRange] = useState(() => storage?.get('range', 'today') || 'today')
  const [hover, setHover] = useState(null)
  const series = usage?.series?.[range]
  const bars = series?.bars || []
  const unit = series?.unit || 'day'

  useEffect(() => setHover(null), [range])

  const periodTotals = useMemo(() => {
    const out = {}
    for (const k of KINDS) out[k.key] = bars.reduce((acc, b) => acc + (Number(b[k.key]) || 0), 0)
    return out
  }, [bars])

  const focused = hover !== null ? bars[hover] : null
  const shown = focused || periodTotals
  const shownTotal = sumKinds(shown)

  return jsxs('section', {
    className: 'qg-card qg-usage',
    children: [
      jsxs('div', {
        className: 'qg-card-head',
        children: [
          jsx('span', { className: 'qg-logo qg-logo-usage', children: jsx(icons.BarChart3, { size: 14 }) }),
          jsx('span', { className: 'qg-name', children: 'Tokens' }),
          jsx('span', { className: 'qg-spacer' }),
          jsx(SegmentedControl, {
            options: RANGES,
            value: range,
            onChange: id => {
              setRange(id)
              storage?.set('range', id)
            },
            sizing: 'content'
          })
        ]
      }),
      jsxs('div', {
        className: 'qg-summary',
        children: [
          jsx(Tip, {
            label: `${exact(shownTotal)} tokens`,
            children: jsx('span', { className: 'qg-big', tabIndex: 0, children: loading && !usage ? '—' : abbreviate(shownTotal) })
          }),
          jsx('span', { className: 'qg-caption', children: focused ? barTitle(focused, unit) : rangeCaption(range, usage) })
        ]
      }),
      jsx('div', {
        className: 'qg-legend',
        children: KINDS.map(k => {
          const v = Number(shown?.[k.key]) || 0
          const share = shownTotal > 0 ? Math.round((v / shownTotal) * 100) : 0
          return jsx(
            Tip,
            {
              label: `${k.label}: ${exact(v)} tokens (${share}%)`,
              children: jsxs('span', {
                className: 'qg-chip',
                style: { '--c': k.color },
                tabIndex: 0,
                children: [jsx(KindIcon, { kind: k }), jsx('span', { children: abbreviate(v) })]
              })
            },
            k.key
          )
        })
      }),
      bars.length ? jsx(Bars, { bars, unit, range, hover, setHover, nowMs }) : jsx('div', { className: 'qg-chart qg-chart-loading' })
    ]
  })
}

function SetupCard() {
  return jsxs('section', {
    className: 'qg-card',
    children: [
      jsxs('div', { className: 'qg-card-head', children: [jsx(icons.Plug, { size: 14 }), jsx('span', { className: 'qg-name', children: 'Backend is off' })] }),
      jsxs('div', {
        className: 'qg-msg',
        children: [
          jsxs('span', {
            children: [
              'Quota Gauge reads usage through its Python half. Enable it with ',
              jsx('code', { children: 'hermes plugins enable quota-gauge' }),
              ', then reopen this pane.'
            ]
          })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------------------------
// Pane
// ---------------------------------------------------------------------------------------------

function QuotaPane() {
  const nowMs = useNow(15_000)
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', [])
  const dayKey = localDayKey(nowMs)
  const [refreshing, setRefreshing] = useState(false)

  const quota = useQuery(quotaQuery())

  // The local day is part of the key, so the "Today" chart rolls over at midnight on its own.
  const usage = useQuery({
    queryKey: [ID, 'usage', tz, dayKey],
    queryFn: () => call(`/usage?tz=${encodeURIComponent(tz)}`, { timeoutMs: 30_000 }),
    refetchInterval: REFRESH_MS,
    staleTime: 20_000,
    placeholderData: previous => previous,
    retry: 1
  })

  const refresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      const fresh = await call('/quota?force=1', { timeoutMs: 45_000 })
      queryClient.setQueryData(QUOTA_KEY, fresh)
      await queryClient.invalidateQueries({ queryKey: [ID, 'usage'] })
    } catch {
      // the next poll reports the error
    } finally {
      setRefreshing(false)
    }
  }

  const missing = (quota.error && backendMissing(quota.error)) || (usage.error && backendMissing(usage.error))
  const providers = quota.data?.providers || {}
  const totals = usage.data?.providers || {}
  const since = usage.data?.tracking_since
  const updated = Math.max(quota.data?.generated_at || 0, usage.data?.generated_at || 0)
  const busy = refreshing || quota.isFetching || usage.isFetching

  return jsxs('div', {
    className: 'qg',
    children: [
      jsx('style', { children: CSS }),
      jsxs('header', {
        className: 'qg-head',
        children: [
          jsx('span', { className: 'qg-title', children: 'Subscriptions' }),
          jsx('span', { className: 'qg-spacer' }),
          updated ? jsx('span', { className: 'qg-sub', children: `Updated ${agoText(updated, nowMs)}` }) : null,
          jsx(Tip, {
            label: 'Refresh now',
            children: jsx('button', {
              type: 'button',
              className: `qg-iconbtn${busy ? ' qg-spin' : ''}`,
              onClick: refresh,
              'aria-label': 'Refresh quota',
              children: jsx(icons.RefreshCw, { size: 14 })
            })
          })
        ]
      }),
      missing
        ? jsx(SetupCard, {})
        : PROVIDERS.map(meta =>
            jsx(ProviderCard, {
              meta,
              data: providers[meta.id] || (quota.isLoading ? { status: 'loading', message: 'Loading…' } : { status: 'error', message: quota.error ? String(quota.error.message || quota.error) : 'No data yet.' }),
              totals: totals[meta.id],
              since,
              now: nowMs
            }, meta.id)
          ),
      missing ? null : jsx(UsageCard, { usage: usage.data, nowMs, loading: usage.isLoading })
    ]
  })
}

// ---------------------------------------------------------------------------------------------
// Status-bar chip: the lowest remaining account window; click opens the pane
// ---------------------------------------------------------------------------------------------

function lowestWindows(providers) {
  const rows = []
  for (const meta of PROVIDERS) {
    const p = providers?.[meta.id]
    if (p?.status !== 'ok') continue
    const account = (p.windows || []).filter(w => w.scope !== 'model')
    if (!account.length) continue
    const low = account.reduce((a, b) => (Number(b.remaining_percent) < Number(a.remaining_percent) ? b : a))
    rows.push({ meta, w: low })
  }
  return rows
}

function QuotaChip() {
  const quota = useQuery(quotaQuery())
  const rows = lowestWindows(quota.data?.providers)
  const min = rows.length ? rows.reduce((a, b) => (Number(b.w.remaining_percent) < Number(a.w.remaining_percent) ? b : a)) : null
  const remaining = min ? Number(min.w.remaining_percent) : null
  const label = rows.length
    ? jsx('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 3 },
        children: [
          jsx('div', { style: { fontWeight: 650 }, children: 'Quota remaining' }, 'h'),
          ...rows.map(({ meta, w }) =>
            jsx('div', { style: { fontVariantNumeric: 'tabular-nums' }, children: `${meta.name} · ${w.label}: ${pct(w.remaining_percent)}%` }, meta.id)
          ),
          jsx('div', { style: { opacity: 0.7, fontSize: '10.5px', marginTop: 2 }, children: 'Click to open Quota Gauge' }, 'f')
        ]
      })
    : 'Open Quota Gauge'
  return jsx(Tip, {
    label,
    children: jsxs('button', {
      type: 'button',
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground',
      onClick: revealPane,
      'aria-label': 'Open Quota Gauge',
      children: [
        jsx(icons.Activity, { size: 13, stroke: 2 }),
        remaining !== null
          ? jsx('span', {
              style: { color: `color-mix(in srgb, ${levelColor(remaining)} 78%, var(--ui-text-primary))`, fontWeight: 600, fontVariantNumeric: 'tabular-nums' },
              children: `${pct(remaining)}%`
            })
          : null
      ]
    })
  })
}

// ---------------------------------------------------------------------------------------------
// Styles (rendered with the pane, so they leave with it)
// ---------------------------------------------------------------------------------------------

const CSS = `
.qg{--qg-input:color-mix(in srgb,var(--ui-blue) 55%,#5b8cff);--qg-output:color-mix(in srgb,var(--ui-purple) 45%,#a35cf0);
  --qg-cache-read:color-mix(in srgb,var(--ui-cyan) 45%,#14b8c8);--qg-cache-write:color-mix(in srgb,var(--ui-orange) 60%,#f5841f);
  --qg-cc:var(--ui-text-primary);
  display:flex;flex-direction:column;gap:10px;height:100%;overflow-y:auto;padding:12px 12px 18px;box-sizing:border-box;
  font-size:12px;line-height:1.35;color:var(--ui-text-primary);font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.qg *{box-sizing:border-box}
.qg-head{display:flex;align-items:center;gap:8px;padding:0 2px}
.qg-title{font-weight:600;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ui-text-tertiary)}
.qg-sub{color:var(--ui-text-tertiary);font-size:10.5px}
.qg-spacer{flex:1 1 auto}
.qg-iconbtn{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:6px;border:0;
  background:transparent;color:var(--ui-text-tertiary);cursor:pointer}
.qg-iconbtn:hover{background:var(--ui-control-hover-background);color:var(--ui-text-primary)}
.qg-spin svg{animation:qg-spin .9s linear infinite}
@keyframes qg-spin{to{transform:rotate(360deg)}}

.qg-card{display:flex;flex-direction:column;gap:11px;padding:11px 12px 12px;border-radius:12px;
  border:1px solid var(--ui-stroke-tertiary);background:color-mix(in srgb,var(--ui-text-primary) 2.5%,transparent)}
.qg-card-head{display:flex;align-items:center;gap:8px;min-width:0}
.qg-logo{display:inline-flex;align-items:center;justify-content:center;flex:none;width:22px;height:22px;border-radius:7px}
.qg-logo svg{width:13px;height:13px}
.qg-logo-usage{color:var(--ui-blue);background:color-mix(in srgb,var(--ui-blue) 13%,transparent)}
.qg-name{font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qg-badge{flex:none;font-size:9.5px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;border-radius:999px;
  color:var(--ui-text-secondary);background:color-mix(in srgb,var(--ui-text-primary) 8%,transparent)}
.qg-stale{display:inline-flex;color:var(--ui-yellow)}
.qg-flag{flex:none;font-size:9.5px;font-weight:650;padding:2px 6px;border-radius:999px;cursor:default;
  color:color-mix(in srgb,var(--ui-yellow) 70%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-yellow) 16%,transparent)}
.qg-flag-limit{color:color-mix(in srgb,var(--ui-red) 75%,var(--ui-text-primary));background:color-mix(in srgb,var(--ui-red) 15%,transparent)}
.qg-total{display:inline-flex;align-items:baseline;gap:4px;padding:2px 6px;margin-right:-6px;border-radius:6px;cursor:default;outline:none}
.qg-total:hover,.qg-total:focus-visible{background:var(--ui-control-hover-background)}
.qg-total b{font-weight:650;font-size:12.5px}
.qg-total span{color:var(--ui-text-tertiary);font-size:10.5px}

.qg-row{display:flex;flex-direction:column;gap:6px}
.qg-row-top{display:flex;align-items:baseline;gap:6px;min-width:0}
.qg-wlabel{font-weight:550;color:var(--ui-text-secondary)}
.qg-reset{color:var(--ui-text-tertiary);font-size:10.5px;white-space:nowrap}
.qg-pct{font-weight:700;font-size:13px;letter-spacing:-.01em}
.qg-rem{color:var(--ui-text-tertiary);font-size:10.5px}
.qg-track{position:relative;height:7px;border-radius:999px;background:color-mix(in srgb,var(--ui-text-primary) 8%,transparent)}
.qg-fill{height:100%;border-radius:999px;min-width:2px;transition:width .7s cubic-bezier(.2,.8,.2,1)}
.qg-msg{display:flex;align-items:flex-start;gap:7px;color:var(--ui-text-tertiary);font-size:11px;line-height:1.45}
.qg-msg code{font-size:10.5px;padding:0 4px;border-radius:4px;background:var(--ui-inline-code-background);color:var(--ui-inline-code-foreground)}
.qg-detail{color:var(--ui-text-tertiary);font-size:10.5px;margin-top:-3px}

.qg-tip{display:flex;flex-direction:column;gap:3px;font-variant-numeric:tabular-nums}
.qg-tip-strong{font-weight:650}
.qg-tip-muted{opacity:.7;font-size:10.5px}
.qg-tip-grid{display:grid;grid-template-columns:auto auto;gap:3px 12px;margin-top:3px}
.qg-tip-row{display:flex;align-items:center;gap:5px}

.qg-summary{display:flex;align-items:baseline;gap:8px;margin-top:-2px}
.qg-big{font-size:24px;font-weight:700;letter-spacing:-.02em;line-height:1;outline:none;cursor:default}
.qg-caption{color:var(--ui-text-tertiary);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qg-legend{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}
.qg-chip{display:flex;align-items:center;justify-content:center;gap:5px;padding:4px 4px;border-radius:7px;font-size:11px;font-weight:600;
  color:var(--ui-text-secondary);background:color-mix(in srgb,var(--c) 11%,transparent);outline:none;cursor:default;white-space:nowrap}
.qg-chip:hover,.qg-chip:focus-visible{background:color-mix(in srgb,var(--c) 18%,transparent)}

.qg-chart-wrap{display:flex;flex-direction:column}
.qg-chart{position:relative;height:120px;padding-top:20px}
.qg-chart-loading{background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--ui-text-primary) 4%,transparent),transparent);border-radius:8px}
.qg-grid{position:absolute;left:0;right:0;top:20px;border-top:1px dashed var(--ui-stroke-tertiary);pointer-events:none}
.qg-grid span{position:absolute;right:0;top:-15px;font-size:9.5px;color:var(--ui-text-tertiary);transition:opacity .15s}
.qg-chart-wrap:has(.qg-col.on) .qg-grid span{opacity:0}
.qg-empty{position:absolute;inset:20px 0 0;display:flex;align-items:center;justify-content:center;color:var(--ui-text-quaternary);font-size:11px;pointer-events:none}
.qg-cols{position:relative;display:flex;align-items:stretch;gap:var(--qg-gap);height:100%;border-bottom:1px solid var(--ui-stroke-tertiary)}
.qg-col{position:relative;flex:1 1 0;min-width:0;display:flex;flex-direction:column;justify-content:flex-end;outline:none;
  transition:opacity .18s ease,transform .18s ease}
.qg-col.dim{opacity:.28}
.qg-col.future{opacity:.45}
.qg-col.dim.future{opacity:.12}
.qg-stack{display:flex;flex-direction:column-reverse;overflow:hidden;border-radius:4px 4px 1px 1px;
  transition:height .55s cubic-bezier(.2,.8,.2,1),filter .18s ease}
.qg-col.on .qg-stack{filter:saturate(1.15) brightness(1.06)}
.qg-col.now .qg-ghost,.qg-col.now .qg-stack{box-shadow:0 0 0 1px color-mix(in srgb,var(--ui-text-primary) 18%,transparent)}
.qg-seg{flex-basis:0;min-height:1px}
.qg-ghost{height:2px;border-radius:2px;background:color-mix(in srgb,var(--ui-text-primary) 9%,transparent)}
.qg-pill{position:absolute;padding:1px 6px;border-radius:6px;font-size:10px;font-weight:650;white-space:nowrap;pointer-events:none;z-index:2;
  color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-text-primary) 10%,var(--ui-bg-elevated));
  border:1px solid color-mix(in srgb,var(--ui-text-primary) 22%,transparent);
  box-shadow:0 4px 12px -6px color-mix(in srgb,#000 45%,transparent)}
.qg-pill-mid{left:50%;transform:translateX(-50%)}
.qg-pill-start{left:0}
.qg-pill-end{right:0}
.qg-axis{display:flex;gap:var(--qg-gap);height:14px;margin-top:5px}
.qg-axis span{flex:1 1 0;min-width:0;display:flex;justify-content:center;font-size:9.5px;color:var(--ui-text-tertiary);white-space:nowrap;overflow:visible}
.qg-axis span.on{color:var(--ui-text-primary);font-weight:600}
.qg-axis-abs{position:relative}
.qg-axis-abs span{position:absolute;top:0;transform:translateX(-50%)}
.qg-axis-abs span.first{transform:none}
.qg-axis-abs span.last{transform:translateX(-100%)}
`

// ---------------------------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------------------------

export default {
  id: ID,
  name: 'Quota Gauge',
  register(ctx) {
    rest = (path, opts) => ctx.rest(path, opts)
    storage = ctx.storage
    ctx.onDispose(() => {
      rest = null
      storage = null
    })

    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Quota',
      data: { placement: 'right', width: '340px' },
      render: () => jsx(QuotaPane, {})
    })

    ctx.register({ id: 'chip', area: 'statusBar.right', order: 120, render: () => jsx(QuotaChip, {}) })

    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: `${ID}.open`,
        label: 'Show Quota Gauge',
        keywords: ['quota', 'usage', 'tokens', 'limits', 'subscription'],
        icon: icons.Activity,
        run: revealPane
      }
    })

    // A plugin pane joins the right zone as a background tab. Bring it forward once, the first
    // time the plugin loads, so turning it on visibly does something; after that it stays where
    // the user leaves it.
    if (!ctx.storage.get('revealedOnce', false)) {
      ctx.setTimeout(() => {
        revealPane()
        ctx.storage.set('revealedOnce', true)
      }, 1500)
    }
  }
}
