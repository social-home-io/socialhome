/**
 * ProfileTab — Settings › Profile: "Your profile, at a glance".
 *
 * Leads with a slim "How others see you" card (the PersonSheet header
 * anatomy: photo, name, @handle, status, bio) that carries the photo
 * actions, then ONE form — name, status, about you, @handle — with a save
 * bar that only appears while something changed (decision 7).
 *
 * The fields live on three endpoints with their own validation, so Save
 * sends only what changed, one request per field: ``PATCH /api/me``
 * (``display_name``; ``bio``; the ``status_*`` keys — the route handles
 * one group per call) and ``POST /api/me/handle``. Each failure (a 422
 * "handle taken", say) lands on its own field and stays unsaved while the
 * others save — a partial failure never loses the parts that worked.
 *
 * The edits are module state, so a hop to another Settings tab keeps
 * them; leaving Settings with unsaved edits asks first (the router's
 * ``navigationGuard`` plus ``beforeunload``).
 */
import { useEffect, useRef } from 'preact/hooks'
import { computed, signal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import {
  StatusFields, formatClearsAt, liveStatus, sameStatus, statusDraftFrom,
  statusPatchBody, type StatusDraft,
} from '@/components/StatusEditor'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { isOne, t } from '@/i18n/i18n'
import { setNavigationGuard, useBeforeUnload } from '@/router/navigationGuard'
import { currentUser } from '@/store/auth'
import type { User } from '@/types'
import { HANDLE_INPUT_ID, HandleEditor, handleBaseline } from './HandleEditor'
import { ProtectedAccountSection } from './ProtectedAccountSection'

/** The fields of the one form, in save (and focus) order. */
export type ProfileField = 'name' | 'bio' | 'status' | 'handle'
const FIELDS: ProfileField[] = ['name', 'bio', 'status', 'handle']

interface ProfileValues {
  name: string
  bio: string
  status: StatusDraft
  handle: string
}

/** Server caps (``socialhome/domain/user.py``). */
const NAME_MAX = 64
const BIO_MAX = 300

/** Only the fields the user touched; the rest read from ``currentUser``
 *  so a status changed on another device never shows up as "unsaved". */
export const profileEdits = signal<Partial<ProfileValues>>({})
export const profileErrors = signal<Partial<Record<ProfileField, string>>>({})
const saving = signal(false)

function savedValues(user: User | null): ProfileValues {
  return {
    name: user?.display_name ?? '',
    bio: user?.bio ?? '',
    status: statusDraftFrom(user?.status),
    handle: handleBaseline(user),
  }
}

function changed(field: ProfileField, edits: Partial<ProfileValues>, saved: ProfileValues): boolean {
  switch (field) {
    case 'name': return edits.name !== undefined && edits.name.trim() !== saved.name
    case 'bio': return edits.bio !== undefined && edits.bio.trim() !== saved.bio.trim()
    case 'handle': return edits.handle !== undefined && edits.handle.trim() !== saved.handle
    case 'status': return edits.status !== undefined && !sameStatus(edits.status, saved.status)
  }
}

const dirtyFields = computed<ProfileField[]>(() => {
  const saved = savedValues(currentUser.value)
  return FIELDS.filter(f => changed(f, profileEdits.value, saved))
})

/** True while the form holds changes Save would send. */
export function profileIsDirty(): boolean {
  return dirtyFields.value.length > 0
}

/** Drop every edit and error — Discard, leaving, or a fresh visit. */
export function resetProfileDraft(): void {
  profileEdits.value = {}
  profileErrors.value = {}
}

function edit<K extends keyof ProfileValues>(field: K, value: ProfileValues[K]): void {
  profileEdits.value = { ...profileEdits.value, [field]: value }
  if (profileErrors.value[field]) {
    const { [field]: _gone, ...rest } = profileErrors.value
    profileErrors.value = rest
  }
}

function mergeUser(patch: Partial<User>): void {
  if (currentUser.value) currentUser.value = { ...currentUser.value, ...patch }
}

function errorText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message || fallback
  return (err as Error)?.message || fallback
}

