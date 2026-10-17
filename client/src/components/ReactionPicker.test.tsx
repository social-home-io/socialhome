import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { createRef } from 'preact'
import { ReactionPicker } from './ReactionPicker'

describe('ReactionPicker', () => {
  it('puts its ref on the picker root', () => {
    const ref = createRef<HTMLDivElement>()
    const { container } = render(<ReactionPicker ref={ref} onSelect={() => {}} onClose={() => {}} />)
    expect(ref.current).toBe(container.querySelector('.sh-reaction-picker'))
  })

  it('renders frequent emoji', () => {
    const { container } = render(<ReactionPicker onSelect={() => {}} onClose={() => {}} />)
    expect(container.textContent).toContain('👍')
    expect(container.textContent).toContain('❤️')
  })

  it('calls onSelect when emoji clicked', () => {
    const fn = vi.fn()
    const { container } = render(<ReactionPicker onSelect={fn} onClose={() => {}} />)
    const buttons = container.querySelectorAll('.sh-emoji-btn')
    if (buttons.length > 0) fireEvent.click(buttons[0])
    expect(fn).toHaveBeenCalled()
  })

  it('has search input', () => {
    const { container } = render(<ReactionPicker onSelect={() => {}} onClose={() => {}} />)
    expect(container.querySelector('.sh-reaction-search')).toBeTruthy()
  })

  it('calls onClose when close clicked', () => {
    const fn = vi.fn()
    const { container } = render(<ReactionPicker onSelect={() => {}} onClose={fn} />)
    const close = container.querySelector('.sh-reaction-close')
    if (close) fireEvent.click(close)
    expect(fn).toHaveBeenCalledOnce()
  })
})
