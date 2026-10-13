/**
 * SpaceZonesAdmin — admin CRUD UI for the per-space zone catalogue
 * (§23.8.7): the shared :component:`ZonesAdmin` editor pointed at
 * ``/api/spaces/{id}/zones``.
 */
import { ZonesAdmin } from './ZonesAdmin'
import { t } from '@/i18n/i18n'

export function SpaceZonesAdmin({ spaceId }: { spaceId: string }) {
  return (
    <ZonesAdmin
      basePath={`/api/spaces/${spaceId}/zones`}
      title={t('zones.title')}
      emptyText={t('zones.empty')}
    />
  )
}
