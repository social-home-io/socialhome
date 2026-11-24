/**
 * MentionText — plain text with known @-mentions highlighted (§23.42) and
 * web links clickable (:func:`LinkifiedText`, the one link grammar).
 *
 * JSX all the way down: user text is never parsed as HTML. ``mentions`` is
 * the lower-cased token set of the scope's members (the server-issued
 * ``mention`` tokens), so only tokens that really resolve to a member are
 * highlighted; the viewer's own token gets ``--self``, ``@here`` gets
 * ``--here``. A mention inside a link stays part of the link.
 *
 * With ``onMention`` a person mention is a real ``<button>`` (Enter /
 * Space, focus ring) that hands its token back — the caller resolves it
 * against its roster and opens the person sheet. ``@here`` is never a
 * person and stays a plain chip.
 */
import { HERE_TOKEN, splitMentions } from '@/utils/mentions'
import { LinkifiedText } from './LinkifiedText'

interface Props {
  text: string
  mentions: ReadonlySet<string>
  selfMention?: string | null
  /** A person mention was activated — ``token`` is lower-cased. */
  onMention?: (token: string) => void
}

export function MentionText({ text, mentions, selfMention, onMention }: Props) {
  const self = selfMention?.toLocaleLowerCase() ?? null
  const renderMentions = (part: string) => splitMentions(part, mentions).map((p, i) => {
    if (typeof p === 'string') return p
    if (p.token === HERE_TOKEN) {
      return <span key={i} class="sh-mention sh-mention--here">{p.raw}</span>
    }
    const cls = p.token === self ? 'sh-mention sh-mention--self' : 'sh-mention'
    if (!onMention) return <span key={i} class={cls}>{p.raw}</span>
    return (
      <button
        key={i}
        type="button"
        class={cls}
        data-mention={p.token}
        onClick={(e) => {
          // Bubbles carry their own tap handlers (long-press menu).
          e.stopPropagation()
          onMention(p.token)
        }}
      >
        {p.raw}
      </button>
    )
  })
  return <LinkifiedText text={text} renderText={renderMentions} />
}
