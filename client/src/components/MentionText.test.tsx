import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
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
})
