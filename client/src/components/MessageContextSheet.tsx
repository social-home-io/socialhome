/**
 * MessageContextSheet — touch-only bottom sheet shown when a DM
 * bubble is long-pressed. Mirrors the WhatsApp / Telegram idiom:
 * a quick-react emoji row pinned at the top, then Reply / Copy /
 * Delete entries below.
 *
 * Keyboard / hover users get the inline ``.sh-message-reply-btn``
 * chip on hover; this sheet is the touch-equivalent so the always-
 * on chip can stay out of the way on phones.
 */
import { useEffect, useRef } from 'preact/hooks'
import { t } from '@/i18n/i18n'

export interface ContextSheetAction {
  label: string
  glyph: string
  destructive?: boolean
  onClick: () => void
}

const QUICK_EMOJI = ['👍', '❤️', '😂', '😮', '😢', '🙏']

interface Props {
  /** A quick reaction tapped. Absent → no reaction row (a read-only
   *  thread, e.g. an archived space's chat). */
  onReact?: (emoji: string) => void
  /** "+" tapped — open the full emoji picker. */
  onPickMore?: () => void
  actions: ContextSheetAction[]
  onClose: () => void
}

export function MessageContextSheet(props: Props) {
  const sheetRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') props.onClose()
    }
    document.addEventListener('keydown', onKey)
    sheetRef.current?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  return (
    <div class="sh-context-sheet-backdrop" onClick={props.onClose}>
      <div
        ref={sheetRef}
        class="sh-context-sheet"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        {props.onReact && (
        <div class="sh-context-sheet__quick" role="group" aria-label={t('dms.context.quick_reactions')}>
          {QUICK_EMOJI.map(em => (
            <button
              key={em}
              type="button"
              class="sh-context-sheet__emoji"
              aria-label={t('dms.context.react_with', { emoji: em })}
              onClick={() => { props.onReact?.(em); props.onClose() }}
            >
              {em}
            </button>
          ))}
          <button
            type="button"
            class="sh-context-sheet__emoji sh-context-sheet__more"
            aria-label={t('dms.context.more_emoji')}
            onClick={() => { props.onPickMore?.(); props.onClose() }}
          >
            +
          </button>
        </div>
        )}
        <ul class="sh-context-sheet__actions">
          {props.actions.map(a => (
            <li key={a.label}>
              <button
                type="button"
                class={
                  'sh-context-sheet__action'
                  + (a.destructive ? ' sh-context-sheet__action--destructive' : '')
                }
                onClick={() => { a.onClick(); props.onClose() }}
              >
                <span aria-hidden="true">{a.glyph}</span>
                <span>{a.label}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
