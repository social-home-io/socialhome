/**
 * EmojiPickButton — 😀 button that opens the full :class:`ReactionPicker`
 * popover and inserts the chosen emoji into a target text input.
 *
 * Two flavours:
 *
 * 1. Signal-target — pass ``target: Signal<string>`` plus the ``field``
 *    bound to it. The chosen emoji goes in at the field's caret
 *    (replacing a selection, like typing would), and focus and the caret
 *    — right after the emoji — go back to the field. Used by
 *    :mod:`CommentThread`.
 *
 * 2. Callback-target — pass ``onInsert: (emoji) => void`` for owners
 *    that splice into a richer state shape (the post composer, the DM
 *    composer, a Highlight caption). Each inserts at the caret, replacing
 *    a selection, and hands focus and the caret back the same way (their
 *    splice is shared with their ``:emoji`` autocomplete).
 *
 * The picker hangs off the button with ``position: fixed`` (see
 * ``ReactionPicker``'s ``anchor``): it opens on whichever side has room
 * and stays inside the visible area, wherever the button sits.
 *
 * A module-level ``openFor`` signal keeps at most one picker open
 * across the whole page; callers pass an ``openKey`` string so each
 * mount instance can claim its own picker without colliding with
 * other inputs.
 */
import { signal, type Signal } from '@preact/signals'
import type { RefObject } from 'preact'
import { useEffect, useRef } from 'preact/hooks'
import { ReactionPicker } from './ReactionPicker'
import { t } from '@/i18n/i18n'

const openFor = signal<string | null>(null)

type TextField = HTMLTextAreaElement | HTMLInputElement

/**
 * ``text`` with ``insert`` at ``field``'s caret (replacing its selection)
 * — at the end when there is no field. Returns the new text and where
 * the caret goes: right after the insert.
 */
export function spliceAtCaret(
  text: string, insert: string, field: TextField | null | undefined,
): { text: string, caret: number } {
  const start = field?.selectionStart ?? text.length
  const end = field?.selectionEnd ?? start
  const before = text.slice(0, start) + insert
  return { text: before + text.slice(end), caret: before.length }
}

/** Focus ``field`` with the caret at ``caret`` once the new text has
 *  rendered (setting the value earlier would move the caret to the end). */
function restoreCaret(field: TextField | null | undefined, caret: number): void {
  if (!field) return
  requestAnimationFrame(() => {
    field.focus()
    field.setSelectionRange(caret, caret)
  })
}

interface BaseProps {
  /** Unique-per-page key identifying this picker instance. Re-used
   *  when toggling so a second click on the same button closes it. */
  openKey: string
  /** Optional CSS class on the trigger button. */
  class?: string
  /** Optional ARIA label override. Defaults to ``"Insert emoji"``. */
  ariaLabel?: string
}

interface SignalTargetProps extends BaseProps {
  target: Signal<string>
  /** The text field bound to ``target``: the emoji lands at its caret. */
  field: RefObject<TextField | null>
  onInsert?: never
}

interface CallbackTargetProps extends BaseProps {
  target?: never
  field?: never
  onInsert: (emoji: string) => void
}

export type EmojiPickButtonProps = SignalTargetProps | CallbackTargetProps

export function EmojiPickButton(props: EmojiPickButtonProps) {
  const isOpen = openFor.value === props.openKey
  // The picker is anchored to this button (``position: fixed``), so the
  // comment sheet's scroll area or a card around it can't cut it off.
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  // An open picker belongs to this mount under this key: close it when
  // the key changes (e.g. the DM composer switching conversations) or
  // the button unmounts, so coming back later never finds it popped
  // open.
  const key = props.openKey
  useEffect(() => () => {
    if (openFor.value === key) openFor.value = null
  }, [key])
  const insert = (emoji: string) => {
    if (props.target) {
      const el = props.field.current
      const next = spliceAtCaret(props.target.value, emoji, el)
      props.target.value = next.text
      restoreCaret(el, next.caret)
    } else {
      props.onInsert(emoji)
    }
  }
  return (
    <div class="sh-emoji-pick-wrap">
      <button
        ref={buttonRef}
        type="button"
        class={`sh-emoji-pick-btn ${props.class ?? ''}`}
        aria-label={props.ariaLabel ?? t('reaction.insert')}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        onClick={() => {
          openFor.value = isOpen ? null : props.openKey
        }}
      >
        😀
      </button>
      {isOpen && (
        <ReactionPicker
          anchor={buttonRef}
          onSelect={(emoji) => insert(emoji)}
          onClose={() => { openFor.value = null }}
        />
      )}
    </div>
  )
}
