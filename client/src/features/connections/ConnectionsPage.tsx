/**
 * ConnectionsPage — federation connections (§23.86, §11).
 *
 * Top to bottom (HANDOFF §1):
 *   A  RequestList      pending pair requests (admin) — always above the hero
 *   B  ConnHero         network summary (hidden when the list failed to load)
 *   C  HouseholdsSection list / map / skeleton / LoadError / PairingSteps
 *   D  GfsSection
 *   E  AdvancedSection  admin only
 *
 * This file only orchestrates: loading, WS wiring and the actions that
 * talk to the server. Each region renders in its own component.
 *
 * Members see a read-only page: no requests, no CTAs, no version data, no
 * Advanced, static rows, and no ``/api/admin/*`` call is ever made for them.
 */
import { useEffect, useState } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { AutoPairDialog, openAutoPair } from '@/components/AutoPairDialog'
import { ConnectionDetail } from '@/components/ConnectionDetail'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import {
  connections,
  connectionsLoadError,
  connectionsLoadedOnce,
  loadConnections,
  type Connection,
  type TransportState,
} from '@/store/connections'
import { compatOurs, compatPeers, loadFederationCompat } from '@/store/federationCompat'
import { useTitle } from '@/store/pageTitle'
import type { GfsConnection } from '@/types'
import { t } from '@/i18n/i18n'
import { isSupervisorAddon } from '@/platform'
import { AdminContactNote } from './AdminContactNote'
import { AdvancedSection } from './AdvancedSection'
import { revealAdvancedRow } from './AdvancedRow'
import { CancelPairingDialog } from './CancelPairingDialog'
import { cardDomId } from './ConnCard'
import { ConnHero } from './ConnHero'
import { HeroSkeleton } from './ConnSkeleton'
import { HouseholdsSection } from './HouseholdsSection'
import { GfsSection } from './GfsSection'
import { RequestList } from './RequestList'
import { SetupWarning } from './SetupWarning'
import type { ConnView } from './ViewToggle'
import {
  autoPairRequests,
  dropAutoPairRequest,
  externalUrl,
  gfsConnections,
  gfsError,
  gfsLoading,
  householdAdmins,
  loadAutoPairRequests,
  loadExternalUrl,
  loadGfsConnections,
  loadHouseholdAdmins,
  loadSelfHome,
  type AutoPairRequest,
} from './connectionsData'
import {
  buildHouseholds, heroVariant, isHousehold, originOf, pendingKind, pendingRows, spaceOnlyRows,
} from './model'

function errorText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

async function approveAutoPair(r: AutoPairRequest) {
  try {
    await api.post(`/api/pairing/auto-pair-requests/${encodeURIComponent(r.request_id)}/approve`, {})
    // Not paired yet: the requester confirms on our ack, a moment later.
    showToast(t('connections.requests.approved', { name: r.from_a_display }), 'success')
    dropAutoPairRequest(r.request_id)
    // The approval is now a pending row until their household confirms.
    void reloadConnections()
  } catch (err: unknown) {
    if ((err as { status?: number })?.status === 410) {
      // Expired trust-relay request: the mapped message says what to do,
      // and the card goes — it can never be approved.
      showToast(errorText(err), 'error')
      dropAutoPairRequest(r.request_id)
      void loadAutoPairRequests()
      return
    }
    showToast(t('connections.requests.approve_failed', { error: errorText(err) }), 'error')
  }
}

async function declineAutoPair(r: AutoPairRequest) {
  if (!await confirmDialog(
    t('connections.requests.decline_confirm', { name: r.from_a_display }), { destructive: true },
  )) return
  try {
    await api.post(`/api/pairing/auto-pair-requests/${encodeURIComponent(r.request_id)}/decline`, {})
    showToast(t('connections.requests.declined'), 'info')
    dropAutoPairRequest(r.request_id)
  } catch (err: unknown) {
    showToast(t('connections.requests.decline_failed', { error: errorText(err) }), 'error')
  }
}

async function copyGfsAddress(g: GfsConnection) {
  try {
    await navigator.clipboard.writeText(originOf(g.inbox_url))
    showToast(t('connections.gfs.copied'), 'success')
  } catch {
    showToast(t('connections.gfs.copy_failed'), 'error')
  }
}

/** (Re)load the list. A failed REFETCH keeps the known list on screen and
 *  says so in a toast — only a failure with nothing to show becomes the
 *  LoadError state. */
async function reloadConnections() {
  await loadConnections()
  if (connectionsLoadError.value && connections.value.length > 0) {
    showToast(t('connections.error.refresh'), 'error')
  }
}

/** After a row disappeared (cancelled, unpaired) focus would fall to
 *  <body>; put it on the Households heading instead. */
function focusHouseholdsHeading() {
  requestAnimationFrame(() => {
    // No households yet → the section shows "How pairing works" instead.
    const heading = document.getElementById('sh-conn-hh-title') ?? document.getElementById('sh-conn-steps')
    heading?.focus()
  })
}

