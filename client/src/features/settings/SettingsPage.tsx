import { useEffect, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal, useSignal } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { api, ApiError } from '@/api'
import { Avatar } from '@/components/Avatar'
import type { User } from '@/types'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { theme, type Theme } from '@/store/theme'
import { HouseholdThemeStudio } from '@/components/HouseholdThemeStudio'
import { locale, setLocale } from '@/i18n/i18n'
import localeMeta from '@/i18n/locales/_meta.json'
import {
  getLandingPath,
  getPreferences,
  setPreference,
  type LandingPath,
} from '@/utils/preferences'
import { confirmDialog } from '@/components/confirm'
import { blockedUsers, loadBlocks, unblockUser } from '@/store/blocks'
import { followedUsers, loadFollows, unfollowUser } from '@/store/follows'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { relativeDocsTime } from '@/utils/relativeTime'
import { userPreferences } from '@/store/userPreferences'
import { isHomeAssistant } from '@/platform'
import {
  currentPushSubscription, disableWebPush, enableWebPush, webPushSupported,
} from '@/utils/webPush'
import { UsernameEditor } from './UsernameEditor'
import { HandleEditor } from './HandleEditor'

interface SpaceLocationRow {
  space_id: string
  space_name: string
  space_emoji: string | null
  location_share_enabled: boolean
}

export const spaceLocationRows = signal<SpaceLocationRow[]>([])
export const spaceLocationLoading = signal(false)

type SettingsTab = 'profile' | 'privacy' | 'notifications' | 'appearance'

const activeTab = signal<SettingsTab>('profile')
const displayName = signal('')
const bio = signal('')
const landingPath = signal<LandingPath>('/')
const avatarUrl = signal<string | null>(null)
// Per-user HA Companion-app notify service (§25.3). Only meaningful in
// ha / haos mode; the field is hidden otherwise.
const haNotifyService = signal('')
const haNotifySaving = signal(false)
const onlineStatusVisible = signal(true)
const pushEnabled = signal(false)
const pushBusy = signal(false)

/** Read ``online_status_visible`` from the cached
 *  ``currentUser.preferences_json``. The previous implementation
 *  GET'd a non-existent ``/api/me/privacy`` endpoint, hard-failed
 *  the route after PR #126's load-error chip surfaced the 404, and
 *  showed every user a "Couldn't load your privacy settings" panel.
 *  Privacy is just a user preference (same store as Highlights prefs);
 *  read it inline from the user we already loaded on cold start. */
function syncOnlineStatusFromUser(): void {
  const raw = (currentUser.value as unknown as { preferences_json?: string } | null)
    ?.preferences_json
  if (!raw) return
  try {
    const prefs = JSON.parse(raw) as { online_status_visible?: boolean }
    if (typeof prefs.online_status_visible === 'boolean') {
      onlineStatusVisible.value = prefs.online_status_visible
    }
  } catch { /* keep the default */ }
}

