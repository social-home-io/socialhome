/**
 * uploadQueue — the gallery's upload queue (household and space albums).
 *
 * A module store, so a holiday's 300 photos keep going while the person
 * browses elsewhere in the app. Each file is one entry with its own
 * state:
 *
 *   blocked → can't go up as it is (too big, not a photo or video, empty,
 *             a household video over 5 minutes): waits for the person to
 *             remove it — never sent.
 *   waiting → uploading (n %) → processing (bytes are in, the server is
 *             converting) → done
 *                           ↘ failed (a translated reason; Try again)
 *
 * A video for the household gallery waits (still ``waiting``) until the
 * browser has read its length from the file: one over 5 minutes is
 * ``blocked`` with its length ("7:12 long — the gallery keeps videos up to
 * 5 minutes"), never sent and never cut. When the browser can't tell, it
 * goes up and the server checks the same rule.
 *
 * Three files go up at once. The gallery has its own rate bucket on the
 * server (300 uploads a minute); a 429 stops every worker until the
 * server's ``Retry-After`` (else a growing back-off) and sends the same
 * file again. A lost connection waits until the browser is online and
 * the page visible again, then tries the file again — a phone that
 * switched apps carries on when it comes back.
 *
 * While anything is uploading the screen is kept on (Screen Wake Lock,
 * fail-soft) and closing the tab asks first.
 *
 * Every upload goes to ``POST api/gallery/albums/{id}/items``; the server
 * stores each one in the defined format (WebP photos, WebM videos). With
 * no album chosen in the household gallery, the first file asks
 * ``POST /api/gallery/default-album`` for the household's default album.
 */