/** Move focus to a household card (the hero's problem chips). */
function focusCard(instanceId: string) {
  const el = document.getElementById(cardDomId(instanceId))
  el?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
  el?.focus({ preventScroll: true })
}

export default function ConnectionsPage() {
  useTitle(t('connections.title'))
  const isAdmin = !!currentUser.value?.is_admin
  const [detail, setDetail] = useState<Connection | null>(null)
  const [disconnectTarget, setDisconnectTarget] = useState<GfsConnection | null>(null)
  const [cancelTarget, setCancelTarget] = useState<Connection | null>(null)
  const [cancelBusy, setCancelBusy] = useState(false)
  /** List / Map — resets to List on every visit. */
  const view = useSignal<ConnView>('list')

  useEffect(() => {
    void reloadConnections()
    void loadSelfHome()
    void loadGfsConnections()
    if (currentUser.value?.is_admin) {
      void loadFederationCompat()
      void loadAutoPairRequests()
      void loadExternalUrl()
    } else {
      void loadHouseholdAdmins()
    }

    // ``pairing.confirmed`` / ``connection.removed`` are handled by the
    // connections store (``wireConnectionsWs``) so every view of the
    // list — this page and the dashboard map — updates live.
    const offs = [
      ws.on('pairing.aborted', () => { void reloadConnections() }),
      ws.on('pairing.auto_pair_requested', () => {
        if (currentUser.value?.is_admin) void loadAutoPairRequests()
      }),
      ws.on('peer.transport_changed', (msg) => {
        const { instance_id, transport } = msg.data as { instance_id: string; transport: TransportState }
        connections.value = connections.value.map((c) =>
          c.instance_id === instance_id ? { ...c, transport } : c,
        )
      }),
    ]
    return () => { offs.forEach((off) => off()) }
  }, [])

  const compatById = new Map(compatPeers.value.map((p) => [p.instance_id, p]))
  const ours = isAdmin ? compatOurs.value : 0
  const households = buildHouseholds(connections.value, compatById, ours, isAdmin)
  const pending = isAdmin ? pendingRows(connections.value) : []
  const spaceOnly = spaceOnlyRows(connections.value)
  // Skeleton only before the first load ever finished; a refetch keeps
  // whatever is on screen (an empty list included).
  const firstLoad = !connectionsLoadedOnce.value && connections.value.length === 0
  // LoadError only when there is nothing to show; a failed refetch of a
  // known list keeps the list (reloadConnections toasts instead).
  const loadFailed = connectionsLoadError.value && connections.value.length === 0
  const variant = heroVariant({
    loading: firstLoad,
    error: loadFailed,
    households,
  })
  const activeGfs = gfsConnections.value.some((g) => g.status === 'active')
  const ext = externalUrl.value
  const extRowShown = isAdmin && !isSupervisorAddon() && ext.loaded && ext.manualSupported

  const jumpTo = (instanceId: string) => {
    if (view.value !== 'list') {
      view.value = 'list'
      requestAnimationFrame(() => focusCard(instanceId))
    } else {
      focusCard(instanceId)
    }
  }

  const cancelPending = async () => {
    const target = cancelTarget
    if (!target) return
    setCancelBusy(true)
    const drop = () => {
      connections.value = connections.value.filter((c) => c.instance_id !== target.instance_id)
    }
    try {
      const r = await api.delete(`/api/pairing/pending/${encodeURIComponent(target.instance_id)}`) as
        { ok?: boolean; peer_notified?: boolean } | null
      drop()
      focusHouseholdsHeading()
      // The other household couldn't be told (offline): say so — its side
      // keeps waiting until it times out on its own. A request we sent
      // through a trust relay has no one to tell — only a household that
      // holds a half of the pairing can miss the news.
      const unnotified = r?.peer_notified === false && pendingKind(target) !== 'request'
      showToast(
        unnotified
          ? t('connections.pending.cancelled_unnotified', { name: target.display_name })
          : t('connections.pending.cancelled', { name: target.display_name }),
        'info',
      )
    } catch (err: unknown) {
      const e = err as { status?: number; code?: string | null }
      if (e?.status === 404) {
        // Already gone (cancelled elsewhere, or expired) — the outcome the
        // admin asked for, so no toast.
        drop()
        focusHouseholdsHeading()
        void reloadConnections()
      } else if (e?.code === 'PAIRING_CONFIRMED') {
        // The other household finished pairing meanwhile: the cancel did
        // NOT happen, so this is a failure to report, not news.
        showToast(t('error.pairing_confirmed'), 'error')
        void reloadConnections()
      } else {
        showToast(t('connections.pending.cancel_failed'), 'error')
      }
    } finally {
      setCancelBusy(false)
      setCancelTarget(null)
    }
  }

  const disconnectGfs = async (g: GfsConnection) => {
    try {
      await api.delete(`/api/gfs/connections/${encodeURIComponent(g.id)}`)
      showToast(t('gfs.disconnect'), 'success')
      gfsConnections.value = gfsConnections.value.filter((c) => c.id !== g.id)
    } catch (e: unknown) {
      showToast((e as Error).message || t('gfs.pairing_failed'), 'error')
    }
    setDisconnectTarget(null)
  }

  const confirmedHouseholds = households.map((h) => ({
    instance_id: h.conn.instance_id,
    display_name: h.conn.display_name,
  }))

  return (
    <div class="sh-conn">
      {isAdmin && (
        <RequestList
          requests={autoPairRequests.value}
          onApprove={(r) => void approveAutoPair(r)}
          onDecline={(r) => void declineAutoPair(r)}
        />
      )}

      {variant === 'loading' && <HeroSkeleton />}
      {(variant === 'problem' || variant === 'healthy' || variant === 'empty') && (
        <ConnHero
          variant={variant}
          households={households}
          isAdmin={isAdmin}
          gfs={activeGfs}
          ours={ours}
          onPair={() => openPairing('household')}
          onPairVia={households.length > 0 ? () => openAutoPair(confirmedHouseholds) : undefined}
          onJump={jumpTo}
          memberNote={<AdminContactNote admins={householdAdmins.value} />}
          setupWarning={isAdmin && variant === 'empty' && ext.loaded && !ext.effective
            && !gfsLoading.value && !activeGfs && (
            <SetupWarning
              onExternalAddress={extRowShown ? () => revealAdvancedRow('ext') : undefined}
              onAddGfs={() => openPairing('gfs')}
            />
          )}
        />
      )}

      <HouseholdsSection
        view={view.value}
        onView={(v) => { view.value = v }}
        loading={firstLoad}
        error={loadFailed}
        isAdmin={isAdmin}
        households={households}
        pending={pending}
        spaceOnly={spaceOnly}
        compat={compatById}
        onManage={isAdmin ? setDetail : undefined}
        onCancelPending={setCancelTarget}
        onRestartPending={() => openPairing('household')}
        onRetry={() => void reloadConnections()}
      />

      <GfsSection
        gfs={gfsConnections.value}
        loading={gfsLoading.value}
        error={gfsError.value}
        isAdmin={isAdmin}
        onAdd={() => openPairing('gfs')}
        onRetry={() => void loadGfsConnections()}
        onCopy={(g) => void copyGfsAddress(g)}
        onDisconnect={setDisconnectTarget}
      />

      {isAdmin && <AdvancedSection showExternalAddress={extRowShown} />}

      {isAdmin && (
        <>
          <PairingFlow onGfsConnected={loadGfsConnections} />
          <AutoPairDialog onPaired={() => void reloadConnections()} />
          <CancelPairingDialog
            target={cancelTarget ? { name: cancelTarget.display_name, kind: pendingKind(cancelTarget) } : null}
            busy={cancelBusy}
            onConfirm={() => void cancelPending()}
            onClose={() => setCancelTarget(null)}
          />
        </>
      )}
      {detail && isHousehold(detail) && (
        <ConnectionDetail
          conn={{
            instance_id: detail.instance_id,
            display_name: detail.display_name,
            federated_display_name: detail.federated_display_name,
            local_alias: detail.local_alias ?? null,
            status: detail.status ?? 'confirmed',
            unreachable_since: detail.unreachable_since ?? null,
            // Live flag from the store (WS-patched), not the snapshot taken
            // when the sheet opened.
            reachable: connections.value.find((c) => c.instance_id === detail.instance_id)?.reachable
              ?? detail.reachable,
            last_reachable_at: detail.last_reachable_at ?? null,
            last_relay_accepted_at: detail.last_relay_accepted_at ?? null,
            relay_only: detail.relay_only ?? false,
            queued_envelopes: detail.queued_envelopes ?? 0,
            dropped_envelopes: detail.dropped_envelopes ?? 0,
            paired_at: detail.paired_at ?? null,
            transport: detail.transport ?? null,
            proto_version: detail.proto_version,
            share_home: detail.share_home,
            source: detail.source,
            gfs_relay: detail.gfs_relay,
            gfs_routes: detail.gfs_routes,
            peer_keywrap_known: detail.peer_keywrap_known,
            gfs_relay_available: detail.gfs_relay_available,
          }}
          compat={compatById.get(detail.instance_id)}
          ours={ours}
          relayGfs={gfsConnections.value.some(
            (g) => g.status === 'active' && g.envelope_relay === true,
          )}
          // Re-read on close: the per-pair switches (home sharing, GFS
          // fallback) changed server state the listing still has stale.
          onClose={() => { setDetail(null); void reloadConnections() }}
          onRevoke={() => { setDetail(null); focusHouseholdsHeading(); void reloadConnections() }}
          // The sheet stays open on a rename; the list behind it refreshes.
          onAliasSaved={() => { void reloadConnections() }}
        />
      )}
      <ConfirmDialog
        open={disconnectTarget !== null}
        title={t('gfs.disconnect')}
        message={t('gfs.confirm_disconnect')}
        confirmLabel={t('gfs.disconnect')}
        destructive
        onConfirm={() => { if (disconnectTarget) void disconnectGfs(disconnectTarget) }}
        onCancel={() => setDisconnectTarget(null)}
      />
    </div>
  )
}
