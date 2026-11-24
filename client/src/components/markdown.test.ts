import { describe, expect, test } from 'vitest'
import { cutOutsideLinks, linkHrefs, linkLabel, renderMarkdown, splitLinks } from './markdown'

/** A link's text without its decorative ↗ and screen-reader suffix. */
function visibleText(a: Element): string {
  const copy = a.cloneNode(true) as Element
  copy.querySelectorAll('.sh-ext-link__icon, .sr-only').forEach(n => n.remove())
  return copy.textContent ?? ''
}

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
    expect(out).toContain('href="https://example.com"')
    expect(out).toContain('rel="noopener noreferrer nofollow ugc"')
    expect(out).toContain('target="_blank"')
  })

  test.each([['//evil.example/x'], ['/\\evil.example/x'], ['\\\\evil.example/x']])(
    'drops protocol-relative link %s (like safeHref), keeps /app paths',
    (bad) => {
      const out = renderMarkdown(`[x](${bad}) [y](/feed)`)
      expect(out).not.toContain('evil.example/x"')
      expect(out).not.toMatch(/href="[/\\]{2}/)
      expect(out).toContain('href="/feed"')
    },
  )

  test.each([['/\t/evil.example/x'], ['/\r/evil.example/x'], ['\u0001//evil.example/x']])(
    'drops %j: browsers strip TAB/CR and edge controls, so it is protocol-relative',
    (bad) => {
      const out = renderMarkdown(`[x](${bad}) [y](/feed)`)
      expect(out).not.toContain('evil.example')
      expect(out).toContain('href="/feed"')
    },
  )

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

  test('@here gets its own class when the token set carries it', () => {
    const out = renderMarkdown('@here dinner', { mentions: new Set(['here']) })
    expect(out).toBe('<span class="sh-mention sh-mention--here">@here</span> dinner')
    expect(renderMarkdown('@here dinner', { mentions: tokens })).toBe('@here dinner')
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

describe('renderMarkdown — no Referer to third parties', () => {
  test('links open in a new tab with rel="noopener noreferrer"', () => {
    const html = renderMarkdown('[site](https://example.org/x)')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer nofollow ugc"')
  })

  test('image syntax never produces an <img> (no third-party fetch on view)', () => {
    // This renderer has no image grammar; ``![alt](url)`` stays a link the
    // viewer must click, so a post can't make the browser fetch from a
    // third party (and leak the household origin) just by being shown.
    const html = renderMarkdown('![cat](https://img.example.net/cat.png)')
    expect(html).not.toMatch(/<img\b/i)
    expect(html).toContain('rel="noopener noreferrer nofollow ugc"')
  })
})

describe('renderMarkdown — interactive @mentions (person sheet)', () => {
  const tokens = new Set(['anna', 'bob@k3f9x2', 'here'])

  test('a person mention becomes a typed button carrying its token', () => {
    const out = renderMarkdown('hi @Anna!', { mentions: tokens, interactive: true })
    expect(out).toBe(
      'hi <button type="button" class="sh-mention" data-mention="anna">@Anna</button>!',
    )
    const self = renderMarkdown('@anna', { mentions: tokens, selfMention: 'anna', interactive: true })
    expect(self).toContain('class="sh-mention sh-mention--self" data-mention="anna"')
  })

  test('@here stays a plain chip — it is not a person', () => {
    const out = renderMarkdown('@here dinner', { mentions: tokens, interactive: true })
    expect(out).toBe('<span class="sh-mention sh-mention--here">@here</span> dinner')
  })

  test('a mention inside link text stays a span (no control inside a link)', () => {
    const out = renderMarkdown('[@anna rocks](https://example.com) @anna', {
      mentions: tokens, interactive: true,
    })
    const doc = new DOMParser().parseFromString(out, 'text/html')
    expect(doc.querySelectorAll('a button')).toHaveLength(0)
    expect(doc.querySelectorAll('a span.sh-mention')).toHaveLength(1)
    expect(doc.querySelectorAll('button[data-mention="anna"]')).toHaveLength(1)
  })

  test('never inside code', () => {
    const out = renderMarkdown('`@anna` ```\n@anna\n```', { mentions: tokens, interactive: true })
    expect(out).not.toContain('<button')
  })

  test('a hostile token cannot break out of data-mention', () => {
    const evil = new Set(['x" onclick="alert(1)', '<b>', 'anna'])
    const out = renderMarkdown('@x" onclick="alert(1) @<b> @anna', {
      mentions: evil, interactive: true,
    })
    const doc = new DOMParser().parseFromString(out, 'text/html')
    expect(doc.querySelectorAll('[onclick], b')).toHaveLength(0)
    const buttons = [...doc.querySelectorAll('button')]
    expect(buttons.map(b => b.getAttribute('data-mention'))).toEqual(['anna'])
    expect(buttons[0].getAttributeNames().sort()).toEqual(['class', 'data-mention', 'type'])
  })
})

// Owner bug 2026-11-24: "I made a post with a link, but the link is not
// linked". Only ``[text](url)`` became an <a>; a pasted link stayed text.
describe('renderMarkdown — bare links are clickable', () => {
  const links = (html: string) => {
    const doc = new DOMParser().parseFromString(html, 'text/html')
    return [...doc.querySelectorAll('a')].map(a => ({
      href: a.getAttribute('href'),
      text: visibleText(a),
      rel: a.getAttribute('rel'),
      target: a.getAttribute('target'),
    }))
  }

  test('a bare URL becomes a safe link', () => {
    expect(links(renderMarkdown('https://www.home-assistant.io/'))).toEqual([{
      href: 'https://www.home-assistant.io/',
      text: 'https://www.home-assistant.io/',
      rel: 'noopener noreferrer nofollow ugc',
      target: '_blank',
    }])
  })

  test('a URL inside a sentence links only the URL', () => {
    const html = renderMarkdown('Look at https://example.org/a/b it is great')
    expect(links(html).map(l => l.href)).toEqual(['https://example.org/a/b'])
    expect(html.startsWith('Look at <a ')).toBe(true)
    expect(html.endsWith('</a> it is great')).toBe(true)
  })

  test('www. without a scheme links to https', () => {
    expect(links(renderMarkdown('Our site www.home-assistant.io is it'))).toEqual([{
      href: 'https://www.home-assistant.io',
      text: 'www.home-assistant.io',
      rel: 'noopener noreferrer nofollow ugc',
      target: '_blank',
    }])
  })

  test('a query string survives; trailing punctuation stays text', () => {
    const html = renderMarkdown('Read https://example.org/?a=1&b=two_words. Now!')
    expect(links(html).map(l => [l.href, l.text])).toEqual([
      ['https://example.org/?a=1&b=two_words', 'https://example.org/?a=1&b=two_words'],
    ])
    expect(html).toContain('</a>. Now!')
  })

  test.each([
    ['(see https://example.org/x)', 'https://example.org/x'],
    ['"https://example.org/q"', 'https://example.org/q'],
    ['<https://example.org/z>', 'https://example.org/z'],
    ['https://example.org/end,', 'https://example.org/end'],
    ['go: http://example.org:', 'http://example.org'],
    ['Link:https://example.org/l', 'https://example.org/l'],
    ['see https://example.org/a?b&', 'https://example.org/a?b&'],
    ['see https://example.org/a?b&.', 'https://example.org/a?b&'],
  ])('%s links %s only', (src, href) => {
    expect(links(renderMarkdown(src)).map(l => l.href)).toEqual([href])
  })

  test('several links on several lines', () => {
    const html = renderMarkdown('one https://a.example/1\ntwo www.b.example/2')
    expect(links(html).map(l => l.href)).toEqual([
      'https://a.example/1', 'https://www.b.example/2',
    ])
  })

  test('a markdown link is not linked twice', () => {
    const html = renderMarkdown('A [markdown link](https://x.example/home) here')
    expect(links(html)).toEqual([{
      href: 'https://x.example/home',
      text: 'markdown link',
      rel: 'noopener noreferrer nofollow ugc',
      target: '_blank',
    }])
  })

  test('a markdown link whose text is the URL is one link', () => {
    const html = renderMarkdown('[https://x.example/a](https://x.example/a)')
    expect(links(html)).toHaveLength(1)
  })

  test('code stays code', () => {
    const html = renderMarkdown('`https://x.example/a` and\n```\nwww.y.example\n```')
    expect(links(html)).toHaveLength(0)
  })

  test('bold links still link', () => {
    const html = renderMarkdown('**https://x.example/a**')
    expect(html).toContain('<strong><a class="sh-ext-link" href="https://x.example/a"')
  })

  test.each([
    ['javascript:alert(1)'],
    ['data:text/html,<script>alert(1)</script>'],
    ['vbscript:msgbox'],
    ['file:///etc/passwd'],
    ['ftp://example.org/x'],
    ['foo.www.example.org'],
    ['mail@www.example.org'],
    ['https://'],
    ['www.'],
    ['https://\u0001'],
    ['www.\u0001.\u0001'],
    ['https://accounts.example.com@evil.example/x'],
    ['www.good.example@evil.example'],
  ])('%s is not linked', (src) => {
    expect(links(renderMarkdown(src))).toHaveLength(0)
  })

  test('an attribute break-out attempt stays inside the href', () => {
    const html = renderMarkdown('https://x.example/"onmouseover="alert(1)')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelectorAll('[onmouseover]')).toHaveLength(0)
    expect(links(html).map(l => l.href)).toEqual(['https://x.example/'])
  })

  test('an escaped tag in a URL never becomes markup', () => {
    const html = renderMarkdown('https://x.example/<img src=x onerror=alert(1)>')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelectorAll('img, [onerror]')).toHaveLength(0)
    expect(links(html).map(l => l.href)).toEqual(['https://x.example/'])
  })

  test('a code span inside a markdown href never makes a rel-less link', () => {
    const html = renderMarkdown('[x](https://evil.example/`b`)')
    const doc = new DOMParser().parseFromString(html, 'text/html')
    for (const a of doc.querySelectorAll('a')) {
      expect(a.getAttribute('rel')).toBe('noopener noreferrer nofollow ugc')
      expect(a.getAttribute('href')).not.toMatch(/[<>"]/)
    }
  })

  test.each([
    ['[x](https://evil.example/**@anna**)'],
    ['[x](https://evil.example/ *@anna* c)'],
    ['[x](https://evil.example/**b**)'],
  ])('bold / italic never reach into a markdown href: %s', (src) => {
    const html = renderMarkdown(src, { mentions: new Set(['anna']), interactive: true })
    const doc = new DOMParser().parseFromString(html, 'text/html')
    for (const a of doc.querySelectorAll('a')) {
      expect(a.getAttributeNames().sort()).toEqual(['class', 'href', 'rel', 'target'])
      expect(a.getAttribute('rel')).toBe('noopener noreferrer nofollow ugc')
    }
  })

  test('a mention inside a URL is not a button inside the link', () => {
    const html = renderMarkdown('https://social.example/@anna', {
      mentions: new Set(['anna']), interactive: true,
    })
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.querySelectorAll('button')).toHaveLength(0)
    expect(links(html).map(l => l.href)).toEqual(['https://social.example/@anna'])
  })
})

// UX review 2026-11-24 on #1431.
describe('links — parentheses, labels, a11y', () => {
  const anchors = (html: string) =>
    [...new DOMParser().parseFromString(html, 'text/html').querySelectorAll('a')]

  test.each([
    ['Planet: https://en.wikipedia.org/wiki/Mercury_(planet) is hot.',
      'https://en.wikipedia.org/wiki/Mercury_(planet)'],
    ['(see https://en.wikipedia.org/wiki/Mercury_(planet))',
      'https://en.wikipedia.org/wiki/Mercury_(planet)'],
    ['https://x.example/a_(b)_(c).', 'https://x.example/a_(b)_(c)'],
    ['(https://x.example/y)', 'https://x.example/y'],
    ['https://x.example/q)', 'https://x.example/q'],
  ])('balanced parentheses stay in the link: %s', (src, href) => {
    expect(anchors(renderMarkdown(src)).map(a => a.getAttribute('href'))).toEqual([href])
  })

  test('a long link shows host + path shortened, the full URL in title', () => {
    const url = 'https://www.home-assistant.io/integrations/mqtt/?utm_source=newsletter&utm_medium=email'
    const [a] = anchors(renderMarkdown(`Long one: ${url}`))
    expect(a.getAttribute('href')).toBe(url)
    expect(a.getAttribute('title')).toBe(url)
    expect(visibleText(a)).toBe('www.home-assistant.io/integrations/mqtt/…')
  })

  test('a short link keeps its text and gets no title', () => {
    const [a] = anchors(renderMarkdown('https://example.org/a'))
    expect(visibleText(a)).toBe('https://example.org/a')
    expect(a.hasAttribute('title')).toBe(false)
  })

  test('↗ is hidden from screen readers; "(opens in a new tab)" is read', () => {
    const [a] = anchors(renderMarkdown('https://example.org/a'))
    expect(a.querySelector('.sh-ext-link__icon')?.getAttribute('aria-hidden')).toBe('true')
    expect(a.querySelector('.sr-only')?.textContent).toBe('(opens in a new tab)')
  })

  test('linkLabel', () => {
    expect(linkLabel('https://a.example/x')).toBe('https://a.example/x')
    expect(linkLabel('https://a.example/' + 'p'.repeat(60))).toBe(`a.example/${'p'.repeat(30)}…`)
  })

  test('splitLinks gives plain strings and safe links', () => {
    expect(splitLinks('see www.a.example/x, and https://b.example/?q=1&r=2.')).toEqual([
      'see ',
      { href: 'https://www.a.example/x', text: 'www.a.example/x' },
      ', and ',
      { href: 'https://b.example/?q=1&r=2', text: 'https://b.example/?q=1&r=2' },
      '.',
    ])
    expect(splitLinks('javascript:alert(1) data:x https://u@evil.example')).toEqual([
      'javascript:alert(1) data:x https://u@evil.example',
    ])
    expect(splitLinks('no links here')).toEqual(['no links here'])
  })

  test('linkHrefs lists markdown and plain links in order', () => {
    expect(linkHrefs('[a](https://a.example/) then https://b.example/x?y=1&z=2')).toEqual([
      'https://a.example/', 'https://b.example/x?y=1&z=2',
    ])
  })
})

describe('cutOutsideLinks', () => {
  test('never cuts inside a link', () => {
    const text = 'see https://example.org/long/path more'
    expect(cutOutsideLinks(text, 10)).toBe(4)
    expect(cutOutsideLinks(text, 2)).toBe(2)
    expect(cutOutsideLinks(text, 36)).toBe(36)
    expect(cutOutsideLinks('no links at all', 5)).toBe(5)
  })
})

describe('review round 2', () => {
  const anchors = (html: string) =>
    [...new DOMParser().parseFromString(html, 'text/html').querySelectorAll('a')]

  test('a markdown link keeps balanced parentheses in its href', () => {
    const html = renderMarkdown('[Mercury](https://en.wikipedia.org/wiki/Mercury_(planet)) is hot')
    const [a] = anchors(html)
    expect(a.getAttribute('href')).toBe('https://en.wikipedia.org/wiki/Mercury_(planet)')
    expect(visibleText(a)).toBe('Mercury')
    expect(html).not.toContain('</a>)')
  })

  test('an external markdown link reads like a pasted one', () => {
    const [a] = anchors(renderMarkdown('[site](https://example.org/x)'))
    expect(a.className).toBe('sh-ext-link')
    expect(a.querySelector('.sh-ext-link__icon')?.getAttribute('aria-hidden')).toBe('true')
    expect(a.querySelector('.sr-only')?.textContent).toBe('(opens in a new tab)')
    // An in-app path is no external link.
    const [local] = anchors(renderMarkdown('[here](/spaces/x)'))
    expect(local.querySelector('.sr-only')).toBeNull()
  })

  test('the cut lands outside a link, never at its end', () => {
    const text = 'see https://example.org/long/path more'
    expect(cutOutsideLinks(text, 10)).toBe(4)
    // A text that starts with a link keeps the whole link.
    expect(cutOutsideLinks('https://example.org/long/path more', 10)).toBe(29)
  })

  test('trailing parentheses trim in linear time', () => {
    const t0 = performance.now()
    const parts = splitLinks('https://x.example/' + ')'.repeat(10000))
    expect(performance.now() - t0).toBeLessThan(100)
    expect(parts[0]).toEqual({ href: 'https://x.example/', text: 'https://x.example/' })
  })

  test('a long label never splits an emoji', () => {
    const url = 'https://a.example/' + 'p'.repeat(21) + '😀' + 'q'.repeat(40)
    expect(linkLabel(url)).toBe(`a.example/${'p'.repeat(21)}😀${'q'.repeat(8)}…`)
  })
})
