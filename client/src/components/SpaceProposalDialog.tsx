/**
 * SpaceProposalDialog — one open multi-admin approval proposal (v_16),
 * opened from its row in the space's "Needs you" card.
 *
 * Dissolving a space and changing its publication tier need a majority
 * of the space's admins; a forwarded admin action needs the owner. The
 * dialog says what is proposed, the live tally and who asked, and offers
 * Approve / Reject to a viewer who may vote and hasn't yet
 * (``voted_by_me`` — the proposer and anyone who already voted are never
 * asked again). The vote forwards to the host even from a remote
 * household, so it is gated on the viewer's *role*, not on locality.
 *
 * The Needs-you card re-reads the space detail on the
 * ``space.proposal.updated`` frame, so the tally and the row stay live.
 */
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from './Button'
import { Modal } from './Modal'
import { isRestricted } from './ProtectedNotice'
import { locale, t } from '@/i18n/i18n'
import { parseSpaceRole } from '@/features/spaces/spaceRoles'
import { appLook } from '@/features/spaces/games/spaceGames'
import { catalog, installedApps } from '@/store/apps'
import { showToast } from './Toast'
import { awaitsMyVote } from '@/store/spaceAttention'

export type { SpaceProposal } from '@/features/spaces/spaceProposal'
import type { SpaceProposal } from '@/features/spaces/spaceProposal'

interface Props {
  spaceId: string
  proposal: SpaceProposal & { voted_by_me?: boolean }
  /** Viewer can vote (owner / admin on any household). */
  canVote: boolean
  /** Viewer is the space owner — required to vote on owner-only proposals. */
  isOwner: boolean
  onClose: () => void
  /** A vote was recorded (the caller re-reads the space detail). */
  onVoted: () => void
}

/** Phrase for a forwarded admin action — fills ``{action}`` in
 *  ``space.proposal.owner_body`` ("A proposal to …"). Literal ``t()``
 *  calls so the i18n check sees every key. */
const FWD_ACTION_COPY: Record<string, () => string> = {
  ban: () => t('space.proposal.fwd.ban'),
  unban: () => t('space.proposal.fwd.unban'),
  update_config: () => t('space.proposal.fwd.update_config'),
  archive: () => t('space.proposal.fwd.archive'),
  unarchive: () => t('space.proposal.fwd.unarchive'),
  invite: () => t('space.proposal.fwd.invite'),
}

/** "make Carol …" — the role a forwarded ``set_member_role`` asks for. */
const ROLE_PHRASE: Record<string, (name: string) => string> = {
  owner: (name) => t('space.proposal.role.owner', { name }),
  admin: (name) => t('space.proposal.role.admin', { name }),
  moderator: (name) => t('space.proposal.role.moderator', { name }),
  member: (name) => t('space.proposal.role.member', { name }),
  subscriber: (name) => t('space.proposal.role.subscriber', { name }),
}

/** The games list a forwarded ``update_config`` asks for, when that is
 *  all it changes (``fwd_params: {features: {allowed_apps}}``), else
 *  ``null``. */
export function proposedGames(p: SpaceProposal): string[] | null {
  if (p.action !== 'remote_admin_action' || p.fwd_action !== 'update_config') return null
  const feats = p.fwd_params?.features
  if (!feats || typeof feats !== 'object' || Array.isArray(feats)) return null
  const keys = Object.keys(feats)
  const list = (feats as Record<string, unknown>).allowed_apps
  if (keys.length !== 1 || !Array.isArray(list)) return null
  return list.filter((x): x is string => typeof x === 'string')
}

