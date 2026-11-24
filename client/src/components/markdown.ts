/**
 * Minimal safe-subset markdown → HTML renderer (§23.43).
 *
 * Covers bold, italic, inline code, code blocks, links, line breaks.
 * Links are ``[text](url)`` and plain-text web links (``https://…``,
 * ``www.…`` — a pasted link is clickable, :func:`_autolink`).
 * No raw HTML passes through — every produced tag originates here, so
 * the output is XSS-safe by construction. Link hrefs are filtered to
 * ``http:`` / ``https:`` / ``mailto:`` / an app path (``/…``) —
 * ``javascript:``, data URLs and protocol-relative ``//host`` are
 * stripped. Links open in a new tab with ``rel="noopener noreferrer"`` so
 * the household's origin never reaches the target as a Referer. There is
 * deliberately no image grammar: ``![alt](url)`` stays a link, so showing
 * a post never makes the viewer's browser fetch from a third party.
 *
 * This is deliberately tiny (no dep). When we need tables, footnotes,
 * or embed syntax we'll swap for ``marked`` + ``DOMPurify``.
 */

import { splitMentions } from '@/utils/mentions'

const _SAFE_SCHEMES = /^(https?:|mailto:|\/)/i

export interface RenderOptions {
  /** Lower-cased @-tokens of the current space's members
   *  (:func:`mentionTokenSet`). Matching ``@token``s are wrapped in
   *  ``<span class="sh-mention">`` — only known members, so a stray ``@``
   *  or an e-mail address stays plain text. */
  mentions?: ReadonlySet<string>
  /** The viewer's own token — that mention gets ``sh-mention--self``. */
  selfMention?: string | null
  /** Person mentions become ``<button type="button" data-mention="…">``
   *  so the caller can open the person sheet from a delegated click
   *  (keyboard-operable for free). ``@here`` and a mention inside a link
   *  stay a ``<span>`` — never a control nested in another. */
  interactive?: boolean
}

/** Rewrite the text between tags of the already-escaped HTML ``html``.
 *
 *  Every ``<…>`` in ``html`` was produced by this module (user text was
 *  escaped first), so skipping tag strings keeps attribute values
 *  (``href``) intact; ``<code>`` / ``<pre>`` bodies are never handed to
 *  ``fn`` and stay literal. ``inLink`` tells ``fn`` the text sits inside
 *  an ``<a>``. */
function _mapText(
  html: string,
  fn: (text: string, inLink: boolean) => string,
): string {
  let codeDepth = 0
  let linkDepth = 0
  return html.split(/(<[^>]*>)/).map((part) => {
    if (part.startsWith('<')) {
      if (/^<(code|pre)\b/i.test(part)) codeDepth += 1
      else if (/^<\/(code|pre)>/i.test(part)) codeDepth = Math.max(0, codeDepth - 1)
      else if (/^<a\b/i.test(part)) linkDepth += 1
      else if (/^<\/a>/i.test(part)) linkDepth = Math.max(0, linkDepth - 1)
      return part
    }
    if (codeDepth > 0 || !part) return part
    return fn(part, linkDepth > 0)
  }).join('')
}

