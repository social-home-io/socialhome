/**
 * SpaceVersionBanner — advisory notice for space owners/admins when one or
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
 * so a 403 or any error just leaves the banner empty). Informational tone, no
 * actions: a future PR adds an upgrade nudge.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'

export interface BehindMember {
  instance_id: string
  display_name: string
  proto_version: number
  lacking_features: string[]
}

export interface SpaceCompat {
  ours: number
  min_member_proto_version: number | null
  lagging_features: string[]
  behind_members: BehindMember[]
}

interface Props {
  spaceId: string
}

export function SpaceVersionBanner({ spaceId }: Props) {
  const [compat, setCompat] = useState<SpaceCompat | null>(null)

  useEffect(() => {
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
  }, [spaceId])

  if (!compat || compat.lagging_features.length === 0) return null

  return (
    <div
      class="sh-space-proposals"
      role="region"
      aria-label={t('space.version_banner.region')}
    >
      <div class="sh-proposal-banner sh-version-banner" role="status">
        <div class="sh-proposal-banner__body">
          <span class="sh-proposal-banner__icon" aria-hidden="true">
            🔌
          </span>
          <div class="sh-proposal-banner__text">
            <strong>{t('space.version_banner.title')}</strong>
            <p class="sh-muted">
              {t('space.version_banner.body')}{' '}
              {compat.lagging_features.map((f, i) => (
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
        </div>
      </div>
    </div>
  )
}
