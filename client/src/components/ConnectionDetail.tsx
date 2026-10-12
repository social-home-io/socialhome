/**
 * ConnectionDetail — the Manage sheet for a paired household (§23.88,
 * §23.89, §23.90). Admin-only: members never reach it.
 *
 * Top to bottom (Connections HANDOFF §2, "Manage sheet"):
 *   header      ConnAvatar + name + 44 px pencil → inline rename (✓ / Cancel
 *               replace the dialog ×; Esc or Cancel discard only the edit)
 *               + one status line
 *   facts       Last connected · Connection · App version (tiles)
 *   notes       only when something needs saying (unreachable, backlog,
 *               dropped messages, relay-only, slow path)
 *   Sharing     home location + GFS fallback as setting cards
 *   Who can see per-person visibility (``/visible-users``)
 *   Technical   collapsed: ID + address (copy buttons), paired date,
 *               missing features, chat route, Check version again
 *   Danger zone outlined "Unpair {name}" → confirm → DELETE
 *
 * A dialog on desktop, a bottom sheet ≤ 640 px (``.sh-modal`` rules).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { normaliseTimestamp, relativeDocsTime } from '@/utils/relativeTime'
import { featureLabels } from '@/utils/capabilityLabels'
import { Modal } from './Modal'
import { Avatar } from './Avatar'
import { ConfirmDialog } from './ConfirmDialog'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { ShareHomeToggle } from './ShareHomeToggle'
import { GfsFallbackToggle } from './GfsFallbackToggle'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import {
  peerSupportsResync,
  resyncPeerCapabilities,
  loadFederationCompat,
  type CompatPeer,
} from '@/store/federationCompat'
import { ConnAvatar, type AvatarStatus } from '@/features/connections/ConnAvatar'
import { ConnIcon, type ConnIconName } from '@/features/connections/ConnIcon'
import { formatSince, householdStatus, initialOf, tintFor, versionsBehind } from '@/features/connections/model'

interface Connection {
  instance_id: string; display_name: string; status: string
  unreachable_since: string | null; paired_at: string | null
  /** Live reachability (WS-patched). When absent, ``unreachable_since``
   *  decides — see ``householdStatus``. */
  reachable?: boolean
  /** Last moment an outbound envelope to this peer was accepted
   *  (``remote_instances.last_reachable_at``). ``null`` when it has never
   *  been reached. A peer that dropped a minute ago and one that has been
   *  dead for three weeks look alike without it. */
  last_reachable_at?: string | null
  /** Last time the GFS relay ACCEPTED an envelope for this peer.
   *  Acceptance is not delivery — see ``relay_only``. */
  last_relay_accepted_at?: string | null
  /** The relay has accepted traffic for this peer more recently than any
   *  proven delivery: "accepted by the GFS", not "delivered". */
  relay_only?: boolean
  /** Whether our household's home pin is shared with this peer (§23.90).
   *  Defaults to true when absent (old API responses). */
  share_home?: boolean
  /** The raw name the peer advertised via the federation handshake. */
  federated_display_name?: string
  /** Local-only alias the admin set; ``null`` until they set one. When
   *  non-null, ``display_name`` already reflects it. */
  local_alias?: string | null
  /** Undelivered federation envelopes still queued for this peer. */
  queued_envelopes?: number
  /** Envelopes permanently given up on — never retried, so never folded
   *  into the queued count. */
  dropped_envelopes?: number
  /** Active federation transport for this peer. */
  transport?: 'rtc' | 'https' | 'gfs_relay' | null
  /** Protocol version the peer last advertised. */
  proto_version?: number
  /** ``manual`` (paired directly) or ``space_session`` (invite link).
   *  Only a direct pair gets the GFS fallback switch. */
  source?: string
  /** GFS fallback (v_54): our own opt-in for this household. */
  gfs_relay?: boolean
  /** GFSes both households were proven to use — a count, never a list. */
  gfs_routes?: number
  /** We hold the other household's key-wrap key. */
  peer_keywrap_known?: boolean
  /** The switch can work with this household at all. */
  gfs_relay_available?: boolean
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

const TRANSPORT: Record<'rtc' | 'https' | 'gfs_relay', { icon: ConnIconName; key: string; hint: string }> = {
  rtc: { icon: 'bolt', key: 'connections.transport.direct', hint: 'connections.detail.direct_hint' },
  https: { icon: 'globe', key: 'connections.transport.internet', hint: 'connections.detail.internet_hint' },
  gfs_relay: { icon: 'cloud', key: 'connections.transport.gfs', hint: 'connections.detail.gfs_hint' },
}

