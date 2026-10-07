/**
 * ConnectionsPage — federation connections (§23.86, §11).
 *
 * Three sections:
 *   1. Incoming auto-pair requests — admin inbox populated when one
 *      of our paired peers has introduced a new household to us.
 *      One click approves (no QR or SAS; B's vouch signature takes
 *      the place of the out-of-band verification).
 *   2. Households (HFS) with a polished paired-peer list + the
 *      outgoing "pair via a trusted peer" flow.
 *   3. Global Federation Servers.
 */
import { useEffect, useState, lazy, Suspense } from 'preact/compat'
import type { ComponentChildren } from 'preact'
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AutoPairDialog, openAutoPair } from '@/components/AutoPairDialog'
import { ConnectionDetail } from '@/components/ConnectionDetail'
import { showToast } from '@/components/Toast'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import {
  connections,
  selfLat,
  selfLon,
  type Connection,
  type TransportState,
} from '@/store/connections'
import {
  compatPeers,
  compatOurs,
  loadFederationCompat,
  peersBehindCount,
  type CompatPeer,
} from '@/store/federationCompat'

import { useTitle } from '@/store/pageTitle'
import type { GfsConnection } from '@/types'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { isSupervisorAddon } from '@/platform'
import { confirmDialog } from '@/components/confirm'
import { relativeDocsTime } from '@/utils/relativeTime'
import { featureLabels } from '@/utils/capabilityLabels'

const FederationMap = lazy(() => import('./FederationMap'))

interface AutoPairRequest {
  request_id: string
  from_a_id: string
  from_a_display: string
  via_b_id: string
  via_b_display: string
  ts: string
  received_at: string
}

const gfsConnections = signal<GfsConnection[]>([])
const autoPairRequests = signal<AutoPairRequest[]>([])
const loading = signal(true)
const gfsLoading = signal(true)
const disconnectTarget = signal<GfsConnection | null>(null)


/** Re-pair-class auth failures: the GFS no longer recognizes this home,
 *  so reconnecting can never succeed without re-pairing. Current GFS builds
 *  close with one ``auth-failed`` reason (an unknown id must not be told
 *  apart from a bad signature); older ones still send the two split reasons. */
const GFS_REPAIR_ERRORS = new Set(['auth-failed', 'unknown-instance', 'bad-signature'])

interface GfsLiveState {
  /** Full ``sh-status-dot …`` class for the leading dot. */
  dotClass: string
  /** i18n key for the human label, or null to render no label. */
  labelKey: string | null
  /** CSS class for the label span (warning vs muted), or null. */
  labelClass: string | null
}

/** Map a GFS connection's PAIRING status + LIVE WS health to what the
 *  card should show. ``status`` is the stored pairing state; ``connected``
 *  / ``last_error`` are the supervisor's live signal. A stored
 *  ``status='active'`` whose socket is down must NOT read as connected —
 *  ``last_error`` is GFS-controlled and is only ever surfaced via these
 *  mapped, translated strings, never rendered verbatim. */
function gfsLiveState(gfs: GfsConnection): GfsLiveState {
  if (gfs.status === 'pending')
    return {
      dotClass: 'sh-status-dot sh-status-dot--pending',
      labelKey: 'gfs.status_pending',
      labelClass: 'sh-text-warning',
    }
  if (gfs.status === 'suspended')
    return {
      dotClass: 'sh-status-dot sh-status-dot--unreachable',
      labelKey: 'gfs.status_suspended',
      labelClass: 'sh-muted',
    }
  // status === 'active' — defer to live WS liveness.
  if (gfs.connected)
    return {
      dotClass: 'sh-status-dot sh-status-dot--active',
      labelKey: 'gfs.status_connected',
      labelClass: 'sh-muted',
    }
  if (gfs.last_error && GFS_REPAIR_ERRORS.has(gfs.last_error))
    return {
      dotClass: 'sh-status-dot sh-status-dot--unreachable',
      labelKey: 'gfs.status_repair_needed',
      labelClass: 'sh-text-warning',
    }
  if (gfs.last_error === 'ts-skew')
    return {
      dotClass: 'sh-status-dot sh-status-dot--unreachable',
      labelKey: 'gfs.status_clock_skew',
      labelClass: 'sh-text-warning',
    }
  // Transient / reconnecting (any other reason, or none recorded yet).
  return {
    dotClass: 'sh-status-dot sh-status-dot--pending',
    labelKey: 'gfs.status_reconnecting',
    labelClass: 'sh-muted',
  }
}

