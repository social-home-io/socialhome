/**
 * Hashtag rendering helpers — shared between the inbox row and the
 * archive row so a ``#tag`` substring inside a moment's content
 * becomes a clickable link to ``/momentum?tab=archive&tag=<tag>``.
 *
 * The rule mirrors the server-side extractor in
 * ``socialhome/domain/moment.py``: ``#`` + letters / marks / numbers /
 * ``_`` in any script (``#Kürbisfest``, ``#東京``), not right after such
 * a character (``issue#42`` is no tag), at most 32 code points (a longer
 * run is no tag), stored NFC + lowercased. Keeping the two in sync means
 * the chip a user sees and the tag the server stored are the same.
 */
import type { ComponentChildren, MouseEventHandler } from 'preact'
import { addBase } from '@/baseUrl'

/** Server: ``MOMENT_MAX_HASHTAG_LEN`` (code points). */
const MAX_TAG_LEN = 32
const HASHTAG_RE = /(?<![\p{L}\p{M}\p{N}_])#([\p{L}\p{M}\p{N}_]+)/gu

/** The stored form of a tag (server: ``normalize_hashtag``). */
export function normalizeHashtag(tag: string): string {
  return tag.trim().replace(/^#+/, '').normalize('NFC').toLowerCase()
}

export function renderHashtagged(
  content: string,
  onClick: (tag: string, ev: MouseEvent) => void,
): ComponentChildren {
  if (!content) return content
  // NFC first, like the server, so a decomposed "ü" is one tag char.
  content = content.normalize('NFC')
  const out: ComponentChildren[] = []
  let last = 0
  let match: RegExpExecArray | null
  HASHTAG_RE.lastIndex = 0
  while ((match = HASHTAG_RE.exec(content)) !== null) {
    const raw = match[1]
    // A run longer than the cap is no tag (the server drops it too).
    if ([...raw].length > MAX_TAG_LEN) continue
    if (match.index > last) {
      out.push(content.slice(last, match.index))
    }
    const tag = normalizeHashtag(raw)
    const onClickHandler: MouseEventHandler<HTMLAnchorElement> = (ev) => {
      onClick(tag, ev as unknown as MouseEvent)
    }
    out.push(
      <a
        key={`${match.index}-${tag}`}
        href={addBase(`/momentum?tab=archive&tag=${encodeURIComponent(tag)}`)}
        class="sh-hashtag"
        onClick={onClickHandler}
      >#{raw}</a>,
    )
    last = match.index + match[0].length
  }
  if (last < content.length) {
    out.push(content.slice(last))
  }
  return out.length === 0 ? content : out
}
