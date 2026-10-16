/**
 * SpaceGfsListing — where a space is listed on GFSs (space settings).
 *
 * Only a GLOBAL space is listed on GFSs, and only on the ones its owner
 * picks. A PUBLIC space is visible to connected households only and never
 * touches a GFS — listing it would make it global, a tier change — so it
 * gets a short note instead of Publish buttons.
 *
 * - The OWNER, on the owner household, edits the list: remove a GFS
 *   (``DELETE /publish/{gfs_id}``) or add another active connection
 *   (``PUT /gfs-servers``). "Global, not listed" gets a banner.
 * - Everyone else sees the owner's signed list read-only
 *   (``space.gfs_servers``), with a hint for each GFS their own household
 *   isn't connected to.
 * - A PRIVATE space whose owner turned the GFS on (``private_gfs``) is
 *   never listed: it shows the GFSs carrying its private channel,
 *   read-only, with no public links (the owner picks them under the GFS
 *   toggle in the settings above).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { JSX } from 'preact'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { currentUser } from '@/store/auth'
import { t } from '@/i18n/i18n'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { gfsHost } from './GfsChecklist'
import { showToast } from './Toast'
import type {
  Space, SpaceGfsServer, SpaceGfsServersView,
} from '@/types'

type Listing = SpaceGfsServersView['listings'][number]

/**
 * The GFS-side public page of a space. ``inbox_url`` is the GFS root, so
 * stripping a trailing slash is enough (``global_server/public.py`` serves
 * ``/spaces/{space_id}``).
 */
export function publicSpaceUrl(inboxUrl: string, spaceId: string): string {
  return `${inboxUrl.replace(/\/+$/, '')}/spaces/${spaceId}`
}

async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    showToast(t('space.gfs.public_link_copied'), 'success')
  } catch {
    // Older browsers / locked-down WebViews: say how to copy by hand.
    showToast(t('space.gfs.public_link_copy_failed'), 'info')
  }
}

/** "Global, not listed" — the space is global but no GFS lists it. */
function UnlistedBanner({ owner }: { owner: boolean }): JSX.Element {
  return (
    <p class="sh-gfs-banner" role="status" data-testid="gfs-unlisted-banner">
      <span aria-hidden="true">⚠️</span> {owner
        ? t('space.gfs_listing.unlisted_owner')
        : t('space.gfs_listing.unlisted_member')}
    </p>
  )
}

/** "Connect a GFS" pointer — a link for a household admin (connecting is
 *  admin-only), a plain sentence for everyone else. */
function NoConnections(): JSX.Element {
  const isAdmin = currentUser.value?.is_admin === true
  return (
    <p class="sh-muted" data-testid="gfs-no-connections">
      {isAdmin ? (
        <>
          {t('space.gfs_listing.none_admin_before')}
          <a href={addBase('/connections')}>{t('space.gfs_listing.none_admin_link')}</a>
          {t('space.gfs_listing.none_admin_after')}
        </>
      ) : t('space.gfs_listing.none_member')}
    </p>
  )
}

function statusOf(row: Listing): { label: string; cls: string } {
  // A listing held over a connection that isn't active can't be refreshed
  // or used — say that before what the GFS last said about it.
  if (row.connection_status !== 'active') {
    return { label: t('space.gfs_listing.connection_inactive'), cls: 'sh-text-warning' }
  }
  switch (row.listing_status) {
    case 'pending': return { label: t('space.publish_pending'), cls: 'sh-text-warning' }
    case 'banned': return { label: t('space.publish_rejected'), cls: 'sh-muted' }
    default: return { label: t('space.gfs_listing.listed'), cls: 'sh-text-success' }
  }
}

