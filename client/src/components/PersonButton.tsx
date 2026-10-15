/**
 * PersonButton — makes a name or an avatar open the person sheet.
 *
 * A plain ``<button>`` with the look reset to whatever it wraps, so a
 * post author, a comment author, a chat sender or a member row keeps its
 * typography and gains a real, keyboard-reachable control (Enter /
 * Space, the global focus ring).
 *
 * Where an avatar sits right next to the name it belongs to, wrap the
 * avatar with ``decorative``: it stays tappable for touch users but is
 * skipped by Tab and hidden from screen readers, so the person is one
 * stop, not two.
 *
 * The hint is resolved on click (pass a function) so a row doesn't
 * build one per render for every person on screen.
 */
import type { ComponentChildren } from 'preact'
import { openPerson, type PersonContext } from './PersonSheet'
import { personHint, type PersonHint } from '@/utils/personHint'
import { spaceMembers } from '@/store/spaceMembers'
import { findMentionMember } from '@/utils/mentions'

interface Props {
  userId: string
  /** What the caller knows — or a thunk that builds it on click. */
  hint?: PersonHint | (() => PersonHint)
  children: ComponentChildren
  class?: string
  /** Accessible name when the content alone isn't one (an avatar). */
  label?: string
  /** The avatar twin of a name button: tappable, but not a tab stop
   *  and not announced (the name button is). */
  decorative?: boolean
  /** Runs before the sheet opens (e.g. close the dialog it sits in). */
  onBeforeOpen?: () => void
  /** The viewer saved / cleared a nickname from the sheet. */
  onAliasChanged?: PersonContext['onAliasChanged']
  'data-testid'?: string
}

export function PersonButton({
  userId, hint, children, class: cls, label, decorative = false,
  onBeforeOpen, onAliasChanged, 'data-testid': testId,
}: Props) {
  return (
    <button
      type="button"
      class={cls ? `sh-person-btn ${cls}` : 'sh-person-btn'}
      aria-label={decorative ? undefined : label}
      aria-hidden={decorative ? 'true' : undefined}
      tabIndex={decorative ? -1 : undefined}
      data-testid={testId}
      data-person-id={userId}
      onClick={(e) => {
        // Cards and bubbles underneath may have their own click
        // handlers (open thread, long-press menu) — this tap is ours.
        e.stopPropagation()
        onBeforeOpen?.()
        const person = typeof hint === 'function' ? hint() : hint
        openPerson(userId, {
          ...(person ? { person } : {}),
          ...(onAliasChanged ? { onAliasChanged } : {}),
        })
      }}
    >
      {children}
    </button>
  )
}

/** Open the sheet for ``userId`` with the best hint the caches hold. */
export function openKnownPerson(spaceId: string | null | undefined, userId: string): void {
  openPerson(userId, { person: personHint(spaceId, userId) })
}

/** Open the sheet for the member an ``@token`` in ``spaceId`` names.
 *  A token the roster doesn't know (yet) does nothing. */
export function openSpaceMention(spaceId: string | null | undefined, token: string): void {
  if (!spaceId) return
  const m = findMentionMember(spaceMembers.value[spaceId]?.values(), token)
  if (m) openKnownPerson(spaceId, m.user_id)
}
