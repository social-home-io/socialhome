import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/preact'

const send = vi.fn()
vi.mock('@/ws', () => ({ ws: { send: (...a: unknown[]) => send(...a) } }))

import { activeViewOwner, useActiveView } from './useActiveView'

function View({ k, onClaim }: { k: string | null; onClaim?: () => void }) {
  useActiveView(k, onClaim)
  return null
}

const claims = () => send.mock.calls.filter(c => c[0] === 'dm.active').map(c => c[1])

let visibility: ReturnType<typeof vi.spyOn> | null = null
function setVisibility(state: DocumentVisibilityState) {
  visibility ??= vi.spyOn(document, 'visibilityState', 'get')
  visibility.mockReturnValue(state)
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
}

beforeEach(() => send.mockClear())
afterEach(() => {
  // Inside act: Preact runs an unmounted view's effect cleanup on the
  // next flush, which would otherwise land in the next test.
  act(() => { cleanup() })
  visibility?.mockRestore()
  visibility = null
})

describe('useActiveView', () => {
  it('claims its key on mount and lets go on unmount', () => {
    const r = render(<View k="conv-a" />)
    expect(claims()).toEqual([{ conversation_id: 'conv-a' }])
    act(() => { r.unmount() })
    expect(claims()).toEqual([{ conversation_id: 'conv-a' }, { conversation_id: null }])
    expect(activeViewOwner()).toBeNull()
  })

  it('a null key claims nothing', () => {
    render(<View k={null} />)
    expect(claims()).toEqual([])
  })

  it('an app and a thread on screen together never fight over the claim', () => {
    // A DM thread opens, then an app on top of it: the app holds the claim.
    const thread = render(<View k="conv-a" />)
    const app = render(<View k="app:chess" />)
    expect(claims().at(-1)).toEqual({ conversation_id: 'app:chess' })
    send.mockClear()
    // The thread closing must not clear the app's claim …
    act(() => { thread.unmount() })
    expect(claims()).toEqual([])
    expect(activeViewOwner()).not.toBeNull()
    // … and the app closing does.
    act(() => { app.unmount() })
    expect(claims()).toEqual([{ conversation_id: null }])
  })

  it('the other way round: the app closing leaves the newer thread claimed', () => {
    const app = render(<View k="app:chess" />)
    const thread = render(<View k="conv-a" />)
    send.mockClear()
    act(() => { app.unmount() })
    expect(claims()).toEqual([])
    act(() => { thread.unmount() })
    expect(claims()).toEqual([{ conversation_id: null }])
  })

  it('a tab in the background lets go, and claims again when back in front', () => {
    const onClaim = vi.fn()
    render(<View k="app:chess" onClaim={onClaim} />)
    expect(onClaim).toHaveBeenCalledTimes(1)
    setVisibility('hidden')
    expect(claims().at(-1)).toEqual({ conversation_id: null })
    setVisibility('visible')
    expect(claims().at(-1)).toEqual({ conversation_id: 'app:chess' })
    expect(onClaim).toHaveBeenCalledTimes(2)
  })

  it('a view mounted in a background tab claims nothing until it is shown', () => {
    visibility = vi.spyOn(document, 'visibilityState', 'get')
    visibility.mockReturnValue('hidden')
    render(<View k="conv-a" />)
    expect(claims()).toEqual([])
    setVisibility('visible')
    expect(claims()).toEqual([{ conversation_id: 'conv-a' }])
  })
})
