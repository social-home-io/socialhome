import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

const route = vi.fn()
vi.mock('preact-iso', () => ({ useLocation: () => ({ route }) }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
vi.mock('@/api', () => ({ api: { get: vi.fn() } }))

import { useSpaceConfigWs, SPACE_DISSOLVED_TOAST } from './useSpaceConfigWs'
import { markLocalDissolve, clearLocalDissolve } from '@/store/spaces'

function Probe({ spaceId, onChanged }: { spaceId: string; onChanged: () => void }) {
  useSpaceConfigWs(spaceId, onChanged)
  return null
}

beforeEach(() => {
  handlers.clear()
  route.mockReset()
  showToast.mockReset()
  clearLocalDissolve('sp-1')
})

describe('useSpaceConfigWs', () => {
  it('a config change of the open space calls onChanged', () => {
    const onChanged = vi.fn()
    render(<Probe spaceId="sp-1" onChanged={onChanged} />)
    // Host path (SpaceConfigEventType) and remote-host path
    // (federation event type) both land here.
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'rename', sequence: 3 })
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'space_config_changed' })
    expect(onChanged).toHaveBeenCalledTimes(2)
    expect(route).not.toHaveBeenCalled()
  })

  it('ignores frames for other spaces', () => {
    const onChanged = vi.fn()
    render(<Probe spaceId="sp-1" onChanged={onChanged} />)
    emit('space.config.changed', { space_id: 'sp-2', event_type: 'rename' })
    emit('space.config.changed', { space_id: 'sp-2', event_type: 'dissolved' })
    expect(onChanged).not.toHaveBeenCalled()
    expect(route).not.toHaveBeenCalled()
  })

  it('a dissolve (host or remote) leaves for the spaces list with a toast', () => {
    const onChanged = vi.fn()
    render(<Probe spaceId="sp-1" onChanged={onChanged} />)
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'dissolved' })
    expect(showToast).toHaveBeenCalledWith(SPACE_DISSOLVED_TOAST, 'info')
    expect(route).toHaveBeenCalledWith('/spaces', true)
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('leaves the redirect to this tab when it is the one dissolving', () => {
    markLocalDissolve('sp-1')
    render(<Probe spaceId="sp-1" onChanged={vi.fn()} />)
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'dissolved' })
    expect(showToast).not.toHaveBeenCalled()
    expect(route).not.toHaveBeenCalled()
  })

  it('unsubscribes on unmount', () => {
    const onChanged = vi.fn()
    const view = render(<Probe spaceId="sp-1" onChanged={onChanged} />)
    view.unmount()
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'rename' })
    expect(onChanged).not.toHaveBeenCalled()
  })
})
