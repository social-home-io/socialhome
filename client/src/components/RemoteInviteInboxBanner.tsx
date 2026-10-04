/**
 * RemoteInviteInboxBanner — surfaces pending inbound private-space
 * invitations from other households (§D1b). Sits at the top of the
 * space list so a user who just received one doesn't miss it.
 *
 * One-click Accept consumes the invite token and seats the user as a
 * remote member of the host's private space. Decline tells the host
 * via ``SPACE_PRIVATE_INVITE_DECLINE``.
 *
 * UX notes:
 *
 * * The inviter's household is shown by its human-readable display
 *   name (looked up against ``/api/friends``) — the raw
 *   ``inviter_instance_id`` hex would be unreadable and the user
 *   couldn't tell *who* is inviting them. When the lookup misses
 *   (peer-directory hasn't synced yet, or the inviter isn't on a
 *   confirmed household), we fall back to a short hash so the
 *   distinguishing prefix is still visible.
 * * Accept + Decline are disabled while either is in flight on the
 *   same invite. Mesh-routed accepts can take 5-10 s end-to-end
 *   (discovery probe + SPACE_ROUTED forward leg + ACK leg) — without
 *   the disable, an impatient user can double-click Accept or click
 *   Decline mid-Accept, racing the backend.
 * * Decline is a two-step confirm (click → "Confirm decline" → click
 *   again). A single misclick used to fire SPACE_PRIVATE_INVITE_DECLINE
 *   and remove the invitation row with no undo; the second-click
 *   pattern is the lightest-weight way to add a guard without
 *   shipping a full confirm dialog. The confirm state auto-clears
 *   after 4 s so a paused user doesn't accidentally confirm later.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import type { LocalInvite, RemoteInvite } from '@/types'

interface HouseholdRow {
  instance_id: string
  display_name?: string
  local_alias?: string | null
  federated_display_name?: string | null
}

interface FriendsResponse {
  households?: HouseholdRow[]
}

type InFlightState = Record<string, 'accept' | 'decline' | null>
type ConfirmState = Record<string, boolean>

const CONFIRM_RESET_MS = 4000

/** "from {name}" with the name in bold — split on the placeholder so
 *  each language keeps its own word order. */
function FromLine({ name }: { name: string }) {
  const [before, after = ''] = t('remote_invite.from').split('{name}')
  return (
    <span class="sh-muted">
      {' '}{before}<strong>{name}</strong>{after}
    </span>
  )
}