/** The owner's editor: the chosen GFSs with Remove, and an Add picker. */
function OwnerGfsListing({ spaceId }: { spaceId: string }): JSX.Element {
  const [view, setView] = useState<SpaceGfsServersView | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [addChoice, setAddChoice] = useState('')
  // The last listed GFS awaiting a confirm (removing it leaves the space
  // "global, not listed").
  const [confirmLast, setConfirmLast] = useState<Listing | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)

  const load = async () => {
    try {
      const v = await api.get<SpaceGfsServersView>(`/api/spaces/${spaceId}/gfs-servers`)
      setView(v)
      setLoadFailed(false)
    } catch {
      setLoadFailed(true)
    }
  }
  useEffect(() => { void load() }, [spaceId])

  if (loadFailed) {
    return (
      <p class="sh-muted" data-testid="gfs-listing-load-failed">
        {t('space.gfs_listing.load_failed')}{' '}
        <Button variant="secondary" onClick={() => void load()}>
          {t('common.retry')}
        </Button>
      </p>
    )
  }
  if (view === null) return <p class="sh-muted">{t('common.loading')}</p>

  // Every listing, whatever its connection's state (so a listing on a
  // suspended / pending connection can still be withdrawn); the Add
  // picker offers the active connections that don't list it yet.
  const listed = view.listings
  const listedIds = new Set(listed.map(l => l.gfs_id))
  const others = view.available.filter(a => !a.listed && !listedIds.has(a.gfs_id))
  const atMax = listed.length >= view.max
  // The picker's value: the chosen one while it's still on offer, else
  // the first offered GFS (so "Add" always means something visible).
  const choice = others.some(o => o.gfs_id === addChoice)
    ? addChoice
    : (others[0]?.gfs_id ?? '')

  /** The removed row's button is gone: put focus somewhere that still
   *  exists — the Add picker, else the section heading. */
  const refocusAfterRemove = () => {
    setTimeout(() => {
      const root = rootRef.current
      const target = root?.querySelector<HTMLElement>('[data-testid="gfs-add-select"]')
        ?? root?.closest('section')?.querySelector<HTMLElement>('h3')
      target?.focus()
    }, 0)
  }

  const remove = async (row: Listing) => {
    if (busy) return
    setBusy(row.gfs_id)
    let removed = false
    try {
      await api.delete(`/api/spaces/${spaceId}/publish/${row.gfs_id}`)
      removed = true
    } catch (e: any) {
      // 404: this household holds no such listing any more (another tab
      // or a reconnect already withdrew it) — the outcome the owner asked
      // for, so not an error.
      if ((e as { status?: number } | null)?.status === 404) removed = true
      else showToast(e.message || t('common.error'), 'error')
    } finally {
      // Stay busy until the fresh list is in, so a second click can't
      // fire another DELETE for a row that's already gone.
      await load()
      setBusy(null)
    }
    if (removed) {
      showToast(t('space.gfs_listing.removed', { name: row.display_name }), 'success')
      refocusAfterRemove()
    }
  }

  // One GFS at a time (POST /publish/{gfs_id}): an atomic add on the
  // server, so it never drops a listing this view doesn't show (a held
  // connection) or undo a change made in another tab.
  const add = async () => {
    const row = others.find(o => o.gfs_id === choice)
    if (!row || busy) return
    setBusy('add')
    try {
      await api.post(`/api/spaces/${spaceId}/publish/${row.gfs_id}`)
      showToast(t('space.gfs_listing.added', { name: row.display_name }), 'success')
    } catch (e: any) {
      showToast(
        e.message || t('space.gfs_listing.add_failed', { name: row.display_name }),
        'error',
      )
    } finally {
      await load()
      setBusy(null)
    }
  }

  return (
    <div class="sh-gfs-listing" data-testid="gfs-listing-owner" ref={rootRef}>
      <p class="sh-muted" style={{ marginTop: 0 }}>{t('space.gfs_listing.intro_owner')}</p>
      {view.unlisted && <UnlistedBanner owner />}
      {listed.length > 0 && (
        <ul class="sh-federation-list sh-gfs-listing__list">
          {listed.map(row => {
            const { label, cls } = statusOf(row)
            const active = row.connection_status === 'active'
            const isLive = active && row.listing_status === 'active'
            const publicUrl = publicSpaceUrl(row.inbox_url, spaceId)
            return (
              <li key={row.gfs_id} class="sh-federation-row"
                  data-testid={`gfs-row-${row.gfs_id}`}>
                <div class="sh-connection-info sh-gfs-listing__info">
                  <strong>{row.display_name}</strong>
                  <span class="sh-muted">{gfsHost(row.inbox_url)}</span>
                </div>
                <div class="sh-federation-actions">
                  <span class={cls}>{label}</span>
                  <Button
                    variant="secondary"
                    loading={busy === row.gfs_id}
                    disabled={busy !== null && busy !== row.gfs_id}
                    aria-label={t('space.gfs_listing.remove_aria', { name: row.display_name })}
                    onClick={() => {
                      if (listed.length === 1) setConfirmLast(row)
                      else void remove(row)
                    }}
                  >
                    {t('space.gfs_listing.remove')}
                  </Button>
                </div>
                {isLive && (
                  <div class="sh-federation-public-url">
                    <span class="sh-muted">🔗 {t('space.gfs.public_link')}</span>
                    <a
                      href={publicUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      class="sh-federation-public-url__link"
                      title={publicUrl}
                    >
                      {publicUrl}
                    </a>
                    <button
                      type="button"
                      class="sh-federation-public-url__copy"
                      onClick={() => void copyToClipboard(publicUrl)}
                      aria-label={t('space.gfs.copy_public_link')}
                      title={t('space.gfs.copy')}
                    >
                      📋
                    </button>
                  </div>
                )}
                {!active && (
                  <p class="sh-muted sh-federation-pending-hint"
                     data-testid={`gfs-row-inactive-${row.gfs_id}`}>
                    {t('space.gfs_listing.connection_inactive_hint')}
                  </p>
                )}
                {active && row.listing_status === 'pending' && (
                  <p class="sh-muted sh-federation-pending-hint">
                    {t('space.publish_pending_hint')}
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {view.available.length === 0 ? (
        <NoConnections />
      ) : others.length === 0 ? (
        <p class="sh-muted" data-testid="gfs-all-listed">{t('space.gfs_listing.all_listed')}</p>
      ) : atMax ? (
        <p class="sh-muted" data-testid="gfs-at-max">
          {t('error.gfs_selection.too_many', { max: String(view.max) })}
        </p>
      ) : (
        <div class="sh-gfs-listing__add">
          <label>
            {t('space.gfs_listing.add_label')}
            <select
              data-testid="gfs-add-select"
              value={choice}
              onChange={(e) => setAddChoice((e.target as HTMLSelectElement).value)}
            >
              {others.map(o => (
                <option key={o.gfs_id} value={o.gfs_id}>
                  {o.display_name} ({gfsHost(o.inbox_url)})
                </option>
              ))}
            </select>
          </label>
          <Button
            variant="primary"
            loading={busy === 'add'}
            disabled={busy !== null && busy !== 'add'}
            onClick={() => void add()}
          >
            {t('space.gfs_listing.add')}
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={confirmLast !== null}
        title={t('space.gfs_listing.remove_last_title')}
        message={t('space.gfs_listing.remove_last_body')}
        confirmLabel={t('space.gfs_listing.remove')}
        destructive
        onConfirm={() => {
          const row = confirmLast
          setConfirmLast(null)
          if (row) void remove(row)
        }}
        onCancel={() => setConfirmLast(null)}
      />
    </div>
  )
}

/** Whether a member has anything to see: a global space whose list is
 *  known, or that is "global, not listed". */
export function hasGfsServersToShow(space: Space): boolean {
  return space.space_type === 'global'
    && (Boolean(space.gfs_unlisted) || (space.gfs_servers?.length ?? 0) > 0)
}

/** Everyone but the owner: the owner's list, read-only. */
export function SpaceGfsServersReadOnly({ space }: { space: Space }): JSX.Element | null {
  if (!hasGfsServersToShow(space)) return null
  const servers: SpaceGfsServer[] = space.gfs_servers ?? []
  const isAdmin = currentUser.value?.is_admin === true
  return (
    <div class="sh-gfs-listing" data-testid="gfs-listing-readonly">
      <p class="sh-muted" style={{ marginTop: 0 }}>{t('space.gfs_listing.intro_member')}</p>
      {space.gfs_unlisted && <UnlistedBanner owner={false} />}
      {servers.length > 0 && (
        <ul class="sh-federation-list sh-gfs-listing__list">
          {servers.map(s => (
            <li key={s.inbox_url} class="sh-federation-row">
              <div class="sh-connection-info sh-gfs-listing__info">
                <strong>{s.display_name}</strong>
                <span class="sh-muted">{gfsHost(s.inbox_url)}</span>
              </div>
              {s.connected ? (
                <span class="sh-text-success sh-gfs-listing__state">
                  ✓ {t('space.gfs_listing.connected')}
                </span>
              ) : (
                <p class="sh-muted sh-gfs-listing__hint" data-testid="gfs-connect-hint">
                  {isAdmin ? (
                    <>
                      {t('space.gfs_listing.connect_hint', { name: s.display_name })}{' '}
                      <a href={addBase('/connections')}>{t('space.gfs_listing.connect_link')}</a>
                    </>
                  ) : t('space.gfs_listing.connect_hint_member', { name: s.display_name })}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** A private space using the GFS: its channel servers are known. */
export function hasChannelServersToShow(space: Space): boolean {
  return space.space_type === 'private'
    && Boolean(space.features?.private_gfs)
    && (space.gfs_servers?.length ?? 0) > 0
}

/** "Private channel on" — a private space's channel GFSs, read-only. Never
 *  a public link: nothing of a private space is public anywhere. */
export function SpaceChannelServers({
  space,
  isOwner,
}: {
  space: Space
  isOwner: boolean
}): JSX.Element | null {
  if (!hasChannelServersToShow(space)) return null
  const servers: SpaceGfsServer[] = space.gfs_servers ?? []
  const isAdmin = currentUser.value?.is_admin === true
  return (
    <div class="sh-gfs-listing" data-testid="gfs-channel-servers">
      <p class="sh-gfs-listing__label">
        <span aria-hidden="true">🛰️</span> {t('space.gfs_listing.channel_label')}
      </p>
      <p class="sh-muted" style={{ marginTop: 0 }}>{t('space.gfs_listing.channel_intro')}</p>
      <ul class="sh-federation-list sh-gfs-listing__list">
        {servers.map(s => (
          <li key={s.inbox_url} class="sh-federation-row">
            <div class="sh-connection-info sh-gfs-listing__info">
              <strong>{s.display_name}</strong>
              <span class="sh-muted">{gfsHost(s.inbox_url)}</span>
            </div>
            {s.connected ? (
              <span class="sh-text-success sh-gfs-listing__state">
                ✓ {t('space.gfs_listing.connected')}
              </span>
            ) : (
              <p class="sh-muted sh-gfs-listing__hint" data-testid="gfs-connect-hint">
                {isAdmin ? (
                  <>
                    {t('space.gfs_listing.connect_hint', { name: s.display_name })}{' '}
                    <a href={addBase('/connections')}>{t('space.gfs_listing.connect_link')}</a>
                  </>
                ) : t('space.gfs_listing.connect_hint_member', { name: s.display_name })}
              </p>
            )}
          </li>
        ))}
      </ul>
      {isOwner && (
        <p class="sh-muted sh-gfs-listing__hint" data-testid="gfs-channel-owner-hint">
          {t('space.gfs_listing.channel_owner_hint')}
        </p>
      )}
    </div>
  )
}

/**
 * The settings section. ``null`` when there's nothing to say (a private /
 * household space, or a global one with no list known yet for a member).
 */
export function SpaceGfsListing({
  space,
  isOwner,
  isRemoteSpace,
}: {
  space: Space
  isOwner: boolean
  isRemoteSpace: boolean
}): JSX.Element | null {
  let body: JSX.Element | null
  if (space.space_type === 'public') {
    body = (
      <p class="sh-muted" data-testid="gfs-public-note">
        {t('space.gfs_listing.public_note')}
      </p>
    )
  } else if (space.space_type === 'private' && space.features?.private_gfs) {
    body = hasChannelServersToShow(space)
      ? <SpaceChannelServers space={space} isOwner={isOwner && !isRemoteSpace} />
      : null
  } else if (space.space_type !== 'global') {
    body = isRemoteSpace ? null : (
      <p class="sh-muted" data-testid="gfs-publish-tier-note">
        {t('space.gfs_publish_needs_public')}
      </p>
    )
  } else if (isOwner && !isRemoteSpace) {
    body = <OwnerGfsListing spaceId={space.id} />
  } else {
    body = hasGfsServersToShow(space) ? <SpaceGfsServersReadOnly space={space} /> : null
  }
  if (body === null) return null
  return (
    <section class="sh-gfs-listing-section" aria-labelledby={`gfs-listing-${space.id}`}>
      <hr />
      <h3 tabIndex={-1} id={`gfs-listing-${space.id}`}>
        {/* A private space is never "listed": its section is just "GFS". */}
        {space.space_type === 'private' ? t('space.private_gfs.legend') : t('space.federation')}
      </h3>
      {body}
    </section>
  )
}
