/**
 * UploadSheet — the gallery's upload queue on screen.
 *
 * Desktop: a panel on the right, the gallery stays usable beside it.
 * Phone: a bottom sheet above the bottom bar. Both minimise to a pill.
 *
 * Top to bottom: how many are up ("12 of 40 uploaded", time left, what
 * failed), the album they go into (with the household's default album
 * when nobody picked one), then the rows that need the person first
 * (a file that can't go up, a failed one — Try again / Remove), the
 * finished ones folded into one thumbnail strip, and the rest
 * (waiting / uploading n % / processing). The list is virtualised: a
 * pick of 2 000 files renders only the rows on screen, and only those
 * decode a preview.
 *
 * Screen readers hear milestones in one polite live region ("10 of 40
 * uploaded", the summary) — never every percent.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { t, isOne } from '@/i18n/i18n'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import {
  cancelAll, clearFinished, counts, entries, lastAlbumId, pause, pauseReason,
  paused, remove, removedCount, removeNeedsYou, resume, retry, retryAll,
  setTarget, sheetMinimised, sheetOpen, slowUntil, target,
  type UploadEntry, type UploadTarget,
} from './uploadQueue'
import { canPreview, formatSize } from './uploadFiles'
import { albumDisplayName } from './albumName'

export interface SheetAlbum {
  id: string
  name: string
  space_id: string | null
  is_system?: boolean
  /** The default album under its default name (shown in the reader's language). */
  default_name?: boolean
}

export interface UploadSheetProps {
  /** The gallery the sheet sits in (``null``: the household). */
  spaceId: string | null
  /** The albums loaded on the page — the choices for "Album". */
  albums: SheetAlbum[]
  /** Open an album in the gallery ("Open album" when done). */
  onOpenAlbum: (albumId: string) => void
  /** An album was made from the sheet: re-read the list. */
  onAlbumCreated: () => void
  /** Present when the page has a camera input ("Take another"). */
  onCamera?: () => void
  /** The last files came from the camera. */
  fromCamera?: boolean
}

const DEFAULT = '__default__'
const NEW = '__new__'

/** The household default album, as last read from the server. */
interface DefaultAlbum { id: string; name: string; default_name?: boolean }

export function UploadSheet(props: UploadSheetProps) {
  return (
    <>
      <Announcer />
      {sheetOpen.value && (sheetMinimised.value ? <Pill /> : <Sheet {...props} />)}
    </>
  )
}

/** Can this browser keep the screen on (Screen Wake Lock)? Only then
 *  does the sheet promise it. */
function canKeepAwake(): boolean {
  return typeof navigator !== 'undefined' && 'wakeLock' in navigator
}

function titleText(): string {
  const c = counts.value
  if (c.settled && c.sendable === 0) return t('gallery.upload.title_nothing')
  if (c.settled && c.failed === 0) {
    return t('gallery.upload.title_done', { n: String(c.done - c.alreadyHere - c.restored) })
  }
  return t('gallery.upload.title_progress', { done: String(c.done), n: String(c.sendable) })
}

function Pill() {
  const c = counts.value
  const pct = c.bytesTotal ? Math.round((c.bytesDone / c.bytesTotal) * 100) : 100
  return (
    <button
      type="button"
      class="sh-upload-pill"
      aria-label={`${titleText()} — ${t('gallery.upload.show')}`}
      onClick={() => { sheetMinimised.value = false }}
    >
      <span class="sh-upload-pill__text">
        {paused.value ? `${titleText()} · ${t('gallery.upload.sub_paused')}` : titleText()}
      </span>
      {c.needsYou > 0 && (
        <span class="sh-upload-pill__badge">{c.needsYou}</span>
      )}
      <span class="sh-upload-pill__bar" aria-hidden="true">
        <span style={{ width: `${pct}%` }} />
      </span>
    </button>
  )
}

/** "about 2 min left", from the bytes sent over the last seconds. */
function useEta(): string | null {
  const [eta, setEta] = useState<string | null>(null)
  const samples = useRef<Array<[number, number]>>([])
  useEffect(() => {
    const timer = setInterval(() => {
      const c = counts.value
      if (c.settled || paused.value) { samples.current = []; setEta(null); return }
      const now = Date.now()
      samples.current.push([now, c.bytesDone])
      samples.current = samples.current.filter(([at]) => now - at <= 20_000)
      const [t0, b0] = samples.current[0]
      const rate = (c.bytesDone - b0) / ((now - t0) / 1000)
      // Five seconds of data before guessing.
      if (now - t0 < 5000 || rate <= 0) { setEta(null); return }
      const left = (c.bytesTotal - c.bytesDone) / rate
      setEta(left < 60
        ? t('gallery.upload.sub_eta_soon')
        : t('gallery.upload.sub_eta_min', { n: String(Math.ceil(left / 60)) }))
    }, 1000)
    return () => clearInterval(timer)
  }, [])
  return eta
}

