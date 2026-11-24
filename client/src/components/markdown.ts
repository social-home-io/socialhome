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
 * stripped. Links open in a new tab with ``rel="noopener noreferrer
 * nofollow ugc"`` so the household's origin never reaches the target as
 * a Referer. There is
 * deliberately no image grammar: ``![alt](url)`` stays a link, so showing
 * a post never makes the viewer's browser fetch from a third party.
 *
 * This is deliberately tiny (no dep). When we need tables, footnotes,
 * or embed syntax we'll swap for ``marked`` + ``DOMPurify``.
 */

import { t } from '@/i18n/i18n'
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

// A web link typed as plain text: ``http(s)://…`` or ``www.…``, not right
// after a character that belongs to a host, path or address (so
// ``foo.www.x`` and ``mail@www.x`` stay text; ``Link:https://…`` links,
// as the server's ``first_url`` finds it). It runs on RAW text (the
// renderer unescapes its own escaping first) and stops at whitespace,
// control characters, bidi controls (which could flip how the link's
// tail reads), ``<>"'``, ``[]{}`` and backtick. Parentheses are
// part of a link while they balance — the GFM rule, so
// ``…/wiki/Mercury_(planet)`` links in full and ``(see https://x/y)``
// ends before the ``)``.
// eslint-disable-next-line no-control-regex
const _BARE_LINK = /(?<![\w/.@-])(?:https?:\/\/|www\.)[^\s\u0000-\u001f\u007f\u200e\u200f\u202a-\u202e\u2066-\u2069<>"'[\]{}`]+/gi
// Punctuation that ends a sentence, not a URL (as the server trims it).
const _TRAILING_PUNCT = /[.,;:!?*_~]$/
// A ``www.`` link needs one more label (``www.example.org``); a scheme
// link needs a host.
const _LINK_HAS_HOST = /^(?:https?:\/\/[^/]|www\.[^./]+\.[^./])/i

/** *raw* without trailing sentence punctuation and without a closing
 *  ``)`` that has no ``(`` inside the link. */
function _trimTrailing(raw: string): string {
  // Counted once, then kept up to date — linear in the link's length.
  let opens = 0
  let closes = 0
  for (const c of raw) {
    if (c === '(') opens += 1
    else if (c === ')') closes += 1
  }
  let end = raw.length
  for (;;) {
    const last = raw[end - 1]
    if (end > 0 && _TRAILING_PUNCT.test(last)) {
      end -= 1
    } else if (last === ')' && opens < closes) {
      closes -= 1
      end -= 1
    } else {
      return raw.slice(0, end)
    }
  }
}

/** ``https://name@host`` — refused like ``safeWebUrl`` and the server's
 *  fetcher do: the text before the ``@`` only disguises the real host. */
function _hasUserinfo(href: string): boolean {
  try {
    const u = new URL(href)
    return u.username !== '' || u.password !== ''
  } catch {
    return true
  }
}

/** A plain-text web link found by :func:`splitLinks`. */
export interface TextLink {
  /** Safe ``http(s)`` href (a ``www.`` link opens over ``https``). */
  href: string
  /** The link as the author wrote it. */
  text: string
}

/** Split RAW text into plain strings and web links — the one link
 *  grammar for every place that shows user text (post bodies through
 *  :func:`renderMarkdown`, comments, DMs, moments, calendar, bazaar…).
 *  Only ``http``/``https`` hrefs come out (``_safeHref``), never one with
 *  user-info or control characters. */
export function splitLinks(text: string): Array<string | TextLink> {
  const out: Array<string | TextLink> = []
  let last = 0
  for (const m of text.matchAll(_BARE_LINK)) {
    const url = _trimTrailing(m[0])
    if (!_LINK_HAS_HOST.test(url)) continue
    const href = _safeHref(/^www\./i.test(url) ? `https://${url}` : url)
    if (href === null || _hasUserinfo(href)) continue
    const at = m.index ?? 0
    if (at > last) out.push(text.slice(last, at))
    out.push({ href, text: url })
    last = at + url.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

/** Where to cut *text* for a "show more" preview at about *at*: never
 *  inside a link (a cut link would open a different page) — the cut
 *  moves back to the link's start, or past its end when the text starts
 *  with it. The caller shows its ``…`` outside the linked text. */
export function cutOutsideLinks(text: string, at: number): number {
  let pos = 0
  for (const p of splitLinks(text)) {
    const len = typeof p === 'string' ? p.length : p.text.length
    if (typeof p !== 'string' && pos < at && at < pos + len) {
      return pos > 0 ? pos : pos + len
    }
    pos += len
    if (pos >= at) break
  }
  return at
}

/** ``rel`` of every link to another site, inline and on the link card:
 *  no ``window.opener``, no Referer, and no endorsement of user links. */
export const EXTERNAL_LINK_REL = 'noopener noreferrer nofollow ugc'

/** The decorative mark after an external link: ↗ in text presentation
 *  (U+FE0E), so it never turns into a colour emoji, glued to the link's
 *  last word (U+2060 word joiner) so it never wraps onto a line alone. */
export const EXTERNAL_LINK_ICON = '\u2060↗\uFE0E'

/** Longest link text shown in full; a longer one shows host + path cut
 *  to :data:`_LABEL_CUT` characters and ``…`` (the full URL stays the
 *  ``href`` and the ``title``). */
const _LABEL_MAX = 48
const _LABEL_CUT = 40

/** The visible text of a link: as written when short, else host + path
 *  (no scheme) shortened with ``…``. */
export function linkLabel(text: string): string {
  if (text.length <= _LABEL_MAX) return text
  const bare = text.replace(/^https?:\/\//i, '')
  // The host is never shortened — ``www.bank.example.evil.example/…``
  // must show where it really goes. Only path and query are cut, by
  // code point (never half an emoji).
  const hostEnd = bare.search(/[/?#]/)
  const host = hostEnd === -1 ? bare : bare.slice(0, hostEnd)
  const rest = [...bare.slice(host.length)]
  const room = Math.max(0, _LABEL_CUT - [...host].length)
  return rest.length <= room ? bare : `${host}${rest.slice(0, room).join('')}…`
}

function _unescape(html: string): string {
  return html
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&')
}

/** One external link as HTML (see :func:`splitLinks`): new tab,
 *  :data:`EXTERNAL_LINK_REL`, a decorative ``↗`` and a hidden "(opens in a
 *  new tab)" for screen readers; long links shortened, the full URL in
 *  ``title``. */
function _linkHtml(link: TextLink): string {
  const label = linkLabel(link.text)
  const title = label === link.text ? '' : ` title="${_escape(link.text)}"`
  return _externalAnchor(_escape(link.href), _escape(label), title)
}

/** The ``<a>`` every external link in a body gets (*href* / *labelHtml* /
 *  *titleAttr* already escaped). Reads the locale at render time — a
 *  memo of the output must key on the locale too. */
function _externalAnchor(href: string, labelHtml: string, titleAttr = ''): string {
  return `<a class="sh-ext-link" href="${href}"${titleAttr} target="_blank" rel="${EXTERNAL_LINK_REL}">`
    + `${labelHtml}<span class="sh-ext-link__icon" aria-hidden="true">${EXTERNAL_LINK_ICON}</span>`
    + `<span class="sr-only">${_escape(t('link.new_tab'))}</span></a>`
}

/** Turn plain-text web links in escaped text into safe anchors. Never
 *  inside an existing link. */
function _autolink(text: string, inLink: boolean): string {
  if (inLink) return text
  const parts = splitLinks(_unescape(text))
  if (parts.length === 1 && typeof parts[0] === 'string') return text
  return parts.map((p) => (typeof p === 'string' ? _escape(p) : _linkHtml(p))).join('')
}

/** The hrefs of every link :func:`renderMarkdown` makes from *input*, in
 *  order — markdown links and plain-text links alike. */
export function linkHrefs(input: string): string[] {
  const out: string[] = []
  for (const m of renderMarkdown(input).matchAll(/<a (?:class="[^"]*" )?href="([^"]*)"/g)) {
    out.push(_unescape(m[1]))
  }
  return out
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
  // User text arrives escaped, so a raw ``<`` / ``>`` / ``"`` can only be
  // markup an earlier pass made (``[x](https://a/`b`)`` puts a <code> tag
  // into the href) — it would end the attribute and drop ``rel``.
  if (/[<>"]/.test(read)) return null
  // The bold / italic passes run after the link pass over the whole
  // string; a ``*`` left in an href would let them put ``<strong>`` into
  // the attribute (``[x](https://a/**@anna**)``). ``%2A`` is the same URL.
  return read.replace(/\*/g, '%2A')
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
  // One level of balanced parentheses in the href, so
  // ``[x](…/wiki/Mercury_(planet))`` opens the right page.
  out = out.replace(
    /\[([^\]]+)\]\(((?:[^()]|\([^()]*\))+)\)/g,
    (_m, text, href) => {
      const safe = _safeHref(href)
      if (safe === null) return _escape(text)
      if (/^https?:/i.test(safe)) {
        // A label with nothing to see (spaces, zero-width characters)
        // would hide where the link goes: show the URL instead.
        const blank = _unescape(text).replace(/[\s\u200b-\u200d\u2060\ufeff]/g, '') === ''
        return blank
          ? _linkHtml({ href: _unescape(safe), text: _unescape(safe) })
          : _externalAnchor(safe, text)
      }
      return `<a href="${safe}" target="_blank" rel="${EXTERNAL_LINK_REL}">${text}</a>`
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
