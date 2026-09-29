/**
 * KeyboardShortcutsDialog — the ``?`` help sheet.
 *
 * Opened by the ``?`` key (``lib/shortcuts.ts``) or the sidebar
 * "Keyboard shortcuts" entry. Composes on :class:`Modal`, which owns
 * the focus trap, Escape-to-close and focus restore.
 */
import { Fragment } from 'preact'
import { shortcutsHelpOpen } from '@/lib/shortcuts'
import { t } from '@/i18n/i18n'
import { Modal } from './Modal'

function isApple(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent)
}

interface Row {
  keys: string[]
  label: string
}

export function KeyboardShortcutsDialog() {
  if (!shortcutsHelpOpen.value) return null
  const mod = isApple() ? '⌘' : 'Ctrl'
  const rows: Row[] = [
    { keys: [mod, 'K'], label: t('shortcuts.quick_switcher') },
    { keys: ['/'], label: t('shortcuts.search') },
    { keys: ['N'], label: t('shortcuts.composer') },
    { keys: ['?'], label: t('shortcuts.help') },
    { keys: ['Esc'], label: t('shortcuts.close') },
  ]
  return (
    <Modal
      open
      title={t('shortcuts.title')}
      onClose={() => { shortcutsHelpOpen.value = false }}
    >
      <dl class="sh-shortcuts">
        {rows.map(r => (
          <div class="sh-shortcuts__row" key={r.label}>
            <dt class="sh-shortcuts__keys">
              {r.keys.map((k, i) => (
                <Fragment key={k}>
                  {i > 0 && <span class="sh-shortcuts__plus" aria-hidden="true">+</span>}
                  <kbd class="sh-kbd">{k}</kbd>
                </Fragment>
              ))}
            </dt>
            <dd class="sh-shortcuts__label">{r.label}</dd>
          </div>
        ))}
      </dl>
      <p class="sh-muted sh-shortcuts__note">{t('shortcuts.typing_note')}</p>
    </Modal>
  )
}
