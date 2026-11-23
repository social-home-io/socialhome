/**
 * ReactionPicker — emoji reaction selection (§23.45).
 */
import type { ComponentChildren, RefObject } from 'preact'
import { signal } from '@preact/signals'
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { pinFixed, placeAnchored, visibleArea } from '@/utils/popoverPlacement'
import {
  ALL_EMOJI,
  ALL_EMOJI_WITH_KEYWORDS,
  FREQUENT_EMOJI,
  emojiMatches,
} from '@/data/emojis'

interface ReactionPickerProps {
  onSelect: (emoji: string) => void
  onClose: () => void
  /** Render in document flow (static, full-width) instead of as an
   *  absolutely-positioned popover. Used by :class:`EmojiField` inside
   *  scroll-clipping containers (the create-space modal, settings form)
   *  where an absolute popover would be clipped by ``overflow: auto``. */
  inline?: boolean
  /** Extra choices above the search (a space's icon field: the Home
   *  Assistant logo). Inside the panel, so pressing one doesn't count
   *  as an outside press that closes the picker. */
  header?: ComponentChildren
  /** The button that opened the picker. Set → the picker is
   *  ``position: fixed`` next to it (below when it fits, else on the side
   *  with more room, height capped to that room, kept inside the visible
   *  width) and follows it on scroll / resize. Fixed, so no scrolling
   *  sheet or ``overflow: hidden`` card around the button can clip it.
   *  Escape hands focus back to it. */
  anchor?: RefObject<HTMLElement | null>
  /** Line the picker up with the anchor's ``end`` (default) or ``start``. */
  align?: 'start' | 'end'
}

const search = signal('')

export function ReactionPicker(
  { onSelect, onClose, inline = false, header, anchor, align = 'end' }: ReactionPickerProps,
) {
  const root = useRef<HTMLDivElement | null>(null)
  const filtered = search.value
    ? ALL_EMOJI_WITH_KEYWORDS.filter(e => emojiMatches(e, search.value)).map(e => e.emoji)
    : ALL_EMOJI

  // Outside-click → close. Ignores clicks on the picker itself AND
  // on the trigger button that opened the picker — the trigger
  // already toggles open/close on its own ``onClick`` and double-
  // firing here would either flicker the picker or leave it stuck
  // open after a re-toggle. Triggers identify themselves via
  // ``aria-haspopup="dialog"`` (set by :class:`EmojiPickButton` and
  // the reaction-add button on post cards).
  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null
      if (!t) return
      if (t.closest('.sh-reaction-picker')) return
      if (t.closest('[aria-haspopup="dialog"]')) return
      onClose()
    }
    // Escape closes the picker — only the picker: caught on the way down
    // (window, capture) so the sheet or dialog around it, which listens
    // for Escape on ``document``, stays open.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      e.preventDefault()
      onClose()
      anchor?.current?.focus()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    window.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [onClose, anchor])

  // Anchored: place next to the button, and again whenever the page,
  // a sheet or the visual viewport (phone keyboard) moves.
  useLayoutEffect(() => {
    if (!anchor) return
    const place = () => {
      const el = root.current
      const button = anchor.current
      if (!el || !button) return
      // Measure the picker at its natural size, then fit it.
      el.style.maxHeight = ''
      el.style.width = ''
      el.classList.remove('sh-reaction-picker--above')
      const natural = el.getBoundingClientRect()
      const p = placeAnchored(
        button.getBoundingClientRect(),
        { width: natural.width, height: natural.height },
        visibleArea(),
        align,
      )
      if (p.width < natural.width) el.style.width = `${p.width}px`
      el.style.maxHeight = `${p.maxHeight}px`
      el.classList.toggle('sh-reaction-picker--above', p.placement === 'above')
      pinFixed(el, p.left, p.top, p.placement)
    }
    // Scrolling the emoji grid itself doesn't move the picker.
    const onScroll = (e: Event) => {
      if (e.target instanceof Node && root.current?.contains(e.target)) return
      place()
    }
    place()
    const vv = window.visualViewport
    window.addEventListener('resize', place)
    document.addEventListener('scroll', onScroll, true)
    vv?.addEventListener('resize', place)
    vv?.addEventListener('scroll', place)
    return () => {
      window.removeEventListener('resize', place)
      document.removeEventListener('scroll', onScroll, true)
      vv?.removeEventListener('resize', place)
      vv?.removeEventListener('scroll', place)
    }
  }, [anchor, align])

  return (
    <div ref={root}
      class={`sh-reaction-picker${inline ? ' sh-reaction-picker--inline' : ''}${anchor ? ' sh-reaction-picker--anchored' : ''}`}
      onClick={(e) => e.stopPropagation()}>
      {header}
      <div class="sh-reaction-picker-header">
        <input class="sh-reaction-search" placeholder={t('reaction.search')}
          value={search.value}
          onInput={(e) => search.value = (e.target as HTMLInputElement).value} />
        <button
          type="button"
          class="sh-reaction-close"
          aria-label={t('reaction.close')}
          onClick={onClose}
        >✕</button>
      </div>
      <div class="sh-reaction-frequent">
        {FREQUENT_EMOJI.map(e => (
          <button key={e} type="button" class="sh-emoji-btn" onClick={() => { onSelect(e); onClose() }}>
            {e}
          </button>
        ))}
      </div>
      <div class="sh-reaction-grid">
        {filtered.map(e => (
          <button key={e} type="button" class="sh-emoji-btn" onClick={() => { onSelect(e); onClose() }}>
            {e}
          </button>
        ))}
      </div>
    </div>
  )
}
