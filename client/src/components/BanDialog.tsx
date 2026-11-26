/**
 * BanDialog — the one confirm for banning a person or a whole household
 * from a space (P7b; ban UX review, P8).
 *
 * It says who and what happens: "Ban {name}?" / "Ban {household}?", the
 * people a household ban removes and how many, that nobody banned can come
 * back through an invite, a join or a follow until the ban is lifted, and
 * where to lift it. An optional reason (owner decision A, 2026-11-26) is
 * stored with the ban and seen only by the owner and admins. A refusal
 * stays in the dialog (translated, never the server's English), and the
 * button can't be pressed twice.
 *
 * Opened from the member sheet, the bans list in Settings › Moderation
 * (a banned person's household) and a pending join request (owner
 * decision B): a household needs no current seat to be banned.
 */
import { useEffect, useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { isOne, t } from '@/i18n/i18n'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'

/** The server's cap on a ban's reason (``MAX_BAN_REASON_CHARS``). */
export const BAN_REASON_MAX = 500

export interface BanTarget {
  kind: 'person' | 'household'
  spaceId: string
  /** A person ban. */
  userId?: string
  /** A household ban. */
  instanceId?: string
  /** The person's name (person ban) — shown in the title and toast. */
  name?: string
  /** The household's name, when known. */
  household?: string | null
  /** A household ban: the names of its people seated now (may be empty). */
  people?: string[]
}

/** A refused ban / unban in the viewer's words — the network included. */
export function banErrorCopy(e: unknown): string {
  if (e instanceof ApiError) return e.message || t('member_actions.failed')
  return t('error.network')
}

function householdLabel(target: BanTarget): string {
  return target.household || t('member_actions.this_household')
}

function body(target: BanTarget): string {
  if (target.kind === 'person') {
    return t('ban.body_person', { name: target.name ?? t('person.someone') })
  }
  const household = householdLabel(target)
  const people = target.people ?? []
  if (people.length === 0) return t('ban.body_household', { household })
  return t(isOne(people.length) ? 'ban.body_household_people_one' : 'ban.body_household_people', {
    household,
    people: people.join(', '),
    n: String(people.length),
  })
}

export function BanDialog({ target, onClose, onBanned }: {
  /** ``null`` = closed. */
  target: BanTarget | null
  onClose: () => void
  onBanned: () => void
}) {
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // A new target starts clean.
  useEffect(() => { setReason(''); setError(null); setBusy(false) }, [target])
  if (!target) return null
  const household = target.kind === 'household'
  const title = household
    ? t('ban.title_household', { household: householdLabel(target) })
    : t('ban.title_person', { name: target.name ?? t('person.someone') })

  const confirm = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    const why = reason.trim().slice(0, BAN_REASON_MAX)
    try {
      await api.post(`/api/spaces/${target.spaceId}/ban`, {
        ...(household ? { instance_id: target.instanceId } : { user_id: target.userId }),
        ...(why ? { reason: why } : {}),
      })
      showToast(household
        ? t('ban.done_household', { household: householdLabel(target) })
        : t('ban.done_person', { name: target.name ?? t('person.someone') }), 'info')
      onBanned()
    } catch (e: unknown) {
      setError(banErrorCopy(e))
    } finally {
      setBusy(false)
    }
  }

  const reasonId = `sh-ban-reason-${target.spaceId}`
  return (
    <Modal open={true} onClose={onClose} title={title} class="sh-ban-dialog">
      <p class="sh-confirm-message">{body(target)}</p>
      <label class="sh-ban-dialog__reason" for={reasonId}>
        {t('ban.reason_label')}
        <textarea id={reasonId} rows={2} maxLength={BAN_REASON_MAX} value={reason}
                  aria-describedby={`${reasonId}-hint`}
                  onInput={(e) => setReason((e.target as HTMLTextAreaElement).value)} />
      </label>
      <p class="sh-muted sh-ban-dialog__hint" id={`${reasonId}-hint`}>{t('ban.reason_hint')}</p>
      {error && <p class="sh-member-actions-error" role="alert">{error}</p>}
      <div class="sh-confirm-actions">
        <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
        <Button variant="danger" loading={busy} disabled={busy} data-action="ban-confirm"
                onClick={() => void confirm()}>
          {household ? t('member_actions.ban_household') : t('member_actions.ban')}
        </Button>
      </div>
    </Modal>
  )
}
