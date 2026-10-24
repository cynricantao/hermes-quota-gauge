/**
 * Quota Gauge — desktop half.
 *
 * A pane with one card per live subscription: each window drains from 100% remaining toward 0%,
 * and the header shows tokens tracked since day one (hover for the exact count). Providers this
 * profile has no sign-in or paid plan for collapse into a "Hidden/Inactive" dropdown. Below,
 * stacked token bars for Today / 7d / All time, every provider combined.
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

/** Plain text with `backtick` spans rendered as inline code (setup hints name commands). */
function richText(text) {
  const parts = String(text || '').split('`')
  return parts.length < 3 ? text : parts.map((part, i) => (i % 2 ? jsx('code', { children: part }, i) : part))
}

function svg(viewBox, children, extra = {}) {
  return jsx('svg', { viewBox, 'aria-hidden': true, focusable: 'false', ...extra, children })
}

// Glyph paths from @lobehub/icons 1.98.0 (MIT), 24x24, even-odd fill.
const LOBE_PATHS = {
  codex: ['M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z'],
  antigravity: ['M21.751 22.607c1.34 1.005 3.35.335 1.508-1.508C17.73 15.74 18.904 1 12.037 1 5.17 1 6.342 15.74.815 21.1c-2.01 2.009.167 2.511 1.507 1.506 5.192-3.517 4.857-9.714 9.715-9.714 4.857 0 4.522 6.197 9.714 9.715z'],
  opencode: ['M16 6H8v12h8V6zm4 16H4V2h16v20z'],
  devin: ['M2.033 9.867l2.554 1.483a.589.589 0 00.592 0l2.554-1.483.01-.008a.608.608 0 00.11-.084l.013-.015a.631.631 0 00.076-.1c.003-.005.008-.01.01-.016a.558.558 0 00.052-.125l.007-.028a.611.611 0 00.019-.14V7.868c0-.572.307-1.105.8-1.392a1.595 1.595 0 011.598 0l1.277.742a.54.54 0 00.129.053l.028.01c.044.01.088.015.133.016h.006l.013-.002a.587.587 0 00.27-.074l.011-.004 2.554-1.483a.596.596 0 00.297-.516V2.253a.595.595 0 00-.297-.516L12.293.257a.587.587 0 00-.591 0L9.148 1.737l-.01.01a.609.609 0 00-.109.083l-.014.015a.632.632 0 00-.076.1c-.003.005-.008.01-.01.016a.57.57 0 00-.052.124l-.007.028a.612.612 0 00-.018.14v1.483c0 .572-.307 1.105-.8 1.393a1.597 1.597 0 01-1.599 0l-1.276-.742a.603.603 0 00-.13-.053l-.028-.008a.658.658 0 00-.133-.018h-.02a.57.57 0 00-.269.074c-.003.002-.008.002-.012.005L2.033 5.872a.596.596 0 00-.297.515v2.966c0 .213.113.41.297.515z', 'M15.943 10.607a1.596 1.596 0 011.599 0l1.276.74c.041.025.085.04.13.055l.028.008c.043.01.088.016.133.018h.005c.005 0 .01-.002.014-.003a.474.474 0 00.122-.016l.021-.005a.616.616 0 00.126-.052c.004-.002.009-.002.013-.005l2.554-1.482a.597.597 0 00.297-.516V6.383a.596.596 0 00-.297-.515l-2.552-1.483a.587.587 0 00-.592 0l-2.553 1.482-.011.008a.61.61 0 00-.108.084l-.014.016a.637.637 0 00-.076.1c-.003.005-.008.01-.01.016a.57.57 0 00-.052.124l-.007.029a.612.612 0 00-.018.14v1.482c0 .572-.307 1.105-.8 1.393a1.597 1.597 0 01-1.599 0l-1.276-.742a.584.584 0 00-.13-.053l-.028-.008a.62.62 0 00-.133-.018h-.02a.587.587 0 00-.269.074l-.012.004L9.15 10a.596.596 0 00-.296.516v2.966c0 .212.112.409.296.515l2.554 1.483s.008.002.012.005c.04.022.082.04.126.052l.02.004a.57.57 0 00.123.017l.014.002h.006c.054 0 .108-.01.16-.025a.587.587 0 00.13-.054l1.277-.741a1.597 1.597 0 012.398 1.392v1.482c0 .049.007.095.019.14l.007.028a.619.619 0 00.051.125c.004.006.008.01.01.016a.6.6 0 00.076.1l.014.015c.033.032.069.06.108.084.004.002.006.006.011.008l2.554 1.483a.59.59 0 00.593 0l2.554-1.483a.597.597 0 00.296-.516v-2.965a.595.595 0 00-.296-.516l-2.554-1.483s-.008-.002-.012-.005a.54.54 0 00-.126-.051c-.007-.003-.013-.003-.02-.005a.635.635 0 00-.125-.017h-.018a.557.557 0 00-.16.026.588.588 0 00-.13.053l-1.276.742a1.595 1.595 0 01-1.598 0 1.615 1.615 0 010-2.785l-.005-.001z', 'M14.848 18.265l-2.554-1.482-.012-.005a.526.526 0 00-.126-.052c-.007-.002-.014-.002-.02-.005a.64.64 0 00-.124-.017h-.02a.56.56 0 00-.16.026.588.588 0 00-.13.053l-1.276.742a1.594 1.594 0 01-1.598 0c-.493-.286-.8-.82-.8-1.393V14.65a.563.563 0 00-.018-.14l-.008-.028a.604.604 0 00-.051-.124l-.01-.017a.603.603 0 00-.076-.1l-.014-.015a.596.596 0 00-.109-.084c-.003-.002-.005-.006-.01-.008L5.178 12.65a.587.587 0 00-.591 0l-2.554 1.483a.596.596 0 00-.297.516v2.965c0 .213.113.41.297.516l2.554 1.483.012.004a.618.618 0 00.267.074l.016.002h.007a.55.55 0 00.16-.026.584.584 0 00.129-.053l1.277-.742a1.597 1.597 0 012.398 1.393v1.482c0 .05.007.095.019.14l.007.028c.013.044.03.085.051.125l.01.016c.022.036.047.07.076.1l.014.015c.032.032.069.06.109.084l.01.008 2.554 1.483a.587.587 0 00.593 0l2.554-1.483a.596.596 0 00.296-.515v-2.966a.596.596 0 00-.296-.516h-.002z'],
  copilot: ['M19.245 5.364c1.322 1.36 1.877 3.216 2.11 5.817.622 0 1.2.135 1.592.654l.73.964c.21.278.323.61.323.955v2.62c0 .339-.173.669-.453.868C20.239 19.602 16.157 21.5 12 21.5c-4.6 0-9.205-2.583-11.547-4.258-.28-.2-.452-.53-.453-.868v-2.62c0-.345.113-.679.321-.956l.73-.963c.392-.517.974-.654 1.593-.654l.029-.297c.25-2.446.81-4.213 2.082-5.52 2.461-2.54 5.71-2.851 7.146-2.864h.198c1.436.013 4.685.323 7.146 2.864zm-7.244 4.328c-.284 0-.613.016-.962.05-.123.447-.305.85-.57 1.108-1.05 1.023-2.316 1.18-2.994 1.18-.638 0-1.306-.13-1.851-.464-.516.165-1.012.403-1.044.996a65.882 65.882 0 00-.063 2.884l-.002.48c-.002.563-.005 1.126-.013 1.69.002.326.204.63.51.765 2.482 1.102 4.83 1.657 6.99 1.657 2.156 0 4.504-.555 6.985-1.657a.854.854 0 00.51-.766c.03-1.682.006-3.372-.076-5.053-.031-.596-.528-.83-1.046-.996-.546.333-1.212.464-1.85.464-.677 0-1.942-.157-2.993-1.18-.266-.258-.447-.661-.57-1.108-.32-.032-.64-.049-.96-.05zm-2.525 4.013c.539 0 .976.426.976.95v1.753c0 .525-.437.95-.976.95a.964.964 0 01-.976-.95v-1.752c0-.525.437-.951.976-.951zm5 0c.539 0 .976.426.976.95v1.753c0 .525-.437.95-.976.95a.964.964 0 01-.976-.95v-1.752c0-.525.437-.951.976-.951zM7.635 5.087c-1.05.102-1.935.438-2.385.906-.975 1.037-.765 3.668-.21 4.224.405.394 1.17.657 1.995.657h.09c.649-.013 1.785-.176 2.73-1.11.435-.41.705-1.433.675-2.47-.03-.834-.27-1.52-.63-1.813-.39-.336-1.275-.482-2.265-.394zm6.465.394c-.36.292-.6.98-.63 1.813-.03 1.037.24 2.06.675 2.47.968.957 2.136 1.104 2.776 1.11h.044c.825 0 1.59-.263 1.995-.657.555-.556.765-3.187-.21-4.224-.45-.468-1.335-.804-2.385-.906-.99-.088-1.875.058-2.265.394zM12 7.615c-.24 0-.525.015-.84.044.03.16.045.336.06.526l-.001.159a2.94 2.94 0 01-.014.25c.225-.022.425-.027.612-.028h.366c.187 0 .387.006.612.028-.015-.146-.015-.277-.015-.409.015-.19.03-.365.06-.526a9.29 9.29 0 00-.84-.044z'],
  zai: ['M12.105 2L9.927 4.953H.653L2.83 2h9.276zM23.254 19.048L21.078 22h-9.242l2.174-2.952h9.244zM24 2L9.264 22H0L14.736 2H24z'],
  kimi: ['M21.846 0a1.923 1.923 0 110 3.846H20.15a.226.226 0 01-.227-.226V1.923C19.923.861 20.784 0 21.846 0z', 'M11.065 11.199l7.257-7.2c.137-.136.06-.41-.116-.41H14.3a.164.164 0 00-.117.051l-7.82 7.756c-.122.12-.302.013-.302-.179V3.82c0-.127-.083-.23-.185-.23H3.186c-.103 0-.186.103-.186.23V19.77c0 .128.083.23.186.23h2.69c.103 0 .186-.102.186-.23v-3.25c0-.069.025-.135.069-.178l2.424-2.406a.158.158 0 01.205-.023l6.484 4.772a7.677 7.677 0 003.453 1.283c.108.012.2-.095.2-.23v-3.06c0-.117-.07-.212-.164-.227a5.028 5.028 0 01-2.027-.807l-5.613-4.064c-.117-.078-.132-.279-.028-.381z'],
  minimax: ['M16.278 2c1.156 0 2.093.927 2.093 2.07v12.501a.74.74 0 00.744.709.74.74 0 00.743-.709V9.099a2.06 2.06 0 012.071-2.049A2.06 2.06 0 0124 9.1v6.561a.649.649 0 01-.652.645.649.649 0 01-.653-.645V9.1a.762.762 0 00-.766-.758.762.762 0 00-.766.758v7.472a2.037 2.037 0 01-2.048 2.026 2.037 2.037 0 01-2.048-2.026v-12.5a.785.785 0 00-.788-.753.785.785 0 00-.789.752l-.001 15.904A2.037 2.037 0 0113.441 22a2.037 2.037 0 01-2.048-2.026V18.04c0-.356.292-.645.652-.645.36 0 .652.289.652.645v1.934c0 .263.142.506.372.638.23.131.514.131.744 0a.734.734 0 00.372-.638V4.07c0-1.143.937-2.07 2.093-2.07zm-5.674 0c1.156 0 2.093.927 2.093 2.07v11.523a.648.648 0 01-.652.645.648.648 0 01-.652-.645V4.07a.785.785 0 00-.789-.78.785.785 0 00-.789.78v14.013a2.06 2.06 0 01-2.07 2.048 2.06 2.06 0 01-2.071-2.048V9.1a.762.762 0 00-.766-.758.762.762 0 00-.766.758v3.8a2.06 2.06 0 01-2.071 2.049A2.06 2.06 0 010 12.9v-1.378c0-.357.292-.646.652-.646.36 0 .653.29.653.646V12.9c0 .418.343.757.766.757s.766-.339.766-.757V9.099a2.06 2.06 0 012.07-2.048 2.06 2.06 0 012.071 2.048v8.984c0 .419.343.758.767.758.423 0 .766-.339.766-.758V4.07c0-1.143.937-2.07 2.093-2.07z'],
}

