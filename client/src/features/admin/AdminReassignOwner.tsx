/**
 * Admin › Spaces › "Choose a new owner" — for a space hosted here whose
 * owner has left the household (account deactivated or deleted). While the
 * owner is still here they hand the space over themselves in its settings,
 * so the row offers nothing then (the server refuses with OWNER_ACTIVE).
 *
 * Lists the space's local full members (``GET /api/admin/spaces/{id}/
 * ownership``), asks to confirm, then ``POST``s the pick. The departed
 * owner's seat drops to a plain member.
 */
import { useEffect, useId, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

interface Candidate { user_id: string; display_name: string; role: string }

export function AdminReassignOwner({ spaceId, spaceName, open, onClose, onDone }: {
  spaceId: string
  spaceName: string
  open: boolean
  onClose: () => void
  onDone: () => void
}) {
  const [rows, setRows] = useState<Candidate[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const selectId = useId()

  const load = () => {
    setFailed(false)
    setRows(null)
    api.get(`/api/admin/spaces/${spaceId}/ownership`)
      .then((b: { candidates?: Candidate[] }) => setRows(Array.isArray(b?.candidates) ? b.candidates : []))
      .catch(() => setFailed(true))
  }
  useEffect(() => { if (open) { setPick(''); load() } }, [open, spaceId])

  const chosen = rows?.find(r => r.user_id === pick) ?? null

  const submit = async () => {
    if (!chosen) return
    const ok = await confirmDialog(
      t('admin.spaces.reassign_confirm', { name: chosen.display_name, space: spaceName }),
      { title: t('admin.spaces.reassign_title'), confirmLabel: t('space.transfer.confirm_ok') },
    )
    if (!ok) return
    setBusy(true)
    try {
      await api.post(`/api/admin/spaces/${spaceId}/ownership`, { to_user_id: chosen.user_id })
      showToast(t('space.transfer.done', { name: chosen.display_name, space: spaceName }), 'success')
      onDone()
      onClose()
    } catch (err: unknown) {
      showToast((err as Error).message || t('space.transfer.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={t('admin.spaces.reassign_title')}>
      <div class="sh-owner-transfer" data-testid="admin-reassign">
        <p class="sh-muted sh-owner-transfer__intro">
          {t('admin.spaces.reassign_intro', { space: spaceName })}
        </p>
        {failed && (
          <p class="sh-form-error" role="alert">
            {t('space.transfer.load_failed')}{' '}
            <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
          </p>
        )}
        {rows !== null && rows.length === 0 && (
          <p class="sh-muted" data-testid="admin-reassign-empty">{t('admin.spaces.reassign_nobody')}</p>
        )}
        {rows !== null && rows.length > 0 && (
          <div class="sh-owner-transfer__row">
            <label for={selectId} class="sh-owner-transfer__label">{t('space.transfer.label')}</label>
            <select id={selectId} class="sh-select" value={pick}
                    onChange={(e) => setPick((e.target as HTMLSelectElement).value)}>
              <option value="">{t('space.transfer.placeholder')}</option>
              {rows.map(r => <option key={r.user_id} value={r.user_id}>{r.display_name}</option>)}
            </select>
            <Button disabled={!chosen} loading={busy} onClick={() => void submit()}>
              {t('space.transfer.button')}
            </Button>
          </div>
        )}
      </div>
    </Modal>
  )
}