// A web link typed as plain text: ``http(s)://…`` or ``www.…``, after a
// start / space / punctuation that is not part of a host, path or address
// (so ``foo.www.x`` and ``mail@www.x`` stay text). It runs on ESCAPED
// text, so ``&`` only ever starts an entity: ``&amp;`` (a literal ``&``,
// part of a query) continues the link, every other entity (``&lt;``,
// ``&gt;``, ``&quot;``, ``&#39;`` — a ``<>"'`` in the source) ends it,
// as do whitespace and ``()[]{}`` / backtick — the same stop set as the
// server's ``domain.link_preview.first_url``, so the link and its card
// agree on where the URL ends.
const _BARE_LINK = /(^|[^\w/.@:-])((?:https?:\/\/|www\.)(?:[^\s&<>"'()[\]{}`]|&amp;)+)/gi
// Punctuation that ends a sentence, not a URL (as the server trims it).
const _TRAILING_PUNCT = /[.,;:!?*_~]+$/
// A ``www.`` link needs one more label (``www.example.org``); a scheme
// link needs a host.
const _LINK_HAS_HOST = /^(?:https?:\/\/[^/]|www\.[^./]+\.[^./])/i

/** Turn plain-text web links in escaped text into safe anchors. A
 *  ``www.`` link opens over ``https``. Never inside an existing link. */
function _autolink(text: string, inLink: boolean): string {
  if (inLink) return text
  return text.replace(_BARE_LINK, (m, lead: string, raw: string) => {
    const url = raw.replace(_TRAILING_PUNCT, '')
    if (!_LINK_HAS_HOST.test(url)) return m
    const href = _safeHref(/^www\./i.test(url) ? `https://${url}` : url)
    if (href === null) return m
    const rest = raw.slice(url.length)
    return `${lead}<a href="${href}" target="_blank" rel="noopener noreferrer">${url}</a>${rest}`
  })
}

/** Wrap known @-mentions in the already-escaped HTML ``html``.
 *
 *  Runs last, over text between tags only (:func:`_mapText`). The
 *  wrapper carries no user-derived attribute but ``data-mention`` — the
 *  matched token, which is word chars / ``.`` / ``-`` / ``@`` by grammar
 *  AND one of the server-issued member tokens, escaped once more anyway. */
function _wrapMentions(
  html: string,
  tokens: ReadonlySet<string>,
  self: string | null,
  interactive: boolean,
): string {
  return _mapText(html, (part, inLink) => {
    return splitMentions(part, tokens).map((p) => {
      if (typeof p === 'string') return p
      if (p.token === 'here') {
        return `<span class="sh-mention sh-mention--here">${p.raw}</span>`
      }
      const cls = p.token === self ? 'sh-mention sh-mention--self' : 'sh-mention'
      if (!interactive || inLink) return `<span class="${cls}">${p.raw}</span>`
      return `<button type="button" class="${cls}" data-mention="${_escape(p.token)}">${p.raw}</button>`
    }).join('')
  })
}

function _escape(raw: string): string {
  return raw
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

// ``//host`` / ``/\host`` are protocol-relative — they leave for another
// host. Refused, as ``utils/safeHref`` does.
const _PROTOCOL_RELATIVE = /^[/\\]{2}/

// Read the URL the way a browser does: TAB / CR / LF are removed anywhere
// and C0 controls + space at the edges, so ``/<TAB>/evil`` is ``//evil``.
const _URL_STRIPPED = /[\t\n\r]/g
// eslint-disable-next-line no-control-regex
const _URL_EDGE = /^[\u0000- ]+|[\u0000- ]+$/g

function _safeHref(href: string): string | null {
  const read = href.replace(_URL_STRIPPED, '').replace(_URL_EDGE, '')
  if (!_SAFE_SCHEMES.test(read) || _PROTOCOL_RELATIVE.test(read)) return null
  return read
}

/** Render a markdown-ish string to safe HTML. */
export function renderMarkdown(input: string, opts: RenderOptions = {}): string {
  if (!input) return ''
  // Escape first — everything we splice back in is intentional.
  let out = _escape(input)

  // Fenced code blocks ```…```
  out = out.replace(/```([\s\S]*?)```/g, (_m, body) => (
    `<pre class="sh-md-code"><code>${body}</code></pre>`
  ))
  // Inline code `…`
  out = out.replace(/`([^`\n]+)`/g, (_m, body) => (
    `<code class="sh-md-inline-code">${body}</code>`
  ))
  // Links [text](url) — reject unsafe schemes.
  out = out.replace(
    /\[([^\]]+)\]\(([^)]+)\)/g,
    (_m, text, href) => {
      const safe = _safeHref(href)
      if (safe === null) return _escape(text)
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${text}</a>`
    },
  )
  // Bold **…** (must come before italic so ** doesn't eat * greedily).
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  // Italic *…*
  out = out.replace(/(^|\s)\*([^*\n]+)\*(?=\s|$)/g, '$1<em>$2</em>')
  // Soft line breaks — preserve newlines inside a single paragraph.
  out = out.replace(/\n/g, '<br>')
  // Plain-text web links → anchors (a pasted link is clickable). After
  // the markup above so it never lands inside a tag or a code span.
  out = _mapText(out, _autolink)

  if (opts.mentions && opts.mentions.size > 0) {
    out = _wrapMentions(
      out, opts.mentions, opts.selfMention?.toLocaleLowerCase() ?? null,
      opts.interactive === true,
    )
  }
  return out
}
