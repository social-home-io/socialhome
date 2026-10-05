/**
 * Markdown rendering for the Pages feature (§23.58 / §23.72).
 *
 * Pipeline:
 *   raw markdown → wikilink pre-pass → marked (GFM + breaks) → DOMPurify
 *
 * DOMPurify is configured with a narrow allow-list so a hostile page body
 * (Pages and space about text arrive over federation) cannot sneak in
 * `<script>`, `<iframe>`, a phishing `<form>`/`<button>`/`<select>`,
 * autoplaying `<video>`/`<audio>`, `class`/style/onclick attrs, or
 * `javascript:` URLs. Never pass ``USE_PROFILES`` here — DOMPurify then
 * replaces ``ALLOWED_TAGS``/``ALLOWED_ATTR`` with its whole HTML profile.
 * Images + links resolve only `http:` / `https:` / `mailto:` / local
 * (`/…`, `#…`, `api/…`); anything else — including `data:` (which
 * DOMPurify would otherwise allow on `<img>`) and protocol-relative
 * `//host` / `\\host` — is stripped before rendering.
 * The one `<input>` kept is the GFM task-list checkbox, forced disabled.
 *
 * Wikilinks: `[[Page Title]]` rewrites to an anchor pointing at
 * `/pages?title=Page+Title` — the Pages router reads the `title` query
 * param and opens the matching local page. Unresolved titles still land
 * on the Pages index, where the user can create the page.
 */

import DOMPurify from 'dompurify'
import { marked } from 'marked'

// Per-call renderer setup — marked is a singleton but we want a single
// clean configuration here so we don't leak options into other call
// sites in the app.
marked.use({
  gfm:       true,
  breaks:    true,
  pedantic:  false,
})

// Strip the leading ``/`` from ``src`` / ``href`` attributes that
// point at one of our own ``/api/...`` paths so the browser resolves
// them against ``<base href>`` instead of the origin. Under HA
// Supervisor ingress the document base is
// ``/api/hassio_ingress/<token>/`` and an absolute ``/api/media/foo``
// would bypass it entirely (HTML resolves leading-``/`` against the
// **origin**, not the base) — every embedded image in a Page body
// would 404. Stripping the slash makes it ``api/media/foo``, which
// resolves correctly under any deployment shape (standalone +
// ingress + Vite dev). Registered once at module load; DOMPurify
// hooks are global so this runs for every ``DOMPurify.sanitize``
// call across the app, which is what we want.
//
// The same hook also refuses two URL shapes the allow-list regex below
// would let through, read the way a browser reads them (tabs / newlines
// dropped anywhere, edge controls / spaces trimmed — as ``safeHref``):
//   * ``data:`` — DOMPurify admits it on ``<img src>`` regardless of
//     ``ALLOWED_URI_REGEXP``. Pages never need it (pictures are uploads,
//     ``api/media/…``), and a peer-written data: SVG has no business in
//     a federated body.
//   * ``//host`` and its backslash spellings (``\\host``, ``/\host``) —
//     protocol-relative, so they leave for another host. ``safeHref``
//     refuses them; so does the markdown renderer.
// eslint-disable-next-line no-control-regex
const _URL_STRIPPED = /[\t\n\r]/g
// eslint-disable-next-line no-control-regex
const _URL_EDGE = /^[\u0000- ]+|[\u0000- ]+$/g
const _PROTOCOL_RELATIVE = /^[/\\]{2}/

DOMPurify.addHook('uponSanitizeAttribute', (_node, data) => {
  if (
    (data.attrName !== 'src' && data.attrName !== 'href') ||
    typeof data.attrValue !== 'string'
  ) {
    return
  }
  const read = data.attrValue.replace(_URL_STRIPPED, '').replace(_URL_EDGE, '')
  if (/^data:/i.test(read) || _PROTOCOL_RELATIVE.test(read)) {
    data.keepAttr = false
    return
  }
  if (data.attrValue.startsWith('/api/')) {
    data.attrValue = data.attrValue.slice(1)
  }
})

