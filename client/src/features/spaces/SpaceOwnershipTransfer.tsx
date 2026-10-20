/**
 * Hand a space to someone else — the owner's own way out (§23.48).
 *
 * Ownership is a person inside the household that hosts the space: the
 * space's keys, the owner statement and the signed GFS list belong to
 * that household and never move. So the new owner is picked from the
 * LOCAL members (no follower, nobody from another household — the server
 * refuses both too), and the outgoing owner stays on as an admin.
 *
 * Owner-only: the caller renders it only for the owner; the server answers
 * anyone else with 403.
 */
import { useEffect, useId, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { personLabel } from '@/utils/names'

interface RosterRow {
  user_id: string
  role: string
  display_name?: string | null
  space_display_name?: string | null
  /** Set on a member seated from another household. */
  instance_id?: string | null
}

/** Roles that can take ownership: anyone with a full seat. */
const CAN_OWN = new Set(['admin', 'moderator', 'member'])

/** The roster rows that may become owner, and whether anyone from another
 *  household sits in the space (they can't). Exported for tests. */
export function ownerCandidates(
  rows: RosterRow[], selfId: string | null,
): { candidates: RosterRow[]; hasRemote: boolean } {
  const local = rows.filter(r => !r.instance_id)
  return {
    candidates: local.filter(r => r.user_id !== selfId && CAN_OWN.has(r.role)),
    hasRemote: rows.some(r => Boolean(r.instance_id)),
  }
}

function nameOf(row: RosterRow): string {
  return personLabel(row.space_display_name, row.display_name)
}

interface Props {
  spaceId: string
  spaceName: string
  /** Called after the server took the transfer (the viewer is now admin). */
  onTransferred: () => void
}

export function SpaceOwnershipTransfer({ spaceId, spaceName, onTransferred }: Props) {
  const [rows, setRows] = useState<RosterRow[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const selectId = useId()

  const load = () => {
    setFailed(false)
    api.get(`/api/spaces/${spaceId}/members`)
      .then((body: RosterRow[]) => setRows(Array.isArray(body) ? body : []))
      .catch(() => setFailed(true))
  }
  useEffect(load, [spaceId])

  const { candidates, hasRemote } = ownerCandidates(
    rows ?? [], currentUser.value?.user_id ?? null,
  )
  const chosen = candidates.find(c => c.user_id === pick) ?? null

  const transfer = async () => {
    if (!chosen) return
    const name = nameOf(chosen)
    const ok = await confirmDialog(
      t('space.transfer.confirm_body', { name, space: spaceName }),
      {
        title: t('space.transfer.confirm_title', { name }),
        confirmLabel: t('space.transfer.confirm_ok'),
      },
    )
    if (!ok) return
    setBusy(true)
    try {
      await api.post(`/api/spaces/${spaceId}/ownership`, { to_user_id: chosen.user_id })
      showToast(t('space.transfer.done', { name, space: spaceName }), 'success')
      onTransferred()
    } catch (err: unknown) {
      showToast((err as Error).message || t('space.transfer.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-owner-transfer" id="space-owner-transfer" data-testid="owner-transfer">
      <p class="sh-muted sh-owner-transfer__intro">{t('space.transfer.intro')}</p>
      {failed && (
        <p class="sh-form-error" role="alert">
          {t('space.transfer.load_failed')}{' '}
          <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
        </p>
      )}
      {rows !== null && candidates.length === 0 && (
        <p class="sh-muted" data-testid="owner-transfer-empty">{t('space.transfer.nobody')}</p>
      )}
      {candidates.length > 0 && (
        <div class="sh-owner-transfer__row">
          <label for={selectId} class="sh-owner-transfer__label">
            {t('space.transfer.label')}
          </label>
          <select
            id={selectId}
            class="sh-select"
            value={pick}
            onChange={(e) => setPick((e.target as HTMLSelectElement).value)}
          >
            <option value="">{t('space.transfer.placeholder')}</option>
            {candidates.map(c => (
              <option key={c.user_id} value={c.user_id}>{nameOf(c)}</option>
            ))}
          </select>
          <Button variant="secondary" disabled={!chosen} loading={busy} onClick={transfer}>
            {t('space.transfer.button')}
          </Button>
        </div>
      )}
      {hasRemote && (
        <p class="sh-muted sh-owner-transfer__note" data-testid="owner-transfer-remote-note">
          {t('space.transfer.other_households')}
        </p>
      )}
    </div>
  )
}
