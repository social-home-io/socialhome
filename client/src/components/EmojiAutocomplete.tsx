/**
 * EmojiAutocomplete — Slack-style ``:shortcode`` dropdown for any
 * ``<input>`` / ``<textarea>``.
 *
 * As the user types ``:smi``, a small popover lists matching emoji.
 * Selecting one (click, Enter, or Tab) replaces the ``:smi`` token
 * with the emoji glyph. Escape closes the popover.
 *
 * Module-level signals
 * keep the popover state global — only one autocomplete is open at a
 * time across the page.
 *
 * Wiring: each text-input owner calls :func:`checkForEmojiTrigger` on
 * every input/keyup event with ``(text, cursorPos, anchorEl)`` and
 * renders one ``<EmojiAutocomplete onSelect={…} />`` somewhere stable
 * (typically next to its input). The ``onSelect`` callback receives
 * the emoji glyph and the byte range to replace, and is responsible
 * for splicing the emoji into its own state.
 */
import { signal } from '@preact/signals'
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks'
import {
  type EmojiEntry,
  searchEmoji,
} from '@/data/emojis'
import {
  clampHorizontally,
  pinFixed,
  placeVertically,
  visibleArea,
} from '@/utils/popoverPlacement'

type SpliceCallback = (emoji: string, range: [number, number]) => void

interface AutocompleteState {
  /** Viewport (``position: fixed``) position of the popover, anchored
   *  to the input's bounding box; ``top`` is its bottom edge when it
   *  opens ``above``. */
  top: number
  left: number
  /** Room between the input and the visible edge on the chosen side —
   *  the list scrolls rather than running behind the keyboard. */
  maxHeight: number
  /** ``'below'`` places the popover under the anchor (the default,
   *  for top-of-page inputs). ``'above'`` flips it above the anchor —
   *  used when the input is near the bottom of the *visible* area (a
   *  chat composer, any field with the phone keyboard up) where a
   *  popover below would render off-screen or behind the keyboard.
   *  ``translateY(-100%)`` anchors the popover's bottom edge at the
   *  stored ``top`` in that case. */
  placement: 'below' | 'above'
  /** ``[startInclusive, endExclusive]`` byte range in the input that
   *  the chosen emoji replaces (the ``:foo`` token, including the
   *  leading colon). */
  range: [number, number]
  /** Currently matching emoji entries (≤ ``MAX_RESULTS``). */
  matches: EmojiEntry[]
  /** Highlighted index for keyboard navigation. */
  active: number
  /** Splice callback the input owner provides at trigger time. The
   *  mounted ``<EmojiAutocomplete>`` calls this on click selection.
   *  Stored here so a single mount can dispatch to whichever input
   *  triggered the popover. */
  splice: SpliceCallback
}

const state = signal<AutocompleteState | null>(null)

const MAX_RESULTS = 8

/** The list's height cap (matches the CSS ``max-height``) and the room
 *  it wants below the input before flipping above. */
const MAX_LIST_HEIGHT = 280
/** Narrowest the list renders (CSS ``min-width``) — for the clamp. */
const MIN_WIDTH = 200

/** Return true while the popover is open. Owners wire keyboard events
 *  through :func:`handleEmojiAutocompleteKey` only when this is true. */
export function isEmojiAutocompleteOpen(): boolean {
  return state.value !== null
}

/** Force the popover closed. Call when the input loses focus or the
 *  user navigates away. */
export function closeEmojiAutocomplete(): void {
  state.value = null
}

/** Inspect ``text`` and ``cursorPos`` for a ``:partial`` token ending
 *  at the cursor. If found, refresh the popover; otherwise close it.
 *
 *  ``anchor`` is the input element used to position the dropdown.
 *  ``splice`` is the callback the popover invokes when the user picks
 *  a match (click, Enter, Tab) — owners pass their own splice fn so
 *  one ``<EmojiAutocomplete>`` mount can serve any number of inputs.
 */
