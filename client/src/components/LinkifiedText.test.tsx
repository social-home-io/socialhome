import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { ExternalLink, LinkifiedText } from './LinkifiedText'

describe('LinkifiedText', () => {
  it('keeps plain text and links web links safely', () => {
    const { container } = render(
      <LinkifiedText text="Agenda at https://www.home-assistant.io/blog/ and www.example.com." />,
    )
    expect(container.textContent).toContain('Agenda at ')
    const links = [...container.querySelectorAll('a')]
    expect(links.map(a => a.getAttribute('href'))).toEqual([
      'https://www.home-assistant.io/blog/', 'https://www.example.com',
    ])
    for (const a of links) {
      expect(a.getAttribute('rel')).toBe('noopener noreferrer nofollow ugc')
      expect(a.getAttribute('target')).toBe('_blank')
    }
  })

  it('passes plain parts to renderText, never link text', () => {
    const seen: string[] = []
    render(
      <LinkifiedText text="a https://x.example/#b c"
        renderText={(p) => { seen.push(p); return p }} />,
    )
    expect(seen).toEqual(['a ', ' c'])
  })

  it('a long link is shortened, full URL in title, hidden new-tab text', () => {
    const url = 'https://www.home-assistant.io/integrations/mqtt/?utm_source=newsletter'
    const { container } = render(<ExternalLink link={{ href: url, text: url }} />)
    const a = container.querySelector('a')!
    expect(a.getAttribute('href')).toBe(url)
    expect(a.getAttribute('title')).toBe(url)
    const shown = a.cloneNode(true) as Element
    shown.querySelectorAll('.sh-ext-link__icon, .sr-only').forEach(n => n.remove())
    expect(shown.textContent).toBe('www.home-assistant.io/integrations/mqtt/…')
    // The last character and the arrow never part.
    expect(a.querySelector('.sh-ext-link__tail')!.textContent).toBe('…\u2060↗\ufe0e')
    expect(a.querySelector('.sh-ext-link__icon')!.getAttribute('aria-hidden')).toBe('true')
    expect(a.querySelector('.sr-only')!.textContent).toBe('(opens in a new tab)')
  })

  it('renders no link for unsafe schemes or userinfo', () => {
    const { container } = render(
      <LinkifiedText text="javascript:alert(1) https://good.example@evil.example/" />,
    )
    expect(container.querySelector('a')).toBeNull()
  })
})
