/**
 * Minimal safe-subset markdown → HTML renderer (§23.43).
 *
 * Covers bold, italic, inline code, code blocks, links, line breaks.
 * No raw HTML passes through — every produced tag originates here, so
 * the output is XSS-safe by construction. Link hrefs are filtered to
 * ``http:`` / ``https:`` / ``mailto:`` / an app path (``/…``) —
 * ``javascript:``, data URLs and protocol-relative ``//host`` are
 * stripped.
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
}

/** Wrap known @-mentions in the already-escaped HTML ``html``.
 *
 *  Runs last, over text between tags only: every ``<…>`` in ``html`` was
 *  produced by this module (user text was escaped first), so skipping tag
 *  strings keeps attribute values (``href``) intact, and ``<code>`` /
 *  ``<pre>`` bodies stay literal. The span carries no user-derived
 *  attribute — only the matched text, which is token chars by grammar. */
function _wrapMentions(
  html: string,
  tokens: ReadonlySet<string>,
  self: string | null,
): string {
  let codeDepth = 0
  return html.split(/(<[^>]*>)/).map((part) => {
    if (part.startsWith('<')) {
      if (/^<(code|pre)\b/i.test(part)) codeDepth += 1
      else if (/^<\/(code|pre)>/i.test(part)) codeDepth = Math.max(0, codeDepth - 1)
      return part
    }
    if (codeDepth > 0 || !part) return part
    return splitMentions(part, tokens).map((p) => {
      if (typeof p === 'string') return p
      const cls = p.token === 'here'
        ? 'sh-mention sh-mention--here'
        : p.token === self ? 'sh-mention sh-mention--self' : 'sh-mention'
      return `<span class="${cls}">${p.raw}</span>`
    }).join('')
  }).join('')
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

  if (opts.mentions && opts.mentions.size > 0) {
    out = _wrapMentions(
      out, opts.mentions, opts.selfMention?.toLocaleLowerCase() ?? null,
    )
  }
  return out
}