function subline(eta: string | null): string {
  const c = counts.value
  const parts: string[] = []
  if (pauseReason.value === 'storage_full') parts.push(t('gallery.upload.sub_storage_full'))
  else if (paused.value) parts.push(t('gallery.upload.sub_paused'))
  else if (!c.settled && typeof navigator !== 'undefined' && navigator.onLine === false) {
    parts.push(t('gallery.upload.sub_offline'))
  } else if (!c.settled && slowUntil.value > Date.now()) parts.push(t('gallery.upload.sub_slow'))
  else if (!c.settled && eta) parts.push(eta)
  if (c.settled) {
    if (c.alreadyHere) parts.push(t('gallery.upload.summary_already', { n: String(c.alreadyHere) }))
    if (c.restored) parts.push(t('gallery.upload.summary_restored', { n: String(c.restored) }))
    if (removedCount.value > 0) {
      parts.push(t('gallery.upload.summary_skipped', { n: String(removedCount.value) }))
    }
  }
  if (c.failed > 0) parts.push(t('gallery.upload.sub_failed', { n: String(c.failed) }))
  if (c.blocked > 0) {
    parts.push(t(isOne(c.blocked) ? 'gallery.upload.sub_blocked_one' : 'gallery.upload.sub_blocked',
      { n: String(c.blocked) }))
  }
  return parts.join(' · ')
}

function Sheet({ spaceId, albums, onOpenAlbum, onAlbumCreated, onCamera, fromCamera }: UploadSheetProps) {
  const c = counts.value
  const eta = useEta()
  const headingRef = useRef<HTMLHeadingElement | null>(null)
  const [confirmCancel, setConfirmCancel] = useState(false)

  // Focus the heading when the sheet opens, so the keyboard and a screen
  // reader land in it; Esc minimises it.
  useEffect(() => { headingRef.current?.focus() }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) sheetMinimised.value = true
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  useEffect(() => { if (c.active === 0) setConfirmCancel(false) }, [c.active])

  const pct = c.bytesTotal ? Math.round((c.bytesDone / c.bytesTotal) * 100) : (c.settled ? 100 : 0)
  const doneAlbum = lastAlbumId.value

  return (
    <section
      class="sh-upload-sheet"
      role="dialog"
      aria-modal="false"
      aria-labelledby="sh-upload-sheet-title"
    >
      <div class="sh-upload-sheet__grab" aria-hidden="true" />
      <header class="sh-upload-sheet__head">
        <div class="sh-upload-sheet__titlerow">
          <h2 id="sh-upload-sheet-title" tabIndex={-1} ref={headingRef}>{titleText()}</h2>
          <button
            type="button"
            class="sh-upload-sheet__min"
            aria-label={t('gallery.upload.minimise')}
            title={t('gallery.upload.minimise')}
            onClick={() => { sheetMinimised.value = true }}
          >
            <span aria-hidden="true">⌄</span>
          </button>
        </div>
        <p class="sh-upload-sheet__sub">{subline(eta) || ' '}</p>
        <div
          class="sh-upload-sheet__bar"
          role="progressbar"
          aria-label={t('gallery.upload.sheet_label')}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
          aria-valuetext={titleText()}
        >
          <span style={{ width: `${pct}%` }} />
        </div>
        <AlbumChoice spaceId={spaceId} albums={albums} onAlbumCreated={onAlbumCreated} />
      </header>

      <UploadList />

      <footer class="sh-upload-sheet__foot">
        {c.active > 0 && !paused.value && (
          <p class="sh-upload-sheet__note">
            {t(canKeepAwake() ? 'gallery.upload.keep_awake' : 'gallery.upload.keep_open')}
          </p>
        )}
        {confirmCancel ? (
          <div class="sh-upload-sheet__actions" role="group" aria-label={t('gallery.upload.cancel_all')}>
            <span class="sh-upload-sheet__confirm">
              {t(isOne(c.waiting + c.uploading) ? 'gallery.upload.cancel_confirm_one' : 'gallery.upload.cancel_confirm',
                { n: String(c.waiting + c.uploading) })}
            </span>
            <Button variant="secondary" onClick={() => setConfirmCancel(false)}>
              {t('gallery.upload.cancel_no')}
            </Button>
            <Button variant="danger" onClick={() => { cancelAll(); setConfirmCancel(false) }}>
              {t('gallery.upload.cancel_yes')}
            </Button>
          </div>
        ) : (
          <div class="sh-upload-sheet__actions">
            {onCamera && fromCamera && (
              <Button variant="secondary" onClick={onCamera}>
                <span aria-hidden="true">📷</span> {t('gallery.upload.take_another')}
              </Button>
            )}
            {c.active > 0 ? (
              <>
                {paused.value ? (
                  <Button variant="secondary" onClick={resume}>
                    <span aria-hidden="true">▶</span> {t('gallery.upload.resume')}
                  </Button>
                ) : (
                  <Button variant="secondary" onClick={pause}>
                    <span aria-hidden="true">❙❙</span> {t('gallery.upload.pause')}
                  </Button>
                )}
                {c.waiting + c.uploading > 0 && (
                  <Button variant="ghost" onClick={() => setConfirmCancel(true)}>
                    {t('gallery.upload.cancel_all')}
                  </Button>
                )}
              </>
            ) : (
              <>
                {doneAlbum && c.done > 0 && (
                  <Button
                    variant="secondary"
                    onClick={() => {
                      onOpenAlbum(doneAlbum)
                      clearFinished()
                      sheetOpen.value = false
                    }}
                  >
                    {t('gallery.upload.open_album')}
                  </Button>
                )}
                <Button onClick={() => { clearFinished(); sheetOpen.value = false }}>
                  {t('gallery.upload.close')}
                </Button>
              </>
            )}
          </div>
        )}
      </footer>
    </section>
  )
}

