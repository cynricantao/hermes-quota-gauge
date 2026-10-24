// Renders desktop/plugin.js outside the app for screenshots. Data: a fixture file, never a live profile.
import { createRoot } from 'react-dom/client'
import plugin from '../../desktop/plugin.js'
import fixture from './fixture.json'

const params = new URLSearchParams(location.search)
if (params.get('dark') === '1') document.documentElement.classList.add('dark')
if (fixture.now) {
  // Freeze the clock at the fixture's moment so countdowns and "Today" line up.
  const offset = fixture.now * 1000 - Date.now()
  const RealDate = Date
  globalThis.Date = class extends RealDate {
    constructor(...a) { super(...(a.length ? a : [RealDate.now() + offset])) }
    static now() { return RealDate.now() + offset }
  }
}
const regs = []
const ctx = {
  register: c => { regs.push(c); return () => {} },
  rest: async path => {
    if (path.startsWith('/quota')) return fixture.quota
    if (path.startsWith('/usage')) return fixture.usage
    throw new Error('HTTP 404 Not Found')
  },
  storage: { get: (k, d) => (k === 'range' && params.get('range')) || (k === 'hiddenOpen' && params.get('hidden') === '1') || d, set() {}, remove() {} },
  onDispose() {},
  setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); return () => clearTimeout(t) }
}
plugin.register(ctx)
createRoot(document.getElementById('root')).render(regs.find(r => r.area === 'panes').render())
const hover = params.get('hover')
if (hover) setTimeout(() => {
  const el = document.querySelectorAll('.qg-col')[Number(hover)]
  el?.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: document.body }))
}, 600)