// Pages and space "about" text arrive over federation and may embed
// third-party images / links. Under the server's
// ``Referrer-Policy: strict-origin-when-cross-origin`` the browser would
// still send this household's origin to that third party — on image load
// (no click needed) and on link navigation. Pin every ``<img>`` to
// ``referrerpolicy="no-referrer"`` + ``loading="lazy"`` and every link that
// is not an in-app path (``/…`` / ``#…``, read the way the browser reads
// it) to ``rel="noopener noreferrer"``. Runs AFTER the
// attribute allow-list, so an author can't supply (or override) any of
// these — ``rel`` / ``referrerpolicy`` / ``loading`` are not in
// ``ALLOWED_ATTR`` and are dropped from the input first.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'IMG') {
    node.setAttribute('referrerpolicy', 'no-referrer')
    node.setAttribute('loading', 'lazy')
  } else if (node.tagName === 'A') {
    const read = (node.getAttribute('href') ?? '')
      .replace(_URL_STRIPPED, '').replace(_URL_EDGE, '')
    if (!/^[/#]/.test(read)) node.setAttribute('rel', 'noopener noreferrer')
  }
})

const WIKILINK_RE = /\[\[([^\]|]+)\]\]/g

const ALLOWED_TAGS = [
  'a', 'p', 'br', 'hr',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'strong', 'em', 'del', 's', 'u',
  'ul', 'ol', 'li',
  'code', 'pre',
  'blockquote',
  'img',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'span', 'input',  // input for GFM checklist items — see _onlyTaskBoxes
]

const ALLOWED_ATTR = [
  'href', 'title', 'alt', 'src', 'width', 'height',
  'colspan', 'rowspan', 'align',
  'type', 'checked', 'disabled',
  'id',
]

// DOMPurify tests EVERY allowed attribute value that is not in its
// URI-safe set (``type``, ``align``, ``width``…) against this regex, so it
// must admit plain scheme-less values too. Allowed: ``http:`` / ``https:``
// / ``mailto:``, or anything without a URL scheme — a leading non-letter
// (``/feed``, ``#x``, ``100``) or a word that ends before any ``:``
// (``api/media/…``, ``checkbox``). ``javascript:``, ``data:``, ``blob:``
// and ``httpsx:`` all have a scheme that is not on the list.
const ALLOWED_URI = /^(?:https?:|mailto:|[^a-z]|[a-z][a-z0-9+.-]*(?:[^a-z0-9+.\-:]|$))/i

/** Keep only ``<input type=checkbox>`` (GFM task lists) and pin it
 *  disabled; any other input type is removed outright. */
function _onlyTaskBoxes(root: DocumentFragment): void {
  for (const input of Array.from(root.querySelectorAll('input'))) {
    if ((input.getAttribute('type') || '').toLowerCase() !== 'checkbox') {
      input.remove()
      continue
    }
    input.setAttribute('disabled', '')
  }
}

/** Pre-pass that converts `[[Page Title]]` into plain anchor markdown. */
function replaceWikilinks(src: string): string {
  return src.replace(WIKILINK_RE, (_m, title) => {
    const trimmed = String(title).trim()
    const href = `/pages?title=${encodeURIComponent(trimmed)}`
    return `[${trimmed}](${href})`
  })
}

/** Render and sanitise a Markdown body. Returns an HTML string safe to
 * splice into the DOM via `dangerouslySetInnerHTML`. */
export function renderMarkdown(src: string): string {
  const withLinks = replaceWikilinks(src || '')
  const rawHtml = marked.parse(withLinks, { async: false }) as string
  const frag = DOMPurify.sanitize(rawHtml, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // ``api/…`` (scheme-less) must stay valid: the ``uponSanitizeAttribute``
    // hook above rewrites ``/api/...`` → ``api/...`` for ingress.
    ALLOWED_URI_REGEXP: ALLOWED_URI,
    FORBID_TAGS:      ['style', 'script', 'iframe', 'object', 'embed'],
    FORBID_ATTR:      ['style', 'onerror', 'onload', 'onclick'],
    RETURN_DOM_FRAGMENT: true,
  })
  _onlyTaskBoxes(frag)
  const out = document.createElement('div')
  out.appendChild(frag)
  return out.innerHTML
}

/** Auto-generate a flat table of contents from ``##`` / ``###`` headings.
 * Returns `[{depth, text, slug}]` for the viewer's TOC rail. */
export function extractHeadings(
  src: string,
): { depth: number, text: string, slug: string }[] {
  const out: { depth: number, text: string, slug: string }[] = []
  const lines = (src || '').split('\n')
  for (const raw of lines) {
    const m = /^(#{2,3})\s+(.+?)\s*$/.exec(raw)
    if (!m) continue
    const depth = m[1].length
    const text = m[2].trim()
    const slug = text
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, '')
      .trim()
      .replace(/\s+/g, '-')
    out.push({ depth, text, slug })
  }
  return out
}
