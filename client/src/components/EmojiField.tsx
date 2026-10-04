/**
 * EmojiField — single-emoji "icon" picker for spaces (and any other
 * surface that stores one emoji as an icon).
 *
 * Replaces the bare ``<input maxLength={2}>`` that made setting a space
 * icon awkward — there was no way to *search* an emoji by name and no
 * visual *chooser*; a desktop user had to know an OS emoji shortcut and
 * the 2-char cap silently truncated multi-codepoint emoji.
 *
 * Instead this renders a tappable preview tile next to a short hint.
 * Tapping opens the shared :class:`ReactionPicker` **inline** (in flow,
 * not an absolute popover) so it can't be clipped by the create-space
 * modal's ``overflow: auto`` scroll box. The picker gives both paths the
 * user asked for: a search box (type "house" → 🏠 — "writing") and a
 * visual grid + frequent row (the "chooser"). Selecting *sets* the value
 * (single emoji, no truncation); "Remove" clears it back to the default.
 *
 * Bound to a ``Signal<string>`` so it drops into both the module-signal
 * create dialog and the ``useSignal`` settings form unchanged.
 */
import { signal, type Signal } from '@preact/signals'
import { ReactionPicker } from './ReactionPicker'
import { t } from '@/i18n/i18n'

/** At most one EmojiField picker open per page (keyed by ``openKey``). */
const openFor = signal<string | null>(null)

interface EmojiFieldProps {
  /** The emoji value, two-way bound. Empty string = no icon set. */
  value: Signal<string>
  /** Unique-per-page key so a second tap on the same tile closes it
   *  and two fields can't both be open. */
  openKey: string
  /** Visible field label. Defaults to the translated ``"Icon"``. */
  label?: string
  /** Optional helper line under the label. */
  hint?: string
}

export function EmojiField({ value, openKey, label, hint }: EmojiFieldProps) {
  const isOpen = openFor.value === openKey
  const has = value.value !== ''

  const toggle = () => {
    openFor.value = isOpen ? null : openKey
  }
  const choose = (emoji: string) => {
    value.value = emoji
    openFor.value = null
  }
  const clear = () => {
    value.value = ''
    openFor.value = null
  }

  return (
    <div class="sh-emoji-field">
      <span class="sh-emoji-field-label">{label ?? t('emoji_field.label')}</span>
      <div class="sh-emoji-field-row">
        <button
          type="button"
          class={`sh-emoji-field-tile${has ? '' : ' is-empty'}`}
          aria-haspopup="dialog"
          aria-expanded={isOpen}
          aria-label={has ? t('emoji_field.change_aria', { emoji: value.value }) : t('emoji_field.choose_aria')}
          onClick={toggle}
        >
          {has
            ? <span class="sh-emoji-field-glyph">{value.value}</span>
            : <span class="sh-emoji-field-plus" aria-hidden="true">+</span>}
        </button>
        <div class="sh-emoji-field-meta">
          <span class="sh-emoji-field-hint">
            {hint ?? (has ? t('emoji_field.hint_change') : t('emoji_field.hint_pick'))}
          </span>
          {has && (
            <button type="button" class="sh-emoji-field-clear" onClick={clear}>
              {t('emoji_field.remove')}
            </button>
          )}
        </div>
      </div>
      {isOpen && (
        <ReactionPicker
          inline
          onSelect={choose}
          onClose={() => { openFor.value = null }}
        />
      )}
    </div>
  )
}
