import { describe, expect, test } from 'vitest'
import { renderMarkdown } from './markdown'

describe('renderMarkdown', () => {
  test('escapes HTML to prevent XSS', () => {
    const out = renderMarkdown('<script>alert(1)</script>')
    expect(out).not.toContain('<script>')
    expect(out).toContain('&lt;script&gt;')
  })

  test('renders bold with **', () => {
    expect(renderMarkdown('**bold**')).toContain('<strong>bold</strong>')
  })

  test('renders italic with *', () => {
    expect(renderMarkdown('a *italic* b')).toContain('<em>italic</em>')
  })

  test('renders inline code', () => {
    expect(renderMarkdown('a `x=1` b')).toContain(
      '<code class="sh-md-inline-code">x=1</code>',
    )
  })

  test('renders code blocks with triple-backtick', () => {
    const out = renderMarkdown('```\nfoo();\n```')
    expect(out).toContain('<pre class="sh-md-code">')
    expect(out).toContain('foo();')
  })

  test('renders safe links', () => {
    const out = renderMarkdown('[home](https://example.com)')
    expect(out).toContain('<a href="https://example.com"')
    expect(out).toContain('rel="noopener noreferrer"')
    expect(out).toContain('target="_blank"')
  })

  test('strips javascript: URLs as a security measure', () => {
    const out = renderMarkdown('[click](javascript:alert(1))')
    expect(out).not.toContain('javascript:')
    // Fall through to plain text.
    expect(out).toContain('click')
  })

  test('strips data: URLs as a security measure', () => {
    const out = renderMarkdown('[x](data:text/html,<script>alert(1)</script>)')
    expect(out).not.toContain('data:')
  })

  test('preserves newlines as <br>', () => {
    expect(renderMarkdown('line1\nline2')).toContain('line1<br>line2')
  })

  test('handles empty and null-ish input', () => {
    expect(renderMarkdown('')).toBe('')
  })

  test('bold and italic on separate runs both render', () => {
    const out = renderMarkdown('**bold** and *italic*')
    expect(out).toContain('<strong>bold</strong>')
    expect(out).toContain('<em>italic</em>')
  })
})

describe('renderMarkdown — @mentions', () => {
  const tokens = new Set(['anna', 'bob@k3f9x2'])

  test('wraps known member tokens, leaves unknown ones plain', () => {
    const out = renderMarkdown('hi @anna and @nobody', { mentions: tokens })
    expect(out).toBe('hi <span class="sh-mention">@anna</span> and @nobody')
  })

  test('marks the viewer’s own mention', () => {
    const out = renderMarkdown('@Anna!', { mentions: tokens, selfMention: 'anna' })
    expect(out).toBe('<span class="sh-mention sh-mention--self">@Anna</span>!')
  })

  test('qualified tokens and no options → unchanged', () => {
    expect(renderMarkdown('@bob@k3f9x2', { mentions: tokens }))
      .toBe('<span class="sh-mention">@bob@k3f9x2</span>')
    expect(renderMarkdown('hi @anna')).toBe('hi @anna')
  })

  test('never inside code, link targets or e-mail addresses', () => {
    const out = renderMarkdown(
      '`@anna` [x](https://example.com/@anna) mail me@anna.org',
      { mentions: tokens },
    )
    expect(out).not.toContain('sh-mention')
    expect(out).toContain('href="https://example.com/@anna"')
  })

  test('a hostile member token cannot inject markup', () => {
    // Even if a (compromised) roster handed us a token full of markup,
    // the renderer escapes the text first and only ever wraps token
    // characters — the payload stays inert text.
    const evil = new Set([
      '<img src=x onerror=alert(1)>',
      'x" onmouseover="alert(1)',
      'anna',
    ])
    const out = renderMarkdown(
      '@<img src=x onerror=alert(1)> @x" onmouseover="alert(1) @anna',
      { mentions: evil },
    )
    expect(out).not.toContain('<img')
    expect(out).not.toMatch(/<span[^>]*onmouseover/)
    expect(out).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(out).toContain('<span class="sh-mention">@anna</span>')
    const doc = new DOMParser().parseFromString(out, 'text/html')
    expect(doc.querySelectorAll('img, [onerror], [onmouseover]')).toHaveLength(0)
    expect(doc.querySelectorAll('span.sh-mention')).toHaveLength(1)
  })
})