const lobeMark = id => () =>
  svg('0 0 24 24', LOBE_PATHS[id].map((d, i) => jsx('path', { fill: 'currentColor', fillRule: 'evenodd', d }, i)))

// Anthropic, xAI and the LOBE_PATHS glyphs: @lobehub/icons (MIT). Command Code: the glyph of its
// official symbol (commandcode.ai/brand), cropped to the glyph so every mark sits on the same tile.
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
  commandcode: () => svg('24 24 88 88', jsx('path', { fill: 'currentColor', d: CC_GLYPH })),
  // Nous Research's mark is a detailed illustration that doesn't survive 13 px; a letter reads better.
  nous: () => svg('0 0 24 24', jsx('text', { x: 12, y: 18, textAnchor: 'middle', fontSize: 17, fontWeight: 700, fill: 'currentColor', children: 'N' })),
  ...Object.fromEntries(Object.keys(LOBE_PATHS).map(id => [id, lobeMark(id)]))
}

// Display order. The backend decides which cards are live; the rest go under Hidden/Inactive.
const PROVIDERS = [
  { id: 'anthropic', name: 'Anthropic', tile: 'var(--ui-orange)' },
  { id: 'codex', name: 'ChatGPT / Codex', tile: 'var(--ui-text-primary)' },
  { id: 'commandcode', name: 'Command Code', tile: 'var(--qg-cc)' },
  { id: 'xai', name: 'xAI', tile: 'var(--ui-text-primary)' },
  { id: 'antigravity', name: 'Antigravity', tile: 'var(--ui-blue)' },
  { id: 'opencode', name: 'OpenCode Go', tile: 'var(--ui-text-primary)' },
  { id: 'devin', name: 'Devin', tile: 'var(--ui-cyan)' },
  { id: 'copilot', name: 'GitHub Copilot', tile: 'var(--ui-purple)' },
  { id: 'nous', name: 'Nous Portal', tile: 'var(--ui-text-primary)' },
  { id: 'zai', name: 'Z.ai GLM', tile: 'var(--ui-blue)' },
  { id: 'kimi', name: 'Kimi Code', tile: 'var(--ui-text-primary)' },
  { id: 'minimax', name: 'MiniMax', tile: 'var(--ui-red)' }
]

