import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { theme, type Theme } from '@/store/theme'
import { HouseholdThemeStudio } from '@/components/HouseholdThemeStudio'
import { locale, setLocale } from '@/i18n/i18n'
import localeMeta from '@/i18n/locales/_meta.json'
import {
  getLandingPath,
  setPreference,
  type LandingPath,
} from '@/utils/preferences'

type SettingsTab = 'profile' | 'privacy' | 'notifications' | 'appearance'

const activeTab = signal<SettingsTab>('profile')
const displayName = signal('')
const bio = signal('')
const landingPath = signal<LandingPath>('/')
const avatarUrl = signal<string | null>(null)
const onlineStatusVisible = signal(true)
const pushEnabled = signal(
  typeof Notification !== 'undefined' ? Notification.permission === 'granted' : false
)

export default function SettingsPage() {
  useEffect(() => {
    if (currentUser.value) {
      displayName.value = currentUser.value.display_name
      bio.value = currentUser.value.bio || ''
      avatarUrl.value = currentUser.value.picture_url
    }
    landingPath.value = getLandingPath()
    api.get('/api/me/privacy').then((data: { online_status_visible?: boolean }) => {
      if (typeof data.online_status_visible === 'boolean') {
        onlineStatusVisible.value = data.online_status_visible
      }
    }).catch(() => {})
  }, [])

  return (
    <div class="sh-settings">
      <h1>Settings</h1>
      <nav class="sh-settings-tabs" role="tablist">
        {(['profile', 'privacy', 'notifications', 'appearance'] as SettingsTab[]).map(t => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={activeTab.value === t}
            class={activeTab.value === t ? 'sh-tab sh-tab--active' : 'sh-tab'}
            onClick={() => { activeTab.value = t }}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      {activeTab.value === 'profile' && <ProfileTab />}
      {activeTab.value === 'privacy' && <PrivacyTab />}
      {activeTab.value === 'notifications' && <NotificationsTab />}
      {activeTab.value === 'appearance' && <AppearanceTab />}
    </div>
  )
}

function ProfileTab() {
  const refresh = async () => {
    try {
      const me = await api.get('/api/me') as {
        display_name: string
        bio: string | null
        picture_url: string | null
      }
      displayName.value = me.display_name
      bio.value = me.bio ?? ''
      avatarUrl.value = me.picture_url
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
    if (!confirm('Remove your profile picture?')) return
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
      showToast(
        `Could not fetch from HA: ${(err as Error).message ?? err}`,
        'error',
      )
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
          <span class="sh-profile-avatar-hint" aria-hidden="true">
            📷 Change
          </span>
          <input type="file" accept="image/*"
                 onChange={handleAvatarUpload} hidden />
        </label>
        <div class="sh-profile-card-meta">
          <div class="sh-profile-identity">
            <strong class="sh-profile-name">
              {displayName.value || '—'}
            </strong>
            <span class="sh-muted">@{currentUser.value?.username}</span>
          </div>
          <span class={`sh-profile-source sh-profile-source--${isHaUser ? 'ha' : 'manual'}`}>
            {isHaUser ? '🏠 Synced from Home Assistant' : '✏️ Set manually'}
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
          : 'Landing page set to the feed',
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
            <span class="sh-muted">Tasks, events, notifications at a glance</span>
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
      await api.patch('/api/me/privacy', { online_status_visible: onlineStatusVisible.value })
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
        <input type="checkbox" checked={onlineStatusVisible.value} onChange={toggleOnlineStatus} />
        Show online status to other household members
      </label>
    </section>
  )
}

function NotificationsTab() {
  const requestPush = async () => {
    if (typeof Notification === 'undefined') return
    const result = await Notification.requestPermission()
    pushEnabled.value = result === 'granted'
    if (result === 'granted') {
      showToast('Push notifications enabled', 'success')
    }
  }

  const disablePush = async () => {
    try {
      const reg = await navigator.serviceWorker.getRegistration()
      const sub = await reg?.pushManager?.getSubscription()
      if (sub) {
        await sub.unsubscribe()
        await api.post('/api/push/unsubscribe', sub.toJSON())
      }
      pushEnabled.value = false
      showToast('Push notifications disabled', 'info')
    } catch {
      showToast('Failed to disable push', 'error')
    }
  }

  return (
    <section class="sh-settings-section">
      <h2>Notifications</h2>
      <div class="sh-settings-row">
        <span>Push notifications</span>
        {pushEnabled.value ? (
          <Button variant="secondary" onClick={disablePush}>Disable</Button>
        ) : (
          <Button onClick={requestPush}>Enable</Button>
        )}
      </div>
      <p class="sh-muted">
        {pushEnabled.value
          ? 'You will receive push notifications for new messages and mentions.'
          : 'Enable push notifications to stay updated when you are away.'}
      </p>
    </section>
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
