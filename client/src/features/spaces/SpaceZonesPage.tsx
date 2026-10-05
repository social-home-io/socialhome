/**
 * SpaceZonesPage — admin-only landing for ``/spaces/:id/zones``.
 *
 * Thin wrapper that pulls the space id from the route params, asserts
 * the caller is an admin/owner of this space (via the existing space
 * detail endpoint, which 403s otherwise), and renders the
 * :component:`SpaceZonesAdmin` two-pane CRUD UI.
 *
 * Members who navigate here directly get a friendly access message.
 * The link from :component:`SpaceSettings` is gated on the location
 * feature being on, but a member who guesses the URL still hits this
 * gate — see §23.8.7.
 */
import { useEffect, useState } from 'preact/hooks'
import { useRoute } from 'preact-iso'
import { api } from '@/api'
import { Spinner } from '@/components/Spinner'
import { SpaceZonesAdmin } from '@/components/SpaceZonesAdmin'
import type { Space } from '@/types'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'

export default function SpaceZonesPage() {
  const { params } = useRoute()
  const spaceId = (params as { id?: string }).id ?? ''
  const [space, setSpace] = useState<Space | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!spaceId) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api.get<Space>(`/api/spaces/${spaceId}`)
      .then((s) => { if (!cancelled) setSpace(s) })
      .catch((e: Error) => { if (!cancelled) setError(e.message) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [spaceId])

  if (!spaceId) {
    return <div class="sh-error-state">{t('space.zones_page.missing_id')}</div>
  }
  if (loading) return <Spinner />
  if (error) {
    return (
      <div class="sh-error-state" role="alert">
        {t('space.zones_page.load_failed', { error })}
      </div>
    )
  }
  if (!space) return null

  if (!space.features?.location) {
    return (
      <div class="sh-page sh-muted">
        <p>{t('space.zones_page.location_off')}</p>
        <p>
          <a href={addBase(`/spaces/${spaceId}/settings`)}>← {t('space.zones_page.back')}</a>
        </p>
      </div>
    )
  }

  return (
    <div class="sh-page">
      <header class="sh-page__header">
        <h2>📍 {t('space.zones_page.title', { name: space.name })}</h2>
        <p class="sh-muted">
          <a href={addBase(`/spaces/${spaceId}/settings`)}>← {t('space.zones_page.back')}</a>
        </p>
      </header>
      <SpaceZonesAdmin spaceId={spaceId} />
    </div>
  )
}
