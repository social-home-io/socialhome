/**
 * Space version notice — advisory for space owners/admins when one or
 * more member households run an older Social Home protocol version (issue
 * #319 ¶5).
 *
 * Some shared-space features (Media DataChannel, remote admin actions,
 * multi-admin approvals, …) gate on a peer's ``proto_version``. When a member
 * household lags behind ours those features silently won't work *for everyone*
 * until that household updates. This banner names the affected features and
 * the households we're waiting on, so an admin understands why a feature
 * appears not to reach part of the group.
 *
 * Seeded from ``GET /api/spaces/{id}/compat`` (owner/admin only — best-effort,
 * so a 403 or any error just shows nothing). It is a row of the space's
 * "Needs you" card that opens these details; informational, no actions.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { featureLabels } from '@/utils/capabilityLabels'

export interface BehindMember {
  instance_id: string
  display_name: string
  proto_version: number
  lacking_features: string[]
  /** Stable slugs of ``lacking_features``, same order. */
  lacking_feature_keys?: string[]
}

export interface SpaceCompat {
  ours: number
  min_member_proto_version: number | null
  lagging_features: string[]
  /** Stable slugs of ``lagging_features``, same order. */
  lagging_feature_keys?: string[]
  behind_members: BehindMember[]
}

/** The space's compat for an owner / admin (``enabled``), or ``null``:
 *  not allowed, still loading, failed (best-effort), or nothing lags. */
export function useSpaceCompat(spaceId: string, enabled: boolean): SpaceCompat | null {
  const [compat, setCompat] = useState<SpaceCompat | null>(null)
  useEffect(() => {
    setCompat(null)
    if (!enabled) return
    let cancelled = false
    api
      .get<SpaceCompat>(`/api/spaces/${spaceId}/compat`)
      .then((r) => {
        if (!cancelled) setCompat(r)
      })
      .catch(() => {
        /* best-effort — a 403 for a non-admin or any error leaves it empty */
      })
    return () => {
      cancelled = true
    }
  }, [spaceId, enabled])
  return Array.isArray(compat?.lagging_features) && compat.lagging_features.length > 0
    ? { ...compat, behind_members: compat.behind_members ?? [] }
    : null
}

/** What lags and whom the space waits on — the body of the Needs-you
 *  row's dialog. */
export function SpaceVersionDetails({ compat }: { compat: SpaceCompat }) {
  return (
    <div class="sh-proposal-banner__text">
      <p>
        {t('space.version_banner.body')}{' '}
        {featureLabels(compat.lagging_features, compat.lagging_feature_keys).map((f, i) => (
          <span key={f}>
            {i > 0 ? ', ' : ''}
            <strong>{f}</strong>
          </span>
        ))}
        .
      </p>
      {compat.behind_members.length > 0 && (
        <p class="sh-muted">
          {t('space.version_banner.waiting_on')}{' '}
          {compat.behind_members
            .map((m) => `${m.display_name} (v${m.proto_version})`)
            .join(', ')}
          .
        </p>
      )}
    </div>
  )
}