function hfsStatusDotClass(conn: Connection): string {
  if (!conn.reachable) return 'sh-status-dot sh-status-dot--unreachable'
  if (conn.status === 'confirmed') return 'sh-status-dot sh-status-dot--active'
  return 'sh-status-dot sh-status-dot--pending'
}

function transportIcon(kind: Connection['transport']) {
  if (kind === 'rtc') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--rtc"
        title={t('connections.transport.direct_title')}
        aria-label={t('connections.transport.direct')}
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" />
        </svg>
      </span>
    )
  }
  if (kind === 'https') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--https"
        title={t('connections.transport.internet_title')}
        aria-label={t('connections.transport.internet')}
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M19 18H6a4 4 0 010-8 5 5 0 019.6-2A4 4 0 0119 18z" />
        </svg>
      </span>
    )
  }
  if (kind === 'gfs_relay') {
    return (
      <span
        class="sh-transport-icon sh-transport-icon--gfs-relay"
        title={t('connections.transport.gfs')}
        aria-label={t('connections.transport.gfs')}
      >
        <svg width="14" height="14" viewBox="0 0 24 24"
             fill="currentColor" aria-hidden="true">
          <path d="M4 7h10l-2-2 1.4-1.4L17.8 8l-4.4 4.4L12 11l2-2H4V7zm16 10H10l2 2-1.4 1.4L6.2 16l4.4-4.4L12 13l-2 2h10v2z" />
        </svg>
      </span>
    )
  }
  return (
    <span class="sh-transport-icon" aria-hidden="true" />
  )
}

/**
 * Compatibility badge for a confirmed household, derived from its matched
 * CompatPeer. Mirrors AdminPage's `_compatStatus` three-state logic but kept
 * local to this surface (no cross-feature import):
 *   * no compat row yet            → nothing.
 *   * caps not yet learned         → "version unknown".
 *   * caps known, none lacking     → "up to date ✓".
 *   * caps known, N features short → "N behind" (titled with the list).
 */
function compatBadge(peer: CompatPeer | undefined) {
  if (peer === undefined) return null
  if (!peer.capabilities_known) {
    return <span class="sh-chip sh-chip--muted">{t('connections.compat.unknown')}</span>
  }
  if (peer.lacking_features.length === 0) {
    return <span class="sh-chip sh-chip--success">{t('connections.compat.up_to_date')}</span>
  }
  return (
    <span class="sh-chip sh-chip--update" title={featureLabels(peer.lacking_features, peer.lacking_feature_keys).join(', ')}>
      {behindLabel(peer.lacking_features.length)}
    </span>
  )
}

/** "{n} behind" badge text, singular/plural per the UI language. */
function behindLabel(n: number): string {
  return t(isOne(n) ? 'connections.compat.behind_one' : 'connections.compat.behind', { n: String(n) })
}

/** Render a translated sentence with one ``{name}``-style placeholder
 *  replaced by a JSX node (bold name, ``<code>`` URL), so word order
 *  stays the translator's choice. */
function withNode(text: string, placeholder: string, node: ComponentChildren) {
  const [before, after = ''] = text.split(`{${placeholder}}`)
  return <>{before}{node}{after}</>
}

async function loadConnections() {
  loading.value = true
  try {
    connections.value = await api.get('/api/connections') as Connection[]
  } catch {
    connections.value = []
  }
  loading.value = false
}

async function loadSelfHome() {
  try {
    const j = await api.get('/api/friends') as {
      instance?: { home_lat?: number | null; home_lon?: number | null }
    }
    if (j.instance?.home_lat != null && j.instance?.home_lon != null) {
      selfLat.value = j.instance.home_lat
      selfLon.value = j.instance.home_lon
    }
  } catch {
    // Non-fatal — the federation map just won't show the "You" pin
    // until a `local.home_changed` WS frame arrives. The List view
    // doesn't need this data.
  }
}

async function loadGfsConnections() {
  gfsLoading.value = true
  try {
    gfsConnections.value = await api.get('/api/gfs/connections') as GfsConnection[]
  } catch {
    gfsConnections.value = []
  }
  gfsLoading.value = false
}