export default function SettingsPage() {
  useTitle('Settings')
  useEffect(() => {
    if (currentUser.value) {
      displayName.value = currentUser.value.display_name
      bio.value = currentUser.value.bio || ''
      avatarUrl.value = currentUser.value.picture_url
    }
    landingPath.value = getLandingPath()
    haNotifyService.value = getPreferences().ha_notify_service ?? ''
    syncOnlineStatusFromUser()
  }, [])

  const panelId = (t: SettingsTab) => `sh-settings-panel-${t}`
  const tabId   = (t: SettingsTab) => `sh-settings-tab-${t}`

  return (
    <div class="sh-settings">
      <nav class="sh-settings-tabs" role="tablist">
        {(['profile', 'privacy', 'notifications', 'appearance'] as SettingsTab[]).map(t => (
          <button
            key={t}
            type="button"
            role="tab"
            id={tabId(t)}
            aria-selected={activeTab.value === t}
            aria-controls={panelId(t)}
            tabIndex={activeTab.value === t ? 0 : -1}
            class={activeTab.value === t ? 'sh-tab sh-tab--active' : 'sh-tab'}
            onClick={() => { activeTab.value = t }}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <div role="tabpanel" id={panelId('profile')} aria-labelledby={tabId('profile')} hidden={activeTab.value !== 'profile'}>
        {activeTab.value === 'profile' && <ProfileTab />}
      </div>
      <div role="tabpanel" id={panelId('privacy')} aria-labelledby={tabId('privacy')} hidden={activeTab.value !== 'privacy'}>
        {activeTab.value === 'privacy' && <PrivacyTab />}
      </div>
      <div role="tabpanel" id={panelId('notifications')} aria-labelledby={tabId('notifications')} hidden={activeTab.value !== 'notifications'}>
        {activeTab.value === 'notifications' && <NotificationsTab />}
      </div>
      <div role="tabpanel" id={panelId('appearance')} aria-labelledby={tabId('appearance')} hidden={activeTab.value !== 'appearance'}>
        {activeTab.value === 'appearance' && <AppearanceTab />}
      </div>
    </div>
  )
}

function ProfileTab() {
  const refresh = async () => {
    try {
      const me = await api.get('/api/me') as User
      displayName.value = me.display_name
      bio.value = me.bio ?? ''
      avatarUrl.value = me.picture_url
      // Mirror the fresh user onto the auth store so other surfaces
      // (sidenav, profile card, post avatars built from currentUser)
      // pick up the new ``picture_url`` immediately AND so leaving and
      // returning to this page doesn't reset to a stale signed URL
      // from the original ``loadCurrentUser`` fetch.
      if (currentUser.value) {
        currentUser.value = { ...currentUser.value, ...me }
      }
    } catch { /* noop */ }
  }

  const handleSave = async (e: Event) => {
    e.preventDefault()
    try {
      await api.patch('/api/me', {
        display_name: displayName.value,
        bio: bio.value || null,
      })
      showToast('Settings saved', 'success')
    } catch (err: unknown) {
      showToast(
        `Save failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const handleAvatarUpload = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    const fd = new FormData()
    fd.append('file', file)
    try {
      await api.upload('/api/me/picture', fd)
      await refresh()
      showToast('Avatar updated', 'success')
    } catch (err: unknown) {
      showToast(
        `Avatar upload failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
    input.value = ''
  }

  const handleAvatarClear = async () => {
    if (!await confirmDialog('Remove your profile picture?', { destructive: true })) return
    try {
      await api.delete('/api/me/picture')
      await refresh()
      showToast('Avatar removed', 'info')
    } catch (err: unknown) {
      showToast(
        `Clear failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const handleUseHaPicture = async () => {
    try {
      await api.post('/api/me/picture/refresh-from-ha', {})
      await refresh()
      showToast('Synced picture from Home Assistant', 'success')
    } catch (err: unknown) {
      // 422 with the body the route returns when HA has no
      // entity_picture for this person — the friendliest copy says
      // "go set one in HA first", not the raw backend detail.
      if (err instanceof ApiError && err.status === 422) {
        showToast(
          'Home Assistant has no profile picture for you yet. '
          + 'Set one on your person entity in HA, then try again.',
          'info',
        )
        return
      }
      // 501 — adapter doesn't expose HA pictures (standalone mode).
      if (err instanceof ApiError && err.status === 501) {
        showToast(
          'This Social Home isn’t running in Home Assistant mode, '
          + 'so there’s no HA picture to sync.',
          'info',
        )
        return
      }
      // Generic fallthrough — ``err.message`` now carries the
      // backend's ``detail`` field (or a helpful network-error
      // string), so the toast already reads cleanly without us
      // prefixing "Could not fetch from HA:".
      showToast((err as Error).message || 'Couldn’t sync from HA', 'error')
    }
  }

  const isHaUser = currentUser.value?.source === 'ha'
  const bioRemaining = 300 - bio.value.length

  return (
    <section class="sh-settings-section">
      <h2>Profile</h2>
      <div class="sh-profile-card">
        <label class="sh-profile-avatar-slot"
               title="Click or drop an image to change your avatar">
          <Avatar name={displayName.value || '?'} src={avatarUrl.value}
                  size={112} />
          {/* Persistent corner badge — the avatar is clickable, but
           *  on touch devices the previous hover-only overlay never
           *  appeared, so users had no cue the photo could be changed.
           *  The hover overlay below remains for quick desktop tap. */}
          <span class="sh-profile-avatar-badge" aria-hidden="true">📷</span>
          <span class="sh-profile-avatar-hint" aria-hidden="true">
            📷 Change
          </span>
          <input type="file" accept="image/*"
                 onChange={handleAvatarUpload} class="sr-only" />
        </label>
        <div class="sh-profile-card-meta">
          <div class="sh-profile-identity">
            <strong class="sh-profile-name">
              {displayName.value || '—'}
            </strong>
            <span class="sh-muted">
              @{currentUser.value?.handle ?? currentUser.value?.username}
            </span>
          </div>
          {/* Source badge — informational only ("where this profile is
           *  edited from"), not a button. The previous copy "✏️ Set
           *  manually" parsed as an imperative call-to-action; "Edited
           *  here" makes it clear this is just describing the source. */}
          <span class={`sh-profile-source sh-profile-source--${isHaUser ? 'ha' : 'manual'}`}>
            {isHaUser ? '🏠 Synced from Home Assistant' : '✏️ Edited here'}
          </span>
          <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', flexWrap: 'wrap' }}>
            {avatarUrl.value && (
              <Button variant="secondary" onClick={handleAvatarClear}>
                Remove picture
              </Button>
            )}
            {isHaUser && (
              <Button variant="secondary" onClick={handleUseHaPicture}>
                Use Home Assistant picture
              </Button>
            )}
          </div>
        </div>
      </div>
      <form class="sh-form" onSubmit={handleSave}>
        <label>
          Display name
          <input value={displayName.value} maxLength={64}
                 onInput={(e) => displayName.value = (e.target as HTMLInputElement).value} />
        </label>
        <label>
          Bio
          <textarea value={bio.value} maxLength={300} rows={3}
                    onInput={(e) => bio.value = (e.target as HTMLTextAreaElement).value} />
          <span class="sh-char-count">
            {bioRemaining} characters left
          </span>
        </label>
        <div class="sh-form-actions">
          <Button type="submit">Save profile</Button>
        </div>
      </form>

      <HandleEditor />

      <UsernameEditor />

      <LandingPicker />
    </section>
  )
}

function LandingPicker() {
  const handleChange = async (choice: LandingPath) => {
    const prev = landingPath.value
    landingPath.value = choice
    try {
      await setPreference('landing_path', choice)
      showToast(
        choice === '/dashboard'
          ? 'Landing page set to My Corner'
          : choice === '/feed'
            ? 'Landing page set to the feed'
            : 'Landing page set to Welcome',
        'success',
      )
    } catch (err: unknown) {
      landingPath.value = prev
      showToast(
        `Could not save: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  return (
    <div class="sh-landing-picker">
      <h3>Home page</h3>
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)', margin: 0 }}>
        Which page opens when you tap the Social Home logo.
      </p>
      <div class="sh-landing-picker-options" role="radiogroup"
           aria-label="Landing page">
        <label class={`sh-landing-option ${landingPath.value === '/' ? 'sh-landing-option--active' : ''}`}>
          <input type="radio" name="landing" value="/"
                 checked={landingPath.value === '/'}
                 onChange={() => void handleChange('/')} />
          <span class="sh-landing-option-icon">☀️</span>
          <span class="sh-landing-option-body">
            <strong>Welcome</strong>
            <span class="sh-muted">Today's events, pending tasks, catch-up</span>
          </span>
        </label>
        <label class={`sh-landing-option ${landingPath.value === '/feed' ? 'sh-landing-option--active' : ''}`}>
          <input type="radio" name="landing" value="/feed"
                 checked={landingPath.value === '/feed'}
                 onChange={() => void handleChange('/feed')} />
          <span class="sh-landing-option-icon">📰</span>
          <span class="sh-landing-option-body">
            <strong>Household feed</strong>
            <span class="sh-muted">Posts, photos, conversations</span>
          </span>
        </label>
        <label class={`sh-landing-option ${landingPath.value === '/dashboard' ? 'sh-landing-option--active' : ''}`}>
          <input type="radio" name="landing" value="/dashboard"
                 checked={landingPath.value === '/dashboard'}
                 onChange={() => void handleChange('/dashboard')} />
          <span class="sh-landing-option-icon">🏠</span>
          <span class="sh-landing-option-body">
            <strong>My Corner</strong>
            <span class="sh-muted">Full dashboard with bazaar, presence map, more</span>
          </span>
        </label>
      </div>
    </div>
  )
}

function PrivacyTab() {
  const toggleOnlineStatus = async () => {
    onlineStatusVisible.value = !onlineStatusVisible.value
    try {
      // Privacy preferences ride on the existing
      // ``users.preferences_json`` blob — same store ``HighlightsPrefs``
      // uses. PATCH /api/me with a ``preferences`` patch shallow-merges
      // the keys, so unrelated prefs are untouched.
      const updated = await api.patch('/api/me', {
        preferences: { online_status_visible: onlineStatusVisible.value },
      }) as { preferences_json?: string }
      // Mirror the server's authoritative blob onto the auth store so
      // a tab switch / reload reads the same value without an extra
      // /api/me round-trip.
      if (currentUser.value && updated.preferences_json) {
        currentUser.value = {
          ...currentUser.value,
          preferences_json: updated.preferences_json,
        } as User
      }
      showToast('Privacy updated', 'success')
    } catch {
      onlineStatusVisible.value = !onlineStatusVisible.value
      showToast('Failed to update privacy', 'error')
    }
  }

  return (
    <section class="sh-settings-section">
      <h2>Privacy</h2>
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={onlineStatusVisible.value}
          onChange={toggleOnlineStatus}
        />
        Show online status to other household members
      </label>
      <SidebarVisibilityPanel />
      <SpaceLocationSharingPanel />
      <HighlightsPreferencesPanel />
      <MomentumPanel />
      <BlockedAccountsPanel />
      <FollowingPanel />
    </section>
  )
}


/**
 * SidebarVisibilityPanel — lets the signed-in user show or hide the three
 * personal sidebar sections (Highlights, Momentum, Bazaar) that are owned at
 * the user level, not the household level.  PATCH /api/me/preferences is
 * called optimistically; a failure reverts the local signal and shows a toast.
 *
 * The flag semantics are intentionally inverted from the label: the API stores
 * ``hide_*`` booleans, but the checkbox reads "Show … in my sidebar" so a
 * checked box means the section IS visible (hide_* = false).
 */
function SidebarVisibilityPanel() {
  const prefs = userPreferences.value

  const toggle = async (field: 'hide_highlights' | 'hide_momentum' | 'hide_bazaar') => {
    const prev = userPreferences.value[field]
    const next = !prev
    // Optimistic update
    userPreferences.value = { ...userPreferences.value, [field]: next }
    try {
      await api.patch('/api/me/preferences', { [field]: next })
    } catch (e: unknown) {
      // Revert on error
      userPreferences.value = { ...userPreferences.value, [field]: prev }
      showToast((e as Error).message || 'Failed to update sidebar visibility', 'error')
    }
  }

  return (
    <div class="sh-settings-subcard sh-sidebar-visibility-panel" id="sidebar-visibility">
      <h3 class="sh-settings-panel-heading">Show in my sidebar</h3>
      <p class="sh-muted sh-settings-panel-blurb">
        Choose which sections appear in your sidebar. These are personal
        preferences — other household members are not affected.
      </p>
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={!prefs.hide_highlights}
          onChange={() => void toggle('hide_highlights')}
        />
        Highlights
        <span class="sh-toggle-row-hint sh-muted">
          Your curated photo and moment archive.
        </span>
      </label>
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={!prefs.hide_momentum}
          onChange={() => void toggle('hide_momentum')}
        />
        Momentum
        <span class="sh-toggle-row-hint sh-muted">
          Federated moments from people you follow across households.
        </span>
      </label>
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={!prefs.hide_bazaar}
          onChange={() => void toggle('hide_bazaar')}
        />
        Bazaar
        <span class="sh-toggle-row-hint sh-muted">
          Browse listings shared across connected households.
        </span>
      </label>
    </div>
  )
}

/**
 * SpaceLocationSharingPanel — lists every space where the user is a member
 * and ``feature_location`` is on. Each row has a toggle that optimistically
 * flips local state and PATCHes the existing
 * ``/api/spaces/{id}/members/me/location-sharing`` endpoint.
 *
 * Discovery + audit in one place: users no longer have to open each space's
 * Map tab individually.
 */
function SpaceLocationSharingPanel() {
  useEffect(() => {
    spaceLocationLoading.value = true
    api.get('/api/me/space-location-sharing')
      .then((data: { spaces: SpaceLocationRow[] }) => {
        spaceLocationRows.value = data.spaces
        spaceLocationLoading.value = false
      })
      .catch(() => {
        spaceLocationLoading.value = false
      })
  }, [])

  const toggle = async (spaceId: string, currentValue: boolean) => {
    const rows = spaceLocationRows.value
    // Optimistic update
    spaceLocationRows.value = rows.map(r =>
      r.space_id === spaceId ? { ...r, location_share_enabled: !currentValue } : r,
    )
    try {
      await api.patch(`/api/spaces/${spaceId}/members/me/location-sharing`, {
        enabled: !currentValue,
      })
    } catch (e: unknown) {
      // Revert on error
      spaceLocationRows.value = rows
      showToast((e as Error).message || 'Failed to update location sharing', 'error')
    }
  }

  return (
    <div
      class="sh-settings-subcard sh-space-location-panel"
      id="space-location-sharing"
    >
      <h3 class="sh-settings-panel-heading">Space location sharing</h3>
      <p class="sh-muted sh-settings-panel-blurb">
        Choose which spaces see your live location. Admins enable the
        feature per-space; you decide whether to opt in.
      </p>
      {!spaceLocationLoading.value && spaceLocationRows.value.length === 0 && (
        <p class="sh-muted sh-settings-panel-blurb">
          No spaces with location sharing turned on. Ask an admin to
          enable Location in a space's settings.
        </p>
      )}
      {spaceLocationRows.value.map(row => (
        <label key={row.space_id} class="sh-toggle-row">
          <input
            type="checkbox"
            checked={row.location_share_enabled}
            onChange={() => void toggle(row.space_id, row.location_share_enabled)}
          />
          {row.space_emoji ? `${row.space_emoji} ` : ''}{row.space_name}
        </label>
      ))}
    </div>
  )
}

function FollowingPanel() {
  useEffect(() => {
    void loadFollows(true)
    void loadHouseholdUsers()
  }, [])
  const rows = followedUsers.value

  const onUnfollow = async (userId: string) => {
    const name = householdDisplayName(userId)
    if (!await confirmDialog(
      `Unfollow ${name}? Their moments older than 24 hours will stop `
      + `surfacing in your inbox.`,
      { confirmLabel: 'Unfollow' },
    )) return
    try {
      await unfollowUser(userId)
      showToast('Unfollowed', 'success')
    } catch (e: unknown) {
      showToast(`Couldn't unfollow: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  return (
    <div class="sh-following sh-settings-subcard">
      <h3 class="sh-settings-panel-heading">Following</h3>
      <p class="sh-muted sh-settings-panel-blurb">
        Following someone extends the moments retention window from 24
        hours to 7 days for their posts in your inbox.
      </p>
      {rows.length === 0 && (
        <p class="sh-muted sh-settings-panel-blurb">
          You aren't following anyone. Tap a moment author's name and
          choose Follow to start.
        </p>
      )}
      {rows.length > 0 && (
        <ul class="sh-following-list" aria-label="Following">
          {rows.map(f => {
            const name = householdDisplayName(f.user_id)
            return (
              <li key={f.user_id} class="sh-following-row">
                <Avatar
                  name={name}
                  src={householdPictureUrl(f.user_id)}
                  size={32}
                />
                <span class="sh-following-meta">
                  <strong>{name}</strong>
                  <span class="sh-muted">
                    Following since{' '}
                    <time
                      dateTime={f.created_at}
                      title={new Date(f.created_at).toLocaleString()}
                    >
                      {relativeDocsTime(f.created_at)}
                    </time>
                  </span>
                </span>
                <Button
                  variant="secondary"
                  onClick={() => void onUnfollow(f.user_id)}
                >
                  Unfollow
                </Button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}


function BlockedAccountsPanel() {
  useEffect(() => {
    void loadBlocks(true)
    void loadHouseholdUsers()
  }, [])
  const rows = blockedUsers.value

  const onUnblock = async (userId: string) => {
    const name = householdDisplayName(userId)
    if (!await confirmDialog(
      `Unblock ${name}? Their highlights, posts, presence and DMs will be `
      + `visible to you again.`,
      { confirmLabel: 'Unblock' },
    )) return
    try {
      await unblockUser(userId)
      showToast('Unblocked', 'success')
    } catch (e: unknown) {
      showToast(`Couldn't unblock: ${(e as Error)?.message ?? e}`, 'error')
    }
  }

  return (
    <div class="sh-blocked-accounts sh-settings-subcard">
      <h3 class="sh-settings-panel-heading">Blocked accounts</h3>
      {rows.length === 0 && (
        <p class="sh-muted sh-settings-panel-blurb">
          You haven't blocked anyone. Open a highlight or profile and tap
          the ⋯ menu to block someone.
        </p>
      )}
      {rows.length > 0 && (
        <ul class="sh-blocked-accounts-list" aria-label="Blocked accounts">
          {rows.map(b => {
            const name = householdDisplayName(b.user_id)
            return (
              <li key={b.user_id} class="sh-blocked-accounts-row">
                <Avatar
                  name={name}
                  src={householdPictureUrl(b.user_id)}
                  size={32}
                />
                <span class="sh-blocked-accounts-meta">
                  <strong>{name}</strong>
                  <span class="sh-muted">
                    Blocked{' '}
                    <time
                      dateTime={b.blocked_at}
                      title={new Date(b.blocked_at).toLocaleString()}
                    >
                      {relativeDocsTime(b.blocked_at)}
                    </time>
                  </span>
                </span>
                <Button
                  variant="secondary"
                  onClick={() => void onUnblock(b.user_id)}
                >
                  Unblock
                </Button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

interface HighlightsPrefs {
  retention_days?: number
  max_count?: number
  default_audience?: { kind: 'all_paired' | 'households' | 'users' }
}

function HighlightsPreferencesPanel() {
  // Read straight from the cached preferences each render — the
  // `setPreference` helper updates the cache in place, so the form
  // re-renders with the latest values when the user saves.
  const prefs = (() => {
    try {
      const raw = (currentUser.value as unknown as { preferences_json?: string } | null)
        ?.preferences_json
      if (!raw) return {} as HighlightsPrefs
      const parsed = JSON.parse(raw) as { highlights?: HighlightsPrefs }
      return parsed.highlights ?? {}
    } catch { return {} as HighlightsPrefs }
  })()
  const retentionDays = useSignal<number>(prefs.retention_days ?? 30)
  const maxCount = useSignal<number>(prefs.max_count ?? 100)
  const audienceKind = useSignal<'all_paired' | 'households' | 'users'>(
    prefs.default_audience?.kind ?? 'all_paired',
  )

  const save = async () => {
    try {
      await setPreference('highlights', {
        retention_days: retentionDays.value,
        max_count: maxCount.value,
        default_audience: { kind: audienceKind.value },
      })
      showToast('Highlights settings saved', 'success')
    } catch {
      showToast('Failed to save highlights settings', 'error')
    }
  }

  return (
    <div id="highlights" class="sh-settings-highlights-panel sh-settings-subcard">
      <h3>Highlights</h3>
      <p class="sh-muted">
        Control how long your highlights stay listed and who sees them by default.
      </p>
      <label class="sh-form-row">
        Retention (days)
        <input
          type="number"
          min={1}
          max={90}
          value={retentionDays.value}
          onInput={e => {
            const n = Number((e.target as HTMLInputElement).value)
            retentionDays.value = Number.isFinite(n) ? n : 30
          }}
        />
      </label>
      <label class="sh-form-row">
        Max highlights to keep
        <input
          type="number"
          min={10}
          max={500}
          value={maxCount.value}
          onInput={e => {
            const n = Number((e.target as HTMLInputElement).value)
            maxCount.value = Number.isFinite(n) ? n : 100
          }}
        />
      </label>
      <label class="sh-form-row">
        Default audience
        <select
          value={audienceKind.value}
          onChange={e => {
            const v = (e.target as HTMLSelectElement).value as
              'all_paired' | 'households' | 'users'
            audienceKind.value = v
          }}
        >
          <option value="all_paired">All connected households</option>
          <option value="households">Pick households per highlight</option>
          <option value="users">Pick people per highlight (advanced)</option>
        </select>
      </label>
      <div class="sh-form-actions">
        <Button onClick={save}>Save</Button>
      </div>
    </div>
  )
}

function MomentumPanel() {
  const prefs = (getPreferences().moments ?? {}) as { max_hops?: 1 | 2 | 3 }
  const maxHops = useSignal<1 | 2 | 3>((prefs.max_hops ?? 3) as 1 | 2 | 3)
  const save = async () => {
    try {
      await setPreference('moments', { max_hops: maxHops.value })
      showToast('Momentum visibility saved', 'success')
    } catch {
      showToast('Failed to save Momentum settings', 'error')
    }
  }
  return (
    <div id="momentum" class="sh-settings-momentum-panel sh-settings-subcard">
      <h3>Momentum visibility</h3>
      <p class="sh-muted">
        Federated moments hop up to 3 instances. Pick how many hops
        deep you want your inbox to surface — your instance still
        relays farther so other households can see them.
      </p>
      <label class="sh-form-row">
        Show moments up to
        <select
          value={String(maxHops.value)}
          onChange={e => {
            const v = Number(
              (e.target as HTMLSelectElement).value,
            ) as 1 | 2 | 3
            maxHops.value = v
          }}
        >
          <option value="1">1 hop (only direct peers)</option>
          <option value="2">2 hops</option>
          <option value="3">3 hops (default — every relayed moment)</option>
        </select>
      </label>
      <div class="sh-form-actions">
        <Button onClick={save}>Save</Button>
      </div>
    </div>
  )
}

function NotificationsTab() {
  // "Enabled" means this browser holds a live push subscription — not
  // merely that notification permission was granted once.
  useEffect(() => {
    currentPushSubscription()
      .then((sub) => { pushEnabled.value = sub !== null })
      .catch(() => { pushEnabled.value = false })
  }, [])

  const requestPush = async () => {
    pushBusy.value = true
    try {
      if (await enableWebPush()) {
        pushEnabled.value = true
        showToast('Push notifications enabled', 'success')
      } else {
        showToast('Notifications are blocked for this site in your browser', 'info')
      }
    } catch (err: unknown) {
      showToast(`Couldn't enable push: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      pushBusy.value = false
    }
  }

  const disablePush = async () => {
    pushBusy.value = true
    try {
      await disableWebPush()
      pushEnabled.value = false
      showToast('Push notifications disabled', 'info')
    } catch {
      showToast('Failed to disable push', 'error')
    } finally {
      pushBusy.value = false
    }
  }

  return (
    <section class="sh-settings-section">
      <h2>Notifications</h2>
      <div class="sh-settings-row">
        <span>Push notifications</span>
        {pushEnabled.value ? (
          <Button variant="secondary" loading={pushBusy.value} onClick={disablePush}>Disable</Button>
        ) : (
          <Button loading={pushBusy.value} disabled={!webPushSupported()} onClick={requestPush}>Enable</Button>
        )}
      </div>
      <p class="sh-muted">
        {pushEnabled.value
          ? 'You will receive push notifications for new messages and mentions.'
          : webPushSupported()
            ? 'Enable push notifications to stay updated when you are away.'
            : 'This browser does not support push notifications.'}
      </p>
      {isHomeAssistant() && <HaNotifyServiceRow />}
    </section>
  )
}

interface NotifyTarget {
  entity_id: string
  name: string
}

const MANUAL_SENTINEL = '__manual__'

/** ha / haos only: the notify service that pushes to *this* user's HA
 *  Companion app. HA names that service after the device
 *  (`notify.mobile_app_<device>`), not the username. The household's
 *  `notify.*` entities are fetched from `/api/me/notify-targets` and offered
 *  as a dropdown; an "Enter manually…" escape hatch (and a graceful fallback
 *  when discovery is empty/unreachable) keeps power users + legacy values
 *  working. The saved value is still the entity_id stored under the
 *  `ha_notify_service` preference. */
function HaNotifyServiceRow() {
  const [targets, setTargets] = useState<NotifyTarget[]>([])
  const [loading, setLoading] = useState(true)
  const [manual, setManual] = useState(false)

  useEffect(() => {
    let active = true
    const load = async () => {
      let fetched: NotifyTarget[] = []
      try {
        const res = await api.get<{ targets: NotifyTarget[] }>(
          '/api/me/notify-targets',
        )
        fetched = res.targets ?? []
      } catch {
        // HA unreachable / no notify entities — fall back to manual entry.
        fetched = []
      }
      if (!active) return
      setTargets(fetched)
      // Start in manual mode when there's nothing to pick from, or when the
      // saved value isn't a currently-discoverable target (legacy bare names
      // or entities not listed right now) so it's never silently lost.
      const saved = haNotifyService.value.trim()
      const known = fetched.some((t) => t.entity_id === saved)
      if (fetched.length === 0 || (saved !== '' && !known)) {
        setManual(true)
      }
      setLoading(false)
    }
    void load()
    return () => {
      active = false
    }
  }, [])

  const save = async () => {
    haNotifySaving.value = true
    try {
      const v = haNotifyService.value.trim()
      // Store the trimmed value (empty string = disabled; the backend
      // push provider treats blank as "no target" and skips).
      await setPreference('ha_notify_service', v)
      showToast(
        v ? 'HA notification target saved' : 'HA notification target cleared',
        'success',
      )
    } catch {
      showToast('Failed to save', 'error')
    } finally {
      haNotifySaving.value = false
    }
  }

  const onSelectChange = (e: Event) => {
    const v = (e.target as HTMLSelectElement).value
    if (v === MANUAL_SENTINEL) {
      // Reveal the text input, keeping the current value.
      setManual(true)
      return
    }
    haNotifyService.value = v
  }

  const showSelect = !manual && targets.length > 0
  const emptyDiscovery = !loading && targets.length === 0

  return (
    <div class="sh-settings-subsection">
      <h3>Home Assistant app</h3>
      {loading ? (
        <p class="sh-muted">Loading…</p>
      ) : showSelect ? (
        <label class="sh-form-row">
          Notify target
          <select value={haNotifyService.value} onChange={onSelectChange}>
            <option value="">— No HA notifications —</option>
            {targets.map((t) => (
              <option key={t.entity_id} value={t.entity_id}>
                {t.name}
              </option>
            ))}
            <option value={MANUAL_SENTINEL}>Enter manually…</option>
          </select>
        </label>
      ) : (
        <label class="sh-field">
          <span>Notify service</span>
          <input
            type="text"
            placeholder="notify.mobile_app_my_phone"
            value={haNotifyService.value}
            onInput={(e) =>
              haNotifyService.value = (e.target as HTMLInputElement).value}
          />
        </label>
      )}
      {emptyDiscovery && (
        <p class="sh-muted">
          Couldn't list notify targets from Home Assistant — enter the service
          name manually.
        </p>
      )}
      {manual && targets.length > 0 && (
        <div class="sh-settings-row">
          <Button variant="ghost" onClick={() => setManual(false)}>
            Choose from list
          </Button>
        </div>
      )}
      <p class="sh-muted">
        The Home Assistant notify service for your phone — find it under
        Developer Tools → Actions as <code>notify.mobile_app_…</code> (named
        after your device, not your username). Leave empty to disable HA-app
        notifications for your account.
      </p>
      <div class="sh-settings-row">
        <Button onClick={save} loading={haNotifySaving.value}>Save</Button>
      </div>
    </div>
  )
}

function AppearanceTab() {
  const setTheme = (t: Theme) => { theme.value = t }

  return (
    <section class="sh-settings-section">
      <h2>Appearance</h2>
      <div class="sh-theme-picker">
        <h3>Theme</h3>
        <div class="sh-theme-options">
          {(['light', 'dark', 'auto'] as Theme[]).map(t => (
            <button
              key={t}
              type="button"
              class={theme.value === t ? 'sh-theme-option sh-theme-option--active' : 'sh-theme-option'}
              onClick={() => setTheme(t)}
            >
              {t === 'light' ? 'Light' : t === 'dark' ? 'Dark' : 'Auto'}
            </button>
          ))}
        </div>
        <p class="sh-muted">
          {theme.value === 'auto'
            ? 'Follows your system preference.'
            : `Currently using ${theme.value} mode.`}
        </p>
      </div>

      <div class="sh-locale-picker">
        <h3>Language</h3>
        <div class="sh-locale-options" role="radiogroup" aria-label="Language">
          {Object.entries(localeMeta.locales).map(([code, info]) => (
            <button
              key={code}
              type="button"
              role="radio"
              aria-checked={locale.value === code}
              class={
                locale.value === code
                  ? 'sh-locale-option sh-locale-option--active'
                  : 'sh-locale-option'
              }
              onClick={() => { void setLocale(code) }}
              title={(info as { english_name: string }).english_name}
            >
              {(info as { native_name: string }).native_name}
            </button>
          ))}
        </div>
        <p class="sh-muted">
          Translations are contributed by the community. Missing or awkward
          text? <a href={localeMeta.weblate_url} target="_blank" rel="noopener noreferrer">
          Contribute translations on Weblate</a>.
        </p>
      </div>
      {currentUser.value?.is_admin && <HouseholdThemeStudio />}
    </section>
  )
}
