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
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks'
import { ReactionPicker } from './ReactionPicker'
import { t } from '@/i18n/i18n'
import { POPOVER_EDGE, visibleArea, type VisibleArea } from '@/utils/popoverPlacement'

const openFor = signal<string | null>(null)

/** Where to nudge an open picker so it stays on screen. */
export interface PickerFit {
  /** Px to move it right (negative: left). */
  shiftX: number
  /** Open below the button: no room above (e.g. a field near the top). */
  below: boolean
}

/**
 * The picker opens above its button, end-aligned with it — on a phone a
 * 300 px popover hanging off a button near the left edge runs off the
 * screen (the post composer's 😀 sits mid-row). Keep it inside the
 * visible width, and open it below the button when it would leave the
 * top of the visible area while there is more room below.
 */
export function fitPicker(
  picker: { left: number, right: number, top: number, height: number },
  button: { top: number, bottom: number },
  area: VisibleArea,
): PickerFit {
  let shiftX = 0
  if (picker.left < POPOVER_EDGE) shiftX = POPOVER_EDGE - picker.left
  else if (picker.right > area.width - POPOVER_EDGE) shiftX = area.width - POPOVER_EDGE - picker.right
  const roomBelow = area.bottom - button.bottom
  const below = picker.top < area.top + POPOVER_EDGE
    && roomBelow > button.top - area.top
  return { shiftX: Math.round(shiftX), below }
}

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
  const wrapRef = useRef<HTMLDivElement>(null)
  // Keep the open picker on screen (see ``fitPicker``). It is laid out
  // end-aligned above the button by CSS; measured once on open.
  useLayoutEffect(() => {
    if (!isOpen) return
    const wrap = wrapRef.current
    const picker = wrap?.querySelector<HTMLElement>('.sh-reaction-picker')
    const button = wrap?.querySelector('.sh-emoji-pick-btn')
    if (!picker || !button) return
    const fit = fitPicker(
      picker.getBoundingClientRect(), button.getBoundingClientRect(), visibleArea(),
    )
    if (fit.shiftX) picker.style.right = `${-fit.shiftX}px`
    if (fit.below) picker.classList.add('sh-reaction-picker--below')
  }, [isOpen])
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
    <div class="sh-emoji-pick-wrap" ref={wrapRef}>
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
