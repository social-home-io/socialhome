/**
 * Link-preview helpers shared by the composer and the post card.
 *
 * ``firstUrl`` mirrors the backend's ``domain.link_preview.first_url`` so
 * the composer asks for the same link the server will preview when the
 * post is created (the server stays the source of truth — the client only
 * decides whether to *show* a card and whether to opt out).
 */

import { linkHrefs, splitLinks } from '@/components/markdown'

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

/** Whether a card built for *text* opens the same page the text links.
 *
 *  The server builds the card for ``first_url`` (mirrored by
 *  :func:`firstUrl`), which stops at ``(``; the text renderer links
 *  balanced parentheses in full (``…/wiki/Mercury_(planet)``). Teaching
 *  the server the same rule would change ``card_survives_edit`` on every
 *  household (a protocol bump), so instead a card shows only when its URL
 *  is exactly one of the links the text renders — link and card never
 *  point at different pages. */
export function cardFitsText(text: string | null | undefined): boolean {
  const url = firstUrl(text)
  if (!url || !text) return false
  return linkHrefs(text).includes(url.replace(/\*/g, '%2A'))
}

/** The text has a ``www.`` link but no ``http(s)://`` one — it is
 *  clickable but gets no card (the composer says how to get one). */
export function hasOnlyWwwLinks(text: string | null | undefined): boolean {
  if (!text || firstUrl(text)) return false
  return splitLinks(text).some((p) => typeof p !== 'string')
}
