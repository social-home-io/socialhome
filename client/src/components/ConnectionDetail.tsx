/**
 * ConnectionDetail — per-connection settings (§23.88, §23.89, §23.90).
 */
import { signal } from '@preact/signals'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { normaliseTimestamp, relativeDocsTime } from '@/utils/relativeTime'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { ShareHomeToggle } from './ShareHomeToggle'
import { t, isOne } from '@/i18n/i18n'
import {
  peerSupportsResync,
  resyncPeerCapabilities,
  loadFederationCompat,
  type CompatPeer,
} from '@/store/federationCompat'

interface Connection {
  instance_id: string; display_name: string; status: string
  unreachable_since: string | null; paired_at: string | null
  /** Last moment an outbound envelope to this peer was accepted
   *  (``remote_instances.last_reachable_at``). ``null`` when it has never
   *  been reached. "Not connected" is the most common federation support
   *  question and the status chip alone can't answer it: a peer that
   *  dropped a minute ago and one that has been dead for three weeks look
   *  identical, yet one means wait and the other means investigate. */
  last_reachable_at?: string | null
  /** Last time the connection-server relay ACCEPTED an envelope for this
   *  peer. Acceptance is not delivery — see ``relay_only``. Absent on
   *  older API responses and when the relay was never used. */
  last_relay_accepted_at?: string | null
  /** The relay has accepted traffic for this peer more recently than any
   *  proven delivery: "accepted by the connection server", not "delivered". */
  relay_only?: boolean
  /** Whether our household's home pin is shared with this peer (§23.90).
   *  Defaults to true when absent (old API responses pre-dating the field). */
  share_home?: boolean
  /** The raw name the peer advertised via the federation handshake.
   *  Shown read-only so the admin sees "Peer advertises: <name>"
   *  alongside their own editable alias. */
  federated_display_name?: string
  /** Local-only alias the admin set; ``null`` until they set one.
   *  When non-null, ``display_name`` already reflects this value
   *  (the backend pre-resolves the effective name). */
  local_alias?: string | null
  /** Undelivered federation envelopes still queued for this peer.
   *  Rendered only when non-zero, next to "Unreachable since": a peer
   *  that has been dark for months is sitting on a visible backlog, and
   *  seeing it is the difference between "wait" and "re-pair". Absent on
   *  older API responses. */
  queued_envelopes?: number
  /** Envelopes permanently given up on for this peer — a PERMANENT
   *  rejection or an exhausted retry budget. Never retried, so they are
   *  rendered as their own line: folding them into the queued count
   *  would present data loss as "queued for delivery". Absent on older
   *  API responses. */
  dropped_envelopes?: number
  /** Active federation transport for this peer. Shown read-only
   *  in the detail panel so the admin can see whether WebRTC is up. */
  transport?: 'rtc' | 'https' | 'gfs_relay' | null
  /** Monotonic federation protocol version the peer last advertised via
   *  INSTANCE_CAPABILITIES_UPDATED. Shown read-only so an admin can spot a
   *  peer that's behind. Absent on old API responses (defaults to v1 there). */
  proto_version?: number
}

interface VisibleUser {
  user_id: string
  username: string
  display_name: string
  is_admin: boolean
  visible: boolean
}

interface RelayDetail {
  via: string
  ts: string
}

const showRevoke = signal(false)

const STATUS_KEYS: Record<string, string> = {
  confirmed: 'connections.status.confirmed',
  pending_sent: 'connections.status.pending_sent',
  pending_received: 'connections.status.pending_received',
}

/** Translated label for a connection status; unknown values show as-is. */
function statusLabel(status: string): string {
  const key = STATUS_KEYS[status]
  return key ? t(key) : status
}