async function loadAutoPairRequests() {
  try {
    autoPairRequests.value = await api.get(
      '/api/pairing/auto-pair-requests',
    ) as AutoPairRequest[]
  } catch {
    autoPairRequests.value = []
  }
}

async function approveAutoPair(r: AutoPairRequest) {
  try {
    await api.post(
      `/api/pairing/auto-pair-requests/${r.request_id}/approve`, {},
    )
    showToast(t('connections.requests.paired', { name: r.from_a_display }), 'success')
    autoPairRequests.value = autoPairRequests.value.filter(
      x => x.request_id !== r.request_id,
    )
  } catch (err: unknown) {
    showToast(
      t('connections.requests.approve_failed', { error: String((err as Error).message ?? err) }), 'error',
    )
  }
}

async function declineAutoPair(r: AutoPairRequest) {
  if (!await confirmDialog(t('connections.requests.decline_confirm', { name: r.from_a_display }), { destructive: true })) return
  try {
    await api.post(
      `/api/pairing/auto-pair-requests/${r.request_id}/decline`,
      {},
    )
    showToast(t('connections.requests.declined'), 'info')
    autoPairRequests.value = autoPairRequests.value.filter(
      x => x.request_id !== r.request_id,
    )
  } catch (err: unknown) {
    showToast(
      t('connections.requests.decline_failed', { error: String((err as Error).message ?? err) }), 'error',
    )
  }
}

async function disconnectGfs(gfs: GfsConnection) {
  try {
    await api.delete(`/api/gfs/connections/${gfs.id}`)
    showToast(t('gfs.disconnect'), 'success')
    gfsConnections.value = gfsConnections.value.filter(c => c.id !== gfs.id)
  } catch (e: unknown) {
    showToast((e as Error).message || t('gfs.pairing_failed'), 'error')
  }
  disconnectTarget.value = null
}

async function unpair(instanceId: string) {
  if (!await confirmDialog(t('connections.unpair_confirm'), { destructive: true })) return
  try {
    await api.delete(`/api/pairing/connections/${instanceId}`)
    showToast(t('connections.unpaired'), 'info')
    await loadConnections()
  } catch (e: unknown) {
    showToast((e as Error).message || t('connections.unpair_failed'), 'error')
  }
}


interface IceOverview {
  servers: { urls: string[]; kinds: string[]; has_credentials: boolean }[]
  has_turn: boolean
  turn_usable: boolean
  pulls_from_home_assistant: boolean
}

/**
 * DiagnosticsDownload — one file to attach to a bug report.
 *
 * Sits under the connection-servers panel as a plain link-weight action
 * rather than a button: an operator reaches for it once, when asked to,
 * and it should not look like something to click casually.
 *
 * Fetched through the ``api`` client (so it carries auth and respects the
 * ingress base) and saved via a Blob, because a bare ``<a download>``
 * pointing at the endpoint would be an unauthenticated navigation.
 *
 * The copy states plainly what is in it, because "diagnostics" invites
 * the reasonable worry that it might contain messages or keys.
 */
