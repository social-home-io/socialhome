/**
 * A multi-admin approval proposal (v_16) as ``GET /api/spaces/{id}/proposals``
 * and the space detail's ``attention`` send it. A plain type module, so the
 * store and the vote dialog share it without importing each other.
 */
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
