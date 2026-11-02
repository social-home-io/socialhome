/**
 * TrashPage — the household gallery's trash (``/gallery/trash``).
 *
 * Deleted photos and videos wait here 30 days, then the purge removes
 * them. Grouped by the day they were deleted ("Deleted yesterday · by Lena
 * · from “Summer 2026”"); every tile says how long it has left ("29
 * days"). Tap tiles to pick them, then Restore (into their album — a
 * deleted album comes back with them) or Delete forever. A household
 * admin can empty the whole trash from ⋯.
 *
 * A household admin sees, per private album they aren't in, one locked
 * tile ("Private album · from Lena · 12 photos") — never a preview, never
 * pickable (``GET /api/gallery/trash/locked``, D7); its photos go with the
 * purge after 30 days, or their owner brings them back. Anyone else gets
 * no locked tiles at all.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { OverflowMenu } from '@/components/OverflowMenu'
import { openDialogCount } from '@/components/Modal'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { personLabel } from '@/utils/names'
import { SelectionBar } from './SelectionBar'
import { bulk, itemsWord } from './galleryManage'
import { backLeadsTo, galleryPath, markBackTo } from './galleryRoutes'

export interface TrashRow {
  id: string
  album_id: string
  uploaded_by: string
  item_type: 'photo' | 'video'
  thumbnail_url: string
  caption?: string | null
  deleted_at: string
  deleted_by: string | null
  purge_after: string
  album_name: string
  album_in_trash: boolean
  /** A private album the reader can't open: one locked tile, no preview
   *  (private albums — owner decision D7). */
  locked?: { owner_user_id: string; count: number } | null
}

/** ``GET /api/gallery/trash/locked`` — a private album a household admin
 *  isn't in, with photos in the trash. */
export interface LockedTrashAlbum {
  album_id: string
  owner_user_id: string | null
  count: number
  deleted_at: string
  purge_after: string
  album_in_trash: boolean
}

/** The trash rows with one locked row per private album, by deletion
 *  time like the rest (newest first). */
export function withLocked(rows: TrashRow[], locked: LockedTrashAlbum[]): TrashRow[] {
  if (!locked.length) return rows
  const tiles: TrashRow[] = locked.map(l => ({
    id: `locked:${l.album_id}`,
    album_id: l.album_id,
    uploaded_by: '',
    item_type: 'photo',
    thumbnail_url: '',
    deleted_at: l.deleted_at,
    deleted_by: null,
    purge_after: l.purge_after,
    album_name: '',
    album_in_trash: l.album_in_trash,
    locked: { owner_user_id: l.owner_user_id ?? '', count: l.count },
  }))
  return [...rows, ...tiles].sort((a, b) => Date.parse(b.deleted_at) - Date.parse(a.deleted_at))
}

const PAGE = 100
const DAY_MS = 86_400_000

/** Whole days left before the purge — "29 days", "1 day", at least 0. */
export function daysLeft(purgeAfter: string, now = Date.now()): number {
  const end = Date.parse(purgeAfter)
  if (Number.isNaN(end)) return 0
  return Math.max(0, Math.ceil((end - now) / DAY_MS))
}

function localDayKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/** "Deleted today" / "Deleted yesterday" / "Deleted 20 Oct". */
export function deletedHeading(iso: string, now = new Date()): string {
  const d = new Date(iso)
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((start(now) - start(d)) / DAY_MS)
  if (diff <= 0) return t('gallery.trash.deleted_today')
  if (diff === 1) return t('gallery.trash.deleted_yesterday')
  const sameYear = d.getFullYear() === now.getFullYear()
  return t('gallery.trash.deleted_on', {
    date: d.toLocaleDateString(formatLocale(), sameYear
      ? { day: 'numeric', month: 'short' }
      : { day: 'numeric', month: 'short', year: 'numeric' }),
  })
}

function nameOf(userId: string | null): string {
  if (!userId) return ''
  if (userId === currentUser.value?.user_id) return t('gallery.trash.by_you')
  const u = householdUsers.value.get(userId)
  return u ? personLabel(u.display_name, u.username) : t('gallery.manage.someone')
}

