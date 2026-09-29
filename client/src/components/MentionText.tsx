/**
 * MentionText — plain text with known @-mentions highlighted (§23.42).
 *
 * JSX all the way down: user text is never parsed as HTML. ``mentions`` is
 * the lower-cased token set of the scope's members (the server-issued
 * ``mention`` tokens), so only tokens that really resolve to a member are
 * highlighted; the viewer's own token gets ``--self``.
 */
import { splitMentions } from '@/utils/mentions'

interface Props {
  text: string
  mentions: ReadonlySet<string>
  selfMention?: string | null
}

export function MentionText({ text, mentions, selfMention }: Props) {
  const self = selfMention?.toLocaleLowerCase() ?? null
  return (
    <>
      {splitMentions(text, mentions).map((p, i) => (
        typeof p === 'string'
          ? p
          : (
            <span
              key={i}
              class={p.token === self ? 'sh-mention sh-mention--self' : 'sh-mention'}
            >
              {p.raw}
            </span>
          )
      ))}
    </>
  )
}
