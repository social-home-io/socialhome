/**
 * SpaceProposalsBanner — pending multi-admin approval proposals for a
 * space (v_16).
 *
 * Dissolving a space and changing its publication tier require a majority
 * of the space's admins to approve. While such a proposal is open this
 * banner surfaces it to every member (so the group sees what's being
 * decided) with the live tally; admins get Approve / Reject buttons. The
 * vote forwards to the host even from a remote household, so it renders on
 * a remote stub too — gated on the viewer's *role*, not on locality.
 *
 * State is seeded from ``GET /api/spaces/{id}/proposals`` and kept live by
 * the ``space.proposal.updated`` WS frame (the host re-broadcasts the
 * tally after every vote). Resolved proposals (executed / rejected /
 * expired) drop out.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { isRestricted } from './ProtectedNotice'
import { t } from '@/i18n/i18n'
import { parseSpaceRole } from '@/features/spaces/spaceRoles'
import { showToast } from './Toast'

export interface SpaceProposal {
  id: string
  action: 'dissolve' | 'set_public_tier' | 'remote_admin_action'
  params?: { space_type?: string }
  status: 'pending' | 'executed' | 'rejected' | 'expired'
  approvals: number
  total_admins: number
  needed: number
  proposed_by_user: string
  /** The proposer's display name, resolved by the host from the signed
   *  ``(proposed_by_instance, proposed_by_user)`` seat; ``null`` when
   *  unknown (the banner then says "a member of another household"). */
  proposed_by_label?: string | null
  /** Owner-only proposals (forwarded admin actions) may be approved only
   *  by the space owner — a co-admin's vote is rejected by the host. */
  owner_only?: boolean
  /** The forwarded admin action a remote admin asked the owner to run. */
  fwd_action?: string
  fwd_params?: Record<string, unknown>
  /** Resolved display name of the forwarded action's target, when known. */
  fwd_target_label?: string
}

interface Props {
  spaceId: string
  /** Viewer can vote (owner / admin on any household). */
  canVote: boolean
  /** Viewer is the space owner — required to vote on owner-only proposals. */
  isOwner: boolean
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

function describe(p: SpaceProposal): string {
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
  if (p.action === 'remote_admin_action') {
    const copy = p.fwd_action ? FWD_ACTION_COPY[p.fwd_action] : undefined
    const base = copy ? copy() : t('space.proposal.fwd.generic')
    return p.fwd_target_label
      ? t('space.proposal.action.with_target', { action: base, target: p.fwd_target_label })
      : base
  }
  return t('space.proposal.action.critical')
}

export function SpaceProposalsBanner({ spaceId, canVote, isOwner }: Props) {
  const [proposals, setProposals] = useState<SpaceProposal[]>([])
  const [busy, setBusy] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api
      .get<{ proposals: SpaceProposal[] }>(`/api/spaces/${spaceId}/proposals`)
      .then((r) => {
        if (!cancelled) setProposals(r.proposals ?? [])
      })
      .catch(() => {
        /* best-effort — banner just stays empty */
      })

    const off = ws.on('space.proposal.updated', (e: unknown) => {
      const frame = e as { space_id?: string; proposal?: SpaceProposal }
      if (frame.space_id !== spaceId || !frame.proposal) return
      const p = frame.proposal
      setProposals((prev) => {
        const others = prev.filter((x) => x.id !== p.id)
        // Only pending proposals stay on the banner; resolved ones drop.
        return p.status === 'pending' ? [...others, p] : others
      })
    })
    return () => {
      cancelled = true
      off()
    }
  }, [spaceId])

  if (proposals.length === 0) return null

  const vote = async (p: SpaceProposal, approve: boolean) => {
    setBusy(p.id)
    try {
      await api.post(`/api/spaces/${spaceId}/proposals/${p.id}/vote`, { approve })
      showToast(approve ? t('space.proposal.approved_toast') : t('space.proposal.rejected_toast'), 'info')
      // The WS frame updates the tally / removes it; no optimistic edit.
    } catch (err) {
      showToast((err as Error)?.message || t('space.proposal.vote_failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div class="sh-space-proposals" role="region" aria-label={t('space.proposal.region_aria')}>
      {proposals.map((p) => {
        const icon =
          p.action === 'dissolve'
            ? '🗑️'
            : p.action === 'set_public_tier'
              ? '🌐'
              : '🛡️'
        // Owner-only proposals (forwarded admin actions) are votable by the
        // owner alone; other proposals follow the admin-quorum rule.
        const canVoteThis = canVote && (!p.owner_only || isOwner)
        // §CP.R: a protected admin may reject — never approve — making the
        // space public / global (the server refuses the approval).
        const approveLocked =
          p.action === 'set_public_tier'
          && (p.params?.space_type === 'public' || p.params?.space_type === 'global')
          && isRestricted('public_spaces')
        return (
          <div key={p.id} class="sh-proposal-banner" role="status">
            <div class="sh-proposal-banner__body">
              <span class="sh-proposal-banner__icon" aria-hidden="true">
                {icon}
              </span>
              <div class="sh-proposal-banner__text">
                <strong>
                  {p.owner_only ? t('space.proposal.owner_title') : t('space.proposal.admin_title')}
                </strong>
                {p.owner_only ? (
                  <p class="sh-muted">
                    {t('space.proposal.owner_body', { action: describe(p) })}
                  </p>
                ) : (
                  <p class="sh-muted">
                    {t('space.proposal.admin_body', { action: describe(p) })}{' '}
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
            </div>
            {canVoteThis ? (
              <div class="sh-proposal-banner__actions">
                <Button
                  variant="secondary"
                  disabled={busy === p.id}
                  onClick={() => void vote(p, false)}
                >
                  {t('space.proposal.reject')}
                </Button>
                {approveLocked ? (
                  <p class="sh-muted sh-proposal-banner__note">
                    {t('protected.approve_blocked')}
                  </p>
                ) : (
                  <Button
                    variant={p.action === 'dissolve' ? 'danger' : 'primary'}
                    disabled={busy === p.id}
                    onClick={() => void vote(p, true)}
                  >
                    {t('space.proposal.approve')}
                  </Button>
                )}
              </div>
            ) : (
              <p class="sh-muted sh-proposal-banner__note">
                {p.owner_only ? t('space.proposal.waiting_owner') : t('space.proposal.waiting_admins')}
              </p>
            )}
          </div>
        )
      })}
    </div>
  )
}
