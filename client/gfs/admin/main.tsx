/* GFS admin portal — Preact bootstrap.
 *
 * Replaces the previous hand-written ``admin_ui/index.html`` inline
 * JS with a typed Preact bundle. Same behaviour: probe the session
 * via ``/admin/api/overview`` on first paint, surface the login form
 * on 401, then render the tabbed admin surface.
 *
 * Routing is hash-based so the bundle stays static-only — no need
 * to wire ``preact-iso`` for an 8-tab admin app, and a hash anchor
 * means the GFS aiohttp app doesn't need to handle SPA fallback
 * routes for the admin path.
 */
import { render } from 'preact'
import { useCallback, useEffect, useState } from 'preact/hooks'
import { api, UnauthorizedError } from './api'
import { OverviewPanel } from './panels/Overview'
import { ClientsPanel } from './panels/Clients'
import { SpacesPanel } from './panels/Spaces'
import { ReportsPanel } from './panels/Reports'
import { AppealsPanel } from './panels/Appeals'
import { PolicyPanel } from './panels/Policy'
import { BrandingPanel } from './panels/Branding'
import { AuditPanel } from './panels/Audit'
import { ClusterPanel } from './panels/Cluster'
import './index.css'


type TabKey = 'overview' | 'clients' | 'spaces' | 'reports' | 'appeals' |
  'policy' | 'branding' | 'audit' | 'cluster'

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'clients',  label: 'Clients' },
  { key: 'spaces',   label: 'Spaces' },
  { key: 'reports',  label: 'Reports' },
  { key: 'appeals',  label: 'Appeals' },
  { key: 'policy',   label: 'Policy' },
  { key: 'branding', label: 'Branding' },
  { key: 'audit',    label: 'Audit log' },
  { key: 'cluster',  label: 'Cluster' },
]


function readTabFromHash(): TabKey {
  const h = (window.location.hash || '#overview').slice(1)
  if (TABS.some((t) => t.key === h)) return h as TabKey
  return 'overview'
}


function LoginGate({ onSuccess }: { onSuccess: () => void }) {
  const [pw, setPw] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    if (submitting) return
    setSubmitting(true)
    setErr(null)
    try {
      // Relative URL — resolves against ``<base href>`` so the request
      // tracks the document base (drops the leading ``/`` so it doesn't
      // bypass any future ingress prefix). Matches ``api()``'s strip.
      const resp = await fetch('admin/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      })
      if (resp.status === 503) {
        setErr('Admin is disabled until --set-password is run on the server.')
        return
      }
      if (resp.status === 429) {
        setErr('Too many failed attempts — wait 15 minutes.')
        return
      }
      const data = await resp.json().catch(() => ({}))
      if (!resp.ok || (data as { status?: string }).status !== 'ok') {
        setErr('Invalid password.')
        return
      }
      setPw('')
      onSuccess()
    } catch (e) {
      setErr((e as Error).message || 'Login failed.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div class="login">
      <h2>GFS Admin</h2>
      <label>
        Password
        <input
          type="password"
          autoComplete="current-password"
          autofocus
          value={pw}
          onInput={(e) => setPw((e.currentTarget as HTMLInputElement).value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void submit()
          }}
        />
      </label>
      <button class="primary" onClick={() => void submit()} disabled={submitting}>
        {submitting ? 'Signing in…' : 'Sign in'}
      </button>
      {err && <p class="error" style={{ marginTop: '10px' }}>{err}</p>}
    </div>
  )
}


function App() {
  type Phase = 'probing' | 'login' | 'in'
  const [phase, setPhase] = useState<Phase>('probing')
  const [serverName, setServerName] = useState('')
  const [tab, setTab] = useState<TabKey>(readTabFromHash())

  const refreshBranding = useCallback(async () => {
    try {
      const b = await api<{ server_name?: string }>('GET', '/admin/api/branding')
      setServerName(b.server_name ?? '')
    } catch { /* tolerated — header label optional */ }
  }, [])

  // Probe session on first paint.
  useEffect(() => {
    api('GET', '/admin/api/overview')
      .then(() => {
        setPhase('in')
        void refreshBranding()
      })
      .catch((e) => {
        if (e instanceof UnauthorizedError) setPhase('login')
        else setPhase('login')
      })
  }, [refreshBranding])

  // Hash-routed tabs.
  useEffect(() => {
    const onHashChange = () => setTab(readTabFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const openTab = (key: TabKey) => {
    if (window.location.hash !== '#' + key) {
      window.location.hash = '#' + key
    }
    setTab(key)
  }

  const logout = async () => {
    try {
      await fetch('admin/logout', {
        method: 'POST',
        credentials: 'same-origin',
      })
    } catch { /* tolerated */ }
    setPhase('login')
  }

  if (phase === 'probing') {
    return <p class="muted" style={{ padding: '22px' }}>Loading…</p>
  }
  if (phase === 'login') {
    return <LoginGate onSuccess={() => {
      setPhase('in')
      void refreshBranding()
    }} />
  }
  return (
    <>
      <header>
        <h1>GFS Admin</h1>
        <span class="muted">{serverName}</span>
        <div class="right">
          <button class="secondary" onClick={() => void logout()}>Log out</button>
        </div>
      </header>
      <nav>
        {TABS.map((t) => (
          <a
            key={t.key}
            href={'#' + t.key}
            class={tab === t.key ? 'active' : ''}
            onClick={(e) => { e.preventDefault(); openTab(t.key) }}
          >{t.label}</a>
        ))}
      </nav>
      <main>
        <section><Panel which={tab} onBrandingSaved={() => void refreshBranding()} /></section>
      </main>
    </>
  )
}


function Panel({
  which,
  onBrandingSaved,
}: {
  which: TabKey
  onBrandingSaved: () => void
}) {
  switch (which) {
    case 'overview': return <OverviewPanel />
    case 'clients':  return <ClientsPanel />
    case 'spaces':   return <SpacesPanel />
    case 'reports':  return <ReportsPanel />
    case 'appeals':  return <AppealsPanel />
    case 'policy':   return <PolicyPanel />
    case 'branding': return <BrandingPanel onSaved={onBrandingSaved} />
    case 'audit':    return <AuditPanel />
    case 'cluster':  return <ClusterPanel />
  }
}


const root = document.getElementById('root')
if (root) render(<App />, root)
