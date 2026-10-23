// Formatting checks for desktop/plugin.js helpers. Run: node --test tests/
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

// plugin.js imports the SDK, so pull the pure helpers out by source and evaluate them alone.
const src = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
const grab = name => {
  const start = src.indexOf(`function ${name}(`)
  assert.ok(start >= 0, `${name} not found`)
  let depth = 0
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    if (src[i] === '{') depth++
    if (src[i] === '}' && --depth === 0) return src.slice(start, i + 1)
  }
  throw new Error(`unterminated ${name}`)
}
const helpers = new Function(
  `const EXACT = new Intl.NumberFormat('en-US');
   const exact = value => EXACT.format(Math.max(0, Math.round(Number(value) || 0)));
   ${grab('abbreviate')}; ${grab('pct')}; ${grab('resetText')}; ${grab('levelColor')};
   return { abbreviate, exact, pct, resetText, levelColor }`
)()

test('abbreviate', () => {
  const { abbreviate } = helpers
  assert.equal(abbreviate(0), '0')
  assert.equal(abbreviate(999), '999')
  assert.equal(abbreviate(1500), '1.5k')
  assert.equal(abbreviate(100_000), '100k')
  assert.equal(abbreviate(999_999), '1M')
  assert.equal(abbreviate(1_000_000), '1M')
  assert.equal(abbreviate(1_234_567), '1.2M')
  assert.equal(abbreviate(1_000_000_000), '1B')
  assert.equal(abbreviate(100_000_000_000), '100B')
  assert.equal(abbreviate(2.5e12), '2.5T')
})

test('exact uses grouped digits', () => {
  assert.equal(helpers.exact(100_000_000_000), '100,000,000,000')
})

test('pct and colours', () => {
  assert.equal(helpers.pct(0.4), '<1')
  assert.equal(helpers.pct(84.6), '85')
  assert.equal(helpers.levelColor(100), 'var(--ui-green)')
  assert.equal(helpers.levelColor(30), 'var(--ui-yellow)')
  assert.equal(helpers.levelColor(15), 'var(--ui-orange)')
  assert.equal(helpers.levelColor(5), 'var(--ui-red)')
})

test('resetText', () => {
  const now = 1_000_000_000_000
  assert.equal(helpers.resetText(now / 1000 + 30, now), 'resets now')
  assert.equal(helpers.resetText(now / 1000 + 45 * 60, now), 'resets in 45m')
  assert.equal(helpers.resetText(now / 1000 + 2 * 3600 + 14 * 60, now), 'resets in 2h 14m')
  assert.equal(helpers.resetText(now / 1000 + 50 * 3600, now), 'resets in 2d 2h')
  assert.equal(helpers.resetText(null, now), null)
})
