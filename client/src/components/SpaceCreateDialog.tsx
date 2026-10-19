/**
 * SpaceCreateDialog — space creation flow (§23.50).
 *
 * Creating lands you in the new space (not back on the list with a
 * toast), where its owner is asked "What is it for?" once
 * (``SpaceSetupCard``) — a new space starts with feed, chat and calendar.
 *
 * A public space can be a **local community** ("Only for households
 * nearby"): the owner picks a centre on the map — or takes the
 * household's home — and a radius; households join only while their home
 * lies inside. Never this device's position, and no place search (that
 * would be a third-party fetch).
 */
import { signal } from '@preact/signals'
import { useRef } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { currentUser } from '@/store/auth'
import { ensureHouseholdHome, householdHome } from '@/store/householdHome'
import type { LocalArea } from '@/types'
import { areaLabel, radiusLabel } from '@/utils/localArea'
import { coordLabel } from '@/utils/coordLabel'
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
import { LocalAreaMap } from './LocalAreaMap'
import { LocalAreaPicker, PIN_DOT_KM } from './LocalAreaPicker'
import { t } from '@/i18n/i18n'
import { markSetupPending } from '@/features/spaces/spaceSetup'

const open = signal(false)
const name = signal('')
const description = signal('')
const emoji = signal('')
const spaceType = signal('private')
const joinMode = signal('invite_only')
// A public space may be a local community: households join only while
// their home lies inside this area (centre 4 dp + radius).
const localOn = signal(false)
const localArea = signal<LocalArea | null>(null)
// A global space's optional place pin (sorting only) — never the home.
const placePin = signal<{ lat: number; lon: number; radius_km?: number } | null>(null)
const pickingPin = signal(false)
const pickingArea = signal(false)
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
  minAge.value = 0
  category.value = 'general'
  gfsChoices.value = []
  gfsSelected.value = []
  gfsError.value = null
  localOn.value = false
  localArea.value = null
  pickingArea.value = false
  placePin.value = null
  pickingPin.value = false
  void ensureHouseholdHome()
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

const isGlobal = () => spaceType.value === 'global'
const isPublic = () => spaceType.value === 'public'
/** "Only for households nearby" is on but no area is chosen yet. */
const areaMissing = () => isPublic() && localOn.value && localArea.value === null

function toggleLocal(on: boolean) {
  localOn.value = on
  if (on && localArea.value === null) pickingArea.value = true
}
const isDiscoverable = () =>
  spaceType.value === 'public' || spaceType.value === 'global'