export function ConnectionDetail({ conn, compat, onClose, onRevoke, onAliasSaved }: {
  conn: Connection
  /** Matched federation-compat row for this peer, if loaded. Surfaces the
   *  peer's missing-feature list + the "Re-check version" affordance. */
  compat?: CompatPeer
  onClose: () => void
  onRevoke: () => void
  /** Called after the alias was successfully saved so the parent
   *  can refresh its listing — the rendered ``display_name``
   *  changes everywhere the connection is shown. */
  onAliasSaved?: () => void
}) {
  const [visUsers, setVisUsers] = useState<VisibleUser[] | null>(null)
  const [visBusy, setVisBusy] = useState<Set<string>>(new Set())
  const [alias, setAlias] = useState(conn.local_alias ?? '')
  const [aliasBusy, setAliasBusy] = useState(false)
  const [relay, setRelay] = useState<RelayDetail | null>(null)
  /** Peer inbox address — only returned (admin-only, via
   *  ``/transport-detail``) for a confirmed, directly paired household
   *  that isn't relay-only. ``null`` hides the row. */
  const [inboxUrl, setInboxUrl] = useState<string | null>(null)
  const [recheckBusy, setRecheckBusy] = useState(false)
  /** Effective display name as currently rendered — handshake name
   *  if no alias is set, else the alias. Shown above the input as
   *  the "Display this household as" hint. */
  const peerName =
    conn.federated_display_name ?? conn.display_name

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const body = await api.get(
          `/api/pairing/connections/${conn.instance_id}/visible-users`,
        ) as { users: VisibleUser[] }
        if (!cancelled) setVisUsers(body.users)
      } catch {
        if (!cancelled) {
          // 403 (non-admin) or 404 (peer no longer confirmed) — silently
          // hide the section rather than scary-error the whole modal.
          setVisUsers([])
        }
      }
    })()
    return () => { cancelled = true }
  }, [conn.instance_id])

  useEffect(() => {
    let cancelled = false
    api.get(`/api/pairing/connections/${conn.instance_id}/transport-detail`)
      .then((body: unknown) => {
        const b = body as { last_relay: RelayDetail | null, inbox_url?: string | null }
        if (cancelled) return
        setRelay(b?.last_relay ?? null)
        setInboxUrl(b?.inbox_url || null)
      })
      .catch(() => {
        if (cancelled) return
        setRelay(null)
        setInboxUrl(null)
      })
    return () => { cancelled = true }
  }, [conn.instance_id])

  const toggleVisibility = async (u: VisibleUser) => {
    const next = !u.visible
    setVisBusy(b => new Set(b).add(u.user_id))
    try {
      const body = await api.patch(
        `/api/pairing/connections/${conn.instance_id}/visible-users`,
        { updates: [{ user_id: u.user_id, visible: next }] },
      ) as { users: VisibleUser[] }
      setVisUsers(body.users)
      showToast(
        t(next ? 'connections.detail.toast_visible' : 'connections.detail.toast_hidden', {
          user: u.display_name, name: conn.display_name,
        }),
        'success',
      )
    } catch (e: any) {
      showToast(e.message || t('connections.detail.visibility_failed'), 'error')
    } finally {
      setVisBusy(b => {
        const n = new Set(b)
        n.delete(u.user_id)
        return n
      })
    }
  }

  const saveAlias = async () => {
    if (aliasBusy) return
    const trimmed = alias.trim()
    // Empty → null. Same trimmed value as the displayed one → no-op.
    const next: string | null = trimmed || null
    if ((next ?? '') === (conn.local_alias ?? '')) return
    setAliasBusy(true)
    try {
      await api.patch(`/api/pairing/connections/${conn.instance_id}/alias`, {
        alias: next,
      })
      showToast(
        next
          ? t('connections.detail.alias_saved', { name: next })
          : t('connections.detail.alias_cleared'),
        'success',
      )
      onAliasSaved?.()
    } catch (e: any) {
      showToast(e.message || t('connections.detail.save_failed'), 'error')
    } finally {
      setAliasBusy(false)
    }
  }

  /** Ask the peer to re-advertise its protocol version. The fresh
   *  proto_version arrives asynchronously via the peer's reply, so we refresh
   *  the compat store a beat after firing. */
  const recheck = async () => {
    setRecheckBusy(true)
    try {
      await resyncPeerCapabilities(conn.instance_id)
      showToast(t('connections.detail.recheck_sent', { name: conn.display_name }), 'info')
      setTimeout(() => { void loadFederationCompat() }, 2500)
    } catch (e: any) {
      showToast(e.message || t('connections.detail.recheck_failed'), 'error')
    } finally {
      setRecheckBusy(false)
    }
  }

  const revoke = async () => {
    try {
      await api.delete(`/api/pairing/connections/${conn.instance_id}`)
      showToast(t('connections.detail.removed'), 'info')
      showRevoke.value = false
      onRevoke()
    } catch (e: any) { showToast(e.message || t('common.error'), 'error') }
  }

  const aliasDirty = alias.trim() !== (conn.local_alias ?? '').trim()
  return (
    <Modal open={true} onClose={onClose} title={conn.display_name}>
      <div class="sh-connection-detail">
        <section class="sh-connection-alias">
          <label
            class="sh-connection-alias__label"
            for="sh-connection-alias-input"
          >
            {t('connections.detail.alias_label')}
          </label>
          <div class="sh-connection-alias__row">
            <input
              id="sh-connection-alias-input"
              type="text"
              class="sh-input sh-connection-alias__input"
              maxLength={80}
              placeholder={peerName}
              value={alias}
              disabled={aliasBusy}
              onInput={(e) =>
                setAlias((e.target as HTMLInputElement).value)
              }
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void saveAlias()
                }
              }}
            />
            <Button
              variant="secondary"
              onClick={() => void saveAlias()}
              disabled={!aliasDirty || aliasBusy}
            >
              {t('common.save')}
            </Button>
          </div>
          <p class="sh-muted sh-connection-alias__hint">
            {t('connections.detail.alias_hint')}
            {conn.local_alias
              ? null
              : ` ${t('connections.detail.alias_own_name', { name: peerName })}`}
          </p>
        </section>
        <hr />
        <dl>
          <dt>{t('connections.detail.household_id')}</dt><dd class="sh-mono">{conn.instance_id}</dd>
          <dt>{t('connections.detail.status')}</dt><dd class={`sh-status sh-status--${conn.status}`}>{statusLabel(conn.status)}</dd>
          {inboxUrl && (
            <><dt>{t('connections.detail.address')}</dt><dd class="sh-mono sh-muted">{inboxUrl}</dd></>
          )}
          {conn.paired_at && <><dt>{t('connections.detail.paired')}</dt><dd>{new Date(conn.paired_at).toLocaleString()}</dd></>}
          {conn.proto_version != null && (
            <><dt>{t('connections.detail.app_version')}</dt><dd>v{conn.proto_version}</dd></>
          )}
          {compat && compat.capabilities_known && (
            compat.lacking_features.length === 0 ? (
              <><dt>{t('connections.detail.compatibility')}</dt><dd><span class="sh-chip sh-chip--success">{t('connections.compat.up_to_date')}</span></dd></>
            ) : (
              <><dt>{t('connections.detail.missing_features')}</dt><dd>{compat.lacking_features.join(', ')}</dd></>
            )
          )}
          {/* Absolute timestamp AND a relative hint: the absolute one is
              what you quote in a bug report, the relative one is what
              tells you at a glance whether this is a blip or weeks of
              silence. */}
          <dt>{t('connections.detail.last_connected')}</dt>
          <dd>
            {conn.last_reachable_at ? (
              <>
                {new Date(normaliseTimestamp(conn.last_reachable_at)).toLocaleString()}
                <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)' }}>
                  ({relativeDocsTime(conn.last_reachable_at)})
                </span>
              </>
            ) : (
              <span class="sh-muted">{t('connections.detail.never')}</span>
            )}
          </dd>
          {/* Relay acceptance is shown apart from "Last connected": the
              connection server answers the same 202 whether or not the
              household is online, so it must never read as delivery. */}
          {conn.last_relay_accepted_at && (
            <><dt>GFS</dt><dd>
              {conn.relay_only && (
                <span class="sh-chip sh-chip--honey" style={{ marginRight: 'var(--sh-space-xs)' }}>
                  {t('connections.detail.gfs_only')}
                </span>
              )}
              {t('connections.detail.gfs_last_handed', { time: new Date(normaliseTimestamp(conn.last_relay_accepted_at)).toLocaleString() })}
              <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)' }}>
                ({relativeDocsTime(conn.last_relay_accepted_at)})
              </span>
              {conn.relay_only && (
                <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                  {t('connections.detail.gfs_only_hint')}
                </span>
              )}
            </dd></>
          )}
          {conn.unreachable_since && (
            <><dt>{t('connections.detail.unreachable_since')}</dt><dd class="sh-text-warning">
              {new Date(normaliseTimestamp(conn.unreachable_since)).toLocaleString()}
              <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)' }}>
                ({relativeDocsTime(conn.unreachable_since)})
              </span>
            </dd></>
          )}
          {(conn.queued_envelopes ?? 0) > 0 && (
            <><dt>{t('connections.detail.waiting')}</dt><dd>
              {t(isOne(conn.queued_envelopes ?? 0) ? 'connections.detail.queued_one' : 'connections.detail.queued',
                { n: String(conn.queued_envelopes) })}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.queued_hint')}
              </span>
            </dd></>
          )}
          {(conn.dropped_envelopes ?? 0) > 0 && (
            <><dt>{t('connections.detail.undelivered')}</dt><dd class="sh-text-warning">
              {t(isOne(conn.dropped_envelopes ?? 0) ? 'connections.detail.dropped_one' : 'connections.detail.dropped',
                { n: String(conn.dropped_envelopes) })}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.dropped_hint')}
              </span>
            </dd></>
          )}
          {conn.transport === 'rtc' && (
            <><dt>Transport</dt><dd>
              {t('connections.transport.direct')}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.direct_hint')}
              </span>
            </dd></>
          )}
          {conn.transport === 'https' && (
            <><dt>Transport</dt><dd>
              {t('connections.transport.internet')}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.internet_hint')}
              </span>
            </dd></>
          )}
          {conn.transport === 'gfs_relay' && (
            <><dt>Transport</dt><dd>
              {t('connections.transport.gfs')}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.gfs_hint')}
              </span>
            </dd></>
          )}
          {relay !== null && (
            <><dt>{t('connections.detail.chat_route')}</dt><dd>
              {t('connections.detail.you')} → 🔁 {relay.via} → {conn.display_name}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.chat_route_hint', { via: relay.via, time: relativeDocsTime(relay.ts) })}
              </span>
            </dd></>
          )}
        </dl>
        {compat && peerSupportsResync(compat) && (
          <div class="sh-row" style={{ marginBottom: 'var(--sh-space-sm)' }}>
            <Button variant="secondary" onClick={() => void recheck()} loading={recheckBusy}>
              {t('connections.detail.recheck')}
            </Button>
          </div>
        )}
        <section class="sh-connection-share-home">
          <h4 style={{ margin: '12px 0 4px' }}>{t('connections.detail.home_location')}</h4>
          <ShareHomeToggle
            instanceId={conn.instance_id}
            peerName={conn.display_name}
            initialValue={conn.share_home ?? true}
          />
        </section>

        {visUsers !== null && visUsers.length > 0 && (
          <>
            <hr />
            <div class="sh-visible-users">
              <h4 style={{ margin: '0 0 4px' }}>
                {t('connections.detail.visible_title', { name: conn.display_name })}
              </h4>
              <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
                {t('connections.detail.visible_hint')}
              </p>
              <ul class="sh-visible-users-list" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
                {visUsers.map(u => (
                  <li key={u.user_id} class="sh-visible-users-row">
                    <label class="sh-toggle-row">
                      <input
                        type="checkbox"
                        checked={u.visible}
                        disabled={visBusy.has(u.user_id)}
                        onChange={() => void toggleVisibility(u)}
                      />
                      <span>
                        {u.display_name || u.username}
                        {u.is_admin && (
                          <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)', fontSize: 'var(--sh-font-size-xs)' }}>
                            {t('connections.detail.admin')}
                          </span>
                        )}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
        {visUsers === null && (
          <div style={{ textAlign: 'center', padding: 'var(--sh-space-sm)' }}>
            <Spinner />
          </div>
        )}

        <hr />
        <Button variant="danger" onClick={() => showRevoke.value = true}>{t('connections.detail.remove')}</Button>
      </div>
      <ConfirmDialog open={showRevoke.value} title={t('connections.detail.remove_title')}
        message={t('connections.detail.remove_message')}
        confirmLabel={t('connections.detail.remove_ok')} destructive onConfirm={revoke}
        onCancel={() => showRevoke.value = false} />
    </Modal>
  )
}
