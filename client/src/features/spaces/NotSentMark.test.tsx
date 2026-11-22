import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { NotSentMark } from './NotSentMark'

const mark = (c: Element) => c.querySelector('[data-testid="post-not-sent"]') as HTMLButtonElement

describe('NotSentMark', () => {
  it('"○ Not sent yet", the DM "Sending" glyph, opening to the reason', () => {
    const { container } = render(<NotSentMark hostName="The Müllers" hostedHere={false} />)
    const b = mark(container)
    expect(b.textContent).toBe('○Not sent yet')
    expect(b.querySelector('.sh-receipt--pending')?.getAttribute('aria-hidden')).toBe('true')
    expect(b.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(b)
    expect(b.textContent).toBe('○Not sent yet — goes out once The Müllers can be reached')
    expect(b.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(b)
    expect(b.textContent).toBe('○Not sent yet')
  })

  it('names "the host" when the name is not known', () => {
    const { container } = render(<NotSentMark hostName={null} hostedHere={false} />)
    fireEvent.click(mark(container))
    expect(mark(container).textContent).toBe('○Not sent yet — goes out once the host can be reached')
  })

  it('on the host household it waits for the others', () => {
    const { container } = render(<NotSentMark hostName={null} hostedHere />)
    fireEvent.click(mark(container))
    expect(mark(container).textContent).toBe('○Not sent yet — goes out when the others are back')
  })
})
