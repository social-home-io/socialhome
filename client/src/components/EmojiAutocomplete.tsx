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
import { useEffect } from 'preact/hooks'
import {
  type EmojiEntry,
  searchEmoji,
} from '@/data/emojis'

type SpliceCallback = (emoji: string, range: [number, number]) => void

interface AutocompleteState {
  /** Pixel position of the popover (anchored to the input's bounding box). */
  top: number
  left: number
  /** ``'below'`` places the popover under the anchor (the default,
   *  for top-of-page inputs). ``'above'`` flips it above the anchor —
   *  used when the input is near the viewport bottom (chat composer)
   *  where a popover below would render off-screen. CSS handles the
   *  ``translateY(-100%)`` to anchor the popover's bottom edge at the
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

/** Upper bound on the popover height (8 rows × ~40 px row height +
 *  borders). Used at trigger time to decide whether the popover fits
 *  below the input; if not, we flip it above. Generous so we never
 *  end up with a half-clipped popover even when results are dense. */
const ESTIMATED_POPOVER_HEIGHT = 320

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
  // Flip the popover above the input when there isn't room below — the
  // DM-thread composer is pinned to the visual viewport bottom, so the
  // default "open downward" placement renders the popover off-screen.
  const wouldOverflowBelow =
    rect.bottom + ESTIMATED_POPOVER_HEIGHT + 8 > window.innerHeight
  state.value = {
    top: wouldOverflowBelow
      ? rect.top + window.scrollY - 4
      : rect.bottom + window.scrollY + 4,
    left: rect.left + window.scrollX,
    placement: wouldOverflowBelow ? 'above' : 'below',
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
  // Close on outside click. Owners that mount this should still wire
  // ``handleEmojiAutocompleteKey`` for keyboard support.
  useEffect(() => {
    if (s === null) return
    const onDocMouseDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (t && t.closest('.sh-emoji-autocomplete')) return
      closeEmojiAutocomplete()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [s !== null])

  if (s === null) return null
  return (
    <div
      class="sh-emoji-autocomplete"
      role="listbox"
      style={{
        top: `${s.top}px`,
        left: `${s.left}px`,
        // ``placement: 'above'`` anchors the popover's *bottom* at
        // ``s.top`` instead of its top, so its bottom edge sits just
        // above the input — preventing the chat-composer-pinned-to-
        // viewport-bottom case from rendering the popover off-screen.
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
