/**
 * AdminPage — household admin panel (§23.95).
 *
 * Tabs:
 *   * Members      — household roster + admin toggle.
 *   * Moderation   — content reports + the per-space moderation queue.
 *   * Sessions     — active API tokens + login attempts (security audit).
 *   * Settings     — short-cuts to features / theme / connections.
 *
 * Each tab is a thin shell — the real heavy lifting lives in
 * dedicated services / endpoints. New tabs can be added by extending
 * the `tabs` array.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { HouseholdToggles, loadToggles } from '@/components/HouseholdToggles'
import { HaUsersPanel } from './HaUsersPanel'
import { instanceConfig } from '@/store/instance'
import CpAdminPanel from '@/features/child-protection/CpAdminPanel'
import type { User } from '@/types'

type TabId =
  | 'members' | 'ha-users' | 'spaces' | 'moderation'
  | 'sessions' | 'child-protection' | 'storage' | 'backup'
  | 'settings'

interface ModerationItem {
  id:          string
  space_id:    string
  ref_type:    string
  ref_id:      string
  reported_by: string
  reason:      string
  occurred_at: string
}

interface ApiToken {
  token_id:     string
  label:        string
  created_at:   string
  last_used_at: string | null
  user_id?:     string
  username?:    string
  display_name?: string
}

const users         = signal<User[]>([])
const reports       = signal<ModerationItem[]>([])
const tokens        = signal<ApiToken[]>([])
const loading       = signal(true)
const tab           = signal<TabId>('members')

export default function AdminPage() {
  const user = currentUser.value
  if (!user?.is_admin) {
    return (
      <div class="sh-admin">
        <h1>Admin Panel</h1>
        <p>You must be an admin to view this page.</p>
      </div>
    )
  }

  useEffect(() => { void loadAll() }, [])

  if (loading.value) return <Spinner />

  return (
    <div class="sh-admin">
      <h1>Admin Panel</h1>

      <nav class="sh-admin-tabs" role="tablist">
        {([
          'members', 'ha-users', 'spaces', 'moderation',
          'sessions', 'child-protection', 'storage', 'backup',
          'settings',
        ] as TabId[]).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab.value === t}
            class={tab.value === t ? 'sh-tab sh-tab--active' : 'sh-tab'}
            onClick={() => (tab.value = t)}
          >
            {_tabLabel(t)}
          </button>
        ))}
      </nav>

      {tab.value === 'members' && <MembersTab />}
      {tab.value === 'ha-users' && <HaUsersPanel />}
      {tab.value === 'spaces' && <SpacesTab />}
      {tab.value === 'moderation' && <ModerationTab />}
      {tab.value === 'sessions' && <SessionsTab />}
      {tab.value === 'child-protection' && <CpAdminPanel />}
      {tab.value === 'storage' && <StorageTab />}
      {tab.value === 'backup' && <BackupTab />}
      {tab.value === 'settings' && <SettingsTab />}
    </div>
  )
}

function _tabLabel(t: TabId): string {
  if (t === 'ha-users') return 'HA Users'
  if (t === 'child-protection') return 'Child Protection'
  if (t === 'storage') return 'Storage'
  if (t === 'backup') return 'Backup'
  return t.charAt(0).toUpperCase() + t.slice(1)
}

// ─── Tabs ──────────────────────────────────────────────────────────────────

function CreateStandaloneUserForm() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [busy, setBusy] = useState(false)

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!username || !password) {
      showToast('Username and password are required.', 'error')
      return
    }
    if (password.length < 8) {
      showToast('Password must be at least 8 characters.', 'error')
      return
    }
    setBusy(true)
    try {
      await api.post('/api/admin/users', {
        username, password,
        display_name: displayName || username,
        is_admin: isAdmin,
      })
      showToast(`Created @${username}`, 'success')
      setUsername(''); setPassword(''); setDisplayName(''); setIsAdmin(false)
      await loadAll()  // refresh the users list below
    } catch (e: any) {
      showToast(e?.message || 'Create failed', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <details class="sh-admin-section">
      <summary><h3 style="display:inline">Create user</h3></summary>
      <form onSubmit={submit} class="sh-admin-create-user">
        <label>
          Username
          <input
            type="text"
            value={username}
            onInput={(e) => setUsername((e.target as HTMLInputElement).value)}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onInput={(e) => setPassword((e.target as HTMLInputElement).value)}
            required
          />
        </label>
        <label>
          Display name
          <input
            type="text"
            value={displayName}
            onInput={(e) => setDisplayName((e.target as HTMLInputElement).value)}
            placeholder={username || 'Optional'}
          />
        </label>
        <label class="sh-admin-checkbox">
          <input
            type="checkbox"
            checked={isAdmin}
            onChange={(e) => setIsAdmin((e.target as HTMLInputElement).checked)}
          />
          {' '}Make this user an admin
        </label>
        <Button type="submit" disabled={busy}>
          {busy ? 'Creating…' : 'Create user'}
        </Button>
      </form>
    </details>
  )
}

function MembersTab() {
  const toggleAdmin = async (userId: string, isAdmin: boolean) => {
    try {
      await api.patch(`/api/users/${userId}`, { is_admin: !isAdmin })
      users.value = users.value.map((u) =>
        u.user_id === userId ? { ...u, is_admin: !isAdmin } : u,
      )
    } catch (e: unknown) {
      showToast(`Admin toggle failed: ${(e as Error)?.message ?? e}`, 'error')
    }
  }
  const exportUserData = async (u: User) => {
    if (!confirm(
      `Export all data for @${u.username}? The browser will download a\n`
      + 'JSON file containing their posts, comments, DMs, tasks, calendar\n'
      + 'events, and media references. Use responsibly (§GDPR).',
    )) return
    try {
      const resp = await fetch(`/api/users/${u.user_id}/export`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem('sh-token') || ''}`,
        },
      })
      if (!resp.ok) {
        throw new Error(`Export failed: HTTP ${resp.status}`)
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `socialhome-export-${u.username}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      showToast(`Exported data for @${u.username}`, 'success')
    } catch (e: unknown) {
      showToast(`Export failed: ${(e as Error)?.message ?? e}`, 'error')
    }
  }
  const isStandalone = instanceConfig.value?.mode === 'standalone'
  if (users.value.length === 0) {
    return (
      <section class="sh-admin-section">
        <h2>Household Members</h2>
        {isStandalone && <CreateStandaloneUserForm />}
        <p class="sh-muted">No household members yet.</p>
      </section>
    )
  }
  return (
    <section class="sh-admin-section">
      <h2>Household Members</h2>
      {isStandalone && <CreateStandaloneUserForm />}
      <table class="sh-admin-table">
        <thead>
          <tr>
            <th>Name</th><th>Username</th><th>Admin</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.value.map((u) => (
            <tr key={u.user_id}>
              <td>{u.display_name}</td>
              <td>@{u.username}</td>
              <td>{u.is_admin ? '✅' : '—'}</td>
              <td>
                <Button
                  variant="secondary"
                  onClick={() => toggleAdmin(u.user_id, u.is_admin)}
                >
                  {u.is_admin ? 'Revoke admin' : 'Make admin'}
                </Button>
                {' '}
                <button
                  type="button" class="sh-link"
                  onClick={() => void exportUserData(u)}
                  title="Download all of this user's data as JSON (§GDPR)"
                >
                  Export data
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

// ─── Spaces tab ───────────────────────────────────────────────────────

interface SpaceAdminRow {
  id:           string
  name:         string
  description:  string | null
  emoji:        string | null
  space_type:   string
  join_mode:    string
  owner_username: string
  owner_instance_id: string
  member_count: number
  dissolved:    boolean
}

const spaceRows = signal<SpaceAdminRow[]>([])
const spacesLoading = signal(false)

async function loadSpaces() {
  spacesLoading.value = true
  try {
    spaceRows.value = await api.get('/api/admin/spaces') as SpaceAdminRow[]
  } catch {
    spaceRows.value = []
  } finally {
    spacesLoading.value = false
  }
}

async function dissolveSpace(id: string) {
  if (!confirm(`Dissolve space ${id}? This cannot be undone.`)) return
  try {
    await api.delete(`/api/spaces/${id}`)
    await loadSpaces()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Dissolve failed', 'error')
  }
}

interface GfsPublication {
  space_id:          string
  gfs_id:            string
  published_at:      string
  gfs_display_name:  string
  gfs_inbox_url:  string
  space_name:        string | null
  space_emoji:       string | null
}

const publications = signal<GfsPublication[]>([])
const publicationsLoading = signal(false)

async function loadPublications() {
  publicationsLoading.value = true
  try {
    const body = await api.get('/api/gfs/publications') as
      { publications: GfsPublication[] }
    publications.value = body.publications
  } catch {
    publications.value = []
  } finally {
    publicationsLoading.value = false
  }
}

async function unpublishFromGfs(spaceId: string, gfsId: string) {
  if (!confirm(
    'Unpublish this space from the GFS? It will disappear from the '
    + 'global directory. Existing members remain members — this only '
    + 'affects discoverability.',
  )) return
  try {
    await api.delete(`/api/spaces/${spaceId}/publish/${gfsId}`)
    showToast('Unpublished', 'success')
    await loadPublications()
  } catch (e: unknown) {
    showToast(`Unpublish failed: ${(e as Error)?.message ?? e}`, 'error')
  }
}

async function transferSpaceOwnership(id: string, currentOwner: string) {
  const newOwnerId = prompt(
    `Transfer ownership away from @${currentOwner}.\n\n` +
    `Enter the new owner's user_id (a current member of this space):`,
  )
  if (!newOwnerId) return
  try {
    await api.post(`/api/spaces/${id}/ownership`, {
      to_user_id: newOwnerId.trim(),
    })
    showToast('Ownership transferred', 'success')
    await loadSpaces()
  } catch (e: unknown) {
    showToast((e as Error).message || 'Transfer failed', 'error')
  }
}

function SpacesTab() {
  useEffect(() => {
    void loadSpaces()
    void loadPublications()
  }, [])
  if (spacesLoading.value) return <Spinner />
  if (spaceRows.value.length === 0) {
    return (
      <section class="sh-admin-section">
        <h2>All spaces</h2>
        <p class="sh-muted">No active spaces on this household yet.</p>
      </section>
    )
  }
  return (
    <section class="sh-admin-section">
      <h2>All spaces</h2>
      <table class="sh-admin-table">
        <thead><tr>
          <th>Name</th><th>Type</th><th>Owner</th>
          <th>Members</th><th>Join</th><th></th>
        </tr></thead>
        <tbody>
          {spaceRows.value.map(s => (
            <tr key={s.id}>
              <td>{s.emoji} {s.name}</td>
              <td><span class="sh-muted">{s.space_type}</span></td>
              <td>@{s.owner_username}</td>
              <td>{s.member_count}</td>
              <td><span class="sh-muted">{s.join_mode}</span></td>
              <td>
                <a class="sh-link" href={`/spaces/${s.id}`}>Open</a>
                {' · '}
                <button type="button" class="sh-link"
                  onClick={() => void transferSpaceOwnership(s.id, s.owner_username)}>
                  Transfer
                </button>
                {' · '}
                <button type="button" class="sh-danger-link"
                  onClick={() => void dissolveSpace(s.id)}>Dissolve</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Global Federation publications</h2>
      <p class="sh-muted">
        Spaces currently advertised to a paired GFS. Spaces of
        <code> type=global</code> auto-publish; this table lets you
        manually withdraw any publication without changing the space
        type.
      </p>
      {publicationsLoading.value ? (
        <Spinner />
      ) : publications.value.length === 0 ? (
        <p class="sh-muted">
          No active publications. Create a <code>type=global</code>
          space (or publish manually) to populate this list.
        </p>
      ) : (
        <table class="sh-admin-table">
          <thead><tr>
            <th>Space</th><th>GFS</th><th>Published at</th><th></th>
          </tr></thead>
          <tbody>
            {publications.value.map((p) => (
              <tr key={`${p.space_id}-${p.gfs_id}`}>
                <td>
                  {p.space_emoji || ''} {p.space_name || p.space_id}
                </td>
                <td>
                  {p.gfs_display_name}
                  <span class="sh-muted"> · {p.gfs_inbox_url}</span>
                </td>
                <td>
                  {p.published_at
                    ? new Date(p.published_at).toLocaleString()
                    : '—'}
                </td>
                <td>
                  <button
                    type="button" class="sh-danger-link"
                    onClick={
                      () => void unpublishFromGfs(p.space_id, p.gfs_id)
                    }
                  >
                    Unpublish
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}


async function resolveReport(id: string, dismissed: boolean) {
  try {
    await api.post(`/api/admin/reports/${id}/resolve`, { dismissed })
    reports.value = reports.value.filter((r) => r.id !== id)
    showToast(dismissed ? 'Report dismissed' : 'Report resolved', 'success')
  } catch (e: unknown) {
    showToast(`Action failed: ${(e as Error)?.message ?? e}`, 'error')
  }
}

function ModerationTab() {
  if (reports.value.length === 0) {
    return (
      <section class="sh-admin-section">
        <h2>Moderation Queue</h2>
        <p class="sh-muted">
          No pending reports. Items appear here when a member flags a
          post, comment, or sticky for admin review.
        </p>
      </section>
    )
  }
  return (
    <section class="sh-admin-section">
      <h2>Moderation Queue</h2>
      <p class="sh-muted sh-admin-section__hint">
        Reports filed by household members. <strong>Resolve</strong> when
        you've acted (removed / edited the content);
        <strong> Dismiss</strong> when the report is unfounded.
      </p>
      <ol class="sh-admin-queue">
        {reports.value.map((m) => (
          <li key={m.id} class="sh-admin-row">
            <div class="sh-admin-row__hd">
              <strong>{m.ref_type}</strong>
              <span class="sh-muted">in space {m.space_id}</span>
              <time class="sh-muted">
                {new Date(m.occurred_at).toLocaleString()}
              </time>
            </div>
            <p class="sh-admin-row__reason">{m.reason}</p>
            <div class="sh-admin-row__actions">
              <Button
                variant="secondary"
                onClick={() => void resolveReport(m.id, true)}
              >
                Dismiss
              </Button>
              <Button
                variant="primary"
                onClick={() => void resolveReport(m.id, false)}
              >
                Resolve
              </Button>
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}

function SessionsTab() {
  if (tokens.value.length === 0) {
    return (
      <section class="sh-admin-section">
        <h2>Sessions</h2>
        <p class="sh-muted">No active API tokens across the household.</p>
      </section>
    )
  }
  return (
    <section class="sh-admin-section">
      <h2>Sessions</h2>
      <p class="sh-muted">
        Every active session across the household. Each row is one
        signed-in browser or app. Revoke anything unfamiliar.
      </p>
      <table class="sh-admin-table">
        <thead>
          <tr>
            <th>User</th>
            <th>Label</th><th>Created</th><th>Last used</th><th></th>
          </tr>
        </thead>
        <tbody>
          {tokens.value.map((t) => (
            <tr key={t.token_id}>
              <td>
                {t.display_name || t.username || '—'}
                {t.username && (
                  <span class="sh-muted"> @{t.username}</span>
                )}
              </td>
              <td>{t.label}</td>
              <td>{new Date(t.created_at).toLocaleString()}</td>
              <td>
                {t.last_used_at
                  ? new Date(t.last_used_at).toLocaleString()
                  : '—'}
              </td>
              <td>
                <Button
                  variant="danger"
                  onClick={async () => {
                    if (!confirm(
                      `Revoke "${t.label}" (${t.username || 'unknown user'})? `
                      + 'Any client signed in with this token will be '
                      + 'logged out immediately.',
                    )) return
                    try {
                      await api.delete(`/api/admin/tokens/${t.token_id}`)
                      tokens.value = tokens.value.filter(
                        (x) => x.token_id !== t.token_id,
                      )
                      showToast('Token revoked', 'info')
                    } catch (e: unknown) {
                      showToast(
                        `Revoke failed: ${(e as Error)?.message ?? e}`,
                        'error',
                      )
                    }
                  }}
                >
                  Revoke
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function SettingsTab() {
  useEffect(() => { void loadToggles() }, [])

  return (
    <section class="sh-admin-section">
      <h2>Household Settings</h2>
      <HouseholdToggles />
    </section>
  )
}

// ─── Storage tab ──────────────────────────────────────────────────────

interface StorageUsage {
  used_bytes:      number
  quota_bytes:     number
  available_bytes: number
  percent_used:    number
}

const storageUsage = signal<StorageUsage | null>(null)
const storageLoading = signal(false)

async function loadStorage() {
  storageLoading.value = true
  try {
    storageUsage.value = await api.get('/api/storage/usage') as StorageUsage
  } catch {
    storageUsage.value = null
  } finally {
    storageLoading.value = false
  }
}

async function setStorageQuota(bytes: number) {
  try {
    await api.put('/api/admin/storage/quota', { quota_bytes: bytes })
    showToast('Quota updated', 'success')
    await loadStorage()
  } catch (e: unknown) {
    showToast(`Quota update failed: ${(e as Error)?.message ?? e}`, 'error')
  }
}

function _fmtBytes(n: number): string {
  if (n <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = n, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function StorageTab() {
  useEffect(() => { void loadStorage() }, [])
  if (storageLoading.value) return <Spinner />
  const u = storageUsage.value
  if (!u) {
    return (
      <section class="sh-admin-section">
        <h2>Storage</h2>
        <p class="sh-muted">Failed to load storage usage.</p>
      </section>
    )
  }
  const unlimited = u.quota_bytes <= 0
  const onSetQuota = () => {
    const raw = prompt(
      'Household storage quota in gibibytes (GiB).\n'
      + 'Enter 0 to remove the cap.',
      unlimited ? '0' : String(Math.round(u.quota_bytes / (1024 ** 3))),
    )
    if (raw === null) return
    const gib = Number(raw)
    if (!Number.isFinite(gib) || gib < 0) {
      showToast('Invalid quota', 'error')
      return
    }
    void setStorageQuota(Math.round(gib * (1024 ** 3)))
  }
  return (
    <section class="sh-admin-section">
      <h2>Storage</h2>
      <dl class="sh-admin-stats">
        <dt>Used</dt><dd>{_fmtBytes(u.used_bytes)}</dd>
        <dt>Quota</dt>
        <dd>{unlimited ? 'Unlimited' : _fmtBytes(u.quota_bytes)}</dd>
        <dt>Available</dt>
        <dd>{unlimited ? '—' : _fmtBytes(u.available_bytes)}</dd>
        <dt>Used %</dt>
        <dd>{unlimited ? '—' : `${u.percent_used.toFixed(1)}%`}</dd>
      </dl>
      {!unlimited && (
        <div class="sh-admin-progress" aria-label="Storage usage">
          <div
            class="sh-admin-progress__bar"
            style={`width: ${Math.min(100, u.percent_used).toFixed(1)}%`}
          />
        </div>
      )}
      <div class="sh-admin-actions">
        <Button variant="secondary" onClick={onSetQuota}>
          Set quota…
        </Button>
      </div>
      <p class="sh-muted">
        Uploads that would push the household over the cap are rejected
        at ingest time. Removing the cap (quota = 0) disables the
        check entirely.
      </p>
    </section>
  )
}

// ─── Backup tab ───────────────────────────────────────────────────────

function BackupTab() {
  const [importing, setImporting] = useState(false)

  const onExport = () => {
    // Use a plain anchor so the browser streams the download via the
    // standard Bearer-auth flow. `api` wraps JSON; this endpoint
    // returns a gzip tarball.
    fetch('/api/backup/export', {
      headers: {
        Authorization: `Bearer ${localStorage.getItem('sh-token') || ''}`,
      },
    }).then(async (resp) => {
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      a.href = url
      a.download = `socialhome-backup-${stamp}.tar.gz`
      document.body.appendChild(a); a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      showToast('Backup downloaded', 'success')
    }).catch((e: unknown) => {
      showToast(`Export failed: ${(e as Error)?.message ?? e}`, 'error')
    })
  }

  const onImport = async (file: File) => {
    if (!confirm(
      `Restore from "${file.name}"?\n\n`
      + 'This will only succeed on an empty database. Existing data\n'
      + 'prevents the restore so you don\'t accidentally overwrite it.\n'
      + 'Export first if you want to keep the current state.',
    )) return
    setImporting(true)
    try {
      const body = await file.arrayBuffer()
      const resp = await fetch('/api/backup/import', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${localStorage.getItem('sh-token') || ''}`,
          'Content-Type': 'application/gzip',
        },
        body,
      })
      if (!resp.ok) {
        const txt = await resp.text()
        throw new Error(`HTTP ${resp.status}: ${txt}`)
      }
      showToast('Backup restored', 'success')
    } catch (e: unknown) {
      showToast(`Import failed: ${(e as Error)?.message ?? e}`, 'error')
    } finally {
      setImporting(false)
    }
  }

  return (
    <section class="sh-admin-section">
      <h2>Backup &amp; restore</h2>
      <p class="sh-muted">
        Download a complete household backup (WAL-checkpointed SQLite
        plus all media) or restore from one. Restore only works into an
        empty instance — use it when migrating to a new host, not to
        roll back routine changes.
      </p>
      <div class="sh-admin-actions">
        <Button variant="primary" onClick={onExport}>
          ⬇ Download backup
        </Button>
        <label class="sh-btn sh-btn--secondary sh-btn--file">
          {importing ? 'Restoring…' : '⬆ Restore from file…'}
          <input
            type="file"
            accept=".tar.gz,.tgz,application/gzip"
            style="display:none"
            disabled={importing}
            onChange={(e) => {
              const file = (e.target as HTMLInputElement).files?.[0]
              if (file) void onImport(file)
            }}
          />
        </label>
      </div>
    </section>
  )
}

// ─── Loaders ───────────────────────────────────────────────────────────────

async function loadAll() {
  loading.value = true
  try {
    users.value = await api.get('/api/users') as User[]
  } catch {
    users.value = []
  }
  try {
    const body = await api.get('/api/admin/moderation') as
      { items: ModerationItem[] }
    reports.value = body.items
  } catch {
    // Endpoint optional — moderation queue is a §23.95.2 enhancement
    // that ships separately. Silent empty state.
    reports.value = []
  }
  try {
    const body = await api.get('/api/admin/tokens') as { tokens: ApiToken[] }
    tokens.value = body.tokens
  } catch {
    tokens.value = []
  } finally {
    loading.value = false
  }
}
