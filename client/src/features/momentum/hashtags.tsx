/**
 * Hashtag rendering helpers — shared between the inbox row and the
 * archive row so a ``#tag`` substring inside a moment's content
 * becomes a clickable link to ``/momentum?tab=archive&tag=<tag>``.
 *
 * The regex mirrors the server-side extractor in
 * ``socialhome/domain/moment.py`` (negative lookbehind on word
 * characters, ASCII tag charset, 32-char max). Keeping the two in
 * sync means the chip a user sees and the chip the server stored
 * are the same.
 */
import type { ComponentChildren, JSX } from 'preact'
import { addBase } from '@/baseUrl'

const HASHTAG_RE = /(?<!\w)#([A-Za-z0-9_]{1,32})/g

export function renderHashtagged(
  content: string,
  onClick: (tag: string, ev: MouseEvent) => void,
): ComponentChildren {
  if (!content) return content
  const out: ComponentChildren[] = []
  let last = 0
  let match: RegExpExecArray | null
  HASHTAG_RE.lastIndex = 0
  while ((match = HASHTAG_RE.exec(content)) !== null) {
    if (match.index > last) {
      out.push(content.slice(last, match.index))
    }
    const raw = match[1]
    const tag = raw.toLowerCase()
    const onClickHandler: JSX.MouseEventHandler<HTMLAnchorElement> = (ev) => {
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