// ─── Album choice ───────────────────────────────────────────────────────

let defaultCache: DefaultAlbum | null | undefined

function AlbumChoice({ spaceId, albums, onAlbumCreated }: {
  spaceId: string | null
  albums: SheetAlbum[]
  onAlbumCreated: () => void
}) {
  const tgt = target.value
  const household = spaceId === null && (tgt?.spaceId ?? null) === null
  const [def, setDef] = useState<DefaultAlbum | null | undefined>(defaultCache)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!household || def !== undefined) return
    let alive = true
    api.get('/api/gallery/default-album')
      .then((r: { album: DefaultAlbum | null }) => {
        defaultCache = r.album ? { id: r.album.id, name: albumDisplayName(r.album) } : null
        if (alive) setDef(defaultCache)
      })
      .catch(() => { if (alive) setDef(null) })
    return () => { alive = false }
  }, [household, def])

  // The queue learned the default album (its first file made it).
  const learnedId = tgt?.isDefault ? tgt.albumId : null
  const learnedName = tgt?.name ?? ''
  useEffect(() => {
    if (learnedId && def?.id !== learnedId) {
      defaultCache = { id: learnedId, name: learnedName }
      setDef(defaultCache)
    }
  }, [learnedId, learnedName, def?.id])

  const defaultName = def?.name ?? t('gallery.upload.default_album')
  const choices = albums.filter(a => !a.is_system && (a.space_id ?? null) === (tgt?.spaceId ?? spaceId)
    && a.id !== def?.id)
  const value = !tgt ? '' : tgt.isDefault ? DEFAULT : (tgt.albumId ?? '')
  const scope = tgt?.spaceId ?? spaceId

  const pick = (v: string) => {
    if (v === NEW) { setCreating(true); return }
    setCreating(false)
    if (v === DEFAULT) {
      setTarget({ spaceId: null, albumId: def?.id ?? null, name: defaultName, isDefault: true })
      return
    }
    const a = albums.find(x => x.id === v)
    if (a) setTarget({ spaceId: scope, albumId: a.id, name: albumDisplayName(a), isDefault: false })
  }

  const create = async (e: Event) => {
    e.preventDefault()
    const n = name.trim()
    if (!n || busy) return
    setBusy(true)
    try {
      const url = scope ? `/api/spaces/${scope}/gallery/albums` : '/api/gallery/albums'
      const a = await api.post(url, { name: n }) as SheetAlbum
      setTarget({ spaceId: scope, albumId: a.id, name: a.name, isDefault: false })
      setCreating(false)
      setName('')
      onAlbumCreated()
    } catch (err: unknown) {
      showToast(t('gallery.create_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-upload-sheet__album">
      <label class="sh-upload-sheet__album-row">
        <span>{t('gallery.upload.album_label')}</span>
        <select
          value={creating ? NEW : value}
          onChange={(e) => pick((e.target as HTMLSelectElement).value)}
        >
          {!tgt && <option value="" disabled>{t('gallery.upload.choose_album')}</option>}
          {household && <option value={DEFAULT}>{defaultName}</option>}
          {tgt && !tgt.isDefault && tgt.albumId && !choices.some(a => a.id === tgt.albumId) && (
            <option value={tgt.albumId}>{tgt.name}</option>
          )}
          {choices.map(a => <option key={a.id} value={a.id}>{albumDisplayName(a)}</option>)}
          <option value={NEW}>{t('gallery.upload.new_album')}</option>
        </select>
      </label>
      {creating && (
        <form class="sh-upload-sheet__new" onSubmit={create}>
          <input
            type="text"
            maxLength={80}
            value={name}
            aria-label={t('gallery.upload.new_album_name')}
            placeholder={t('gallery.form.name_placeholder')}
            onInput={(e) => setName((e.target as HTMLInputElement).value)}
            autoFocus
          />
          <Button type="submit" loading={busy} disabled={!name.trim()}>{t('gallery.form.create')}</Button>
          <Button type="button" variant="ghost" onClick={() => setCreating(false)}>{t('common.cancel')}</Button>
        </form>
      )}
      {!tgt && !creating && (
        <p class="sh-upload-sheet__hint">{t('gallery.upload.choose_to_start')}</p>
      )}
      {tgt?.isDefault && !creating && (
        <p class="sh-upload-sheet__hint">{t('gallery.upload.default_hint', { name: defaultName })}</p>
      )}
    </div>
  )
}

// ─── The list (virtualised) ─────────────────────────────────────────────

type Row =
  | { kind: 'head'; key: string; label: string; needs?: boolean }
  | { kind: 'entry'; key: string; entry: UploadEntry; needs: boolean }
  | { kind: 'strip'; key: string; done: UploadEntry[] }

/** Row heights (px) — fixed per kind so 2 000 rows need no measuring. */
export const ROW_HEIGHT = { head: 40, entry: 64, needs: 136, strip: 64 } as const
const OVERSCAN_PX = 320

function heightOf(r: Row): number {
  if (r.kind === 'entry') return r.needs ? ROW_HEIGHT.needs : ROW_HEIGHT.entry
  return ROW_HEIGHT[r.kind]
}

export function buildRows(list: UploadEntry[]): Row[] {
  const needs = list.filter(e => e.state === 'blocked' || e.state === 'failed')
  const done = list.filter(e => e.state === 'done')
  const active = list.filter(e => e.state === 'uploading' || e.state === 'processing' || e.state === 'waiting')
  // Going up first, then processing, then waiting: what moves is on top.
  const order = { uploading: 0, processing: 1, waiting: 2 } as Record<string, number>
  active.sort((a, b) => order[a.state] - order[b.state] || a.id - b.id)
  const rows: Row[] = []
  if (needs.length) {
    rows.push({ kind: 'head', key: 'h-needs', label: t('gallery.upload.section_needs', { n: String(needs.length) }), needs: true })
    for (const e of needs) rows.push({ kind: 'entry', key: `e${e.id}`, entry: e, needs: true })
  }
  if (done.length) {
    rows.push({ kind: 'head', key: 'h-done', label: [
      t('gallery.upload.section_done', { n: String(done.length) }), ...duplicateParts(done),
    ].join(' · ') })
    rows.push({ kind: 'strip', key: 'strip', done })
  }
  if (active.length) {
    rows.push({ kind: 'head', key: 'h-active', label: t('gallery.upload.section_active', { n: String(active.length) }) })
    for (const e of active) rows.push({ kind: 'entry', key: `e${e.id}`, entry: e, needs: false })
  }
  return rows
}

function UploadList() {
  const list = entries.value
  const rows = useMemo(() => buildRows(list), [list])
  const offsets = useMemo(() => {
    const out = new Array<number>(rows.length + 1)
    out[0] = 0
    for (let i = 0; i < rows.length; i++) out[i + 1] = out[i] + heightOf(rows[i])
    return out
  }, [rows])
  const ref = useRef<HTMLDivElement | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [viewport, setViewport] = useState(0)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setViewport(el.clientHeight)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setViewport(el.clientHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // jsdom (and a first paint) has no height: render a screenful.
  const height = viewport || 720
  const top = Math.max(0, scrollTop - OVERSCAN_PX)
  const bottom = scrollTop + height + OVERSCAN_PX
  let first = 0
  let lo = 0, hi = rows.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (offsets[mid + 1] <= top) lo = mid + 1
    else hi = mid
  }
  first = lo
  const visible: Array<[Row, number]> = []
  for (let i = first; i < rows.length && offsets[i] < bottom; i++) visible.push([rows[i], offsets[i]])

  return (
    <div
      class="sh-upload-sheet__list"
      ref={ref}
      onScroll={(e) => setScrollTop((e.currentTarget as HTMLDivElement).scrollTop)}
    >
      <ul style={{ height: `${offsets[rows.length]}px` }}>
        {visible.map(([r, y]) => (
          <li
            key={r.key}
            class={`sh-upload-row sh-upload-row--${r.kind}${r.kind === 'entry' && r.needs ? ' sh-upload-row--needs' : ''}`}
            style={{ transform: `translateY(${y}px)`, height: `${heightOf(r)}px` }}
          >
            {r.kind === 'head' ? <Head label={r.label} needs={!!r.needs} />
              : r.kind === 'strip' ? <DoneStrip done={r.done} />
                : <EntryRow entry={r.entry} />}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Head({ label, needs }: { label: string; needs: boolean }) {
  const c = counts.value
  return (
    <div class="sh-upload-row__head">
      <h3>{label}</h3>
      {needs && (
        <span class="sh-upload-row__head-actions">
          {c.failed > 0 && (
            <button type="button" class="sh-upload-linkbtn" onClick={retryAll}>{t('gallery.upload.retry_all')}</button>
          )}
          <button type="button" class="sh-upload-linkbtn" onClick={removeNeedsYou}>{t('gallery.upload.remove_all')}</button>
        </span>
      )}
    </div>
  )
}

const STRIP_MAX = 6

function DoneStrip({ done }: { done: UploadEntry[] }) {
  const shown = done.slice(-STRIP_MAX).reverse()
  const more = done.length - shown.length
  return (
    <div class="sh-upload-strip">
      {shown.map(e => (
        <span key={e.id} class="sh-upload-strip__thumb" title={e.file.name}>
          {e.thumbUrl
            ? <img src={e.thumbUrl} alt="" loading="lazy" decoding="async" />
            : <Placeholder entry={e} />}
        </span>
      ))}
      {more > 0 && <span class="sh-upload-strip__more">{t('gallery.upload.more_done', { n: String(more) })}</span>}
    </div>
  )
}

function Placeholder({ entry }: { entry: UploadEntry }) {
  const heic = entry.check.mime === 'image/heic'
  return (
    <span class={`sh-upload-ph${heic ? ' sh-upload-ph--heic' : ''}`} aria-hidden="true">
      {entry.problem === 'unsupported' ? '📄' : entry.check.kind === 'video' ? '🎬' : heic ? 'HEIC' : '🖼️'}
    </span>
  )
}

/** A local preview, decoded only while the row is on screen. */
function Preview({ entry }: { entry: UploadEntry }) {
  const [url, setUrl] = useState<string | null>(null)
  const [bad, setBad] = useState(false)
  const ok = canPreview(entry.file, entry.check)
  useEffect(() => {
    if (!ok || typeof URL.createObjectURL !== 'function') return
    const u = URL.createObjectURL(entry.file)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [entry.file, ok])
  if (entry.thumbUrl) return <img src={entry.thumbUrl} alt="" decoding="async" />
  if (!url || bad) return <Placeholder entry={entry} />
  return <img src={url} alt="" decoding="async" onError={() => setBad(true)} />
}

function stateText(e: UploadEntry): string {
  switch (e.state) {
    case 'waiting':
      return e.netTries > 0 ? t('gallery.upload.state_retrying') : t('gallery.upload.state_waiting')
    case 'uploading':
      return t('gallery.upload.state_uploading', {
        n: String(e.file.size ? Math.round((e.loaded / e.file.size) * 100) : 0),
      })
    case 'processing': return t('media.processing')
    case 'done': return t('gallery.upload.state_done')
    default: return e.reason ?? ''
  }
}

function EntryRow({ entry: e }: { entry: UploadEntry }) {
  const needs = e.state === 'blocked' || e.state === 'failed'
  const name = e.file.name
  const pct = e.file.size ? Math.round((e.loaded / e.file.size) * 100) : 0
  return (
    <div class="sh-upload-row__entry">
      <span class="sh-upload-row__thumb"><Preview entry={e} /></span>
      <div class="sh-upload-row__body">
        <div class="sh-upload-row__line">
          <span class="sh-upload-row__name" title={name}>{name}</span>
          <span class="sh-upload-row__size">{formatSize(e.file.size)}</span>
        </div>
        <div class={`sh-upload-row__state sh-upload-row__state--${e.state}`} title={needs ? stateText(e) : undefined}>
          {stateText(e)}
        </div>
        {e.state === 'uploading' && (
          <div class="sh-upload-row__bar" aria-hidden="true"><span style={{ width: `${pct}%` }} /></div>
        )}
        {needs && (
          <div class="sh-upload-row__actions">
            {e.state === 'failed' && (
              <Button
                variant="secondary"
                aria-label={t('gallery.upload.retry_aria', { name })}
                onClick={() => retry(e.id)}
              >
                <span aria-hidden="true">↻</span> {t('gallery.upload.retry')}
              </Button>
            )}
            <Button
              variant="ghost"
              aria-label={t('gallery.upload.remove_aria', { name })}
              onClick={() => remove(e.id)}
            >
              {t('gallery.upload.remove')}
            </Button>
          </div>
        )}
      </div>
      {(e.state === 'waiting' || e.state === 'uploading') && (
        <button
          type="button"
          class="sh-upload-row__x"
          aria-label={t('gallery.upload.remove_aria', { name })}
          title={t('gallery.upload.remove')}
          onClick={() => remove(e.id)}
        >
          <span aria-hidden="true">✕</span>
        </button>
      )}
    </div>
  )
}

// ─── Screen-reader milestones ───────────────────────────────────────────

/** One polite live region: "10 of 40 uploaded" every ten files (at most
 *  every ten seconds), pause / resume, and the summary at the end. */
function Announcer() {
  const [msg, setMsg] = useState('')
  const last = useRef({ done: 0, at: 0, settled: true, paused: false })
  const c = counts.value
  const isPaused = paused.value
  const { done, settled, sendable } = c
  // Anything to report: files in the list, or ones the person stopped.
  const total = c.total + removedCount.value
  useEffect(() => {
    const prev = last.current
    const now = Date.now()
    let next: string | null = null
    if (settled && !prev.settled && total > 0) {
      next = t('gallery.upload.announce_done', { summary: summary() })
    } else if (isPaused !== prev.paused && !settled) {
      next = t(isPaused ? 'gallery.upload.announce_paused' : 'gallery.upload.announce_resumed')
    } else if (!settled && done !== prev.done && done > 0
      && (done % 10 === 0 || done - prev.done >= 10) && now - prev.at >= 10_000) {
      next = t('gallery.upload.title_progress', { done: String(done), n: String(sendable) })
    }
    if (next) {
      setMsg(next)
      last.current = { done, at: now, settled, paused: isPaused }
    } else {
      last.current = { ...prev, settled, paused: isPaused }
    }
  }, [done, settled, total, sendable, isPaused])
  return <div class="sr-only" aria-live="polite" role="status">{msg}</div>
}

/** "1 already here", "2 back from the trash" — for the finished rows. */
function duplicateParts(done: UploadEntry[]): string[] {
  const already = done.filter(e => e.duplicate === 'already_here').length
  const back = done.filter(e => e.duplicate === 'restored').length
  const out: string[] = []
  if (already) out.push(t('gallery.upload.summary_already', { n: String(already) }))
  if (back) out.push(t('gallery.upload.summary_restored', { n: String(back) }))
  return out
}

/** "38 added · 1 already here · 2 failed · 1 not uploaded". */
export function summary(): string {
  const c = counts.value
  const added = c.done - c.alreadyHere - c.restored
  const parts = [t('gallery.upload.summary_added', { n: String(added) })]
  if (c.alreadyHere) parts.push(t('gallery.upload.summary_already', { n: String(c.alreadyHere) }))
  if (c.restored) parts.push(t('gallery.upload.summary_restored', { n: String(c.restored) }))
  if (c.failed) parts.push(t('gallery.upload.sub_failed', { n: String(c.failed) }))
  const skipped = removedCount.value + c.blocked
  if (skipped) parts.push(t('gallery.upload.summary_skipped', { n: String(skipped) }))
  return parts.join(' · ')
}

export type { UploadTarget }
