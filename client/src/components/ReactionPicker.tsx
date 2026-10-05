/**
 * ReactionPicker — emoji reaction selection (§23.45).
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { t } from '@/i18n/i18n'
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
}

const search = signal('')

export function ReactionPicker({ onSelect, onClose, inline = false }: ReactionPickerProps) {
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
    // Escape also closes, matching every other modal-ish surface.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div class={`sh-reaction-picker${inline ? ' sh-reaction-picker--inline' : ''}`}
      onClick={(e) => e.stopPropagation()}>
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
