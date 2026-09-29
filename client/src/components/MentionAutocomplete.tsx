/**
 * MentionAutocomplete — ``@name`` member picker for a space composer or a
 * group-chat composer (§23.42).
 *
 * Same wiring shape as :mod:`EmojiAutocomplete`: the input owner calls
 * :func:`checkForMentionTrigger` on every input event with ``(text,
 * cursorPos, anchorEl, scope, splice)``, routes ``onKeyDown`` through
 * :func:`handleMentionAutocompleteKey` first, spreads
 * :func:`mentionInputAria` onto the input, and mounts one
 * ``<MentionAutocomplete />`` next to it. Module-level state keeps a
 * single popover open across the page.
 *
 * ``scope`` is a space id (string) or ``{ conversationId }``. Candidates
 * come from the matching member cache (:mod:`store/spaceMembers` →
 * ``GET /api/spaces/{id}/members``, or :mod:`store/conversationMembers`
 * → ``GET /api/conversations/{id}/members``), loaded once per scope and
 * filtered client-side — typing never hits the network. Picking a member inserts the exact token the server resolves
 * (``mention`` on the member row), so who you pick is who gets notified.
 */
import { signal } from '@preact/signals'
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks'
import { Avatar } from './Avatar'
import { currentUser } from '@/store/auth'
import {
  loadSpaceMembers,
  spaceMembers,
  viewerMayUseHere,
} from '@/store/spaceMembers'
import {
  conversationMembers,
  loadConversationMembers,
} from '@/store/conversationMembers'
import {
  findMentionTrigger,
  mentionCandidates,
  type MentionCandidate,
} from '@/utils/mentions'

type SpliceCallback = (text: string, range: [number, number]) => void

/** Where the ``@`` is typed: a space (its id) or a conversation. */
export type MentionScope = string | { conversationId: string }

interface ResolvedScope {
  kind: 'space' | 'conversation'
  id: string
}

function resolveScope(scope: MentionScope | null | undefined): ResolvedScope | null {
  if (!scope) return null
  if (typeof scope === 'string') return { kind: 'space', id: scope }
  return scope.conversationId ? { kind: 'conversation', id: scope.conversationId } : null
}

interface MentionState {
  scope: ResolvedScope
  query: string
  /** ``[start, end)`` of the ``@partial`` token the pick replaces. */
  range: [number, number]
  active: number
  /** Viewport (``position: fixed``) coordinates. */
  top: number
  left: number
  width: number
  placement: 'below' | 'above'
  anchor: HTMLInputElement | HTMLTextAreaElement
  splice: SpliceCallback
}

const state = signal<MentionState | null>(null)

export const MENTION_LISTBOX_ID = 'sh-mention-listbox'
const optionId = (idx: number) => `sh-mention-opt-${idx}`

/** Popover height budget used to decide whether it fits below the input. */
const ESTIMATED_POPOVER_HEIGHT = 300
const MAX_WIDTH = 320
const EDGE = 8

function matchesFor(s: MentionState): MentionCandidate[] | null {
  const { kind, id } = s.scope
  const members = kind === 'space'
    ? spaceMembers.value[id]
    : conversationMembers.value[id]
  if (!members) return null
  return mentionCandidates(members.values(), s.query, currentUser.value?.user_id, {
    // ``@here`` pages a space; it has no meaning in a chat.
    includeHere: kind === 'space' && viewerMayUseHere(id),
  })
}

export function isMentionAutocompleteOpen(): boolean {
  return state.value !== null
}

export function closeMentionAutocomplete(): void {
  state.value = null
}

/** Open / refresh / close the picker for the ``@partial`` token ending at
 *  ``cursorPos``. ``scope`` null/undefined (household feed, 1:1 chat) →
 *  never. */
export function checkForMentionTrigger(
  text: string,
  cursorPos: number,
  anchor: HTMLInputElement | HTMLTextAreaElement,
  scope: MentionScope | null | undefined,
  splice: SpliceCallback,
): void {
  const resolved = resolveScope(scope)
  const trigger = resolved ? findMentionTrigger(text, cursorPos) : null
  if (!resolved || !trigger) {
    closeMentionAutocomplete()
    return
  }
  // Cached per scope: a no-op once the roster is loaded.
  if (resolved.kind === 'space') void loadSpaceMembers(resolved.id)
  else void loadConversationMembers(resolved.id)
  const rect = anchor.getBoundingClientRect()
  const vv = window.visualViewport
  // The on-screen area is the visual viewport — on a phone with the
  // keyboard up it ends well above ``innerHeight``. Flip above the input
  // when the list wouldn't fit between the input and the keyboard.
  const visibleTop = vv ? vv.offsetTop : 0
  const visibleBottom = vv ? vv.offsetTop + vv.height : window.innerHeight
  const visibleWidth = vv ? vv.width : window.innerWidth
  const spaceBelow = visibleBottom - rect.bottom
  const spaceAbove = rect.top - visibleTop
  const above = spaceBelow < ESTIMATED_POPOVER_HEIGHT + EDGE && spaceAbove > spaceBelow
  const width = Math.min(MAX_WIDTH, visibleWidth - 2 * EDGE)
  const left = Math.max(EDGE, Math.min(rect.left, visibleWidth - width - EDGE))
  const prev = state.value
  const sameToken = prev !== null && prev.anchor === anchor
    && prev.range[0] === trigger.start
  state.value = {
    scope: resolved,
    query: trigger.query,
    range: [trigger.start, cursorPos],
    active: sameToken && prev.query === trigger.query ? prev.active : 0,
    top: above ? rect.top - 4 : rect.bottom + 4,
    left,
    width,
    placement: above ? 'above' : 'below',
    anchor,
    splice,
  }
}