export function checkForEmojiTrigger(
  text: string,
  cursorPos: number,
  anchor: HTMLElement,
  splice: SpliceCallback,
): void {
  const before = text.slice(0, cursorPos)
  // ``:`` followed by 1+ word chars (letters/digits/underscore/+/-),
  // anchored at the cursor. Closing colon is handled separately so
  // ``:smile|`` shows the dropdown even before the user closes it.
  const match = before.match(/(?:^|[^a-zA-Z0-9_])(:([a-zA-Z0-9_+-]{1,30}))$/)
  if (!match) {
    closeEmojiAutocomplete()
    return
  }
  const token = match[1] // includes the leading colon
  const query = match[2]
  const matches = searchEmoji(query, MAX_RESULTS)
  if (matches.length === 0) {
    closeEmojiAutocomplete()
    return
  }
  const rect = anchor.getBoundingClientRect()
  const start = cursorPos - token.length
  // Measured against the visual viewport: with the phone keyboard up
  // the room below the field is often behind the keyboard, so the
  // list flips above it (and is capped to the room it has there).
  const area = visibleArea()
  const v = placeVertically(rect, area, MAX_LIST_HEIGHT)
  state.value = {
    top: v.top,
    left: clampHorizontally(rect.left, MIN_WIDTH, area),
    maxHeight: Math.min(v.maxHeight, MAX_LIST_HEIGHT),
    placement: v.placement,
    range: [start, cursorPos],
    matches,
    active: 0,
    splice,
  }
}

/** Keyboard hook — call from the input's ``onKeyDown``. Returns
 *  ``true`` when the autocomplete consumed the key (caller should
 *  ``preventDefault()`` and skip its own Enter-to-submit). Returns
 *  ``false`` to let the input handle it normally. */
export function handleEmojiAutocompleteKey(e: KeyboardEvent): boolean {
  const s = state.value
  if (s === null) return false
  if (e.key === 'ArrowDown') {
    state.value = { ...s, active: (s.active + 1) % s.matches.length }
    return true
  }
  if (e.key === 'ArrowUp') {
    state.value = {
      ...s,
      active: (s.active - 1 + s.matches.length) % s.matches.length,
    }
    return true
  }
  if (e.key === 'Enter' || e.key === 'Tab') {
    const pick = s.matches[s.active]
    const splice = s.splice
    const range = s.range
    closeEmojiAutocomplete()
    splice(pick.emoji, range)
    return true
  }
  if (e.key === 'Escape') {
    closeEmojiAutocomplete()
    return true
  }
  return false
}

/** No props — the popover dispatches to the splice callback the
 *  triggering input registered via :func:`checkForEmojiTrigger`. */
export function EmojiAutocomplete() {
  const s = state.value
  const ref = useRef<HTMLDivElement | null>(null)
  // Cancel the offset of a transformed / filtered ancestor (the comment
  // sheet) so ``position: fixed`` really means the viewport.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || s === null) return
    pinFixed(el, s.left, s.top, s.placement)
  })
  // Close on outside click. Owners that mount this should still wire
  // ``handleEmojiAutocompleteKey`` for keyboard support.
  const open = s !== null
  useEffect(() => {
    if (!open) return
    const onDocMouseDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (t && t.closest('.sh-emoji-autocomplete')) return
      closeEmojiAutocomplete()
    }
    // Fixed-position popover: a page / list scroll would strand it
    // away from the input, so close (typing re-opens it in place).
    const onScroll = (e: Event) => {
      const t = e.target
      if (t instanceof Element && (t.matches('input, textarea') || t.closest('.sh-emoji-autocomplete'))) return
      closeEmojiAutocomplete()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open])

  if (s === null) return null
  return (
    <div
      ref={ref}
      class="sh-emoji-autocomplete"
      role="listbox"
      style={{
        top: `${s.top}px`,
        left: `${s.left}px`,
        maxHeight: `${s.maxHeight}px`,
        // ``placement: 'above'`` anchors the popover's *bottom* at
        // ``s.top`` instead of its top, so its bottom edge sits just
        // above the input.
        transform: s.placement === 'above' ? 'translateY(-100%)' : 'none',
      }}
    >
      {s.matches.map((entry, idx) => (
        <button
          key={entry.emoji}
          type="button"
          role="option"
          aria-selected={idx === s.active}
          class={
            idx === s.active
              ? 'sh-emoji-autocomplete-row sh-emoji-autocomplete-row--active'
              : 'sh-emoji-autocomplete-row'
          }
          onMouseDown={(e) => {
            e.preventDefault()
            const splice = s.splice
            const range = s.range
            closeEmojiAutocomplete()
            splice(entry.emoji, range)
          }}
        >
          <span class="sh-emoji-autocomplete-glyph">{entry.emoji}</span>
          <span class="sh-emoji-autocomplete-label">
            :{entry.keywords.split(' ')[0]}:
          </span>
        </button>
      ))}
    </div>
  )
}
