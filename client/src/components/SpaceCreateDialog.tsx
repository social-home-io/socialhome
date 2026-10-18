/**
 * SpaceCreateDialog — space creation flow (§23.50).
 */
import { signal } from '@preact/signals'
import { useRef } from 'preact/hooks'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { currentUser } from '@/store/auth'
import { loadSpaces } from '@/store/spaces'
import { Modal } from './Modal'
import { Button } from './Button'
import { ProtectedNotice, isRestricted } from './ProtectedNotice'
import { EmojiField } from './EmojiField'
import { RadioCardGroup } from './RadioCardGroup'
import {
  GfsChecklist, defaultGfsSelection, revealGfsChecklist, type GfsChoice,
} from './GfsChecklist'
import {
  visibilityOptions as tierOptions,
  SPACE_CATEGORIES,
  hasJoinModeChoice,
  joinOptionsForVisibility,
} from './spaceModeOptions'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const open = signal(false)
const name = signal('')
const description = signal('')
const emoji = signal('')
const spaceType = signal('private')
const joinMode = signal('invite_only')
// Public spaces can be pinned on the public map, so they may carry a location.
// Held as strings for the inputs; parsed + (server-side) truncated to 4dp.
// Location is optional — a public space without coords simply isn't pinned.
const lat = signal('')
const lon = signal('')
const locating = signal(false)
const submitting = signal(false)
// Discovery metadata, sent only for public/global spaces (§23.50).
const minAge = signal(0)
const category = signal('general')
// The household's active GFS connections — a global space is listed on the
// ones the creator ticks (never on every connected GFS), and none at all
// gates the Global tier. Loaded on open from /api/gfs/connections.
const gfsChoices = signal<GfsChoice[]>([])
const gfsSelected = signal<string[]>([])
// "Choose at least one GFS" — shown under the checklist after a submit
// with nothing ticked; cleared by the next tick.
const gfsError = signal<string | null>(null)

// Minimum-age options for the discovery audience gate. 0 = no restriction.
const MIN_AGE_OPTIONS = [0, 13, 16, 18]

export function openSpaceCreate() {
  open.value = true
  name.value = ''
  description.value = ''
  emoji.value = ''
  spaceType.value = 'private'
  joinMode.value = 'invite_only'
  lat.value = ''
  lon.value = ''
  locating.value = false
  minAge.value = 0
  category.value = 'general'
  gfsChoices.value = []
  gfsSelected.value = []
  gfsError.value = null
  // The Global tier is only offered when an active GFS connection exists.
  void api.get<(GfsChoice & { status: string })[]>('/api/gfs/connections')
    .then((c) => {
      const active = c
        .filter((g) => g.status === 'active')
        .map(({ id, display_name, inbox_url }) => ({ id, display_name, inbox_url }))
      gfsChoices.value = active
      // Exactly one server: nothing to decide, so it starts ticked.
      gfsSelected.value = defaultGfsSelection(active)
    })
    .catch(() => { gfsChoices.value = [] })
}

const isPublic = () => spaceType.value === 'public'
const isGlobal = () => spaceType.value === 'global'
const isDiscoverable = () =>
  spaceType.value === 'public' || spaceType.value === 'global'

function useMyLocation() {
  if (!navigator.geolocation) {
    showToast(t('space.create.location_unavailable'), 'error')
    return
  }
  locating.value = true
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      // Truncate to 4dp here too (the backend also truncates) — never
      // store/transmit raw device precision (§GPS).
      lat.value = String(Math.round(pos.coords.latitude * 1e4) / 1e4)
      lon.value = String(Math.round(pos.coords.longitude * 1e4) / 1e4)
      locating.value = false
    },
    () => {
      showToast(t('space.create.location_failed'), 'info')
      locating.value = false
    },
  )
}

