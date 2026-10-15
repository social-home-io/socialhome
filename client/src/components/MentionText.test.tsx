import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { MentionText } from './MentionText'

describe('MentionText', () => {
  it('highlights only known tokens and marks the viewer', () => {
    const { container } = render(
      <MentionText
        text="hi @anna and @me, ping @nobody or bob@example.com"
        mentions={new Set(['anna', 'me'])}
        selfMention="Me"
      />,
    )
    const spans = [...container.querySelectorAll('.sh-mention')]
    expect(spans.map(s => [s.textContent, s.className])).toEqual([
      ['@anna', 'sh-mention'],
      ['@me', 'sh-mention sh-mention--self'],
    ])
    expect(container.textContent).toBe(
      'hi @anna and @me, ping @nobody or bob@example.com',
    )
  })

  it('never turns user text into markup', () => {
    const { container } = render(
      <MentionText text={'<img src=x onerror=alert(1)> @anna'} mentions={new Set(['anna'])} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('with onMention, a person mention is a keyboard-operable button', () => {
    const onMention = vi.fn()
    const bubbleTap = vi.fn()
    const { container, getByRole } = render(
      <p onClick={bubbleTap}>
        <MentionText text="ask @Anna or @here" mentions={new Set(['anna', 'here'])}
          onMention={onMention} />
      </p>,
    )
    const btn = getByRole('button', { name: '@Anna' })
    expect(btn.getAttribute('type')).toBe('button')
    expect(btn.getAttribute('data-mention')).toBe('anna')
    // A native button: Enter / Space activate it as a click; focusable.
    btn.focus()
    expect(document.activeElement).toBe(btn)
    fireEvent.click(btn)
    expect(onMention).toHaveBeenCalledWith('anna')
    expect(bubbleTap).not.toHaveBeenCalled()
    // @here is never a person.
    const here = container.querySelector('.sh-mention--here')!
    expect(here.tagName).toBe('SPAN')
    expect(container.querySelectorAll('button')).toHaveLength(1)
  })

  it('without onMention, mentions stay plain spans', () => {
    const { container } = render(<MentionText text="@anna" mentions={new Set(['anna'])} />)
    expect(container.querySelector('button')).toBeNull()
  })
})