import { computed, effect, signal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { token } from '@/store/token'
import { UploadHttpError, uploadHttpError } from '@/utils/uploadErrors'
import {
  HOUSEHOLD_VIDEO_MAX_BYTES, HOUSEHOLD_VIDEO_MAX_SECONDS, checkFile, formatDuration,
  readVideoDuration, videoTooLong, withType, type FileCheck, type FileProblem,
} from './uploadFiles'
import { albumDisplayName } from './albumName'

export type EntryState =
  | 'blocked' | 'waiting' | 'uploading' | 'processing' | 'done' | 'failed'

export interface UploadEntry {
  id: number
  file: File
  check: FileCheck
  state: EntryState
  /** Bytes sent of this file (for the bar and the time estimate). */
  loaded: number
  /** Translated reason a ``failed`` / ``blocked`` row shows. */
  reason: string | null
  /** The pre-upload problem of a ``blocked`` row. */
  problem: FileProblem | null
  /** The stored item's thumbnail, once ``done`` (signed). */
  thumbUrl: string | null
  /** Network retries spent on this file. */
  netTries: number
  /** 429 retries spent on this file. */
  rateTries: number
  /** Not before this time (ms epoch): a retry after a lost connection. */
  retryAt: number
  /** Where this file goes (``null``: nobody picked an album yet). */
  dest: UploadTarget | null
  /** The server already had it (``already_here``) or brought it back
   *  from the trash (``restored``): nothing new was stored. */
  duplicate: 'already_here' | 'restored' | null
  /** Into a household album (its videos may be 5 minutes long). */
  household: boolean
  /** A household video whose length the browser is still reading — it
   *  doesn't start until that is known. */
  checking: boolean
  /** The video's length in seconds, once known. */
  durationS: number | null
}

/** Where the queue's files go. ``albumId`` ``null`` with ``isDefault``:
 *  the household's default album, asked for (made if need be) when the
 *  first file starts. ``null`` target: a space gallery where nobody has
 *  picked an album yet — nothing starts until they do. */
export interface UploadTarget {
  spaceId: string | null
  albumId: string | null
  name: string
  isDefault: boolean
}

/** Files at once. */
export const PARALLEL = 3
/** Network retries per file before it is "failed". */
const MAX_NET_TRIES = 3
/** 429 retries per file before it is "failed". */
const MAX_RATE_TRIES = 6

export const entries = signal<UploadEntry[]>([])
export const target = signal<UploadTarget | null>(null)
export const paused = signal(false)
/** Why the queue paused itself (``storage_full``), else ``null``. */
export const pauseReason = signal<'storage_full' | null>(null)
/** The server asked us to slow down until this time (ms epoch), else 0. */
export const slowUntil = signal(0)
/** The sheet: open, and minimised to its pill. */
export const sheetOpen = signal(false)
export const sheetMinimised = signal(false)
/** The album a finished upload can be opened in. */
export const lastAlbumId = signal<string | null>(null)
/** Files removed by the person before they went up (for the summary). */
export const removedCount = signal(0)

export const counts = computed(() => {
  let blocked = 0, waiting = 0, uploading = 0, processing = 0, done = 0, failed = 0
  let alreadyHere = 0, restored = 0
  let bytesTotal = 0, bytesDone = 0
  for (const e of entries.value) {
    switch (e.state) {
      case 'blocked': blocked++; break
      case 'waiting': waiting++; break
      case 'uploading': uploading++; break
      case 'processing': processing++; break
      case 'done':
        done++
        if (e.duplicate === 'already_here') alreadyHere++
        else if (e.duplicate === 'restored') restored++
        break
      case 'failed': failed++; break
    }
    if (e.state !== 'blocked' && e.state !== 'failed') {
      bytesTotal += e.file.size
      bytesDone += e.state === 'done' || e.state === 'processing' ? e.file.size : e.loaded
    }
  }
  const active = waiting + uploading + processing
  const total = entries.value.length
  return {
    blocked, waiting, uploading, processing, done, failed, active, total,
    /** Of ``done``: already here / back from the trash (nothing new). */
    alreadyHere, restored,
    bytesTotal, bytesDone,
    /** Files that can still go up or went up (the "n of N"). */
    sendable: total - blocked,
    needsYou: blocked + failed,
    settled: active === 0,
  }
})

/** Is anything still going up (or waiting to)? */
export const isBusy = computed(() => counts.value.active > 0)

let nextId = 1
const xhrs = new Map<number, XMLHttpRequest>()
/** Entries aborted on purpose, and what to do with them afterwards. */
const aborts = new Map<number, 'pause' | 'remove'>()
let running = 0
/** Bumped by the test reset, so a stale upload's end changes nothing. */
let generation = 0
let gateTimer: ReturnType<typeof setTimeout> | null = null
let defaultAlbum: Promise<string> | null = null

// ─── Store helpers ──────────────────────────────────────────────────────

function patch(id: number, p: Partial<UploadEntry>): void {
  entries.value = entries.value.map(e => (e.id === id ? { ...e, ...p } : e))
}

function get(id: number): UploadEntry | undefined {
  return entries.value.find(e => e.id === id)
}

// ─── Public actions ─────────────────────────────────────────────────────

/**
 * Add files to the queue and open the sheet; they go to ``into`` (the
 * album open on the page, the household's default album, or ``null`` —
 * a space gallery where the person picks one in the sheet). Returns
 * ``false`` (nothing added) while the queue is busy for another gallery.
 */
export function addFiles(files: File[], into: UploadTarget | null): boolean {
  if (files.length === 0) return true
  const busy = isBusy.value
  if (busy && into && target.value && (target.value.spaceId ?? null) !== (into.spaceId ?? null)) {
    return false
  }
  if (!busy) {
    // A finished batch makes way for the new one; rows that still need
    // a decision stay until the person makes it.
    entries.value = entries.value.filter(e => e.state !== 'done')
    removedCount.value = 0
  }
  const dest = into ?? target.value
  if (into || !busy) target.value = dest
  const household = isHousehold(dest)
  const fresh = files.map<UploadEntry>(file => {
    const check = checkFile(file, household)
    const entry: UploadEntry = {
      id: nextId++,
      file,
      check,
      state: check.problem ? 'blocked' : 'waiting',
      loaded: 0,
      reason: null,
      problem: check.problem,
      thumbUrl: null,
      netTries: 0,
      rateTries: 0,
      retryAt: 0,
      dest,
      duplicate: null,
      household,
      checking: household && check.kind === 'video' && !check.problem,
      durationS: null,
    }
    if (check.problem) entry.reason = problemReason(check.problem, entry)
    return entry
  })
  entries.value = [...entries.value, ...fresh]
  sheetOpen.value = true
  sheetMinimised.value = false
  for (const e of fresh) if (e.checking) void measure(e.id)
  pump()
  return true
}

/** Into a household album? (A space gallery's queue names its space.) */
function isHousehold(dest: UploadTarget | null): boolean {
  return dest !== null && (dest.spaceId ?? null) === null
}

/** Read a household video's length before it goes up; refuse one over
 *  5 minutes with its length (never cut). */
async function measure(id: number): Promise<void> {
  const e = get(id)
  if (!e) return
  const gen = generation
  const seconds = await readVideoDuration(e.file)
  if (gen !== generation || !get(id)) return
  if (seconds !== null && videoTooLong(seconds)) {
    const blocked = { checking: false, durationS: seconds, problem: 'too_long' as const }
    patch(id, {
      ...blocked,
      state: 'blocked',
      reason: problemReason('too_long', { ...e, ...blocked }),
    })
  } else {
    patch(id, { checking: false, durationS: seconds })
  }
  pump()
}

/** Pick (or change) the album. Files already sent stay where they went;
 *  everything not sent yet goes to the new one. */
export function setTarget(next: UploadTarget): void {
  target.value = next
  entries.value = entries.value.map(e => (
    e.state === 'waiting' || e.state === 'blocked' || e.state === 'failed' ? { ...e, dest: next } : e
  ))
  pump()
}

export function pause(): void {
  if (paused.value) return
  paused.value = true
  // Files mid-way go back to the queue; one the server is already
  // converting finishes (aborting now would not stop the server).
  for (const e of entries.value) {
    if (e.state === 'uploading') abort(e.id, 'pause')
  }
}

export function resume(): void {
  paused.value = false
  pauseReason.value = null
  pump()
}

/** Remove one file: never sent if it was waiting or blocked, stopped if
 *  it was going up. A file the server is converting can't be taken back. */
export function remove(id: number): void {
  const e = get(id)
  if (!e || e.state === 'processing' || e.state === 'done') return
  if (e.state === 'uploading') {
    abort(id, 'remove')
    return
  }
  drop(id)
}

export function retry(id: number): void {
  const e = get(id)
  if (!e || e.state !== 'failed') return
  patch(id, { state: 'waiting', reason: null, loaded: 0, netTries: 0, rateTries: 0, retryAt: 0 })
  pump()
}

export function retryAll(): void {
  entries.value = entries.value.map(e => (e.state === 'failed'
    ? { ...e, state: 'waiting', reason: null, loaded: 0, netTries: 0, rateTries: 0, retryAt: 0 }
    : e))
  if (pauseReason.value) resume()
  else pump()
}

/** Drop every row that needs a decision (blocked + failed). */
export function removeNeedsYou(): void {
  const n = entries.value.filter(e => e.state === 'blocked' || e.state === 'failed').length
  removedCount.value += n
  entries.value = entries.value.filter(e => e.state !== 'blocked' && e.state !== 'failed')
}

/** Stop everything that hasn't gone up yet. What is done (or being
 *  converted) stays in the album. */
export function cancelAll(): void {
  for (const e of entries.value) {
    if (e.state === 'uploading') abort(e.id, 'remove')
  }
  const gone = entries.value.filter(e => e.state === 'waiting' || e.state === 'blocked' || e.state === 'failed')
  removedCount.value += gone.length
  entries.value = entries.value.filter(e => !gone.includes(e))
  paused.value = false
  pauseReason.value = null
}

/** Close a settled sheet: forget the finished batch. */
export function clearFinished(): void {
  if (!counts.value.settled) return
  entries.value = []
  removedCount.value = 0
  paused.value = false
  pauseReason.value = null
  slowUntil.value = 0
  defaultAlbum = null
}

/** Test hook: forget everything (no uploads may be running). */
export function _resetForTests(): void {
  for (const x of xhrs.values()) x.abort()
  xhrs.clear()
  aborts.clear()
  running = 0
  generation++
  if (gateTimer) clearTimeout(gateTimer)
  gateTimer = null
  gateAt = 0
  defaultAlbum = null
  entries.value = []
  target.value = null
  paused.value = false
  pauseReason.value = null
  slowUntil.value = 0
  sheetOpen.value = false
  sheetMinimised.value = false
  lastAlbumId.value = null
  removedCount.value = 0
  nextId = 1
}

// ─── Engine ─────────────────────────────────────────────────────────────

function drop(id: number): void {
  entries.value = entries.value.filter(e => e.id !== id)
  removedCount.value += 1
}

function abort(id: number, why: 'pause' | 'remove'): void {
  const x = xhrs.get(id)
  aborts.set(id, why)
  if (x) x.abort()
}

/** Run :func:`pump` again in ``ms`` (the earliest wake-up wins). */
let gateAt = 0
function wakeIn(ms: number): void {
  const at = Date.now() + ms
  if (gateTimer && gateAt <= at) return
  if (gateTimer) clearTimeout(gateTimer)
  gateAt = at
  gateTimer = setTimeout(() => { gateTimer = null; pump() }, ms)
}

const hasDest = (e: UploadEntry) => !!e.dest && (!!e.dest.albumId || e.dest.isDefault)

function pump(): void {
  if (paused.value) return
  // Offline: nothing would get through. The ``online`` event pumps again.
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return
  const now = Date.now()
  const wait = slowUntil.value - now
  if (wait > 0) { wakeIn(wait); return }
  if (slowUntil.value) slowUntil.value = 0
  let soonest = Infinity
  while (running < PARALLEL) {
    const next = entries.value.find(e => {
      if (e.state !== 'waiting' || e.checking || !hasDest(e)) return false
      if (e.retryAt > now) { soonest = Math.min(soonest, e.retryAt); return false }
      return true
    })
    if (!next) {
      if (soonest !== Infinity) wakeIn(soonest - now)
      break
    }
    running++
    patch(next.id, { state: 'uploading', loaded: 0, reason: null })
    const gen = generation
    void run(next.id).finally(() => {
      if (gen !== generation) return
      running--
      pump()
    })
  }
}

/** The album id ``dest`` names (asks for the default album once). */
async function albumId(dest: UploadTarget): Promise<string> {
  if (dest.albumId) return dest.albumId
  if (!defaultAlbum) {
    defaultAlbum = (async () => {
      const a = await api.post('/api/gallery/default-album', {
        name: t('gallery.upload.default_album'),
      }) as { id: string; name: string; default_name?: boolean }
      const known = (d: UploadTarget | null) => (d?.isDefault ? { ...d, albumId: a.id, name: albumDisplayName(a) } : d)
      target.value = known(target.value)
      entries.value = entries.value.map(e => (e.dest?.isDefault ? { ...e, dest: known(e.dest) } : e))
      return a.id
    })()
    defaultAlbum.catch(() => { defaultAlbum = null })
  }
  return defaultAlbum
}

async function run(id: number): Promise<void> {
  const e = get(id)
  if (!e) return
  let dest: string
  try {
    dest = await albumId(e.dest!)
  } catch (err) {
    fail(id, err)
    return
  }
  // Paused or removed while the album was being looked up.
  const early = aborts.get(id)
  if (early) {
    aborts.delete(id)
    if (early === 'remove') drop(id)
    else patch(id, { state: 'waiting', loaded: 0 })
    return
  }
  try {
    const item = await send(e, dest)
    lastAlbumId.value = dest
    patch(id, {
      state: 'done',
      loaded: e.file.size,
      thumbUrl: item.item_type === 'photo' ? item.thumbnail_url ?? null : null,
      duplicate: item.duplicate === 'already_here' || item.duplicate === 'restored' ? item.duplicate : null,
    })
  } catch (err) {
    const why = aborts.get(id)
    aborts.delete(id)
    if (why === 'remove') { drop(id); return }
    if (why === 'pause') { patch(id, { state: 'waiting', loaded: 0 }); return }
    onError(id, err)
  }
}

function onError(id: number, err: unknown): void {
  const e = get(id)
  if (!e) return
  if (err instanceof UploadHttpError && err.status === 429 && e.rateTries < MAX_RATE_TRIES) {
    const after = (err as UploadHttpError & { retryAfterS?: number }).retryAfterS
    const waitS = Math.min(after ?? 5 * 2 ** e.rateTries, 60)
    slowUntil.value = Math.max(slowUntil.value, Date.now() + waitS * 1000)
    patch(id, { state: 'waiting', loaded: 0, rateTries: e.rateTries + 1 })
    return
  }
  if (isNetworkError(err) && e.netTries < MAX_NET_TRIES) {
    // Try again a little later — and only once the browser is online.
    patch(id, {
      state: 'waiting', loaded: 0, netTries: e.netTries + 1,
      retryAt: Date.now() + 1000 * 2 ** e.netTries,
    })
    return
  }
  fail(id, err)
}

function fail(id: number, err: unknown): void {
  patch(id, { state: 'failed', loaded: 0, reason: errorReason(err, get(id)) })
  if (err instanceof UploadHttpError && (err.code === 'STORAGE_FULL' || err.status === 507)) {
    // Every other file would fail the same way: stop, and say why.
    paused.value = true
    pauseReason.value = 'storage_full'
    for (const o of entries.value) {
      if (o.state === 'uploading') abort(o.id, 'pause')
    }
  }
}

function isNetworkError(err: unknown): boolean {
  return err instanceof Error && !(err instanceof UploadHttpError)
    && /network/i.test(err.message)
}

interface UploadedItem {
  id: string
  item_type: 'photo' | 'video'
  thumbnail_url?: string
  /** Set (200) when the household already had these exact bytes. */
  duplicate?: string
}

/** Throttle per-file progress to a few frames a second. */
const PROGRESS_EVERY_MS = 250

function send(e: UploadEntry, album: string): Promise<UploadedItem> {
  return new Promise((resolve, reject) => {
    const fd = new FormData()
    fd.append('file', withType(e.file, e.check.mime))
    const xhr = new XMLHttpRequest()
    xhrs.set(e.id, xhr)
    // Relative URL (no leading slash): resolves against ``<base href>``,
    // so the HA ingress prefix is kept (#303). Raw XHR for upload progress.
    xhr.open('POST', `api/gallery/albums/${encodeURIComponent(album)}/items`, true)
    xhr.withCredentials = true
    if (token.value) xhr.setRequestHeader('Authorization', `Bearer ${token.value}`)
    let last = 0
    xhr.upload.onprogress = (ev) => {
      const now = Date.now()
      if (!ev.lengthComputable || now - last < PROGRESS_EVERY_MS) return
      last = now
      // The form's few hundred bytes of framing are not the person's file.
      patch(e.id, { loaded: Math.min(e.file.size, ev.loaded) })
    }
    // Bytes are in — the server converts it now (WebP / WebM).
    xhr.upload.onload = () => {
      if (get(e.id)?.state === 'uploading') patch(e.id, { state: 'processing', loaded: e.file.size })
    }
    const done = () => xhrs.delete(e.id)
    xhr.onload = () => {
      done()
      if (xhr.status >= 200 && xhr.status < 300) {
        let body: UploadedItem | null = null
        try { body = JSON.parse(xhr.responseText) as UploadedItem } catch { body = null }
        resolve(body ?? { id: '', item_type: e.check.kind })
        return
      }
      const err = uploadHttpError(xhr.status, xhr.responseText)
      const wait = Number(xhr.getResponseHeader('Retry-After'))
      ;(err as UploadHttpError & { retryAfterS?: number }).retryAfterS =
        Number.isFinite(wait) && wait > 0 ? wait : undefined
      reject(err)
    }
    xhr.onerror = () => { done(); reject(new Error('Network error')) }
    xhr.onabort = () => { done(); reject(new Error('Aborted')) }
    xhr.send(fd)
  })
}

// ─── Reasons a row shows (short: the row already names the file) ────────

/** What a row needs to say why: its kind, where it goes, its length. */
type ReasonContext = Pick<UploadEntry, 'household' | 'durationS'> & {
  check: Pick<FileCheck, 'kind'>
}

/** "7:12 long — the gallery keeps videos up to 5 minutes." */
function tooLongReason(seconds: number, maxS = HOUSEHOLD_VIDEO_MAX_SECONDS): string {
  return t('gallery.upload.reason_too_long', {
    length: formatDuration(seconds),
    max: String(Math.round(maxS / 60)),
  })
}

function tooLargeReason(e?: ReasonContext): string {
  if (e?.household && e.check.kind === 'video') {
    return t('gallery.upload.reason_too_large_video', {
      max: String(Math.round(HOUSEHOLD_VIDEO_MAX_BYTES / 1024 ** 3)),
    })
  }
  return t('gallery.upload.reason_too_large')
}

export function problemReason(p: FileProblem, e?: ReasonContext): string {
  switch (p) {
    case 'too_large': return tooLargeReason(e)
    case 'too_long': return tooLongReason(e?.durationS ?? 0)
    case 'empty': return t('gallery.upload.reason_empty')
    case 'unsupported': return t('gallery.upload.reason_unsupported')
  }
}

export function errorReason(err: unknown, e?: ReasonContext): string {
  if (isNetworkError(err)) return t('gallery.upload.reason_network')
  if (!(err instanceof UploadHttpError)) {
    // ``api.post`` (the default album) throws "API <status>: …".
    const m = err instanceof Error ? err.message.match(/\b(4\d\d|5\d\d)\b/) : null
    if (!m) return t('gallery.upload.reason_network')
    err = new UploadHttpError(Number(m[1]))
  }
  const { status, code, params } = err as UploadHttpError
  if (code === 'STORAGE_FULL' || status === 507) return t('gallery.upload.reason_storage_full')
  if (code === 'PAYLOAD_TOO_LARGE' || status === 413) return tooLargeReason(e)
  if (code === 'VIDEO_TOO_LONG') {
    const seconds = Number(params?.duration_s)
    const maxS = Number(params?.max_s)
    return tooLongReason(
      Number.isFinite(seconds) ? seconds : 0,
      Number.isFinite(maxS) && maxS > 0 ? maxS : HOUSEHOLD_VIDEO_MAX_SECONDS,
    )
  }
  if (code === 'TRANSCODE_QUEUE_FULL') return t('gallery.upload.reason_queue_full')
  if (code === 'IMAGE_UNREADABLE' || code === 'UNPROCESSABLE' || status === 422) {
    return t('gallery.upload.reason_unreadable')
  }
  if (code === 'UNSUPPORTED_MEDIA_TYPE' || status === 415) return t('gallery.upload.reason_unsupported')
  if (status === 401 || status === 403) return t('gallery.upload.reason_forbidden')
  if (status === 404) return t('gallery.upload.reason_album_gone')
  if (status === 429) return t('gallery.upload.reason_busy')
  return t('gallery.upload.reason_server')
}

// ─── Screen Wake Lock + leaving the page ─────────────────────────────────

interface WakeLockSentinelLike { release: () => Promise<void>; released?: boolean }
let wakeLock: WakeLockSentinelLike | null = null
let wakeWanted = false

async function takeWakeLock(): Promise<void> {
  const nav = (typeof navigator === 'undefined' ? null : navigator) as
    (Navigator & { wakeLock?: { request: (k: 'screen') => Promise<WakeLockSentinelLike> } }) | null
  if (!nav?.wakeLock || (wakeLock && !wakeLock.released)) return
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
  try {
    wakeLock = await nav.wakeLock.request('screen')
    // Lost between asking and getting it.
    if (!wakeWanted) { void releaseWakeLock() }
  } catch {
    // Battery saver, an iframe without permission, an old browser: the
    // upload carries on, the screen may just sleep.
    wakeLock = null
  }
}

async function releaseWakeLock(): Promise<void> {
  const held = wakeLock
  wakeLock = null
  try { await held?.release() } catch { /* already gone */ }
}

/** Keep the screen on while files are going up (not while paused). */
export function syncWakeLock(): void {
  const want = isBusy.value && !paused.value
  wakeWanted = want
  if (want) void takeWakeLock()
  else void releaseWakeLock()
}

effect(() => syncWakeLock())

if (typeof document !== 'undefined') {
  // The browser drops the lock when the page is hidden: take it again.
  // A phone that switched apps comes back here: carry on.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return
    if (wakeWanted) void takeWakeLock()
    pump()
  })
}
if (typeof window !== 'undefined') {
  window.addEventListener('online', () => pump())
  window.addEventListener('beforeunload', (ev) => {
    if (!isBusy.value) return
    ev.preventDefault()
    // Older browsers want a string; they show their own words anyway.
    ev.returnValue = ''
  })
}
