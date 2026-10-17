import { describe, expect, it, vi } from 'vitest'
import { createRef } from 'preact'
import { assignRef } from './refs'

describe('assignRef', () => {
  it('sets an object ref', () => {
    const ref = createRef<HTMLElement>()
    const el = document.createElement('button')
    assignRef(ref, el)
    expect(ref.current).toBe(el)
    assignRef(ref, null)
    expect(ref.current).toBeNull()
  })

  it('calls a callback ref', () => {
    const ref = vi.fn()
    const el = document.createElement('button')
    assignRef(ref, el)
    expect(ref).toHaveBeenCalledWith(el)
  })

  it('ignores a missing ref', () => {
    expect(() => assignRef(undefined, document.createElement('div'))).not.toThrow()
    expect(() => assignRef(null, document.createElement('div'))).not.toThrow()
  })
})