export function describeProposal(p: SpaceProposal): string {
  if (p.action === 'dissolve') return t('space.proposal.action.dissolve')
  if (p.action === 'set_public_tier') {
    const tier = p.params?.space_type
    return tier
      ? t('space.proposal.action.tier', { tier })
      : t('space.proposal.action.tier_any')
  }
  if (p.action === 'remote_admin_action' && p.fwd_action === 'set_member_role') {
    // "make Carol a moderator" — the owner must see exactly what they
    // approve: who, and to which role.
    const role = parseSpaceRole(p.fwd_params?.role)
    const who = p.fwd_target_label || t('space.proposal.someone')
    return role ? ROLE_PHRASE[role](who) : t('space.proposal.role.change', { name: who })
  }
  const games = proposedGames(p)
  if (games) {
    // A remote admin's or moderator's "Games allowed here" edit, held for
    // the owner (group play D9): say which games, by name.
    const lang = locale.value || 'en'
    const names = games.map((id) => appLook(id, installedApps.value, catalog.value, lang).name)
    return names.length
      ? t('space.proposal.fwd.games', { games: names.join(', ') })
      : t('space.proposal.fwd.games_none')
  }
  if (p.action === 'remote_admin_action') {
    const copy = p.fwd_action ? FWD_ACTION_COPY[p.fwd_action] : undefined
    const base = copy ? copy() : t('space.proposal.fwd.generic')
    return p.fwd_target_label
      ? t('space.proposal.action.with_target', { action: base, target: p.fwd_target_label })
      : base
  }
  return t('space.proposal.action.critical')
}

/** May this viewer still vote on ``p``? Owner-only proposals (forwarded
 *  admin actions) are the owner's alone; anyone who proposed or already
 *  voted isn't asked again. */
export function canVoteOn(
  p: SpaceProposal & { voted_by_me?: boolean }, canVote: boolean, isOwner: boolean,
): boolean {
  return awaitsMyVote(p, canVote, isOwner)
}

export function SpaceProposalDialog({ spaceId, proposal: p, canVote, isOwner, onClose, onVoted }: Props) {
  const [busy, setBusy] = useState(false)

  const vote = async (approve: boolean) => {
    setBusy(true)
    try {
      await api.post(`/api/spaces/${spaceId}/proposals/${p.id}/vote`, { approve })
      showToast(approve ? t('space.proposal.approved_toast') : t('space.proposal.rejected_toast'), 'info')
      onVoted()
      onClose()
    } catch (err) {
      showToast((err as Error)?.message || t('space.proposal.vote_failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  const canVoteThis = canVoteOn(p, canVote, isOwner)
  // §CP.R: a protected admin may reject — never approve — making the
  // space public / global (the server refuses the approval).
  const approveLocked =
    p.action === 'set_public_tier'
    && (p.params?.space_type === 'public' || p.params?.space_type === 'global')
    && isRestricted('public_spaces')
  const title = p.owner_only ? t('space.proposal.owner_title') : t('space.proposal.admin_title')

  return (
    <Modal open onClose={onClose} title={title} class="sh-proposal-dialog">
      <div class="sh-proposal-banner__text">
        {p.owner_only ? (
          <p>{t('space.proposal.owner_body', { action: describeProposal(p) })}</p>
        ) : (
          <p>
            {t('space.proposal.admin_body', { action: describeProposal(p) })}{' '}
            <strong>
              {t('space.proposal.tally', {
                approvals: String(p.approvals),
                needed: String(p.needed),
              })}
            </strong>{' '}
            {t('space.proposal.admin_count', { n: String(p.total_admins) })}
          </p>
        )}
        {p.action === 'remote_admin_action' && (
          <p class="sh-muted sh-proposal-banner__requester">
            {t('space.proposal.requested_by', {
              name: p.proposed_by_label || t('space.proposal.requester_unknown'),
            })}
          </p>
        )}
      </div>
      {canVoteThis ? (
        <div class="sh-proposal-banner__actions">
          <Button variant="secondary" disabled={busy} onClick={() => void vote(false)}>
            {t('space.proposal.reject')}
          </Button>
          {approveLocked ? (
            <p class="sh-muted sh-proposal-banner__note">{t('protected.approve_blocked')}</p>
          ) : (
            <Button
              variant={p.action === 'dissolve' ? 'danger' : 'primary'}
              disabled={busy}
              onClick={() => void vote(true)}
            >
              {t('space.proposal.approve')}
            </Button>
          )}
        </div>
      ) : (
        <p class="sh-muted sh-proposal-banner__note">
          {p.voted_by_me
            ? t('space.proposal.already_voted')
            : p.owner_only ? t('space.proposal.waiting_owner') : t('space.proposal.waiting_admins')}
        </p>
      )}
    </Modal>
  )
}
