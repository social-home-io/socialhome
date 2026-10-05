/**
 * AliasDialog — viewer-private user rename (§4.1.6).
 *
 * Anyone can set a personal nickname for any other user (local or
 * remote) that only shows in their own view. The alias is never
 * federated and never visible to the renamed user.
 *
 * UX contract:
 *   - Single text input, prefilled with the current alias if any.
 *   - Save button is disabled while empty.
 *   - "Reset to default" button appears only when an alias is set.
 *   - Helper text spells out the resolution priority so users
 *     understand why their alias may not show in some spaces.
 *   - Enter saves; Escape closes (handled by Modal).
 *   - Optimistic update: dispatches the new value via ``onSave`` so
 *     the parent component can update its row immediately.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

interface DialogState {
  open: boolean
  /** target user_id being renamed */
  targetUserId: string
  /** current global display name — shown as the "what they're called" reference */
  globalDisplayName: string
  /** current alias if any — prefills the input */
  currentAlias: string
  /** invoked after a successful save / clear with the new alias (or null) */
  onSave?: (newAlias: string | null) => void
}

const state = signal<DialogState>({
  open: false,
  targetUserId: '',
  globalDisplayName: '',
  currentAlias: '',
})
const inputValue = signal('')
const saving = signal(false)

export function openAliasDialog(opts: {
  targetUserId: string
  globalDisplayName: string
  currentAlias?: string | null
  onSave?: (newAlias: string | null) => void
}) {
  state.value = {
    open: true,
    targetUserId: opts.targetUserId,
    globalDisplayName: opts.globalDisplayName,
    currentAlias: opts.currentAlias ?? '',
    onSave: opts.onSave,
  }
  inputValue.value = opts.currentAlias ?? ''
  saving.value = false
}

function closeDialog() {
  state.value = { ...state.value, open: false }
  inputValue.value = ''
  saving.value = false
}

const ALIAS_MAX_LENGTH = 80

export function AliasDialog() {
  const s = state.value

  useEffect(() => {
    if (!s.open) return
    // Auto-focus + select all on open so a quick edit is one keypress.
    queueMicrotask(() => {
      const input = document.querySelector<HTMLInputElement>('#sh-alias-input')
      input?.focus()
      input?.select()
    })
  }, [s.open, s.targetUserId])

  if (!s.open) return null

  const trimmed = inputValue.value.trim()
  const tooLong = trimmed.length > ALIAS_MAX_LENGTH
  const hasChanges = trimmed !== s.currentAlias.trim()
  const saveDisabled = saving.value || !trimmed || tooLong || !hasChanges

  const save = async () => {
    if (saveDisabled) return
    saving.value = true
    try {
      await api.put(
        `/api/aliases/users/${encodeURIComponent(s.targetUserId)}`,
        { alias: trimmed },
      )
      s.onSave?.(trimmed)
      showToast(t('alias.saved', { name: trimmed }), 'success')
      closeDialog()
    } catch (e: any) {
      saving.value = false
      showToast(e?.message || t('alias.save_failed'), 'error')
    }
  }

  const clear = async () => {
    saving.value = true
    try {
      await api.delete(
        `/api/aliases/users/${encodeURIComponent(s.targetUserId)}`,
      )
      s.onSave?.(null)
      showToast(t('alias.cleared'), 'info')
      closeDialog()
    } catch (e: any) {
      saving.value = false
      showToast(e?.message || t('alias.clear_failed'), 'error')
    }
  }

  return (
    <Modal open={s.open} onClose={closeDialog} title={t('alias.title')}>
      <div class="sh-alias-dialog">
        <p class="sh-alias-dialog-lead">
          {t('alias.lead_before')}<strong>{s.globalDisplayName || s.targetUserId}</strong>{t('alias.lead_after')}
        </p>
        <label class="sh-alias-dialog-label" for="sh-alias-input">
          {t('alias.label')}
        </label>
        <input
          id="sh-alias-input"
          class="sh-alias-dialog-input"
          type="text"
          placeholder={t('alias.placeholder')}
          maxLength={ALIAS_MAX_LENGTH + 1}
          value={inputValue.value}
          onInput={(e) =>
            (inputValue.value = (e.currentTarget as HTMLInputElement).value)
          }
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !saveDisabled) {
              e.preventDefault()
              save()
            }
          }}
          aria-describedby="sh-alias-help"
        />
        <p id="sh-alias-help" class="sh-alias-dialog-help">
          {tooLong
            ? t('alias.too_long', { n: String(ALIAS_MAX_LENGTH) })
            : t('alias.help')}
        </p>
        <div class="sh-alias-dialog-actions">
          {s.currentAlias && (
            <Button
              variant="secondary"
              onClick={clear}
              disabled={saving.value}
            >
              {t('alias.reset')}
            </Button>
          )}
          <div class="sh-alias-dialog-actions-spacer" />
          <Button variant="secondary" onClick={closeDialog}>
            {t('common.cancel')}
          </Button>
          <Button onClick={save} disabled={saveDisabled}>
            {saving.value ? t('alias.saving') : t('alias.save')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
