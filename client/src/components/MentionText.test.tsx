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

describe('MentionText — links', () => {
  it('links web links with the shared grammar; mentions keep working', () => {
    const onMention = vi.fn()
    const { container } = render(
      <MentionText
        text="@anna see https://example.org/a?b=1&c=2. and www.example.com"
        mentions={new Set(['anna'])}
        onMention={onMention}
      />,
    )
    const links = [...container.querySelectorAll('a')]
    expect(links.map(a => [a.getAttribute('href'), a.getAttribute('rel'), a.target])).toEqual([
      ['https://example.org/a?b=1&c=2', 'noopener noreferrer nofollow ugc', '_blank'],
      ['https://www.example.com', 'noopener noreferrer nofollow ugc', '_blank'],
    ])
    expect(links[0].querySelector('[aria-hidden="true"]')!.textContent).toBe('↗')
    expect(links[0].querySelector('.sr-only')!.textContent).toBe('(opens in a new tab)')
    fireEvent.click(container.querySelector('button[data-mention="anna"]')!)
    expect(onMention).toHaveBeenCalledWith('anna')
  })

  it('a mention inside a link is part of the link, not a button', () => {
    const { container } = render(
      <MentionText text="https://social.example/@anna" mentions={new Set(['anna'])}
        onMention={() => {}} />,
    )
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('a')!.getAttribute('href')).toBe('https://social.example/@anna')
  })

  it('never links javascript: or data:', () => {
    const { container } = render(
      <MentionText text="javascript:alert(1) data:text/html,x" mentions={new Set()} />,
    )
    expect(container.querySelector('a')).toBeNull()
  })

  it('a link click does not reach the bubble', () => {
    const onBubble = vi.fn()
    const { container } = render(
      <div onClick={onBubble}>
        <MentionText text="https://example.org" mentions={new Set()} />
      </div>,
    )
    container.querySelector('a')!.addEventListener('click', (e) => e.preventDefault())
    fireEvent.click(container.querySelector('a')!)
    expect(onBubble).not.toHaveBeenCalled()
  })
})
