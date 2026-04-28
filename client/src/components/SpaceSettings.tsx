/**
 * SpaceSettings — space admin settings panel (§23.91).
 * Includes a Federation section for GFS publish/unpublish.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import type { Space, GfsConnection, GfsSpacePublication } from '@/types'

const showDissolve = signal(false)
const gfsServers = signal<GfsConnection[]>([])
const publications = signal<GfsSpacePublication[]>([])
const federationLoading = signal(false)

async function loadFederationData(spaceId: string) {
  federationLoading.value = true
  try {
    const [servers, pubs] = await Promise.all([
      api.get<GfsConnection[]>('/api/gfs/connections'),
      api.get<GfsSpacePublication[]>(`/api/spaces/${spaceId}/publications`),
    ])
    gfsServers.value = servers
    publications.value = pubs
  } catch {
    gfsServers.value = []
    publications.value = []
  }
  federationLoading.value = false
}

function isPublished(gfsId: string): boolean {
  return publications.value.some(p => p.gfs_connection_id === gfsId)
}

async function togglePublish(spaceId: string, gfsId: string) {
  try {
    if (isPublished(gfsId)) {
      await api.delete(`/api/spaces/${spaceId}/publish/${gfsId}`)
      publications.value = publications.value.filter(p => p.gfs_connection_id !== gfsId)
      showToast(t('space.unpublish_from_gfs'), 'success')
    } else {
      const pub = await api.post<GfsSpacePublication>(`/api/spaces/${spaceId}/publish/${gfsId}`)
      publications.value = [...publications.value, pub]
      showToast(t('space.publish_to_gfs'), 'success')
    }
  } catch (e: any) {
    showToast(e.message || 'Failed', 'error')
  }
}

export function SpaceSettings({ space, onUpdate }: { space: Space; onUpdate: () => void }) {
  const name = signal(space.name)
  const description = signal(space.description || '')
  const emoji = signal(space.emoji || '')
  const joinMode = signal(space.join_mode)
  const locationEnabled = signal(
    Boolean((space.features as { location?: boolean } | undefined)?.location),
  )
  const locationMode = signal<'gps' | 'zone_only'>(
    ((space.features as { location_mode?: 'gps' | 'zone_only' } | undefined)
      ?.location_mode) ?? 'gps',
  )

  useEffect(() => {
    loadFederationData(space.id)
  }, [space.id])

  const save = async () => {
    const previousMode = (space.features as { location_mode?: string } | undefined)
      ?.location_mode ?? 'gps'
    const modeChanged = locationEnabled.value
      && locationMode.value !== previousMode
    try {
      await api.patch(`/api/spaces/${space.id}`, {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        join_mode: joinMode.value,
        features: {
          ...(space.features as object),
          location: locationEnabled.value,
          location_mode: locationMode.value,
        },
      })
      if (modeChanged) {
        showToast(
          locationMode.value === 'zone_only'
            ? 'Zone-only mode on. Members will see only zone labels within seconds.'
            : 'Live GPS mode on. Members will see GPS pins within seconds.',
          'success',
        )
      } else {
        showToast('Space updated', 'success')
      }
      onUpdate()
    } catch (e: any) {
      showToast(e.message || 'Failed to update', 'error')
    }
  }

  const dissolve = async () => {
    try {
      await api.delete(`/api/spaces/${space.id}`)
      showToast('Space dissolved', 'info')
      location.href = '/spaces'
    } catch (e: any) {
      showToast(e.message || 'Failed to dissolve', 'error')
    }
  }

  return (
    <div class="sh-space-settings">
      <h3>Space Settings</h3>
      <div class="sh-form">
        <label>Name <input value={name.value} onInput={(e) => name.value = (e.target as HTMLInputElement).value} /></label>
        <label>Description <textarea value={description.value} onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value} rows={2} /></label>
        <label>Emoji <input value={emoji.value} maxLength={2} onInput={(e) => emoji.value = (e.target as HTMLInputElement).value} /></label>
        <label>Join mode
          <select value={joinMode.value} onChange={(e) => joinMode.value = (e.target as HTMLSelectElement).value as any}>
            <option value="invite_only">Invite only</option>
            <option value="open">Open</option>
            <option value="link">Link</option>
            <option value="request">Request</option>
          </select>
        </label>
        <fieldset class="sh-form-fieldset">
          <legend>📍 Location sharing</legend>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={locationEnabled.value}
              onChange={(e) => {
                locationEnabled.value = (e.target as HTMLInputElement).checked
              }}
            />
            Show a map tab to members of this space
          </label>
          {locationEnabled.value && (
            <>
              <fieldset class="sh-mode-fieldset" aria-label="Privacy mode">
                <legend>Privacy mode</legend>
                <label class={`sh-mode-option ${locationMode.value === 'gps' ? 'sh-mode-option--selected' : ''}`}>
                  <input
                    type="radio"
                    name={`location-mode-${space.id}`}
                    value="gps"
                    checked={locationMode.value === 'gps'}
                    onChange={() => { locationMode.value = 'gps' }}
                  />
                  <span class="sh-mode-option__body">
                    <span class="sh-mode-option__title">
                      🛰️ Live GPS
                    </span>
                    <span class="sh-muted">
                      Opted-in members broadcast their GPS to the space.
                      Coordinates are rounded to ~10 m before they leave
                      your home server.
                    </span>
                  </span>
                </label>
                <label class={`sh-mode-option ${locationMode.value === 'zone_only' ? 'sh-mode-option--selected' : ''}`}>
                  <input
                    type="radio"
                    name={`location-mode-${space.id}`}
                    value="zone_only"
                    checked={locationMode.value === 'zone_only'}
                    onChange={() => { locationMode.value = 'zone_only' }}
                  />
                  <span class="sh-mode-option__body">
                    <span class="sh-mode-option__title">
                      🔒 Zone only
                      <span class="sh-mode-option__badge">stronger privacy</span>
                    </span>
                    <span class="sh-muted">
                      Your home server matches each member's GPS to a
                      space-defined zone and sends only the zone label.
                      Raw coordinates never leave your household. Members
                      outside every zone show nothing.
                    </span>
                  </span>
                </label>
              </fieldset>
              <p class="sh-muted">
                <a href={`/spaces/${space.id}/zones`}>Manage zones →</a>
                {locationMode.value === 'zone_only'
                  && ' (required for zone-only mode)'}
              </p>
            </>
          )}
          <p class="sh-muted">
            HA-defined zone names are never sent to a space, regardless
            of mode. Per-space zones (managed above) are the only labels
            ever shared.
          </p>
        </fieldset>
        <div class="sh-form-actions">
          <Button onClick={save}>Save changes</Button>
        </div>
      </div>

      <hr />
      <h3>{t('space.federation')}</h3>
      {federationLoading.value ? (
        <p class="sh-muted">{t('common.loading')}</p>
      ) : gfsServers.value.length === 0 ? (
        <p class="sh-muted">{t('space.no_gfs_connections')}</p>
      ) : (
        <div class="sh-federation-list">
          {gfsServers.value.map(gfs => {
            const published = isPublished(gfs.id)
            return (
              <div key={gfs.id} class="sh-federation-row">
                <div class="sh-connection-info">
                  <span class={`sh-status-dot sh-status-dot--${gfs.status === 'active' ? 'active' : gfs.status === 'suspended' ? 'unreachable' : 'pending'}`} />
                  <strong>{gfs.display_name}</strong>
                  <span class="sh-muted">{gfs.inbox_url}</span>
                </div>
                <div class="sh-federation-actions">
                  <span class={published ? 'sh-text-success' : 'sh-muted'}>
                    {published ? t('space.published') : t('space.not_published')}
                  </span>
                  <Button
                    variant={published ? 'danger' : 'primary'}
                    onClick={() => togglePublish(space.id, gfs.id)}
                  >
                    {published ? t('gfs.unpublish') : t('gfs.publish')}
                  </Button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <hr />
      <h3>Danger zone</h3>
      <Button variant="danger" onClick={() => showDissolve.value = true}>Dissolve space</Button>
      <ConfirmDialog open={showDissolve.value} title="Dissolve space?"
        message="This will permanently remove the space and all its content. This cannot be undone."
        confirmLabel="Dissolve" destructive
        onConfirm={() => { showDissolve.value = false; dissolve() }}
        onCancel={() => showDissolve.value = false} />
    </div>
  )
}
