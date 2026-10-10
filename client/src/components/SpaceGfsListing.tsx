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
 */
import { useEffect, useState } from 'preact/hooks'
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
  GfsSelectionOutcome, Space, SpaceGfsServer, SpaceGfsServersView,
} from '@/types'

type Available = SpaceGfsServersView['available'][number]

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
      ⚠️ {owner
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

function statusOf(row: Available): { label: string; cls: string } {
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
  const [confirmLast, setConfirmLast] = useState<Available | null>(null)

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

  const listed = view.available.filter(a => a.listed)
  const others = view.available.filter(a => !a.listed)
  const atMax = listed.length >= view.max
  // The picker's value: the chosen one while it's still on offer, else
  // the first offered GFS (so "Add" always means something visible).
  const choice = others.some(o => o.gfs_id === addChoice)
    ? addChoice
    : (others[0]?.gfs_id ?? '')

  const remove = async (row: Available) => {
    if (busy) return
    setBusy(row.gfs_id)
    try {
      await api.delete(`/api/spaces/${spaceId}/publish/${row.gfs_id}`)
      showToast(t('space.gfs_listing.removed', { name: row.display_name }), 'success')
    } catch (e: any) {
      showToast(e.message || t('common.error'), 'error')
    } finally {
      setBusy(null)
      await load()
    }
  }

  const add = async () => {
    const row = others.find(o => o.gfs_id === choice)
    if (!row || busy) return
    setBusy('add')
    try {
      const res = await api.put<GfsSelectionOutcome>(
        `/api/spaces/${spaceId}/gfs-servers`,
        { gfs_ids: [...listed.map(l => l.gfs_id), row.gfs_id] },
      )
      // One GFS that can't be reached never aborts the rest: name it.
      const failed = res?.failed ?? []
      for (const f of failed) {
        const name = view.available.find(a => a.gfs_id === f.gfs_id)?.display_name
          ?? f.gfs_id
        showToast(
          f.reason === 'unpublish_failed'
            ? t('space.gfs_listing.remove_failed', { name })
            : t('space.gfs_listing.add_failed', { name }),
          'error',
        )
      }
      if (!failed.some(f => f.gfs_id === row.gfs_id)) {
        showToast(t('space.gfs_listing.added', { name: row.display_name }), 'success')
      }
    } catch (e: any) {
      showToast(e.message || t('common.error'), 'error')
    } finally {
      setBusy(null)
      await load()
    }
  }

  return (
    <div class="sh-gfs-listing" data-testid="gfs-listing-owner">
      <p class="sh-muted" style={{ marginTop: 0 }}>{t('space.gfs_listing.intro_owner')}</p>
      {view.unlisted && <UnlistedBanner owner />}
      {listed.length > 0 && (
        <ul class="sh-federation-list sh-gfs-listing__list">
          {listed.map(row => {
            const { label, cls } = statusOf(row)
            const isLive = row.listing_status === 'active'
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
                {row.listing_status === 'pending' && (
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
      <h3 id={`gfs-listing-${space.id}`}>{t('space.federation')}</h3>
      {body}
    </section>
  )
}
