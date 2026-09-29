/**
 * Link-preview helpers shared by the composer and the post card.
 *
 * ``firstUrl`` mirrors the backend's ``domain.link_preview.first_url`` so
 * the composer asks for the same link the server will preview when the
 * post is created (the server stays the source of truth — the client only
 * decides whether to *show* a card and whether to opt out).
 */

const URL_RE = /https?:\/\/[^\s<>"'()[\]{}`]+/i
const TRAILING_PUNCT = /[.,;:!?*_~]+$/

/** The first ``http(s)`` link in *text*, trailing punctuation trimmed. */
export function firstUrl(text: string | null | undefined): string | null {
  if (!text) return null
  const m = URL_RE.exec(text)
  if (!m) return null
  const url = m[0].replace(TRAILING_PUNCT, '')
  return url || null
}

/** *raw* as an ``href`` only when it is a plain ``http(s)`` URL without
 *  credentials; ``null`` for anything else (``javascript:``, ``data:``…). */
export function safeWebUrl(raw: string | null | undefined): string | null {
  if (!raw) return null
  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch {
    return null
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
  if (parsed.username || parsed.password) return null
  return parsed.href
}

/** Host of *raw* without a leading ``www.``, for the card's domain line. */
export function linkDomain(raw: string | null | undefined): string {
  const href = safeWebUrl(raw)
  if (!href) return ''
  return new URL(href).hostname.replace(/^www\./i, '')
}
