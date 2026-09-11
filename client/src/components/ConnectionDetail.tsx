/**
 * ConnectionDetail — per-connection settings (§23.88, §23.89, §23.90).
 */
import { signal } from '@preact/signals'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { relativeDocsTime } from '@/utils/relativeTime'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { ShareHomeToggle } from './ShareHomeToggle'
import {
  peerSupportsResync,
  resyncPeerCapabilities,
  loadFederationCompat,
  type CompatPeer,
} from '@/store/federationCompat'

interface Connection {
  instance_id: string; display_name: string; status: string
  inbox_url: string; intro_relay_enabled: boolean
  unreachable_since: string | null; paired_at: string | null
  /** Last moment an outbound envelope to this peer was accepted
   *  (``remote_instances.last_reachable_at``). ``null`` when it has never
   *  been reached. "Not connected" is the most common federation support
   *  question and the status chip alone can't answer it: a peer that
   *  dropped a minute ago and one that has been dead for three weeks look
   *  identical, yet one means wait and the other means investigate. */
  last_reachable_at?: string | null
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
  /** Active federation transport for this peer. Shown read-only
   *  in the detail panel so the admin can see whether WebRTC is up. */
  transport?: 'rtc' | 'https' | null
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
        const b = body as { last_relay: RelayDetail | null }
        if (!cancelled) setRelay(b?.last_relay ?? null)
      })
      .catch(() => {
        if (!cancelled) setRelay(null)
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
        next
          ? `${u.display_name} is now visible to ${conn.display_name}`
          : `${u.display_name} is now hidden from ${conn.display_name}`,
        'success',
      )
    } catch (e: any) {
      showToast(e.message || 'Failed to update visibility', 'error')
    } finally {
      setVisBusy(b => {
        const n = new Set(b)
        n.delete(u.user_id)
        return n
      })
    }
  }

  const toggleRelay = async () => {
    try {
      await api.patch(`/api/pairing/connections/${conn.instance_id}/settings`, {
        intro_relay_enabled: !conn.intro_relay_enabled,
      })
      showToast('Setting updated', 'success')
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
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
          ? `Renamed to "${next}" — only visible to your household.`
          : 'Local rename cleared. Showing the household’s own name.',
        'success',
      )
      onAliasSaved?.()
    } catch (e: any) {
      showToast(e.message || 'Failed to save', 'error')
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
      showToast(`Asked ${conn.display_name ?? 'peer'} to re-advertise its version`, 'info')
      setTimeout(() => { void loadFederationCompat() }, 2500)
    } catch (e: any) {
      showToast(e.message || 'Re-check failed', 'error')
    } finally {
      setRecheckBusy(false)
    }
  }

  const revoke = async () => {
    try {
      await api.delete(`/api/pairing/connections/${conn.instance_id}`)
      showToast('Connection revoked', 'info')
      showRevoke.value = false
      onRevoke()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
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
            Display this household as
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
              Save
            </Button>
          </div>
          <p class="sh-muted sh-connection-alias__hint">
            Only your household sees this name.
            {conn.local_alias
              ? null
              : ` They advertise themselves as "${peerName}".`}
          </p>
        </section>
        <hr />
        <dl>
          <dt>Instance ID</dt><dd class="sh-mono">{conn.instance_id}</dd>
          <dt>Status</dt><dd class={`sh-status sh-status--${conn.status}`}>{conn.status}</dd>
          <dt>Inbox</dt><dd class="sh-mono sh-muted">{conn.inbox_url}</dd>
          {conn.paired_at && <><dt>Paired</dt><dd>{new Date(conn.paired_at).toLocaleString()}</dd></>}
          {conn.proto_version != null && (
            <><dt>Protocol version</dt><dd>v{conn.proto_version}</dd></>
          )}
          {compat && compat.capabilities_known && (
            compat.lacking_features.length === 0 ? (
              <><dt>Compatibility</dt><dd><span class="sh-chip sh-chip--success">up to date ✓</span></dd></>
            ) : (
              <><dt>Missing features</dt><dd>{compat.lacking_features.join(', ')}</dd></>
            )
          )}
          {/* Absolute timestamp AND a relative hint: the absolute one is
              what you quote in a bug report, the relative one is what
              tells you at a glance whether this is a blip or weeks of
              silence. */}
          <dt>Last connected</dt>
          <dd>
            {conn.last_reachable_at ? (
              <>
                {new Date(conn.last_reachable_at).toLocaleString()}
                <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)' }}>
                  ({relativeDocsTime(conn.last_reachable_at)})
                </span>
              </>
            ) : (
              <span class="sh-muted">never</span>
            )}
          </dd>
          {conn.unreachable_since && (
            <><dt>Unreachable since</dt><dd class="sh-text-warning">
              {new Date(conn.unreachable_since).toLocaleString()}
              <span class="sh-muted" style={{ marginLeft: 'var(--sh-space-xs)' }}>
                ({relativeDocsTime(conn.unreachable_since)})
              </span>
            </dd></>
          )}
          {conn.transport === 'rtc' && (
            <><dt>Transport</dt><dd>
              Direct (WebRTC DataChannel)
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                Low-latency channel open between your add-on and the peer's.
              </span>
            </dd></>
          )}
          {conn.transport === 'https' && (
            <><dt>Transport</dt><dd>
              HTTPS inbox (fallback)
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                Direct channel unavailable — usually a NAT or firewall block.
                Federation works, just at higher latency.
              </span>
            </dd></>
          )}
          {relay !== null && (
            <><dt>DM path</dt><dd>
              You → 🔁 {relay.via} → {conn.display_name}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-sm)' }}>
                Last DM took the relay path {relativeDocsTime(relay.ts)}.
              </span>
            </dd></>
          )}
        </dl>
        {compat && peerSupportsResync(compat) && (
          <div class="sh-row" style={{ marginBottom: 'var(--sh-space-sm)' }}>
            <Button variant="secondary" onClick={() => void recheck()} loading={recheckBusy}>
              Re-check version
            </Button>
          </div>
        )}
        <label class="sh-toggle-row">
          <input type="checkbox" checked={conn.intro_relay_enabled} onChange={toggleRelay} />
          Allow introduced pairing (friend-of-a-friend)
        </label>
        <section class="sh-connection-share-home">
          <h4 style={{ margin: '12px 0 4px' }}>Home location</h4>
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
                Who's visible to {conn.display_name}?
              </h4>
              <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-sm)' }}>
                Unticked members are removed from this household's view.
                Their existing DMs, highlights, and moments are deleted
                on this side; future ones don't arrive at all. Posts in
                shared spaces stay — spaces own their own audience.
                Tick again to restore the contact for new content.
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
                            admin
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
        <Button variant="danger" onClick={() => showRevoke.value = true}>Revoke connection</Button>
      </div>
      <ConfirmDialog open={showRevoke.value} title="Revoke connection?"
        message="This will permanently disconnect this household. All shared spaces will stop syncing. You'll need to re-pair via QR to reconnect."
        confirmLabel="Revoke" destructive onConfirm={revoke}
        onCancel={() => showRevoke.value = false} />
    </Modal>
  )
}
