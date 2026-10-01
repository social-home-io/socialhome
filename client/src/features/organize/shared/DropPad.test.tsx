import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { DropPad } from './DropPad'

describe('DropPad', () => {
  it('renders an aria-hidden pad with the label', () => {
    const { container } = render(<DropPad label="Drop here to move into Aldi" />)
    const pad = container.firstElementChild!
    expect(pad.className).toBe('sh-drop-pad')
    expect(pad.getAttribute('aria-hidden')).toBe('true')
    expect(pad.textContent).toBe('Drop here to move into Aldi')
  })

  it('highlights while active', () => {
    const { container } = render(<DropPad label="x" active />)
    expect(container.firstElementChild!.className).toContain('sh-drop-pad--active')
  })
})