function pick(s: MentionState, c: MentionCandidate): void {
  const insert = `@${c.token} `
  const [start] = s.range
  closeMentionAutocomplete()
  s.splice(insert, s.range)
  requestAnimationFrame(() => {
    const pos = start + insert.length
    s.anchor.focus()
    s.anchor.setSelectionRange(pos, pos)
  })
}

/** Keyboard hook — call first from the input's ``onKeyDown``. Returns
 *  ``true`` when the picker consumed the key (caller ``preventDefault``s
 *  and skips its own Enter-to-submit). */
export function handleMentionAutocompleteKey(e: KeyboardEvent): boolean {
  const s = state.value
  if (s === null) return false
  if (e.key === 'Escape') {
    closeMentionAutocomplete()
    return true
  }
  const matches = matchesFor(s)
  if (!matches || matches.length === 0) return false
  if (e.key === 'ArrowDown') {
    state.value = { ...s, active: (s.active + 1) % matches.length }
    return true
  }
  if (e.key === 'ArrowUp') {
    state.value = {
      ...s,
      active: (s.active - 1 + matches.length) % matches.length,
    }
    return true
  }
  if (e.key === 'Enter' || e.key === 'Tab') {
    pick(s, matches[Math.min(s.active, matches.length - 1)])
    return true
  }
  return false
}

/** Combobox ARIA for the owning input — spread onto it so screen readers
 *  follow the highlighted option while focus stays in the text field. */
export function mentionInputAria(
  anchor: HTMLElement | null,
): Record<string, string | boolean | undefined> {
  const s = state.value
  const open = s !== null && anchor !== null && s.anchor === anchor
  const matches = open ? matchesFor(s) : null
  const hasOptions = !!matches && matches.length > 0
  return {
    'aria-autocomplete': 'list',
    'aria-expanded': open && hasOptions,
    'aria-controls': open && hasOptions ? MENTION_LISTBOX_ID : undefined,
    'aria-activedescendant': open && hasOptions
      ? optionId(Math.min(s.active, matches.length - 1))
      : undefined,
  }
}

export function MentionAutocomplete() {
  const s = state.value
  const ref = useRef<HTMLDivElement>(null)
  // ``position: fixed`` resolves against the nearest transformed /
  // filtered ancestor, not the viewport (the comment overlay's backdrop
  // uses ``backdrop-filter``). Measure where the popover actually landed
  // and cancel the offset, so it sits on the input in any container.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || s === null) return
    el.style.left = `${s.left}px`
    el.style.top = `${s.top}px`
    const r = el.getBoundingClientRect()
    const dx = r.left - s.left
    const dy = (s.placement === 'above' ? r.bottom : r.top) - s.top
    if (dx) el.style.left = `${s.left - dx}px`
    if (dy) el.style.top = `${s.top - dy}px`
  })
  const open = s !== null
  useEffect(() => {
    if (!open) return
    const onDocMouseDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.closest('.sh-mention-autocomplete') || t === state.value?.anchor)) return
      closeMentionAutocomplete()
    }
    // Fixed-position popover: a scroll would strand it away from the
    // input, so close instead (typing re-opens it in the right place).
    const onScroll = (e: Event) => {
      const t = e.target
      // The input scrolling its own text (long textarea) isn't a move.
      if (t === state.value?.anchor) return
      if (t instanceof Element && t.closest('.sh-mention-autocomplete')) return
      closeMentionAutocomplete()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open])

  if (s === null) return null
  const matches = matchesFor(s)
  const style = {
    top: `${s.top}px`,
    left: `${s.left}px`,
    width: `${s.width}px`,
    transform: s.placement === 'above' ? 'translateY(-100%)' : 'none',
  }
  if (matches === null) {
    return (
      <div ref={ref} class="sh-mention-autocomplete" style={style} role="status">
        <div class="sh-mention-autocomplete-note">Loading members…</div>
      </div>
    )
  }
  if (matches.length === 0) {
    if (!s.query) return null
    return (
      <div ref={ref} class="sh-mention-autocomplete" style={style} role="status">
        <div class="sh-mention-autocomplete-note">
          {s.scope.kind === 'space'
            ? `No member of this space matches “@${s.query}”`
            : `Nobody in this chat matches “@${s.query}”`}
        </div>
      </div>
    )
  }
  const active = Math.min(s.active, matches.length - 1)
  return (
    <div
      ref={ref}
      id={MENTION_LISTBOX_ID}
      class="sh-mention-autocomplete"
      role="listbox"
      aria-label="Mention a member"
      style={style}
    >
      {matches.map((c, idx) => (
        <div
          key={c.userId}
          id={optionId(idx)}
          role="option"
          aria-selected={idx === active}
          class={
            idx === active
              ? 'sh-mention-autocomplete-row sh-mention-autocomplete-row--active'
              : 'sh-mention-autocomplete-row'
          }
          onMouseDown={(e) => {
            // Keep focus in the input so the pick doesn't blur-close it.
            e.preventDefault()
            pick(s, c)
          }}
          onMouseEnter={() => {
            if (state.value && state.value.active !== idx) {
              state.value = { ...state.value, active: idx }
            }
          }}
        >
          {c.here
            ? <span class="sh-mention-autocomplete-here" aria-hidden="true">📣</span>
            : <Avatar name={c.name} src={c.pictureUrl} size={28} />}
          <span class="sh-mention-autocomplete-text">
            <span class="sh-mention-autocomplete-name">{c.name}</span>
            <span class="sh-mention-autocomplete-meta">
              @{c.token}
              {c.here && ' · notifies every member'}
              {c.household && (
                <span class="sh-mention-autocomplete-household">
                  {' · '}{c.household}
                </span>
              )}
            </span>
          </span>
        </div>
      ))}
    </div>
  )
}