const FOCUS_SELECTOR: Record<ProfileField, string> = {
  name: '#sh-profile-name',
  bio: '#sh-profile-bio',
  status: '.sh-profile-status .sh-status-text',
  handle: `#${HANDLE_INPUT_ID}`,
}

/** Send one changed field. Throws a user-facing message on failure. */
async function saveField(field: ProfileField, v: ProfileValues, keepUntil: string | null): Promise<void> {
  switch (field) {
    case 'name': {
      const name = v.name.trim()
      if (!name) throw new Error(t('settings.profile.name_required'))
      mergeUser(await api.patch('/api/me', { display_name: name }) as User)
      return
    }
    case 'bio':
      mergeUser(await api.patch('/api/me', { bio: v.bio.trim() || null }) as User)
      return
    case 'status':
      mergeUser(await api.patch('/api/me', statusPatchBody(v.status, keepUntil)) as User)
      return
    case 'handle': {
      const handle = v.handle.trim()
      if (!handle) throw new Error(t('settings.profile.handle_required'))
      const res = await api.post('/api/me/handle', { handle }) as { handle?: string }
      mergeUser({ handle: res?.handle ?? handle })
      return
    }
  }
}

/** Save every changed field; a failed one keeps its edit and gets its
 *  error, the others save. Returns the fields that failed. */
export async function saveProfile(): Promise<ProfileField[]> {
  const fields = dirtyFields.value
  if (fields.length === 0 || saving.value) return []
  const saved = savedValues(currentUser.value)
  const values: ProfileValues = { ...saved, ...profileEdits.value }
  const keepUntil = liveStatus(currentUser.value?.status)?.expires_at ?? null
  saving.value = true
  const errors: Partial<Record<ProfileField, string>> = {}
  const done: ProfileField[] = []
  try {
    for (const field of fields) {
      try {
        await saveField(field, values, keepUntil)
        done.push(field)
      } catch (err) {
        errors[field] = errorText(err, t('settings.profile.field_failed'))
      }
    }
  } finally {
    saving.value = false
  }
  const remaining = { ...profileEdits.value }
  for (const f of done) delete remaining[f]
  profileEdits.value = remaining
  profileErrors.value = errors
  const failed = FIELDS.filter(f => errors[f])
  if (failed.length === 0) showToast(t('settings.profile.saved'), 'success')
  else if (done.length > 0) showToast(t('settings.profile.partly_saved'), 'error')
  else showToast(t('settings.profile.not_saved'), 'error')
  return failed
}

/** Settings' guard: asks before an in-app route change or a reload
 *  would drop unsaved profile edits. Mount once per Settings visit. */
export function useProfileLeaveGuard(path: string): void {
  useEffect(() => setNavigationGuard({
    path,
    isDirty: profileIsDirty,
    confirm: async () => {
      const ok = await confirmDialog(t('settings.profile.leave_body'), {
        title: t('settings.profile.leave_title'),
        confirmLabel: t('settings.profile.leave_discard'),
        cancelLabel: t('settings.profile.leave_stay'),
        destructive: true,
      })
      if (ok) resetProfileDraft()
      return ok
    },
  }), [path])
  useBeforeUnload(dirtyFields.value.length > 0)
}

