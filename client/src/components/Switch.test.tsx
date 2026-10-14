import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, screen, cleanup } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { Switch } from './Switch'

afterEach(cleanup)

describe('Switch', () => {
  it('is a native checkbox with role="switch" that is itself the painted graphic', () => {
    render(<Switch checked={false} aria-label="Keep the screen on" onChange={() => {}} />)
    const sw = screen.getByRole('switch', { name: 'Keep the screen on' }) as HTMLInputElement
    expect(sw.tagName).toBe('INPUT')
    expect(sw.type).toBe('checkbox')
    // The track + thumb are the input's own box (``input.sh-switch``), not a
    // sibling span a click could land on without reaching the input.
    expect(sw.classList.contains('sh-switch')).toBe(true)
    expect(sw.parentElement!.children).toHaveLength(1)
  })

  it('exposes the checked state (aria-checked from the native checkbox)', () => {
    const { rerender } = render(<Switch checked={false} aria-label="S" onChange={() => {}} />)
    expect((screen.getByRole('switch', { checked: false }) as HTMLInputElement).checked).toBe(false)
    rerender(<Switch checked aria-label="S" onChange={() => {}} />)
    expect(screen.getByRole('switch', { checked: true })).toBeTruthy()
  })

  it('toggles when the switch graphic is clicked, on and off', () => {
    const onChange = vi.fn()
    const { rerender } = render(<Switch checked={false} aria-label="S" onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(onChange).toHaveBeenLastCalledWith(true)
    rerender(<Switch checked aria-label="S" onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch'))
    expect(onChange).toHaveBeenLastCalledWith(false)
  })

  it('toggles with no <label> anywhere around it', () => {
    const onChange = vi.fn()
    const { container } = render(<div><Switch checked={false} aria-label="S" onChange={onChange} /></div>)
    expect(container.querySelector('label')).toBeNull()
    fireEvent.click(screen.getByRole('switch'))
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('toggles exactly once when clicked inside a wrapping <label>', () => {
    const onChange = vi.fn()
    render(<label><span>Sync zones</span><Switch checked={false} onChange={onChange} /></label>)
    fireEvent.click(screen.getByRole('switch', { name: 'Sync zones' }))
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('toggles when its text label is clicked (label for=)', () => {
    const onChange = vi.fn()
    render(<div><label for="sw">Keep the screen on</label><Switch id="sw" checked={false} onChange={onChange} /></div>)
    fireEvent.click(screen.getByText('Keep the screen on'))
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('toggles with Enter, without submitting a surrounding form', () => {
    const onChange = vi.fn()
    const onSubmit = vi.fn((e: Event) => e.preventDefault())
    render(<form onSubmit={onSubmit}><Switch checked aria-label="S" onChange={onChange} /></form>)
    const sw = screen.getByRole('switch')
    const ev = fireEvent.keyDown(sw, { key: 'Enter' })
    expect(ev).toBe(false) // default (implicit submit) prevented
    expect(onChange).toHaveBeenCalledWith(false)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('leaves other keys alone (Space is the native checkbox toggle)', () => {
    const onChange = vi.fn()
    render(<Switch checked={false} aria-label="S" onChange={onChange} />)
    expect(fireEvent.keyDown(screen.getByRole('switch'), { key: ' ' })).toBe(true)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('does not toggle while disabled', () => {
    const onChange = vi.fn()
    render(<Switch checked={false} disabled aria-label="S" onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch'))
    fireEvent.keyDown(screen.getByRole('switch'), { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('keeps showing the old state until the parent accepts the new one', () => {
    render(<Switch checked={false} aria-label="S" onChange={() => {}} />)
    const sw = screen.getByRole('switch') as HTMLInputElement
    fireEvent.click(sw)
    expect(sw.checked).toBe(false)
  })

  it('follows the parent state when it does accept', () => {
    function Host() {
      const [on, setOn] = useState(false)
      return <Switch checked={on} aria-label="S" onChange={setOn} />
    }
    render(<Host />)
    const sw = screen.getByRole('switch') as HTMLInputElement
    fireEvent.click(sw)
    expect(sw.checked).toBe(true)
    fireEvent.click(sw)
    expect(sw.checked).toBe(false)
  })

  it('forwards describedby and the hint stays out of the name', () => {
    render(<div>
      <span id="h">Stops the tablet dimming.</span>
      <Switch checked={false} aria-label="Keep the screen on" aria-describedby="h" onChange={() => {}} />
    </div>)
    const sw = screen.getByRole('switch', { name: 'Keep the screen on' })
    expect(sw.getAttribute('aria-describedby')).toBe('h')
  })
})