function capitalise(s: string): string {
  return s ? s.charAt(0).toLocaleUpperCase(formatLocale()) + s.slice(1) : s
}

function absolute(iso: string): string {
  return new Date(normaliseTimestamp(iso)).toLocaleString(formatLocale())
}

/** "v57, same as you" / "v55, 2 behind" / "v58, newer than yours" / "v55". */
export function versionText(peerVersion: number, ours: number, compat?: CompatPeer): string {
  const v = `v${peerVersion}`
  if (ours <= 0 || (compat && !compat.capabilities_known)) return v
  const behind = versionsBehind(compat ?? {
    instance_id: '', display_name: '', proto_version: peerVersion, status: '',
    last_reachable_at: null, capabilities_known: true, lacking_features: [],
  }, ours)
  if (behind > 0) {
    return t(isOne(behind) ? 'connections.manage.version_behind_one' : 'connections.manage.version_behind',
      { v, n: String(behind) })
  }
  if (peerVersion > ours) return t('connections.manage.version_newer', { v })
  return t('connections.manage.version_same', { v })
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    showToast(t('connections.manage.copied'), 'success')
  } catch {
    showToast(t('connections.manage.copy_failed'), 'error')
  }
}

export function ConnectionDetail({ conn, compat, ours = 0, relayGfs = false, onClose, onRevoke, onAliasSaved }: {
  conn: Connection
  /** This household has at least one GFS connection that relays
   *  envelopes — without one the GFS fallback switch is hidden (unless it
   *  is already on, so it can still be turned off). */
  relayGfs?: boolean
  /** Matched federation-compat row for this peer, if loaded. */
  compat?: CompatPeer
  /** Our own protocol version (admin compat data); 0 = unknown. */
  ours?: number
  onClose: () => void
  onRevoke: () => void
  /** Called after the alias was saved so the parent can refresh its
   *  listing. The sheet stays open and shows the new name. */
  onAliasSaved?: () => void
}) {
  const id = encodeURIComponent(conn.instance_id)
  const peerName = conn.federated_display_name ?? conn.display_name
  const [name, setName] = useState(conn.display_name)
  const [savedAlias, setSavedAlias] = useState<string | null>(conn.local_alias ?? null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [aliasBusy, setAliasBusy] = useState(false)
  const [visUsers, setVisUsers] = useState<VisibleUser[] | null>(null)
  const [visBusy, setVisBusy] = useState<Set<string>>(new Set())
  const [relay, setRelay] = useState<RelayDetail | null>(null)
  /** Peer inbox address — only returned (admin-only) for a confirmed,
   *  directly paired household that isn't relay-only. */
  const [inboxUrl, setInboxUrl] = useState<string | null>(null)
  const [recheckBusy, setRecheckBusy] = useState(false)
  const [confirmUnpair, setConfirmUnpair] = useState(false)
  const [techOpen, setTechOpen] = useState(false)
  const pencil = useRef<HTMLButtonElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const refocusPencil = useRef(false)

  useEffect(() => {
    if (editing) {
      input.current?.focus()
      input.current?.select()
    } else if (refocusPencil.current) {
      refocusPencil.current = false
      pencil.current?.focus()
    }
  }, [editing])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const body = await api.get(`/api/pairing/connections/${id}/visible-users`) as { users: VisibleUser[] }
        if (!cancelled) setVisUsers(body.users)
      } catch {
        // 403 / 404 — hide the section rather than error the whole sheet.
        if (!cancelled) setVisUsers([])
      }
    })()
    return () => { cancelled = true }
  }, [id])

  useEffect(() => {
    let cancelled = false
    api.get(`/api/pairing/connections/${id}/transport-detail`)
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
  }, [id])

  const startRename = () => {
    setDraft(name)
    setEditing(true)
  }

  const stopRename = () => {
    refocusPencil.current = true
    setEditing(false)
  }

  const saveAlias = async () => {
    if (aliasBusy) return
    const trimmed = draft.trim()
    // Empty, or the household's own name → no alias.
    const next: string | null = trimmed && trimmed !== peerName ? trimmed : null
    if ((next ?? '') === (savedAlias ?? '')) {
      stopRename()
      return
    }
    setAliasBusy(true)
    try {
      await api.patch(`/api/pairing/connections/${id}/alias`, { alias: next })
      showToast(
        next
          ? t('connections.detail.alias_saved', { name: next })
          : t('connections.detail.alias_cleared'),
        'success',
      )
      setSavedAlias(next)
      setName(next ?? peerName)
      stopRename()
      onAliasSaved?.()
    } catch (e: unknown) {
      showToast((e as Error).message || t('connections.detail.save_failed'), 'error')
    } finally {
      setAliasBusy(false)
    }
  }

  const toggleVisibility = async (u: VisibleUser) => {
    const next = !u.visible
    setVisBusy(b => new Set(b).add(u.user_id))
    try {
      const body = await api.patch(
        `/api/pairing/connections/${id}/visible-users`,
        { updates: [{ user_id: u.user_id, visible: next }] },
      ) as { users: VisibleUser[] }
      setVisUsers(body.users)
      showToast(
        t(next ? 'connections.detail.toast_visible' : 'connections.detail.toast_hidden', {
          user: u.display_name, name,
        }),
        'success',
      )
    } catch (e: unknown) {
      showToast((e as Error).message || t('connections.detail.visibility_failed'), 'error')
    } finally {
      setVisBusy(b => {
        const n = new Set(b)
        n.delete(u.user_id)
        return n
      })
    }
  }

  /** Ask the peer to re-advertise its protocol version; the fresh value
   *  arrives asynchronously, so the compat store is refreshed a beat later. */
  const recheck = async () => {
    setRecheckBusy(true)
    try {
      await resyncPeerCapabilities(conn.instance_id)
      showToast(t('connections.detail.recheck_sent', { name }), 'info')
      setTimeout(() => { void loadFederationCompat() }, 2500)
    } catch (e: unknown) {
      showToast((e as Error).message || t('connections.detail.recheck_failed'), 'error')
    } finally {
      setRecheckBusy(false)
    }
  }

  const unpair = async () => {
    try {
      await api.delete(`/api/pairing/connections/${id}`)
      showToast(t('connections.detail.removed'), 'info')
      setConfirmUnpair(false)
      onRevoke()
    } catch (e: unknown) { showToast((e as Error).message || t('common.error'), 'error') }
  }

  // Esc / overlay while renaming discards only the edit.
  const handleClose = () => {
    if (editing) stopRename()
    else onClose()
  }

  const behind = versionsBehind(compat, ours)
  // Same rule as the list and the map: the live ``reachable`` flag wins.
  const avatarStatus: AvatarStatus = householdStatus(conn, behind)
  const unreachable = avatarStatus === 'down'
  const tx = conn.transport ? TRANSPORT[conn.transport] : null
  const since = formatSince(conn.unreachable_since, 'long', formatLocale())
  const statusLine = unreachable
    ? (since
      ? t('connections.manage.status_unreachable_since', { when: since })
      : t('connections.manage.status_unreachable'))
    : [t('connections.manage.status_connected'), tx ? t(tx.key) : null].filter(Boolean).join(' · ')

  const header = (titleId: string) => (
    <>
      <ConnAvatar letter={initialOf(name)} tint={tintFor(conn.instance_id)} status={avatarStatus} />
      {editing ? (
        <div class="sh-conn-manage__title">
          <h2 id={titleId} class="sh-conn-sr">{name}</h2>
          <label for="sh-conn-rename-input" class="sh-conn-sr">{t('connections.manage.name_label')}</label>
          <div class="sh-conn-rename">
            <input
              ref={input}
              id="sh-conn-rename-input"
              class="sh-input"
              type="text"
              maxLength={80}
              placeholder={peerName}
              value={draft}
              disabled={aliasBusy}
              aria-describedby="sh-conn-rename-hint"
              onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void saveAlias()
                }
              }}
            />
            <button
              type="button"
              class="sh-conn-iconbtn sh-conn-iconbtn--ok"
              aria-label={t('connections.manage.save_name')}
              disabled={aliasBusy}
              onClick={() => void saveAlias()}
            >
              <ConnIcon name="check" />
            </button>
            <button type="button" class="sh-btn sh-conn-btn-text sh-conn-cancel" onClick={stopRename}>
              {t('common.cancel')}
            </button>
          </div>
          <span id="sh-conn-rename-hint">
            {t('connections.detail.alias_hint')}
            {` ${t('connections.detail.alias_own_name', { name: peerName })}`}
          </span>
        </div>
      ) : (
        <>
          <div class="sh-conn-manage__title">
            <div class="sh-conn-manage__name">
              <h2 id={titleId} title={name}>{name}</h2>
              <button
                ref={pencil}
                type="button"
                class="sh-conn-iconbtn"
                aria-label={t('connections.manage.rename')}
                onClick={startRename}
              >
                <ConnIcon name="pencil" />
              </button>
            </div>
            <span class={unreachable ? 'sh-conn-manage__down' : undefined}>{statusLine}</span>
          </div>
          <button type="button" class="sh-modal-close" onClick={onClose} aria-label={t('modal.close')}>
            &times;
          </button>
        </>
      )}
    </>
  )

  const facts: { label: string; value: preact.ComponentChildren; title?: string }[] = [
    {
      label: t('connections.detail.last_connected'),
      value: conn.last_reachable_at
        ? capitalise(relativeDocsTime(conn.last_reachable_at))
        : capitalise(t('connections.detail.never')),
      title: conn.last_reachable_at ? absolute(conn.last_reachable_at) : undefined,
    },
  ]
  if (tx) {
    facts.push({
      label: t('connections.detail.connection'),
      value: <><ConnIcon name={tx.icon} />{t(tx.key)}</>,
    })
  }
  if (conn.proto_version != null) {
    facts.push({ label: t('connections.detail.app_version'), value: versionText(conn.proto_version, ours, compat) })
  }

  const notes: { tone: 'down' | 'warn' | 'info'; icon: ConnIconName; label: string; text: string; hint?: string }[] = []
  if (conn.unreachable_since) {
    notes.push({
      tone: 'down', icon: 'off', label: t('connections.detail.unreachable_since'),
      text: `${absolute(conn.unreachable_since)} (${relativeDocsTime(conn.unreachable_since)})`,
    })
  }
  if ((conn.queued_envelopes ?? 0) > 0) {
    notes.push({
      tone: 'info', icon: 'clock', label: t('connections.detail.waiting'),
      text: t(isOne(conn.queued_envelopes ?? 0) ? 'connections.detail.queued_one' : 'connections.detail.queued',
        { n: String(conn.queued_envelopes) }),
      hint: t('connections.detail.queued_hint'),
    })
  }
  if ((conn.dropped_envelopes ?? 0) > 0) {
    notes.push({
      tone: 'down', icon: 'alert', label: t('connections.detail.undelivered'),
      text: t(isOne(conn.dropped_envelopes ?? 0) ? 'connections.detail.dropped_one' : 'connections.detail.dropped',
        { n: String(conn.dropped_envelopes) }),
      hint: t('connections.detail.dropped_hint'),
    })
  }
  // Relay acceptance is shown apart from "Last connected": the GFS answers
  // the same 202 whether or not the household is online.
  if (conn.last_relay_accepted_at) {
    notes.push({
      tone: conn.relay_only ? 'warn' : 'info', icon: 'cloud',
      label: conn.relay_only ? t('connections.detail.gfs_only') : 'GFS',
      text: `${t('connections.detail.gfs_last_handed', { time: absolute(conn.last_relay_accepted_at) })} (${relativeDocsTime(conn.last_relay_accepted_at)})`,
      hint: conn.relay_only ? t('connections.detail.gfs_only_hint') : undefined,
    })
  }
  if (conn.transport === 'https' || conn.transport === 'gfs_relay') {
    notes.push({ tone: 'info', icon: TRANSPORT[conn.transport].icon, label: t('connections.detail.connection'), text: t(TRANSPORT[conn.transport].hint) })
  }

  const showGfsFallback = conn.status === 'confirmed' && (conn.source ?? 'manual') === 'manual'
    && (relayGfs || conn.gfs_relay)

  return (
    <Modal open={true} onClose={handleClose} title={name} class="sh-conn-manage" header={header}>
      <dl class={`sh-conn-facts sh-conn-facts--n${facts.length}`}>
        {facts.map((f) => (
          <div class="sh-conn-fact" key={f.label}>
            <dt>{f.label}</dt>
            <dd title={f.title}>{f.value}</dd>
          </div>
        ))}
      </dl>

      {notes.length > 0 && (
        <ul class="sh-conn-notes">
          {notes.map((n) => (
            <li key={n.label} class={`sh-conn-note sh-conn-note--${n.tone}`}>
              <ConnIcon name={n.icon} />
              <span>
                <b>{n.label}</b>
                <span>{n.text}</span>
                {n.hint && <span class="sh-conn-note__hint">{n.hint}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}

      <div class="sh-conn-group">
        <h3>{t('connections.manage.sharing')}</h3>
        <div class="sh-conn-stack">
          <ShareHomeToggle
            variant="card"
            instanceId={conn.instance_id}
            peerName={name}
            initialValue={conn.share_home ?? true}
          />
          {showGfsFallback && (
            <GfsFallbackToggle
              variant="card"
              instanceId={conn.instance_id}
              peerName={name}
              initial={{
                gfs_relay: conn.gfs_relay ?? false,
                gfs_routes: conn.gfs_routes ?? 0,
                peer_keywrap_known: conn.peer_keywrap_known ?? false,
                gfs_relay_available: conn.gfs_relay_available ?? false,
              }}
            />
          )}
        </div>
      </div>

      {visUsers === null && (
        <div class="sh-conn-group" aria-busy="true"><Spinner /></div>
      )}
      {visUsers !== null && visUsers.length > 0 && (
        <div class="sh-conn-group">
          <h3>{t('connections.detail.visible_title', { name })}</h3>
          <p class="sh-conn-hint">{t('connections.detail.visible_hint')}</p>
          <ul class="sh-conn-people">
            {visUsers.map(u => {
              const who = u.display_name || u.username
              return (
                <li key={u.user_id}>
                  <label class="sh-conn-person">
                    <Avatar name={who} size={32} />
                    <span class="grow">
                      {who}
                      {u.is_admin && <span class="sh-conn-person__role"> · {t('connections.detail.admin')}</span>}
                    </span>
                    <input
                      type="checkbox"
                      checked={u.visible}
                      disabled={visBusy.has(u.user_id)}
                      aria-label={t('connections.manage.visible_label', { user: who, name })}
                      onChange={() => void toggleVisibility(u)}
                    />
                  </label>
                </li>
              )
            })}
          </ul>
        </div>
      )}

      <details
        class="sh-conn-tech"
        open={techOpen}
        onToggle={(e) => setTechOpen((e.currentTarget as HTMLDetailsElement).open)}
      >
        <summary aria-expanded={techOpen}>
          {t('connections.manage.technical')}
          <span class="sh-conn-caret" aria-hidden="true"><ConnIcon name="chev" /></span>
        </summary>
        <dl>
          <div>
            <dt>{t('connections.detail.household_id')}</dt>
            <dd>
              <code>{conn.instance_id}</code>
              <button type="button" class="sh-conn-copy" aria-label={t('connections.manage.copy_id')}
                onClick={() => void copyText(conn.instance_id)}>
                <ConnIcon name="copy" />
              </button>
            </dd>
          </div>
          {inboxUrl && (
            <div>
              <dt>{t('connections.detail.address')}</dt>
              <dd>
                <code>{inboxUrl}</code>
                <button type="button" class="sh-conn-copy" aria-label={t('connections.manage.copy_address')}
                  onClick={() => void copyText(inboxUrl)}>
                  <ConnIcon name="copy" />
                </button>
              </dd>
            </div>
          )}
          {conn.paired_at && (
            <div>
              <dt>{t('connections.detail.paired')}</dt>
              <dd>{absolute(conn.paired_at)}</dd>
            </div>
          )}
          {compat && compat.capabilities_known && compat.lacking_features.length > 0 && (
            <div>
              <dt>{t('connections.detail.missing_features')}</dt>
              <dd>{featureLabels(compat.lacking_features, compat.lacking_feature_keys).join(', ')}</dd>
            </div>
          )}
          {relay !== null && (
            <div>
              <dt>{t('connections.detail.chat_route')}</dt>
              <dd class="sh-conn-tech__stack">
                <span>{t('connections.detail.you')} → {relay.via} → {name}</span>
                <span class="sh-conn-hint">
                  {t('connections.detail.chat_route_hint', { via: relay.via, time: relativeDocsTime(relay.ts) })}
                </span>
              </dd>
            </div>
          )}
        </dl>
        {compat && peerSupportsResync(compat) && (
          <div class="sh-conn-tech__actions">
            <button type="button" class="sh-btn sh-btn--secondary" disabled={recheckBusy} onClick={() => void recheck()}>
              {t('connections.detail.recheck')}
            </button>
          </div>
        )}
      </details>

      <div class="sh-conn-danger">
        <h3>{t('connections.manage.danger')}</h3>
        <p>{t('connections.manage.danger_body', { name })}</p>
        <button type="button" class="sh-btn sh-conn-btn-danger-outline" onClick={() => setConfirmUnpair(true)}>
          {t('connections.manage.unpair', { name })}
        </button>
      </div>

      <ConfirmDialog
        open={confirmUnpair}
        title={t('connections.detail.remove_title', { name })}
        message={t('connections.detail.remove_message', { name })}
        confirmLabel={t('connections.detail.remove_ok')}
        destructive
        onConfirm={() => void unpair()}
        onCancel={() => setConfirmUnpair(false)}
      />
    </Modal>
  )
}
