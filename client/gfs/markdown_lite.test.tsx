/**
 * A space's About text from a GFS is rendered as nodes, never as HTML:
 * markup in it stays text, and only http(s) / mailto links become anchors.
 */
import { render } from '@testing-library/preact'
import { describe, expect, it } from 'vitest'
import { MAX_SOURCE_CHARS, renderMarkdown } from './markdown_lite'

function html(src: string): string {
  const { container } = render(<div>{renderMarkdown(src)}</div>)
  return (container.firstChild as HTMLElement).innerHTML
}

describe('renderMarkdown', () => {
  it('renders the supported subset', () => {
    expect(html('# Hello')).toBe('<h3>Hello</h3>')
    expect(html('**bold** and _it_ and `x*y*`')).toBe(
      '<p><strong>bold</strong> and <em>it</em> and <code>x*y*</code></p>',
    )
    expect(html('- one\n- two')).toBe('<ul><li>one</li><li>two</li></ul>')
    expect(html('> quoted\n> more')).toBe('<blockquote>quoted more</blockquote>')
    expect(html('a\nb\n\nc')).toBe('<p>a<br>b</p><p>c</p>')
  })

  it('links only safe schemes, guarded', () => {
    expect(html('[site](https://example.org/a?b=1)')).toBe(
      '<p><a href="https://example.org/a?b=1" rel="noopener noreferrer nofollow ugc" target="_blank">site</a></p>',
    )
    expect(html('[x](javascript:alert(1))')).toBe('<p>[x](javascript:alert(1))</p>')
    expect(html('[x](data:text/html,hi)')).toBe('<p>[x](data:text/html,hi)</p>')
  })

  it('keeps markup as text', () => {
    const out = html('<img src=x onerror=alert(1)> <script>alert(1)</script>')
    expect(out).toBe('<p>&lt;img src=x onerror=alert(1)&gt; &lt;script&gt;alert(1)&lt;/script&gt;</p>')
    expect(html('[<b>x</b>](https://e.org/"onmouseover="x)')).toContain('href="https://e.org/&quot;onmouseover=&quot;x"')
  })

  it('is empty for blank input and bounded', () => {
    expect(renderMarkdown('')).toEqual([])
    expect(renderMarkdown('  \n ')).toEqual([])
    expect(renderMarkdown(null)).toEqual([])
    const long = html('a'.repeat(MAX_SOURCE_CHARS + 50))
    expect(long).toBe(`<p>${'a'.repeat(MAX_SOURCE_CHARS)}…</p>`)
  })
})
