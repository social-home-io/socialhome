/**
 * SkipLink tests.
 *
 * The document always carries a ``<base href>`` (``/`` standalone,
 * ``/api/hassio_ingress/<token>/`` under HA ingress), so a bare
 * ``href="#main"`` resolves to ``<base>#main`` — the app root — and
 * following it navigated to Home instead of jumping to the page
 * content. The link must move focus to ``<main id="main">`` in place.
 */
import { render, cleanup } from '@testing-library/preact'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'

import { SkipLink } from './SkipLink'

function Page() {
  return (
    <>
      <SkipLink />
      <nav><a href="/feed">Feed</a></nav>
      <main id="main" tabIndex={-1}>content</main>
    </>
  )
}

function activate(link: HTMLElement): boolean {
  // ``dispatchEvent`` returns false when a listener called
  // ``preventDefault`` — i.e. the browser would NOT have followed the
  // href to ``<base>#main``.
  return link.dispatchEvent(
    new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }),
  )
}

describe('SkipLink under the HA ingress prefix (/api/hassio_ingress/<token>/)', () => {
  let baseEl: HTMLBaseElement
  const before = location.pathname
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    history.replaceState(null, '', '/api/hassio_ingress/tok/spaces/abc')
  })
  afterEach(() => {
    cleanup()
    baseEl.remove()
    history.replaceState(null, '', before)
  })

  it('focuses main content without navigating to the ingress root', () => {
    const { getByText, container } = render(<Page />)
    const link = getByText('Skip to main content')
    // The bug: the raw href resolves to the app root under the prefix.
    expect((link as HTMLAnchorElement).href).toBe(
      `${location.origin}/api/hassio_ingress/tok/#main`,
    )
    const followed = activate(link)
    expect(followed).toBe(false)
    expect(document.activeElement).toBe(container.querySelector('#main'))
    expect(location.pathname).toBe('/api/hassio_ingress/tok/spaces/abc')
  })
})

describe('SkipLink (no prefix, standalone base /)', () => {
  const before = location.pathname
  beforeEach(() => {
    history.replaceState(null, '', '/spaces/abc')
  })
  afterEach(() => {
    cleanup()
    history.replaceState(null, '', before)
  })

  it('focuses main content and stays on the current page', () => {
    const { getByText, container } = render(<Page />)
    const followed = activate(getByText('Skip to main content'))
    expect(followed).toBe(false)
    expect(document.activeElement).toBe(container.querySelector('#main'))
    expect(location.pathname).toBe('/spaces/abc')
  })

  it('keeps an href so it stays a real, keyboard-focusable link', () => {
    const { getByText } = render(<Page />)
    const link = getByText('Skip to main content')
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('#main')
    expect(link.className).toContain('sh-skip-link')
  })

  it('does nothing harmful when the main landmark is missing', () => {
    const { getByText } = render(<SkipLink />)
    expect(() => activate(getByText('Skip to main content'))).not.toThrow()
  })
})
