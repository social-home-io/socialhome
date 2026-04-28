/**
 * HaUsersPanel — admin UI to opt HA ``person.*`` entities into Social Home.
 *
 * Lists the HA users via ``GET /api/admin/ha-users`` and renders a per-row
 * toggle. Flipping on issues a ``POST`` to provision; flipping off issues
 * a ``DELETE``. The ``synced`` flag drives the toggle state; optimistic
 * updates flip it immediately and revert on failure.
 *
 * Only mounted when ``config.mode === 'ha'``; in standalone mode the
 * endpoint 501s and AdminPage hides this tab entirely.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { Spinner } from '@/components/Spinner'
import { Avatar } from '@/components/Avatar'
import { instanceConfig } from '@/store/instance'

interface HaUser {
  username:     string
  display_name: string
  picture_url:  string | null
  is_admin:     boolean
  synced:       boolean
}

const users = signal<HaUser[]>([])
const loading = signal(true)
const error = signal<string | null>(null)
const notAvailable = signal(false)

export function HaUsersPanel() {
  useEffect(() => {
    let cancelled = false
    loading.value = true
    error.value = null
    notAvailable.value = false
    api.get('/api/admin/ha-users')
      .then((data: HaUser[]) => {
        if (!cancelled) users.value = data
      })
      .catch((e: any) => {
        if (cancelled) return
        if (e?.status === 501) {
          notAvailable.value = true
        } else {
          error.value = e.message || 'Failed to load HA users'
        }
      })
      .finally(() => {
        if (!cancelled) loading.value = false
      })
    return () => {
      cancelled = true
    }
  }, [])

  const toggle = async (row: HaUser) => {
    const wasSynced = row.synced
    const mode = instanceConfig.value?.mode
    // ha mode: server requires a password on provision so the picked
    // HA person can sign in via /api/auth/token. haos mode: ingress
    // signs them in, so no password.
    let body: { password?: string } | undefined
    if (!wasSynced && mode === 'ha') {
      const pw = window.prompt(
        `Set a password for ${row.display_name} (${row.username}). They'll use this to log in to Social Home from the web UI or mobile app.`,
      )
      if (pw === null) return  // cancelled
      if (pw.length < 8) {
        showToast('Password must be at least 8 characters.', 'error')
        return
      }
      body = { password: pw }
    }
    users.value = users.value.map(u =>
      u.username === row.username ? { ...u, synced: !wasSynced } : u,
    )
    try {
      if (wasSynced) {
        await api.delete(`/api/admin/ha-users/${row.username}/provision`)
        showToast(`${row.display_name} removed`, 'info')
      } else {
        await api.post(
          `/api/admin/ha-users/${row.username}/provision`,
          body,
        )
        showToast(`${row.display_name} added`, 'success')
      }
    } catch (e: any) {
      users.value = users.value.map(u =>
        u.username === row.username ? { ...u, synced: wasSynced } : u,
      )
      showToast(e.message || 'Toggle failed', 'error')
    }
  }

  if (loading.value) return <Spinner />
  if (notAvailable.value) {
    return (
      <section class="sh-admin-section">
        <h2>Home Assistant Users</h2>
        <p class="sh-muted">
          This instance isn't running as a Home Assistant add-on, so there
          are no HA users to sync. Invite members in the standalone user
          management instead.
        </p>
      </section>
    )
  }
  if (error.value) {
    return (
      <section class="sh-admin-section" role="alert">
        <h2>Home Assistant Users</h2>
        <p class="sh-error">{error.value}</p>
      </section>
    )
  }

  return (
    <section class="sh-admin-section">
      <h2>Home Assistant Users</h2>
      <p class="sh-muted">
        Pick which Home Assistant users should also be Social Home members.
        Turning one off soft-removes them; you can switch them back on later.
      </p>
      <ul class="sh-ha-users">
        {users.value.map(u => (
          <li key={u.username} class="sh-ha-user-row">
            <Avatar name={u.display_name} size={32} />
            <div class="sh-ha-user-info">
              <span class="sh-ha-user-name">{u.display_name}</span>
              <span class="sh-muted">@{u.username}</span>
              {u.is_admin && (
                <span class="sh-badge sh-badge--admin">Admin</span>
              )}
            </div>
            <label class="sh-switch">
              <input
                type="checkbox"
                checked={u.synced}
                aria-label={`Sync ${u.display_name}`}
                onChange={() => toggle(u)}
              />
              <span class="sh-switch-track" />
              <span class="sh-muted">
                {u.synced ? 'Synced' : 'Not synced'}
              </span>
            </label>
          </li>
        ))}
        {users.value.length === 0 && (
          <li class="sh-muted">No Home Assistant users found.</li>
        )}
      </ul>
    </section>
  )
}