export function ProfileTab() {
  const user = currentUser.value
  const saved = savedValues(user)
  const values: ProfileValues = { ...saved, ...profileEdits.value }
  const errors = profileErrors.value
  const dirty = dirtyFields.value.length > 0
  const busy = saving.value
  const formRef = useRef<HTMLFormElement | null>(null)

  const onSubmit = async (e: Event) => {
    e.preventDefault()
    const failed = await saveProfile()
    if (failed.length > 0) {
      formRef.current?.querySelector<HTMLElement>(FOCUS_SELECTOR[failed[0]])?.focus()
    }
  }

  const bioLeft = BIO_MAX - values.bio.length

  return (
    <div class="sh-profile">
      <ProtectedAccountSection />
      <ProfileGlance user={user} />

      <form
        id="sh-profile-form"
        ref={formRef}
        class="sh-profile-form"
        onSubmit={onSubmit}
        noValidate
        aria-label={t('settings.profile.form_aria')}
      >
        <div class="sh-profile-field">
          <label class="sh-profile-field__label" for="sh-profile-name">
            {t('settings.profile.name')}
          </label>
          <input
            id="sh-profile-name"
            value={values.name}
            maxLength={NAME_MAX}
            autocomplete="name"
            disabled={busy}
            aria-invalid={errors.name ? true : undefined}
            aria-describedby={errors.name ? 'sh-profile-name-error' : undefined}
            onInput={(e) => edit('name', (e.target as HTMLInputElement).value)}
          />
          <FormError id="sh-profile-name-error" message={errors.name} />
        </div>

        <div
          class="sh-profile-field sh-profile-status"
          role="group"
          aria-labelledby="sh-profile-status-label"
        >
          <span id="sh-profile-status-label" class="sh-profile-field__label">
            {t('settings.profile.status')}
          </span>
          <StatusFields
            value={values.status}
            keepUntil={liveStatus(user?.status)?.expires_at ?? null}
            onChange={(next) => edit('status', next)}
            onClear={() => edit('status', { emoji: '', text: '', clearAfter: 'none' })}
            disabled={busy}
            invalid={!!errors.status}
            describedBy={errors.status ? 'sh-profile-status-error' : undefined}
          />
          <FormError id="sh-profile-status-error" message={errors.status} />
        </div>

        <div class="sh-profile-field">
          <label class="sh-profile-field__label" for="sh-profile-bio">
            {t('settings.profile.about')}
          </label>
          <textarea
            id="sh-profile-bio"
            value={values.bio}
            maxLength={BIO_MAX}
            rows={3}
            placeholder={t('settings.profile.about_placeholder')}
            disabled={busy}
            aria-invalid={errors.bio ? true : undefined}
            aria-describedby={errors.bio ? 'sh-profile-bio-left sh-profile-bio-error' : 'sh-profile-bio-left'}
            onInput={(e) => edit('bio', (e.target as HTMLTextAreaElement).value)}
          />
          <span id="sh-profile-bio-left" class="sh-profile-field__hint">
            {t(isOne(bioLeft) ? 'settings.profile.chars_left_one' : 'settings.profile.chars_left',
              { n: String(bioLeft) })}
          </span>
          <FormError id="sh-profile-bio-error" message={errors.bio} />
        </div>

        <HandleEditor
          value={values.handle}
          onInput={(v) => edit('handle', v)}
          error={errors.handle}
          disabled={busy}
        />
      </form>

      {dirty && (
        <div class="sh-savebar" role="region" aria-label={t('settings.profile.unsaved')}>
          <span class="sh-savebar__text" role="status">{t('settings.profile.unsaved')}</span>
          <div class="sh-savebar__actions">
            <Button type="button" variant="secondary" onClick={resetProfileDraft} disabled={busy}>
              {t('settings.profile.discard')}
            </Button>
            <Button type="submit" form="sh-profile-form" loading={busy}>
              {t('common.save')}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

/** "How others see you": the saved profile, as the person sheet shows
 *  it, plus the photo actions (Change first; Remove stays quiet — P6). */
function ProfileGlance({ user }: { user: User | null }) {
  const fileRef = useRef<HTMLInputElement | null>(null)
  const name = user?.display_name || '—'
  const status = liveStatus(user?.status)
  const isHaUser = user?.source === 'ha'
  const picture = user?.picture_url ?? null

  const refresh = async () => {
    try {
      mergeUser(await api.get('/api/me') as User)
    } catch { /* the toast below already says what happened */ }
  }

  const pickPhoto = () => fileRef.current?.click()

  const onPhoto = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    const fd = new FormData()
    fd.append('file', file)
    try {
      await api.upload('/api/me/picture', fd)
      await refresh()
      showToast(t('settings.profile.avatar_updated'), 'success')
    } catch (err: unknown) {
      showToast(t('settings.profile.avatar_upload_failed', { error: errorText(err, '') }), 'error')
    }
    input.value = ''
  }

  const onRemove = async () => {
    if (!await confirmDialog(t('settings.profile.remove_confirm'), { destructive: true })) return
    try {
      await api.delete('/api/me/picture')
      await refresh()
      showToast(t('settings.profile.avatar_removed'), 'info')
    } catch (err: unknown) {
      showToast(t('settings.profile.clear_failed', { error: errorText(err, '') }), 'error')
    }
  }

  const onUseHaPicture = async () => {
    try {
      await api.post('/api/me/picture/refresh-from-ha', {})
      await refresh()
      showToast(t('settings.profile.ha_synced'), 'success')
    } catch (err: unknown) {
      // 422: HA has no entity_picture for this person yet.
      if (err instanceof ApiError && err.status === 422) {
        showToast(t('settings.profile.ha_no_picture'), 'info')
        return
      }
      // 501: the adapter doesn't expose HA pictures (standalone mode).
      if (err instanceof ApiError && err.status === 501) {
        showToast(t('settings.profile.ha_not_ha_mode'), 'info')
        return
      }
      showToast((err as Error).message || t('settings.profile.ha_sync_failed'), 'error')
    }
  }

  return (
    <section class="sh-profile-glance" aria-labelledby="sh-profile-glance-name">
      <span class="sh-profile-glance__photo">
        {/* Pointer shortcut to the same picker as "Change photo" below —
         *  out of the Tab order and the accessibility tree so keyboard
         *  and screen-reader users meet one control, not two. */}
        <button
          type="button"
          class="sh-profile-glance__photo-btn"
          tabIndex={-1}
          aria-hidden="true"
          title={t('settings.profile.change_photo')}
          onClick={pickPhoto}
        >
          <Avatar name={name} src={picture} size={72} />
          <span class="sh-profile-glance__cam" aria-hidden="true">
            <ConnIcon name="camera" />
          </span>
        </button>
      </span>
      <div class="sh-profile-glance__body">
        <p class="sh-conn-eyebrow">{t('settings.profile.how_others_see')}</p>
        <h2 id="sh-profile-glance-name" class="sh-profile-glance__name">{name}</h2>
        <p class="sh-profile-glance__handle">@{handleBaseline(user)}</p>
        {status && (
          <p class="sh-person-sheet__status sh-profile-glance__status" data-testid="profile-glance-status">
            {status.emoji && <span aria-hidden="true">{status.emoji}</span>}
            {status.text && <span class="sh-person-sheet__status-text">{status.text}</span>}
            {status.expires_at && (
              <span class="sh-person-sheet__until">
                · {t('person.status_until', { time: formatClearsAt(status.expires_at) })}
              </span>
            )}
          </p>
        )}
        {user?.bio && <p class="sh-profile-glance__bio">{user.bio}</p>}
        {isHaUser && (
          <span class="sh-profile-source sh-profile-source--ha">
            🏠 {t('settings.profile.source_ha')}
          </span>
        )}
        <div class="sh-profile-glance__actions">
          <Button type="button" variant="secondary" onClick={pickPhoto}>
            {t('settings.profile.change_photo')}
          </Button>
          {isHaUser && (
            <Button type="button" variant="secondary" onClick={() => void onUseHaPicture()}>
              {t('settings.profile.use_ha_picture')}
            </Button>
          )}
          {picture && (
            <button type="button" class="sh-profile-glance__quiet" onClick={() => void onRemove()}>
              {t('settings.profile.remove_photo')}
            </button>
          )}
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          class="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => void onPhoto(e)}
        />
      </div>
    </section>
  )
}