export function SpaceCreateDialog() {
  const gfsChecklistRef = useRef<HTMLFieldSetElement | null>(null)
  const handleSubmit = async () => {
    if (!name.value.trim() || submitting.value) return
    // A global space must be listed somewhere: the server refuses an empty
    // choice (GFS_SELECTION_REQUIRED), so say it beside the list instead.
    if (isGlobal() && gfsSelected.value.length === 0) {
      gfsError.value = t('error.gfs_selection_required')
      revealGfsChecklist(gfsChecklistRef)
      return
    }
    submitting.value = true
    try {
      const discoverable = isDiscoverable()
      await api.post('/api/spaces', {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        space_type: spaceType.value,
        join_mode: joinMode.value,
        ...(isPublic() && lat.value.trim() && lon.value.trim()
          ? { lat: Number(lat.value), lon: Number(lon.value) }
          : {}),
        ...(discoverable ? { category: category.value } : {}),
        ...(discoverable && minAge.value ? { min_age: minAge.value } : {}),
        ...(isGlobal() ? { gfs_ids: gfsSelected.value } : {}),
      })
      // Refresh the cached spaces list so the new row appears on the
      // list page without a hard reload.
      await loadSpaces()
      showToast(t('space.create.created'), 'success')
      open.value = false
    } catch (e: any) {
      showToast(e.message || t('space.create.failed'), 'error')
    } finally {
      submitting.value = false
    }
  }

  // Connecting a global server is a household-admin action (connection
  // management is admin-only), so a member is pointed at an admin rather
  // than at a Connections page whose controls they can't use.
  const isAdmin = currentUser.value?.is_admin === true

  // The Global tier is disabled (with an explanatory subtitle) until an
  // active global server connection exists.
  const hasActiveGfs = gfsChoices.value.length > 0
  const visibilityOptions = hasActiveGfs
    ? tierOptions()
    : tierOptions().map((o) =>
      o.value === 'global'
        ? {
          ...o,
          disabled: true,
          subtitle: isAdmin
            ? t('space.create.global_needs_gfs_admin')
            : t('space.create.global_needs_gfs_member'),
        }
        : o,
    )

  // §CP.R: a protected account can create private / household spaces
  // only — the server refuses the discoverable tiers (403
  // ACCOUNT_PROTECTED), so offer them disabled with the reason.
  const tierLocked = isRestricted('public_spaces')
  const offeredOptions = tierLocked
    ? visibilityOptions.map((o) =>
      o.value === 'public' || o.value === 'global'
        ? { ...o, disabled: true, subtitle: t('protected.tier_unavailable') }
        : o,
    )
    : visibilityOptions

  const noLocation = isPublic() && !lat.value.trim() && !lon.value.trim()

  return (
    <Modal open={open.value} onClose={() => open.value = false} title={t('space.create.title')}>
      <div class="sh-form">
        <label>
          {t('space.create.name')}
          <input value={name.value} onInput={(e) => name.value = (e.target as HTMLInputElement).value}
            placeholder={t('space.create.name_placeholder')} />
        </label>
        <label>
          {t('space.create.description')}
          <textarea value={description.value}
            onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value}
            placeholder={t('space.create.description_placeholder')} rows={2} />
        </label>
        <EmojiField value={emoji} openKey="space-create-icon" />
        <RadioCardGroup
          legend={t('space.create.visibility')}
          name="space-create-visibility"
          value={spaceType.value}
          options={offeredOptions}
          onChange={(v) => {
            const was = spaceType.value
            spaceType.value = v
            // A private space is invite-only by definition, and everyone in
            // the home is a member of a household space — neither has a
            // join-mode choice to make, so keep it consistent.
            if (v === 'private') joinMode.value = 'invite_only'
            else if (v === 'household') joinMode.value = 'open'
            else if (was === 'household') {
              // Leaving household for public / global: its implicit "open"
              // was never the person's choice — start from the safe default.
              joinMode.value = 'invite_only'
            }
          }}
        />
        {tierLocked && <ProtectedNotice capability="public_spaces" />}
        {!tierLocked && !hasActiveGfs && (
          <p class="sh-muted" style={{ marginTop: 'calc(-1 * var(--sh-space-sm))' }}>
            {isAdmin ? (
              <>
                {t('space.create.gfs_hint_before')}
                <a href={addBase('/connections')}>{t('space.create.gfs_hint_link')}</a>
                {t('space.create.gfs_hint_after')}
              </>
            ) : (
              <>{t('space.create.gfs_hint_member')}</>
            )}
          </p>
        )}
        {isGlobal() && hasActiveGfs && (
          <GfsChecklist
            ref={gfsChecklistRef}
            name="space-create"
            choices={gfsChoices.value}
            selected={gfsSelected.value}
            onChange={(ids) => {
              gfsSelected.value = ids
              gfsError.value = null
            }}
            error={gfsError.value}
          />
        )}
        {hasJoinModeChoice(spaceType.value) && (
          <RadioCardGroup
            legend={t('space.join.legend')}
            name="space-create-join-mode"
            value={joinMode.value}
            options={joinOptionsForVisibility(spaceType.value)}
            onChange={(v) => joinMode.value = v}
          />
        )}
        {isDiscoverable() && (
          <fieldset class="sh-form-fieldset sh-space-create-discovery">
            <legend>🧭 {t('space.create.discovery')}</legend>
            <div class="sh-row" style={{ gap: 'var(--sh-space-sm)' }}>
              <label>
                {t('space.create.category')}
                <select
                  name="space-create-category"
                  value={category.value}
                  onChange={(e) => category.value = (e.target as HTMLSelectElement).value}
                >
                  {SPACE_CATEGORIES.map((c) => (
                    <option key={c.value} value={c.value}>{c.label}</option>
                  ))}
                </select>
              </label>
              <label>
                {t('space.create.min_age')}
                <select
                  name="space-create-min-age"
                  value={String(minAge.value)}
                  onChange={(e) => minAge.value = Number((e.target as HTMLSelectElement).value)}
                >
                  {MIN_AGE_OPTIONS.map((a) => (
                    <option key={a} value={String(a)}>
                      {a === 0
                        ? t('space.create.min_age_none')
                        : t('space.create.min_age_value', { age: String(a) })}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </fieldset>
        )}
        {isPublic() && (
          <fieldset class="sh-form-fieldset sh-space-create-location">
            <legend>📍 {t('space.create.map_location')}</legend>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              {t('space.create.map_intro')}
            </p>
            <Button
              variant="secondary"
              onClick={useMyLocation}
              loading={locating.value}
            >
              📍 {t('space.create.use_my_location')}
            </Button>
            <div class="sh-row" style={{ gap: 'var(--sh-space-sm)' }}>
              <label>
                {t('space.create.latitude')}
                <input
                  type="number" inputMode="decimal" step="0.0001"
                  min={-90} max={90}
                  placeholder="52.5200"
                  value={lat.value}
                  onInput={(e) => lat.value = (e.target as HTMLInputElement).value}
                />
              </label>
              <label>
                {t('space.create.longitude')}
                <input
                  type="number" inputMode="decimal" step="0.0001"
                  min={-180} max={180}
                  placeholder="13.4050"
                  value={lon.value}
                  onInput={(e) => lon.value = (e.target as HTMLInputElement).value}
                />
              </label>
            </div>
            {noLocation && (
              <p class="sh-muted" style={{ marginBottom: 0 }}>
                {t('space.create.location_optional')}
              </p>
            )}
          </fieldset>
        )}
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => open.value = false}>{t('common.cancel')}</Button>
          <Button onClick={handleSubmit} loading={submitting.value}
            disabled={!name.value.trim()}>
            {t('space.create.submit')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