export function RemoteInviteInboxBanner() {
  const [invites, setInvites] = useState<RemoteInvite[]>([])
  const [localInvites, setLocalInvites] = useState<LocalInvite[]>([])
  const [households, setHouseholds] = useState<Map<string, HouseholdRow>>(
    () => new Map(),
  )
  const [inFlight, setInFlight] = useState<InFlightState>({})
  const [confirming, setConfirming] = useState<ConfirmState>({})

  const load = async () => {
    // Run the lookups in parallel — they share no data and the
    // banner needs them all before it can render a meaningful row.
    const [invitesRes, localRes, friendsRes] = await Promise.allSettled([
      api.get('/api/remote_invites'),
      api.get('/api/local_invites'),
      api.get('/api/friends'),
    ])

    if (invitesRes.status === 'fulfilled') {
      setInvites(invitesRes.value as RemoteInvite[])
    } else {
      setInvites([])
    }
    if (localRes.status === 'fulfilled') {
      setLocalInvites(localRes.value as LocalInvite[])
    } else {
      setLocalInvites([])
    }
    if (friendsRes.status === 'fulfilled') {
      const hs = (friendsRes.value as FriendsResponse).households || []
      const map = new Map<string, HouseholdRow>()
      for (const h of hs) {
        if (h.instance_id) map.set(h.instance_id, h)
      }
      setHouseholds(map)
    }
    // Local invites attribute by ``invited_by`` user_id — the helper
    // resolves it to a display name via the cached household users.
    void loadHouseholdUsers()
  }

  useEffect(() => {
    void load()
    // ``space.local_invite_received`` (Pascal's local-invite flow)
    // fires the moment the admin clicks Invite, so we refresh
    // without waiting on the next navigation.
    const off = ws.on('space.local_invite_received', () => void load())
    return () => off()
  }, [])

  if (invites.length === 0 && localInvites.length === 0) return null

  const householdLabel = (instance_id: string): string => {
    const row = households.get(instance_id)
    if (row) {
      // local alias wins over federated name (admin renamed the
      // household locally) — same precedence the Friends page uses.
      const name = row.local_alias || row.display_name || row.federated_display_name
      if (name) return name
    }
    // Lookup miss — peer-directory hasn't synced yet, or the inviter
    // isn't on any confirmed household. Show a short hash so two
    // distinct instances still look distinct.
    return `${instance_id.slice(0, 8)}…`
  }

  const decide = async (
    invite: RemoteInvite,
    decision: 'accept' | 'decline',
  ) => {
    setInFlight((prev) => ({ ...prev, [invite.invite_token]: decision }))
    try {
      await api.post(
        `/api/remote_invites/${invite.invite_token}/${decision}`, {},
      )
      showToast(
        decision === 'accept' ? t('remote_invite.accepted') : t('remote_invite.declined'),
        decision === 'accept' ? 'success' : 'info',
      )
      await load()
    } catch (exc) {
      showToast((exc as Error).message, 'error')
    } finally {
      setInFlight((prev) => ({ ...prev, [invite.invite_token]: null }))
      // Drop the confirm-pending flag too in case decline failed —
      // user can retry from a clean slate.
      setConfirming((prev) => ({ ...prev, [invite.invite_token]: false }))
    }
  }

  const decideLocal = async (
    invite: LocalInvite,
    decision: 'accept' | 'decline',
  ) => {
    setInFlight((prev) => ({ ...prev, [invite.invitation_id]: decision }))
    try {
      await api.post(
        `/api/local_invites/${invite.invitation_id}/${decision}`, {},
      )
      showToast(
        decision === 'accept' ? t('remote_invite.accepted') : t('remote_invite.declined'),
        decision === 'accept' ? 'success' : 'info',
      )
      await load()
    } catch (exc) {
      showToast((exc as Error).message, 'error')
    } finally {
      setInFlight((prev) => ({ ...prev, [invite.invitation_id]: null }))
      setConfirming((prev) => ({ ...prev, [invite.invitation_id]: false }))
    }
  }

  const handleDeclineLocal = (invite: LocalInvite) => {
    if (confirming[invite.invitation_id]) {
      void decideLocal(invite, 'decline')
      return
    }
    setConfirming((prev) => ({ ...prev, [invite.invitation_id]: true }))
    setTimeout(() => {
      setConfirming((prev) => ({ ...prev, [invite.invitation_id]: false }))
    }, CONFIRM_RESET_MS)
  }

  const handleDecline = (invite: RemoteInvite) => {
    if (confirming[invite.invite_token]) {
      void decide(invite, 'decline')
      return
    }
    setConfirming((prev) => ({ ...prev, [invite.invite_token]: true }))
    // Reset the confirm flag after a short window so a user who got
    // distracted doesn't accidentally fire decline on their next
    // click two minutes later.
    setTimeout(() => {
      setConfirming((prev) => ({ ...prev, [invite.invite_token]: false }))
    }, CONFIRM_RESET_MS)
  }

  return (
    <aside class="sh-remote-invite-banner">
      {localInvites.length > 0 && (
        <>
          <h2>📬 {t('remote_invite.banner_local')}</h2>
          {localInvites.map((inv) => {
            const busy = inFlight[inv.invitation_id] != null
            const declineConfirming = confirming[inv.invitation_id] === true
            const inviter = inv.invited_by
              ? householdDisplayName(inv.invited_by)
              : t('remote_invite.an_admin')
            return (
              <div
                key={inv.invitation_id}
                class="sh-remote-invite-banner__row"
              >
                <div>
                  <strong>{inv.space_id}</strong>
                  <FromLine name={inviter} />
                </div>
                <div class="sh-remote-invite-banner__actions">
                  <Button
                    variant="secondary"
                    onClick={() => handleDeclineLocal(inv)}
                    disabled={busy}
                    loading={inFlight[inv.invitation_id] === 'decline'}
                    data-testid="local-invite-decline"
                  >
                    {declineConfirming ? t('remote_invite.confirm_decline') : t('remote_invite.decline')}
                  </Button>
                  <Button
                    variant="primary"
                    onClick={() => void decideLocal(inv, 'accept')}
                    disabled={busy}
                    loading={inFlight[inv.invitation_id] === 'accept'}
                    data-testid="local-invite-accept"
                  >
                    {t('remote_invite.accept')}
                  </Button>
                </div>
              </div>
            )
          })}
        </>
      )}
      {invites.length > 0 && (
        <h2>📬 {t('remote_invite.banner_remote')}</h2>
      )}
      {invites.map((inv) => {
        const busy = inFlight[inv.invite_token] != null
        const declineConfirming = confirming[inv.invite_token] === true
        return (
          <div key={inv.invite_token} class="sh-remote-invite-banner__row">
            <div>
              <strong>{inv.space_display_hint || inv.space_id}</strong>
              <FromLine name={householdLabel(inv.inviter_instance_id)} />
            </div>
            <div class="sh-remote-invite-banner__actions">
              <Button
                variant="secondary"
                onClick={() => handleDecline(inv)}
                disabled={busy}
                loading={inFlight[inv.invite_token] === 'decline'}
                data-testid="invite-decline"
              >
                {declineConfirming ? t('remote_invite.confirm_decline') : t('remote_invite.decline')}
              </Button>
              <Button
                variant="primary"
                onClick={() => void decide(inv, 'accept')}
                disabled={busy}
                loading={inFlight[inv.invite_token] === 'accept'}
                data-testid="invite-accept"
              >
                {t('remote_invite.accept')}
              </Button>
            </div>
          </div>
        )
      })}
    </aside>
  )
}
