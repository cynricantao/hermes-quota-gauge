
import { QueryClient, QueryClientProvider, useQuery as rqUseQuery } from '@tanstack/react-query'
import { IconLogin, IconSend, IconBolt, IconDeviceFloppy, IconRefresh, IconInfoCircle, IconAlertTriangle, IconChartBar, IconPlug, IconActivity } from '@tabler/icons-react'
import { jsx } from 'react/jsx-runtime'
export const queryClient = new QueryClient()
export const useQuery = opts => rqUseQuery(opts, queryClient)
export { QueryClientProvider }
export const icons = { LogIn: IconLogin, Send: IconSend, Zap: IconBolt, Save: IconDeviceFloppy, RefreshCw: IconRefresh,
  Info: IconInfoCircle, AlertTriangle: IconAlertTriangle, BarChart3: IconChartBar, Plug: IconPlug, Activity: IconActivity }
export function Tip({ children }) { return children }
const cn = (...a) => a.filter(Boolean).join(' ')
// Same markup/classes as apps/desktop/src/components/ui/segmented-control.tsx
export function SegmentedControl({ className, onChange, options, sizing = 'equal', value }) {
  return jsx('div', {
    className: cn('inline-grid w-fit grid-flow-col gap-0.5 rounded-[5px] bg-(--ui-bg-tertiary) p-0.5', sizing === 'equal' ? 'auto-cols-fr' : 'auto-cols-max', className),
    children: options.map(({ id, label }) => jsx('button', {
      'aria-pressed': value === id, type: 'button', onClick: () => onChange(id),
      className: cn('flex items-center justify-center gap-1 rounded-[3px] px-2.5 py-0.5 text-[0.6875rem] font-medium transition-colors hover:transition-none disabled:cursor-default',
        value === id ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'),
      children: label }, id))
  })
}

export const PALETTE_AREA = 'palette'
export const host = { revealPane: id => { window.__revealed = id } }