const HIDDEN_STATUSES = new Set(['inactive', 'no_subscription'])
const isHidden = data => Boolean(data?.hidden) || HIDDEN_STATUSES.has(data?.status)

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

function Logo({ id, small = false }) {
  const meta = PROVIDERS.find(p => p.id === id)
  const Mark = MARKS[id]
  return jsx('span', {
    className: small ? 'qg-logo qg-logo-sm' : 'qg-logo',
    style: { color: meta.tile, background: `color-mix(in srgb, ${meta.tile} 13%, transparent)` },
    children: Mark ? Mark() : meta.name.slice(0, 1)
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

function ProviderCard({ meta, data, totals, since, now, noLogo = false }) {
  const ok = data?.status === 'ok'
  const windows = ok ? data.windows || [] : []
  return jsxs('section', {
    className: 'qg-card',
    children: [
      jsxs('div', {
        className: 'qg-card-head',
        children: [
          noLogo ? jsx('span', { className: 'qg-logo qg-logo-usage', children: jsx(icons.Activity, { size: 14 }) }) : jsx(Logo, { id: meta.id }),
          jsx('span', { className: 'qg-name', children: meta.name }),
          data?.plan ? jsx('span', { className: 'qg-badge', children: data.plan }) : null,
          ok && data.warning ? jsx(Tip, { label: data.warning.detail || data.warning.label, children: jsx('span', { className: `qg-flag qg-flag-${data.warning.level || 'warn'}`, children: data.warning.label }) }) : null,
          data?.stale
            ? jsx(Tip, { label: data.error || 'Showing the last good reading', children: jsx('span', { className: 'qg-stale', children: jsx(icons.AlertTriangle, { size: 12 }) }) })
            : null,
          jsx('span', { className: 'qg-spacer' }),
          noLogo ? null : jsx(TokenTotal, { totals, since: totals?.first_at || since })
        ]
      }),
      windows.length
        ? windows.map(w => jsx(QuotaRow, { w, now }, w.id))
        : jsxs('div', {
            className: 'qg-msg',
            children: [
              data?.status === 'loading' ? jsx('span', { className: 'qg-spin', children: jsx(icons.RefreshCw, { size: 12 }) }) : jsx(icons.Info, { size: 13, style: { flex: 'none', marginTop: 1 } }),
              jsx('span', { children: richText(data?.message || 'Loading…') })
            ]
          }),
      ok && data.details?.length ? jsx('div', { className: 'qg-detail', children: data.details.join(' · ') }) : null
    ]
  })
}

// Hidden / inactive -----------------------------------------------------------------------------

function hiddenTag(data) {
  if (data?.status === 'no_subscription') return 'No plan'
  return data?.signed_in ? 'Inactive' : 'Not signed in'
}

function HiddenRow({ meta, data, totals }) {
  const total = totals?.total || 0
  return jsxs('div', {
    className: 'qg-hrow',
    children: [
      jsx(Logo, { id: meta.id, small: true }),
      jsxs('div', {
        className: 'qg-hbody',
        children: [
          jsxs('div', {
            className: 'qg-hline',
            children: [
              jsx('span', { className: 'qg-hname', children: meta.name }),
              jsx('span', { className: 'qg-htag', children: hiddenTag(data) }),
              jsx('span', { className: 'qg-spacer' }),
              total > 0
                ? jsx(Tip, { label: `${exact(total)} tokens through Hermes`, children: jsx('span', { className: 'qg-htokens', tabIndex: 0, children: abbreviate(total) }) })
                : null
            ]
          }),
          data?.message ? jsx('div', { className: 'qg-hmsg', children: richText(data.message) }) : null
        ]
      })
    ]
  })
}

function HiddenSection({ items, providers, totals }) {
  const [open, setOpen] = useState(() => Boolean(storage?.get('hiddenOpen', false)))
  if (!items.length) return null
  const toggle = () => {
    setOpen(!open)
    storage?.set('hiddenOpen', !open)
  }
  return jsxs('section', {
    className: `qg-hidden${open ? ' open' : ''}`,
    children: [
      jsxs('button', {
        type: 'button',
        className: 'qg-htoggle',
        onClick: toggle,
        'aria-expanded': open,
        'aria-controls': 'qg-hidden-list',
        children: [
          jsx(icons.EyeOff, { size: 13 }),
          jsx('span', { children: 'Hidden/Inactive' }),
          jsx('span', { className: 'qg-hcount', children: items.length }),
          jsx('span', { className: 'qg-spacer' }),
          jsx(icons.ChevronDown, { size: 14, className: 'qg-chev' })
        ]
      }),
      open
        ? jsx('div', {
            id: 'qg-hidden-list',
            className: 'qg-hlist',
            children: items.map(meta => jsx(HiddenRow, { meta, data: providers[meta.id], totals: totals[meta.id] }, meta.id))
          })
        : null
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
  const known = PROVIDERS.filter(meta => providers[meta.id])
  const live = known.filter(meta => !isHidden(providers[meta.id]))
  const hidden = known.filter(meta => isHidden(providers[meta.id]))

  let cards
  if (missing) cards = [jsx(SetupCard, {}, 'setup')]
  else if (!quota.data) {
    const pending = quota.isLoading || !quota.error
    cards = [jsx(ProviderCard, {
      meta: { id: 'checking', name: 'Subscriptions' },
      data: pending ? { status: 'loading', message: 'Checking which subscriptions this profile is signed in to…' } : { status: 'error', message: String(quota.error.message || quota.error) },
      now: nowMs,
      noLogo: true
    }, 'checking')]
  } else if (!live.length) {
    cards = [jsx(EmptyLiveCard, { count: hidden.length }, 'none')]
  } else {
    cards = live.map(meta => jsx(ProviderCard, { meta, data: providers[meta.id], totals: totals[meta.id], since, now: nowMs }, meta.id))
  }

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
      ...cards,
      missing || !quota.data ? null : jsx(HiddenSection, { items: hidden, providers, totals }),
      missing ? null : jsx(UsageCard, { usage: usage.data, nowMs, loading: usage.isLoading })
    ]
  })
}

function EmptyLiveCard({ count }) {
  return jsxs('section', {
    className: 'qg-card',
    children: [
      jsxs('div', {
        className: 'qg-msg',
        children: [
          jsx(icons.Info, { size: 13, style: { flex: 'none', marginTop: 1 } }),
          jsx('span', { children: count ? 'No active subscriptions found in this profile. Sign in to a provider in Hermes and it shows up here.' : 'No providers to show.' })
        ]
      })
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
.qg-msg code{white-space:nowrap;font-size:10.5px;padding:0 4px;border-radius:4px;background:var(--ui-inline-code-background);color:var(--ui-inline-code-foreground)}
.qg-detail{color:var(--ui-text-tertiary);font-size:10.5px;margin-top:-3px}

.qg-logo-sm{width:18px;height:18px;border-radius:6px;opacity:.85}
.qg-logo-sm svg{width:11px;height:11px}
.qg-hidden{display:flex;flex-direction:column;border-radius:12px;border:1px dashed var(--ui-stroke-tertiary)}
.qg-hidden.open{border-style:solid;background:color-mix(in srgb,var(--ui-text-primary) 1.5%,transparent)}
.qg-htoggle{display:flex;align-items:center;gap:7px;width:100%;padding:8px 12px;border:0;border-radius:12px;background:transparent;
  color:var(--ui-text-tertiary);font:inherit;font-size:11.5px;font-weight:600;cursor:pointer;text-align:left}
.qg-htoggle:hover{color:var(--ui-text-primary);background:var(--ui-control-hover-background)}
.qg-htoggle:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.qg-hcount{font-size:10px;font-weight:650;padding:0 6px;border-radius:999px;line-height:16px;
  color:var(--ui-text-secondary);background:color-mix(in srgb,var(--ui-text-primary) 8%,transparent)}
.qg-chev{transition:transform .18s ease}
.qg-hidden.open .qg-chev{transform:rotate(180deg)}
.qg-hlist{display:flex;flex-direction:column;gap:9px;padding:2px 12px 11px}
.qg-hrow{display:flex;align-items:flex-start;gap:8px;min-width:0}
.qg-hbody{display:flex;flex-direction:column;gap:2px;min-width:0;flex:1 1 auto}
.qg-hline{display:flex;align-items:center;gap:6px;min-width:0;line-height:18px}
.qg-hname{font-weight:600;font-size:11.5px;color:var(--ui-text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qg-htag{flex:none;font-size:9.5px;font-weight:600;color:var(--ui-text-tertiary)}
.qg-htokens{flex:none;font-size:10.5px;font-weight:600;color:var(--ui-text-tertiary);padding:0 4px;border-radius:5px;outline:none;cursor:default}
.qg-htokens:hover,.qg-htokens:focus-visible{background:var(--ui-control-hover-background)}
.qg-hmsg{font-size:10.5px;line-height:1.4;color:var(--ui-text-tertiary)}
.qg-hmsg code{white-space:nowrap;font-size:10px;padding:0 3px;border-radius:4px;background:var(--ui-inline-code-background);color:var(--ui-inline-code-foreground)}

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
