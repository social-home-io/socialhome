import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, screen, cleanup } from '@testing-library/preact'
import { SwitchRow } from './DashboardSwitchRow'

afterEach(cleanup)

function mount(checked = false, disabled = false) {
  const onChange = vi.fn()
  const { container } = render(
    <SwitchRow id="sw" label="Keep the screen on" hint="Stops the tablet dimming."
      checked={checked} disabled={disabled} onChange={onChange} />,
  )
  return { onChange, container }
}

describe('SwitchRow', () => {
  it('toggles when the switch graphic itself is clicked', () => {
    const { onChange, container } = mount()
    fireEvent.click(container.querySelector('input.sh-switch')!)
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('toggles off from the graphic when on', () => {
    const { onChange, container } = mount(true)
    fireEvent.click(container.querySelector('input.sh-switch')!)
    expect(onChange).toHaveBeenCalledWith(false)
  })

  it('does not toggle from the graphic while disabled', () => {
    const { onChange, container } = mount(false, true)
    fireEvent.click(container.querySelector('input.sh-switch')!)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('is named by its text label only, with the hint as its description', () => {
    mount()
    const sw = screen.getByRole('switch', { name: 'Keep the screen on' })
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)?.textContent)
      .toBe('Stops the tablet dimming.')
  })

  it('toggles when the text label is clicked', () => {
    const { onChange } = mount()
    fireEvent.click(screen.getByText('Keep the screen on'))
    expect(onChange).toHaveBeenCalledWith(true)
  })
})
