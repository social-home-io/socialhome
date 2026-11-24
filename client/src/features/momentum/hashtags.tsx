/**
 * Hashtag rendering helpers — shared between the inbox row and the
 * archive row so a ``#tag`` substring inside a moment's content
 * becomes a clickable link to ``/momentum?tag=<tag>``.
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
import { ExternalLink } from '@/components/LinkifiedText'
import { splitLinks } from '@/components/markdown'

/** Server: ``MOMENT_MAX_HASHTAG_LEN`` (code points). */
const MAX_TAG_LEN = 32
const HASHTAG_RE = /(?<![\p{L}\p{M}\p{N}_])#([\p{L}\p{M}\p{N}_]+)/gu

/** The stored form of a tag (server: ``normalize_hashtag``). */
export function normalizeHashtag(tag: string): string {
  return tag.trim().replace(/^#+/, '').normalize('NFC').toLowerCase()
}

/** *content* with its web links clickable (``splitLinks``, the one link
 *  grammar) and its ``#tags`` as chips. A ``#`` inside a link (a URL
 *  fragment such as ``https://x.org/#top``) stays part of the link and
 *  shows no chip — deliberately, even where the server's extractor
 *  stores it as a tag: a chip there would cut the link in two. */
export function renderHashtagged(
  content: string,
  onClick: (tag: string, ev: MouseEvent) => void,
): ComponentChildren {
  if (!content) return content
  // NFC first, like the server, so a decomposed "ü" is one tag char.
  content = content.normalize('NFC')
  const parts = splitLinks(content)
  if (parts.length === 1 && typeof parts[0] === 'string') {
    return _hashtagged(content, onClick, '')
  }
  return parts.map((p, i) => (
    typeof p === 'string'
      ? _hashtagged(p, onClick, `${i}:`)
      : <ExternalLink key={`l${i}`} link={p} />
  ))
}

function _hashtagged(
  content: string,
  onClick: (tag: string, ev: MouseEvent) => void,
  keyPrefix: string,
): ComponentChildren {
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
        key={`${keyPrefix}${match.index}-${tag}`}
        href={addBase(`/momentum?tag=${encodeURIComponent(tag)}`)}
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