export default function TrashPage({ fromList = false }: { fromList?: boolean }) {
  useTitle(t('gallery.trash.title'))
  const loc = useLocation()
  // Opened from the albums' ⋯: Back is a step back in history (and stays
  // one after a reload — ``history.state`` remembers).
  useEffect(() => { if (fromList) markBackTo(galleryPath()) }, [fromList])
  const [rows, setRows] = useState<TrashRow[] | null>(null)
  const [more, setMore] = useState(false)
  const [failed, setFailed] = useState(false)
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set())
  const [busy, setBusy] = useState<'restore' | 'purge' | null>(null)
  const rowsRef = useRef<TrashRow[] | null>(null)
  rowsRef.current = rows

  const load = async (quiet = false) => {
    const have = rowsRef.current?.length ?? 0
    const limit = quiet ? Math.min(500, Math.max(PAGE, have)) : PAGE
    try {
      const [own, locked] = await Promise.all([
        api.get<TrashRow[]>(`/api/gallery/trash?limit=${limit}`),
        api.get<LockedTrashAlbum[]>('/api/gallery/trash/locked').catch(() => []),
      ])
      const next = withLocked(own, locked)
      setRows(next)
      setMore(own.length >= limit)
      setFailed(false)
      // A row someone else restored or purged can't stay picked.
      setPicked(prev => new Set([...prev].filter(id => next.some(r => r.id === id))))
    } catch {
      if (!quiet || rowsRef.current === null) setFailed(true)
    }
  }
  const loadMore = async () => {
    const last = rows?.filter(r => !r.locked).at(-1)
    if (!last) return
    try {
      const next = await api.get<TrashRow[]>(`/api/gallery/trash?limit=${PAGE}&before_id=${encodeURIComponent(last.id)}`)
      setRows(prev => [...(prev ?? []), ...next.filter(r => !(prev ?? []).some(p => p.id === r.id))])
      setMore(next.length >= PAGE)
    } catch {
      showToast(t('gallery.refresh_failed'), 'error')
    }
  }

  useEffect(() => {
    void load()
    void loadHouseholdUsers()
    // Other devices restore, delete and purge too — re-read quietly,
    // once for a burst of frames.
    let timer: ReturnType<typeof setTimeout> | null = null
    const soon = (e: { data: { space_id?: string | null } }) => {
      if (e.data?.space_id) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => void load(true), 400)
    }
    const offs = [
      ws.on('gallery.item_deleted', soon),
      ws.on('gallery.item_restored', soon),
      ws.on('gallery.album_deleted', soon),
      ws.on('gallery.album_updated', soon),
    ]
    return () => {
      if (timer) clearTimeout(timer)
      offs.forEach(off => off())
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const back = () => {
    if (backLeadsTo(galleryPath())) history.back()
    else loc.route(galleryPath())
  }

  // Escape lets go of the picked tiles (a dialog on top closes first).
  const anyPicked = picked.size > 0
  useEffect(() => {
    if (!anyPicked) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && openDialogCount.value === 0 && !e.defaultPrevented) setPicked(new Set())
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [anyPicked])

  const toggle = (id: string) => setPicked(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const pickedRows = (rows ?? []).filter(r => picked.has(r.id))

  const restore = async () => {
    if (!pickedRows.length) return
    setBusy('restore')
    try {
      const res = await bulk('restore', pickedRows.map(r => r.id))
      const done = pickedRows.filter(r => res.done.includes(r.id))
      if (done.length) {
        const albums = [...new Set(done.map(r => r.album_id))]
        showToast(
          t('gallery.trash.restored', { what: itemsWord(done) }),
          'success',
          albums.length === 1
            ? { action: { label: t('gallery.trash.open_album'), onClick: () => loc.route(galleryPath(null, albums[0])) } }
            : {},
        )
      }
      if (res.refused.length) showToast(t('gallery.trash.not_yours', { n: String(res.refused.length) }), 'error')
      setPicked(new Set(res.refused))
      await load(true)
    } catch {
      showToast(t('gallery.trash.action_failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  const purge = async () => {
    if (!pickedRows.length) return
    const ok = await confirmDialog(t('gallery.trash.purge_body'), {
      title: t('gallery.trash.purge_title', { what: itemsWord(pickedRows) }),
      confirmLabel: t('gallery.trash.purge_confirm'),
      destructive: true,
    })
    if (!ok) return
    setBusy('purge')
    try {
      const res = await bulk('purge', pickedRows.map(r => r.id))
      if (res.done.length) showToast(t('gallery.trash.purged', { n: String(res.done.length) }), 'success')
      if (res.refused.length) showToast(t('gallery.trash.not_yours', { n: String(res.refused.length) }), 'error')
      setPicked(new Set(res.refused))
      await load(true)
    } catch {
      showToast(t('gallery.trash.action_failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  const emptyAll = async () => {
    const ok = await confirmDialog(t('gallery.trash.empty_body'), {
      title: t('gallery.trash.empty_title'),
      confirmLabel: t('gallery.trash.empty_confirm'),
      destructive: true,
    })
    if (!ok) return
    try {
      const res = await api.delete<{ removed: number }>('/api/gallery/trash')
      showToast(t('gallery.trash.purged', { n: String(res?.removed ?? 0) }), 'success')
      setPicked(new Set())
      await load(true)
    } catch (err) {
      showToast(String((err as Error)?.message ?? t('gallery.trash.action_failed')), 'error')
    }
  }

  const isAdmin = !!currentUser.value?.is_admin
  const groups: { key: string; heading: string; rows: TrashRow[] }[] = []
  for (const r of rows ?? []) {
    const key = localDayKey(r.deleted_at)
    const g = groups[groups.length - 1]
    if (g && g.key === key) g.rows.push(r)
    else groups.push({ key, heading: deletedHeading(r.deleted_at), rows: [r] })
  }

  return (
    <div class={'sh-trash' + (picked.size ? ' sh-trash--selecting' : '')}>
      <header class="sh-page-header sh-trash-header">
        <Button variant="secondary" onClick={back}>← {t('gallery.albums')}</Button>
        <h1>{t('gallery.trash.title')}</h1>
        {isAdmin && (rows?.length ?? 0) > 0 && (
          <OverflowMenu
            label={t('gallery.trash.menu')}
            items={[{ key: 'empty', label: t('gallery.trash.empty_title'), onSelect: () => void emptyAll(), danger: true }]}
            menuWidth={220}
          ><span aria-hidden="true">⋯</span></OverflowMenu>
        )}
      </header>

      <p class="sh-trash-info">
        <span aria-hidden="true">🗑️</span> {t('gallery.trash.info')}
      </p>

      {picked.size > 0 && (
        <SelectionBar
          count={picked.size}
          onClose={() => setPicked(new Set())}
          actions={[
            { key: 'restore', icon: 'restore', label: t('gallery.trash.restore'), onClick: () => void restore(), busy: busy === 'restore', disabled: busy !== null },
            { key: 'purge', icon: 'purge', label: t('gallery.trash.purge'), onClick: () => void purge(), danger: true, busy: busy === 'purge', disabled: busy !== null },
          ]}
        />
      )}

      {failed && rows === null ? (
        <div class="sh-empty-state" role="alert">
          <div aria-hidden="true">📡</div>
          <h2>{t('gallery.trash.load_failed')}</h2>
          <Button onClick={() => { setFailed(false); void load() }}>{t('common.retry')}</Button>
        </div>
      ) : rows === null ? (
        <div class="sh-image-grid" role="status" aria-label={t('common.loading')} aria-busy="true">
          {Array.from({ length: 6 }, (_, i) => (
            <span key={i} class="sh-skeleton sh-skeleton--rect sh-gallery-skel-tile" aria-hidden="true" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">✨</div>
          <h2>{t('gallery.trash.empty_state_title')}</h2>
          <p>{t('gallery.trash.empty_state_body')}</p>
        </div>
      ) : (
        <>
          {groups.map(g => {
            const by = [...new Set(g.rows.map(r => nameOf(r.deleted_by)).filter(Boolean))]
            const from = [...new Set(g.rows.filter(r => !r.locked).map(r => r.album_name))]
            return (
              <section key={g.key} class="sh-trash-group" aria-label={g.heading}>
                <h2 class="sh-trash-day">
                  {g.heading}
                  {by.length > 0 && <span class="sh-trash-day-meta"> · {t('gallery.trash.by', { names: by.join(', ') })}</span>}
                  {from.length === 1 && <span class="sh-trash-day-meta"> · {t('gallery.trash.from', { album: from[0] })}</span>}
                </h2>
                <div class="sh-image-grid">
                  {g.rows.map(r => r.locked
                    ? <LockedTile key={r.id} row={r} />
                    : <TrashTile key={r.id} row={r} picked={picked.has(r.id)} onToggle={() => toggle(r.id)} />)}
                </div>
              </section>
            )
          })}
          {more && (
            <div class="sh-gallery-more">
              <Button variant="secondary" onClick={() => void loadMore()}>{t('feed.load_more')}</Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function TrashTile({ row, picked, onToggle }: { row: TrashRow; picked: boolean; onToggle: () => void }) {
  const left = daysLeft(row.purge_after)
  const leftLabel = left === 0
    ? t('gallery.trash.days_left_today')
    : t(isOne(left) ? 'gallery.trash.days_left_one' : 'gallery.trash.days_left', { n: String(left) })
  const kind = row.item_type === 'video' ? t('gallery.item_video') : t('gallery.item_photo')
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={picked}
      class={'sh-gallery-item sh-gallery-item--selectable' + (picked ? ' sh-gallery-item--picked' : '')}
      aria-label={t('gallery.trash.tile_aria', { type: kind, album: row.album_name, left: leftLabel })}
      onClick={onToggle}
    >
      <img src={row.thumbnail_url} alt="" loading="lazy" decoding="async" />
      {row.item_type === 'video' && <span class="sh-video-badge" aria-hidden="true">▶</span>}
      <span class="sh-select-check" aria-hidden="true" />
      <span class="sh-trash-left" aria-hidden="true">{leftLabel}</span>
    </button>
  )
}

function LockedTile({ row }: { row: TrashRow }) {
  const n = row.locked?.count ?? 1
  const owner = nameOf(row.locked?.owner_user_id ?? null)
  const left = daysLeft(row.purge_after)
  return (
    <div class="sh-trash-locked" role="img" aria-label={t('gallery.trash.locked_aria', { name: owner, n: String(n) })}>
      <span aria-hidden="true">🔒</span>
      <span>{t('gallery.trash.locked_title')}</span>
      <span class="sh-muted">{t(isOne(n) ? 'gallery.trash.locked_meta_one' : 'gallery.trash.locked_meta', { name: owner, n: String(n) })}</span>
      <strong>{t(isOne(left) ? 'gallery.trash.days_left_one' : 'gallery.trash.days_left', { n: String(left) })}</strong>
    </div>
  )
}