function DiagnosticsDownload() {
  const busy = useSignal(false)

  const download = async () => {
    busy.value = true
    try {
      const data = await api.get('/api/admin/diagnostics')
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: 'application/json',
      })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      a.download = `socialhome-diagnostics-${stamp}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      // Revoke on the next tick — revoking synchronously can cancel the
      // download in some browsers before it has read the blob.
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch {
      showToast(t('connections.diag.failed'), 'error')
    } finally {
      busy.value = false
    }
  }

  return (
    <p class="sh-diagnostics-row sh-muted">
      <button
        type="button"
        class="sh-diagnostics-link"
        disabled={busy.value}
        onClick={() => void download()}
      >
        {busy.value ? t('connections.diag.preparing') : t('connections.diag.download')}
      </button>
      <span class="sh-diagnostics-note">
        {t('connections.diag.note')}
      </span>
    </p>
  )
}

/**
 * IceServersPanel — what the federation transport uses to punch through
 * NAT, secrets stripped.
 *
 * Deliberately a collapsed disclosure rather than a visible block: an
 * operator needs this roughly once, when federation won't connect. But
 * when they do need it, the alternative today is reading a log warning
 * they have probably never seen — a missing or credential-less TURN entry
 * degrades RTC to slow HTTPS in complete silence. Collapsed keeps it out
 * of the way while making the answer one click away.
 *
 * The summary line carries the conclusion so the detail is optional: an
 * operator can tell at a glance whether a relay is in play.
 */
function IceServersPanel() {
  // Per-mount state, not module-level: the list changes underneath us (an
  // HA pull replaces it daily), so a panel reopened after navigating away
  // must refetch rather than show whatever it saw last time.
  const iceOpen = useSignal(false)
  const iceLoaded = useSignal(false)
  const iceServers = useSignal<IceOverview | null>(null)
  const data = iceServers.value
  const panelId = 'sh-ice-servers-panel'

  // Fetched on mount, not on open: the badge is the whole point — an
  // operator who doesn't know their TURN got wiped has no reason to
  // click a collapsed row, so the warning has to be visible before they
  // do. One small admin-only request per page view. Reopening after
  // navigating away remounts and refetches, which is what the per-mount
  // state above is for.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const overview = await api.get(
          '/api/admin/federation/ice-servers',
        ) as IceOverview
        if (!cancelled) iceServers.value = overview
      } catch {
        if (!cancelled) iceServers.value = null
      } finally {
        if (!cancelled) iceLoaded.value = true
      }
    })()
    return () => { cancelled = true }
    // ``useSignal`` returns the same object every render, so listing the
    // signals is honest and still runs this exactly once per mount — no
    // lint suppression needed.
  }, [iceLoaded, iceServers])

  const summary: 'unavailable' | 'ready' | 'broken' | 'direct_only' | '' = !iceLoaded.value
    ? ''
    : data === null
      ? 'unavailable'
      : data.turn_usable
        ? 'ready'
        : data.has_turn
          ? 'broken'
          : 'direct_only'

  return (
    <div class="sh-ice-panel">
      <button
        type="button"
        class="sh-ice-panel__toggle"
        aria-expanded={iceOpen.value}
        aria-controls={panelId}
        onClick={() => { iceOpen.value = !iceOpen.value }}
      >
        <span class="sh-ice-panel__caret" aria-hidden="true">
          {iceOpen.value ? '▾' : '▸'}
        </span>
        {t('connections.ice.title')}
        {summary && (
          <span class={`sh-ice-panel__badge sh-ice-panel__badge--${
            summary === 'ready'
              ? 'ok'
              : summary === 'direct_only' || summary === 'broken'
                ? 'warn'
                : 'muted'
          }`}>
            {t(`connections.ice.state_${summary}`)}
          </span>
        )}
      </button>
      {iceOpen.value && (
        <div id={panelId} class="sh-ice-panel__body">
          {!iceLoaded.value && <p class="sh-muted">{t('connections.ice.loading')}</p>}
          {iceLoaded.value && data === null && (
            <p class="sh-muted">{t('connections.ice.read_failed')}</p>
          )}
          {iceLoaded.value && data !== null && (
            <>
              {data.servers.length === 0 ? (
                <p class="sh-muted">
                  {t('connections.ice.none')}
                </p>
              ) : (
                <ul class="sh-ice-list">
                  {data.servers.map(s => (
                    <li key={s.urls.join(',')} class="sh-ice-list__item">
                      <code>{s.urls.join(', ')}</code>
                      <span class="sh-muted sh-ice-list__note">
                        {s.kinds.includes('turn') || s.kinds.includes('turns')
                          ? s.has_credentials
                            ? t('connections.ice.kind_backup_signed_in')
                            : t('connections.ice.kind_backup_no_login')
                          : t('connections.ice.kind_lookup')}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <p class="sh-muted sh-ice-panel__hint">
                {!data.has_turn
                  ? t('connections.ice.hint_no_turn')
                  : !data.turn_usable
                    ? t('connections.ice.hint_turn_broken')
                    : t('connections.ice.hint_ready')}
                {data.pulls_from_home_assistant && t('connections.ice.from_ha')}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}


//: Module-level like ``autoPairRequests`` above, so the loader can be a
//: plain function rather than a component-scoped closure the effect would
//: have to list as a dependency.
const extUrlStored = signal<string | null>(null)
const extUrlEffective = signal<string | null>(null)
const extUrlSource = signal<string | null>(null)
const extUrlDraft = signal('')
const extUrlBusy = signal(false)
const extUrlLoaded = signal(false)

async function loadExternalUrl() {
  try {
    const r = await api.get('/api/admin/federation/external-url') as {
      base: string | null; effective: string | null; source: string | null
    }
    extUrlStored.value = r.base
    extUrlEffective.value = r.effective
    extUrlSource.value = r.source
    extUrlDraft.value = r.base ?? ''
  } catch {
    // Non-fatal — the rest of the page is still useful without it.
  } finally {
    extUrlLoaded.value = true
  }
}

/**
 * ExternalUrlSection — the federation inbox base URL peers POST to.
 *
 * Admin-only, and hidden under the Supervisor add-on (haos): there the
 * companion Home Assistant integration owns this value, and a
 * hand-typed URL would point at an inbox path only that integration
 * registers inside Home Assistant — so offering the field would invite
 * an unreachable address rather than fix one.
 *
 * `effective` is shown alongside the stored value because the two differ when
 * an automatic source is also present (`socialhome.toml` under
 * standalone, the integration under ha). Without it, an admin cannot
 * tell "I typed something" apart from "it is in effect".
 */
function ExternalUrlSection() {
  useEffect(() => { void loadExternalUrl() }, [])

  const save = async () => {
    extUrlBusy.value = true
    try {
      const body = extUrlDraft.value.trim() ? { base: extUrlDraft.value.trim() } : { base: null }
      const r = await api.put('/api/admin/federation/external-url', body) as {
        base: string | null; changed: boolean; peers_notified: number
      }
      extUrlStored.value = r.base
      await loadExternalUrl()
      showToast(
        r.base === null
          ? t('connections.ext.cleared')
          : r.peers_notified > 0
            ? t(isOne(r.peers_notified) ? 'connections.ext.saved_notified_one' : 'connections.ext.saved_notified',
              { n: String(r.peers_notified) })
            : t('connections.ext.saved'),
        'success',
      )
    } catch (err) {
      showToast(
        err instanceof Error && /422/.test(err.message)
          ? t('connections.ext.invalid')
          : t('connections.ext.failed'),
        'error',
      )
    } finally {
      extUrlBusy.value = false
    }
  }

  if (!extUrlLoaded.value) return null

  const dirty = extUrlDraft.value.trim() !== (extUrlStored.value ?? '')

  return (
    <section class="sh-connections-section sh-external-url-section">
      <div class="sh-section-header">
        <div class="sh-section-header__title">
          <h2>{t('connections.ext.title')}</h2>
        </div>
      </div>
      <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
        {t('connections.ext.intro')}
      </p>
      <div class="sh-external-url-row">
        <label class="sh-external-url-label" for="sh-external-url">
          {t('connections.ext.label')}
        </label>
        <input
          id="sh-external-url"
          class="sh-input"
          type="url"
          inputMode="url"
          autocomplete="off"
          placeholder="https://home.example.com"
          value={extUrlDraft.value}
          disabled={extUrlBusy.value}
          onInput={(e) => { extUrlDraft.value = (e.target as HTMLInputElement).value }}
        />
        <Button onClick={() => void save()} disabled={extUrlBusy.value || !dirty}>
          {extUrlBusy.value ? t('connections.ext.saving') : t('common.save')}
        </Button>
      </div>
      {extUrlEffective.value ? (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          {withNode(t('connections.ext.effective'), 'url', <code>{extUrlEffective.value}</code>)}
          {extUrlSource.value === 'auto' && t('connections.ext.from_config')}
          .
        </p>
      ) : (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          {t('connections.ext.not_configured')}
        </p>
      )}
      {extUrlStored.value && (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          {t('connections.ext.clear_hint')}
        </p>
      )}
    </section>
  )
}

/**
 * TroubleshootingSection — the two admin tools you reach for when
 * federation won't connect.
 *
 * Deliberately its own section rather than a tail of
 * ExternalUrlSection, where both used to live. That section is gated
 * ``!isSupervisorAddon()`` because the HA integration owns the external
 * URL under the add-on, and these two silently inherited the gate — so
 * they were missing on haos, the one mode that pulls ICE servers from
 * HA and the one whose users most need a diagnostics file to attach to
 * a bug report. Neither tool has anything to do with the external URL.
 */
function TroubleshootingSection() {
  return (
    <section class="sh-connections-section">
      <div class="sh-section-header">
        <div class="sh-section-header__title">
          <h2>{t('connections.troubleshooting')}</h2>
        </div>
      </div>
      <IceServersPanel />
      <DiagnosticsDownload />
    </section>
  )
}

/**
 * A household seated purely by an invite-link redeem (§D2b) — its
 * ``remote_instances`` row carries ``source = 'space_session'``. It is a
 * CONFIRMED peer for the shared space and nothing else: no DMs, no
 * profile sync, no presence, and it cannot vouch for a third household
 * in the trust-relay pairing flow. The list says so rather than
 * rendering social affordances that would fail closed on use.
 */
function isSpaceOnly(c: Connection): boolean {
  return c.source === 'space_session'
}

/** Same rule as the list's "Manage" button: confirmed household peers. */
function canManagePeer(c: Connection): boolean {
  return c.status === 'confirmed' && !isSpaceOnly(c)
}

export default function ConnectionsPage() {
  useTitle(t('connections.title'))
  const [autoPairBusy, setAutoPairBusy] = useState(false)
  const [detail, setDetail] = useState<Connection | null>(null)
  /** Active view toggle — resets to 'list' on mount. */
  const view = useSignal<'list' | 'map'>('list')

  useEffect(() => {
    void loadConnections()
    void loadSelfHome()
    void loadGfsConnections()
    void loadFederationCompat()
    if (currentUser.value?.is_admin) void loadAutoPairRequests()

    // ``pairing.confirmed`` / ``connection.removed`` are handled by the
    // connections store (``wireConnectionsWs``) so every view of the
    // list — this page and the dashboard map — updates live.
    const off2 = ws.on('pairing.aborted', () => {
      void loadConnections()
    })
    const off3 = ws.on('pairing.auto_pair_requested', () => {
      if (currentUser.value?.is_admin) void loadAutoPairRequests()
    })
    const off4 = ws.on('peer.transport_changed', (msg) => {
      const { instance_id, transport } = msg.data as { instance_id: string; transport: TransportState }
      connections.value = connections.value.map(c =>
        c.instance_id === instance_id
          ? { ...c, transport }
          : c,
      )
    })
    return () => { off2(); off3(); off4() }
  }, [])

  const confirmed = connections.value.filter(
    c => c.status === 'confirmed' && !isSpaceOnly(c),
  )
  const pending = connections.value.filter(c => c.status !== 'confirmed')
  const isAdmin = !!currentUser.value?.is_admin
  const compatByInstance = new Map(compatPeers.value.map(p => [p.instance_id, p]))

  return (
    <div class="sh-connections">

      {/* ── List / Map toggle ─────────────────────────────────────── */}
      <div class="sh-connections-view-toggle">
        <div class="sh-shopping-grouptoggle" role="group" aria-label={t('connections.view')}>
          <button
            type="button"
            class={view.value === 'list' ? 'sh-chip sh-chip--active' : 'sh-chip'}
            aria-pressed={view.value === 'list'}
            onClick={() => { view.value = 'list' }}
          >
            {t('connections.view_list')}
          </button>
          <button
            type="button"
            class={view.value === 'map' ? 'sh-chip sh-chip--active' : 'sh-chip'}
            aria-pressed={view.value === 'map'}
            onClick={() => { view.value = 'map' }}
          >
            {t('connections.view_map')}
          </button>
        </div>
      </div>

      {/* ── Federation Map ────────────────────────────────────────── */}
      {view.value === 'map' && (
        <Suspense fallback={<div class="sh-federation-map__loading">{t('connections.loading_map')}</div>}>
          <FederationMap
            onManage={isAdmin ? setDetail : undefined}
            canManage={canManagePeer}
          />
        </Suspense>
      )}

      {view.value === 'list' && (
      <>
      {/* ── Incoming auto-pair requests (admin-only inbox) ─────────── */}
      {isAdmin && autoPairRequests.value.length > 0 && (
        <section class="sh-auto-pair-inbox">
          <h2>{t('connections.requests.title')}</h2>
          <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
            {t('connections.requests.intro')}
          </p>
          {autoPairRequests.value.map(r => (
            <div key={r.request_id} class="sh-auto-pair-request">
              <div class="sh-auto-pair-request-body">
                <div>
                  {withNode(
                    t('connections.requests.wants_to_pair'), 'name',
                    <strong>{r.from_a_display}</strong>,
                  )}
                </div>
                <div class="sh-muted"
                     style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                  {withNode(
                    t('connections.requests.introduced_by'), 'name',
                    <strong>{r.via_b_display}</strong>,
                  )}
                  {' · '}
                  <time
                    dateTime={r.received_at}
                    title={new Date(r.received_at).toLocaleString(formatLocale())}
                  >
                    {relativeDocsTime(r.received_at)}
                  </time>
                </div>
              </div>
              <div class="sh-row" style={{ gap: 'var(--sh-space-xs)' }}>
                <Button variant="secondary"
                        onClick={() => void declineAutoPair(r)}>
                  {t('connections.requests.decline')}
                </Button>
                <Button onClick={() => void approveAutoPair(r)}>
                  {t('connections.requests.approve')}
                </Button>
              </div>
            </div>
          ))}
        </section>
      )}

      {/* ── External URL (admin-only; the integration owns it on haos) ── */}
      {isAdmin && !isSupervisorAddon() && <ExternalUrlSection />}

      {/* ── Households ─────────────────────────────────────────────── */}
      <section class="sh-connections-section">
        <div class="sh-section-header">
          <div class="sh-section-header__title">
            <h2>{t('connections.households')}</h2>
            {compatOurs.value > 0 && (
              <span class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.your_version', { v: String(compatOurs.value) })}
              </span>
            )}
            {peersBehindCount() > 0 && (
              <span class="sh-chip sh-chip--update"
                    aria-label={t(
                      isOne(peersBehindCount())
                        ? 'connections.compat.households_behind_one'
                        : 'connections.compat.households_behind',
                      { n: String(peersBehindCount()) },
                    )}>
                {behindLabel(peersBehindCount())}
              </span>
            )}
          </div>
          {isAdmin && (
          <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', flexWrap: 'wrap' }}>
            {confirmed.length > 0 && (
              <Button variant="secondary"
                      loading={autoPairBusy}
                      onClick={() => {
                        setAutoPairBusy(false)
                        openAutoPair(confirmed.map(c => ({
                          instance_id: c.instance_id,
                          display_name: c.display_name,
                        })))
                      }}>
                {t('connections.pair_via_peer')}
              </Button>
            )}
            <Button onClick={() => openPairing('household')}>
              + {t('connections.pair')}
            </Button>
          </div>
          )}
        </div>
        {!isAdmin && (
          <p class="sh-muted sh-connections-admin-hint">
            {t('connections.admin_only')}
          </p>
        )}
        {loading.value ? (
          <Spinner />
        ) : connections.value.length === 0 ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">🔗</div>
            <h3>{t('connections.no_connections')}</h3>
            <p class="sh-muted">{t('connections.no_connections_hint')}</p>
            {isAdmin && (
              <Button onClick={() => openPairing('household')}>
                {t('connections.start_pairing')}
              </Button>
            )}
          </div>
        ) : (
          <div class="sh-connection-list">
            {pending.length > 0 && (
              <div class="sh-connections-pending-hint sh-muted">
                ⏳ {t(isOne(pending.length) ? 'connections.pending_one' : 'connections.pending',
                  { n: String(pending.length) })}
              </div>
            )}
            {connections.value.map(c => (
              <div key={c.instance_id}
                   class={`sh-connection-card ${c.status === 'confirmed' ? '' : 'sh-connection-card--pending'}`}>
                <div class="sh-connection-info">
                  <span class={hfsStatusDotClass(c)} />
                  <strong>{c.display_name}</strong>
                  {transportIcon(c.transport)}
                  <span class="sh-type-badge">
                    {isSpaceOnly(c) ? t('connections.type.space_only') : t('connections.type.household')}
                  </span>
                  {c.status === 'confirmed' && compatBadge(compatByInstance.get(c.instance_id))}
                  {c.status !== 'confirmed' && (
                    <span class="sh-muted">
                      {c.status === 'pending_sent' ? t('connections.status.pending_sent') :
                       c.status === 'pending_received' ? t('connections.status.pending_received') :
                       c.status}
                    </span>
                  )}
                  {c.status === 'confirmed' && !c.reachable && (
                    <span class="sh-muted">{t('connections.status.unreachable')}</span>
                  )}
                  {isSpaceOnly(c) && (
                    <span class="sh-muted"
                          style={{ fontSize: 'var(--sh-font-size-xs)' }}
                          data-testid={`space-only-note-${c.instance_id}`}>
                      {t('connections.space_only_note')}
                    </span>
                  )}
                </div>
                <div class="sh-connection-actions">
                  {isAdmin && c.status === 'confirmed' && (
                    <>
                      {!isSpaceOnly(c) && (
                        <Button variant="secondary"
                                onClick={() => setDetail(c)}>
                          {t('connections.manage')}
                        </Button>
                      )}
                      <Button variant="danger"
                              onClick={() => void unpair(c.instance_id)}>
                        {t('connections.unpair')}
                      </Button>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── Global Federation Servers ──────────────────────────────── */}
      <section class="sh-connections-section">
        <div class="sh-section-header">
          <h2>{t('connections.global_servers')}</h2>
          {isAdmin && (
            <Button onClick={() => openPairing('gfs')}>+ {t('gfs.add')}</Button>
          )}
        </div>
        {gfsLoading.value ? (
          <Spinner />
        ) : gfsConnections.value.length === 0 ? (
          <div class="sh-empty-state">
            <div aria-hidden="true">🌐</div>
            <h3>{t('gfs.no_servers')}</h3>
            <p class="sh-muted">{t('gfs.no_servers_hint')}</p>
            {isAdmin && (
              <Button onClick={() => openPairing('gfs')}>+ {t('gfs.add')}</Button>
            )}
          </div>
        ) : (
          <div class="sh-connection-list">
            {gfsConnections.value.map(gfs => {
              const live = gfsLiveState(gfs)
              return (
              <div key={gfs.id} class="sh-connection-card">
                <div class="sh-connection-info">
                  <span class={live.dotClass} />
                  <strong>{gfs.display_name}</strong>
                  <span class="sh-type-badge">{t('connections.type.gfs')}</span>
                  {live.labelKey && (
                    <span class={live.labelClass ?? 'sh-muted'}>
                      {t(live.labelKey)}
                    </span>
                  )}
                  <span class="sh-muted">{gfs.inbox_url}</span>
                </div>
                {isAdmin && (
                  <div class="sh-connection-actions">
                    <Button variant="danger"
                            onClick={() => { disconnectTarget.value = gfs }}>
                      {t('gfs.disconnect')}
                    </Button>
                  </div>
                )}
              </div>
              )
            })}
          </div>
        )}
      </section>

      {/* ── Troubleshooting (admin-only in EVERY mode — haos included) ──
           Last on the page on purpose: it is a once-in-a-while tool, and
           the households list is what people come here for. */}
      {isAdmin && <TroubleshootingSection />}
      </>
      )}

      {isAdmin && (
        <>
          <PairingFlow onGfsConnected={loadGfsConnections} />
          <AutoPairDialog onPaired={() => void loadConnections()} />
        </>
      )}
      {detail && (
        <ConnectionDetail
          conn={{
            instance_id: detail.instance_id,
            display_name: detail.display_name,
            federated_display_name: detail.federated_display_name,
            local_alias: detail.local_alias ?? null,
            status: detail.status ?? 'confirmed',
            unreachable_since: detail.unreachable_since ?? null,
            last_reachable_at: detail.last_reachable_at ?? null,
            last_relay_accepted_at: detail.last_relay_accepted_at ?? null,
            relay_only: detail.relay_only ?? false,
            queued_envelopes: detail.queued_envelopes ?? 0,
            dropped_envelopes: detail.dropped_envelopes ?? 0,
            paired_at: detail.paired_at ?? null,
            transport: detail.transport ?? null,
            proto_version: detail.proto_version,
          }}
          compat={detail ? compatByInstance.get(detail.instance_id) : undefined}
          onClose={() => setDetail(null)}
          onRevoke={() => { setDetail(null); void loadConnections() }}
          onAliasSaved={() => { setDetail(null); void loadConnections() }}
        />
      )}
      <ConfirmDialog
        open={disconnectTarget.value !== null}
        title={t('gfs.disconnect')}
        message={t('gfs.confirm_disconnect')}
        confirmLabel={t('gfs.disconnect')}
        destructive
        onConfirm={() => disconnectTarget.value && disconnectGfs(disconnectTarget.value)}
        onCancel={() => { disconnectTarget.value = null }}
      />
    </div>
  )
}
