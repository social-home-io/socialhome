import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { LengthCounter } from './LengthCounter'

describe('LengthCounter', () => {
  it('stays hidden while the text is far from its cap', () => {
    const { container } = render(<LengthCounter value={'x'.repeat(96)} max={120} />)
    expect(container.textContent).toBe('')
  })

  it('counts once the text nears its cap', () => {
    const { container } = render(<LengthCounter value={'x'.repeat(97)} max={120} />)
    const el = container.querySelector('.sh-length-counter') as HTMLElement
    expect(el.textContent).toBe('97/120')
    expect(el.getAttribute('aria-live')).toBe('polite')
    expect(el.classList.contains('sh-length-counter--full')).toBe(false)
  })

  it('marks a field that reached its cap', () => {
    const { container } = render(<LengthCounter value={'x'.repeat(120)} max={120} id="c" />)
    const el = container.querySelector('#c') as HTMLElement
    expect(el.textContent).toBe('120/120')
    expect(el.classList.contains('sh-length-counter--full')).toBe(true)
  })
})