export function SpaceCreateDialog() {
  const gfsChecklistRef = useRef<HTMLFieldSetElement | null>(null)
  const loc = useLocation()
  const handleSubmit = async () => {
    if (!name.value.trim() || submitting.value || areaMissing()) return
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
      const created = await api.post<{ id: string }>('/api/spaces', {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        space_type: spaceType.value,
        join_mode: joinMode.value,
        ...(discoverable ? { category: category.value } : {}),
        ...(discoverable && minAge.value ? { min_age: minAge.value } : {}),
        ...(isGlobal() ? { gfs_ids: gfsSelected.value } : {}),
        ...(isGlobal() && placePin.value ? { place: placePin.value } : {}),
        ...(isPublic() && localOn.value && localArea.value
          ? {
            lat: localArea.value.lat,
            lon: localArea.value.lon,
            radius_km: localArea.value.radius_km,
          }
          : {}),
      })
      // The owner is asked "What is it for?" on the first visit — a hint:
      // if it can't be stored, the space simply opens without the card.
      try { await markSetupPending(created.id) } catch { /* no card */ }
      // Refresh the cached spaces list (sidebar, Spaces page) without a
      // hard reload, then land in the new space.
      void loadSpaces()
      open.value = false
      loc.route?.(`/spaces/${created.id}`)
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
        {isPublic() && !tierLocked && (
          <fieldset class="sh-form-fieldset sh-space-create-local">
            <legend>🏘️ {t('local_area.create.legend')}</legend>
            <label class="sh-toggle-row">
              <input type="checkbox" checked={localOn.value}
                     onChange={(e) => toggleLocal((e.target as HTMLInputElement).checked)} />
              <span class="sh-toggle-row__text">
                <span>{t('local_area.create.toggle')}</span>
                <span class="sh-toggle-row__sub">{t('local_area.create.toggle_sub')}</span>
              </span>
            </label>
            {localOn.value && (
              localArea.value ? (
                <div class="sh-space-create-local__area">
                  <LocalAreaMap area={localArea.value} home={householdHome.value} height={140} />
                  <p class="sh-muted" role="status">{areaLabel(localArea.value)}</p>
                  <Button variant="secondary" onClick={() => { pickingArea.value = true }}>
                    {t('local_area.change')}
                  </Button>
                </div>
              ) : (
                <div class="sh-space-create-local__area">
                  <p class="sh-muted" role="status">{t('local_area.create.choose_hint')}</p>
                  <Button variant="secondary" onClick={() => { pickingArea.value = true }}>
                    {t('local_area.choose')}
                  </Button>
                </div>
              )
            )}
          </fieldset>
        )}
        {isGlobal() && !tierLocked && (
          <fieldset class="sh-form-fieldset sh-space-create-local sh-space-create-place">
            <legend>📍 {t('place_pin.create.legend')}</legend>
            {placePin.value ? (
              <div class="sh-space-create-local__area">
                <LocalAreaMap area={{ lat: placePin.value.lat, lon: placePin.value.lon,
                                      radius_km: placePin.value.radius_km ?? PIN_DOT_KM }} height={140}
                              zoneName={t('place_pin.map_zone')} ariaLabel={t('place_pin.map_label')} />
                <p class="sh-muted" role="status">
                  {t('place_pin.settings.where', { place: coordLabel(placePin.value.lat, placePin.value.lon) })}
                  {placePin.value.radius_km != null && (
                    <> · {t('place_pin.hint.within', { radius: radiusLabel(placePin.value.radius_km) })}</>
                  )}
                </p>
                <div class="sh-row" style={{ gap: 'var(--sh-space-sm)' }}>
                  <Button variant="secondary" onClick={() => { pickingPin.value = true }}>
                    {t('place_pin.settings.change')}
                  </Button>
                  <Button variant="ghost" onClick={() => { placePin.value = null }}>
                    {t('place_pin.settings.remove')}
                  </Button>
                </div>
              </div>
            ) : (
              <div class="sh-space-create-local__area">
                <p class="sh-muted">{t('place_pin.create.hint')}</p>
                <Button variant="secondary" onClick={() => { pickingPin.value = true }}>
                  {t('place_pin.settings.make')}
                </Button>
              </div>
            )}
            <p class="sh-ss-hint">{t('place_pin.privacy')}</p>
          </fieldset>
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
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => open.value = false}>{t('common.cancel')}</Button>
          <Button onClick={handleSubmit} loading={submitting.value}
            disabled={!name.value.trim() || areaMissing()}>
            {t('space.create.submit')}
          </Button>
        </div>
      </div>
      <LocalAreaPicker
        open={pickingArea.value}
        initial={localArea.value}
        home={householdHome.value}
        onClose={() => {
          pickingArea.value = false
          // Backed out of the first pick: nothing chosen, so it's off again.
          if (localArea.value === null) localOn.value = false
        }}
        onSubmit={(area) => {
          localArea.value = area
          pickingArea.value = false
        }}
      />
      <LocalAreaPicker
        mode="pin"
        open={pickingPin.value}
        initial={placePin.value
          ? { lat: placePin.value.lat, lon: placePin.value.lon, radius_km: PIN_DOT_KM } : null}
        initialHintKm={placePin.value?.radius_km ?? null}
        home={null}
        onClose={() => { pickingPin.value = false }}
        onSubmit={(p, hintKm) => {
          placePin.value = { lat: p.lat, lon: p.lon, ...(hintKm != null ? { radius_km: hintKm } : {}) }
          pickingPin.value = false
        }}
      />
    </Modal>
  )
}
