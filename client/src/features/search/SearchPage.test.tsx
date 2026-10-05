import { describe, it, expect, vi } from 'vitest'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue({ hits: [], counts: {} }),
  },
}))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

describe('SearchPage', () => {
  it('exports a default component', async () => {
    const mod = await import('./SearchPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('renders the five filter chips + All', async () => {
    const { render } = await import('@testing-library/preact')
    const mod = await import('./SearchPage')
    const { getByText } = render(<mod.default />)
    expect(getByText('All')).toBeTruthy()
    expect(getByText('Posts')).toBeTruthy()
    expect(getByText('People')).toBeTruthy()
    expect(getByText('Spaces')).toBeTruthy()
    expect(getByText('Pages')).toBeTruthy()
    expect(getByText('DMs')).toBeTruthy()
  })

  it('renders a hostile snippet as text, keeping only the match highlight', async () => {
    const { render, waitFor } = await import('@testing-library/preact')
    const { api } = await import('@/api')
    // FTS5 ``snippet()`` copies the indexed body verbatim — a post or
    // DM from another household can carry raw markup in it.
    vi.mocked(api.get).mockResolvedValueOnce({
      hits: [{
        scope: 'post', ref_id: 'p1', space_id: null, title: '',
        snippet: '<img src=x onerror="window.__xss=1"> <mark>hello</mark> & <b>x</b>',
      }],
      counts: { post: 1 },
    })
    window.history.replaceState(null, '', '/search?q=hello')
    const mod = await import('./SearchPage')
    const { container } = render(<mod.default />)
    const snippet = await waitFor(() => {
      const el = container.querySelector('.sh-snippet')
      expect(el).not.toBeNull()
      return el!
    })
    expect(snippet.querySelector('img')).toBeNull()
    expect(snippet.querySelector('b')).toBeNull()
    const marks = snippet.querySelectorAll('mark')
    expect(marks).toHaveLength(1)
    expect(marks[0].textContent).toBe('hello')
    expect(snippet.textContent).toBe(
      '<img src=x onerror="window.__xss=1"> hello & <b>x</b>',
    )
    window.history.replaceState(null, '', '/')
  })
})

describe('snippetParts', () => {
  const parts = async (s: string) => (await import('./SearchPage')).snippetParts(s)

  it('splits server <mark> delimiters into highlighted runs', async () => {
    expect(await parts('a <mark>b</mark> c <mark>d</mark>')).toEqual([
      { text: 'a ', mark: false },
      { text: 'b', mark: true },
      { text: ' c ', mark: false },
      { text: 'd', mark: true },
    ])
  })

  it('keeps markup inside a forged <mark> as literal text', async () => {
    // An author can type ``<mark>`` themselves; it is indistinguishable
    // from a delimiter, so it highlights — but what sits inside stays text.
    expect(await parts('<mark><img src=x onerror=alert(1)></mark>')).toEqual([
      { text: '<img src=x onerror=alert(1)>', mark: true },
    ])
  })

  it('never treats a <mark> with attributes as a delimiter', async () => {
    expect(await parts('<mark class="sh-modal" onclick=x>hi</mark>')).toEqual([
      { text: '<mark class="sh-modal" onclick=x>hi</mark>', mark: false },
    ])
  })

  it('leaves an unclosed <mark> as plain text', async () => {
    expect(await parts('before <mark>never closed')).toEqual([
      { text: 'before <mark>never closed', mark: false },
    ])
  })

  it('leaves a stray </mark> as plain text', async () => {
    expect(await parts('x </mark> y')).toEqual([
      { text: 'x </mark> y', mark: false },
    ])
  })

  it('closes a nested <mark> at the first </mark>, the rest stays text', async () => {
    expect(await parts('<mark>a<mark>b</mark>c</mark>')).toEqual([
      { text: 'a<mark>b', mark: true },
      { text: 'c</mark>', mark: false },
    ])
  })

  it('spans newlines inside a match and handles an empty snippet', async () => {
    expect(await parts('<mark>a\nb</mark>')).toEqual([{ text: 'a\nb', mark: true }])
    expect(await parts('')).toEqual([])
  })
})
