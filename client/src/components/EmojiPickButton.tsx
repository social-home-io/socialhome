/**
 * EmojiPickButton — 😀 button that opens the full :class:`ReactionPicker`
 * popover and inserts the chosen emoji into a target text input.
 *
 * Two flavours:
 *
 * 1. Signal-target — pass ``target: Signal<string>`` and the chosen
 *    emoji is appended to the signal value. Used by :mod:`CommentThread`
 *    where the input is bound to a module-level signal.
 *
 * 2. Callback-target — pass ``onInsert: (emoji) => void`` for owners
 *    that need to splice into a richer state shape (e.g. the
 *    composer's textarea ref + ``content`` signal, where insertion
 *    should land at the caret rather than at the end).
 *
 * A module-level ``openFor`` signal keeps at most one picker open
 * across the whole page; callers pass an ``openKey`` string so each
 * mount instance can claim its own picker without colliding with
 * other inputs.
 */
import { signal, type Signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { ReactionPicker } from './ReactionPicker'
import { t } from '@/i18n/i18n'

const openFor = signal<string | null>(null)

interface BaseProps {
  /** Unique-per-page key identifying this picker instance. Re-used
   *  when toggling so a second click on the same button closes it. */
  openKey: string
  /** Optional CSS class on the trigger button. */
  className?: string
  /** Optional ARIA label override. Defaults to ``"Insert emoji"``. */
  ariaLabel?: string
}

interface SignalTargetProps extends BaseProps {
  target: Signal<string>
  onInsert?: never
}

interface CallbackTargetProps extends BaseProps {
  target?: never
  onInsert: (emoji: string) => void
}

export type EmojiPickButtonProps = SignalTargetProps | CallbackTargetProps

export function EmojiPickButton(props: EmojiPickButtonProps) {
  const isOpen = openFor.value === props.openKey
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
      props.target.value = props.target.value + emoji
    } else {
      props.onInsert(emoji)
    }
  }
  return (
    <div class="sh-emoji-pick-wrap">
      <button
        type="button"
        class={`sh-emoji-pick-btn ${props.className ?? ''}`}
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
          onSelect={(emoji) => insert(emoji)}
          onClose={() => { openFor.value = null }}
        />
      )}
    </div>
  )
}
