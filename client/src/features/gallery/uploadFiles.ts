/**
 * uploadFiles — what the gallery upload sheet knows about a file before
 * it sends a byte: its media type, whether the server can take it, and
 * whether this browser can draw a preview of it.
 *
 * Also turns a drop into files, folders included (desktop: drop a whole
 * holiday folder onto the gallery).
 *
 * The server stores every upload in one format (WebP ≤ 2560 px photos,
 * VP9/Opus WebM ≤ 1920 px videos — ``socialhome/domain/media_constraints.py``);
 * these checks only spare a round trip for a file it would refuse.
 *
 * The household gallery keeps videos up to 5 minutes (a space keeps the
 * protocol's 60 s): the length is read from the file here, before a byte
 * is sent, and a longer video is refused with its length — never cut.
 */
import { formatLocale } from '@/i18n/i18n'

/** The server's cap on a photo, or on a space album's video
 *  (``GALLERY_MAX_UPLOAD_BYTES``). */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024
/** A household album's video: 5 minutes of 4K phone video
 *  (``GALLERY_VIDEO_MAX_UPLOAD_BYTES``). */
export const HOUSEHOLD_VIDEO_MAX_BYTES = 2 * 1024 * 1024 * 1024
/** The longest household-gallery video (``GALLERY_VIDEO_MAX_SECONDS``). */
export const HOUSEHOLD_VIDEO_MAX_SECONDS = 300

/** Types the server turns into a WebP photo. */
const PHOTO_TYPES: Record<string, string> = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp',
  gif: 'image/gif', heic: 'image/heic', heif: 'image/heic',
}
/** Types the server transcodes to a WebM video. */
const VIDEO_TYPES: Record<string, string> = {
  mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', webm: 'video/webm',
}
const PHOTO_MIMES = new Set([...Object.values(PHOTO_TYPES), 'image/heif', 'image/jpg'])
const VIDEO_MIMES = new Set(Object.values(VIDEO_TYPES))

/** The file picker's ``accept`` — the same list, plus the extensions a
 *  desktop OS may not map to a type (HEIC on Windows / Linux). */
export const ACCEPT = [
  ...new Set([...Object.values(PHOTO_TYPES), ...Object.values(VIDEO_TYPES)]),
  '.heic', '.heif',
].join(',')

export type FileKind = 'photo' | 'video'
export type FileProblem = 'too_large' | 'unsupported' | 'empty' | 'too_long'

export interface FileCheck {
  kind: FileKind
  /** The type to send — the file's own when the browser knows it, else
   *  the one its extension names (a ``.heic`` from a folder often has
   *  none, and the server reads a type-less body as a video). */
  mime: string
  /** Why it can't go up as it is — ``null`` when it can. */
  problem: FileProblem | null
}

function extension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot + 1).toLowerCase()
}

/** The largest file of ``kind`` an album takes (``household``: a
 *  household album, whose videos may be 5 minutes long). */
export function maxBytes(kind: FileKind, household: boolean): number {
  return kind === 'video' && household ? HOUSEHOLD_VIDEO_MAX_BYTES : MAX_UPLOAD_BYTES
}

/** Classify ``file`` before upload (into a household album when
 *  ``household``, else a space album). */
export function checkFile(file: File, household = false): FileCheck {
  const ext = extension(file.name)
  const own = (file.type || '').toLowerCase()
  let kind: FileKind | null = null
  let mime = ''
  if (PHOTO_MIMES.has(own)) { kind = 'photo'; mime = own === 'image/heif' ? 'image/heic' : own === 'image/jpg' ? 'image/jpeg' : own }
  else if (VIDEO_MIMES.has(own)) { kind = 'video'; mime = own }
  else if (!own || own === 'application/octet-stream') {
    if (PHOTO_TYPES[ext]) { kind = 'photo'; mime = PHOTO_TYPES[ext] }
    else if (VIDEO_TYPES[ext]) { kind = 'video'; mime = VIDEO_TYPES[ext] }
  }
  if (kind === null) {
    return { kind: own.startsWith('video/') ? 'video' : 'photo', mime: own, problem: 'unsupported' }
  }
  if (file.size === 0) return { kind, mime, problem: 'empty' }
  if (file.size > maxBytes(kind, household)) return { kind, mime, problem: 'too_large' }
  return { kind, mime, problem: null }
}

/** Is a video of ``seconds`` longer than ``max``? Whole seconds, the way
 *  a phone shows a length: 5:00.9 is five minutes, 5:01 is not
 *  (``video_fits`` on the server). */
export function videoTooLong(seconds: number, max = HOUSEHOLD_VIDEO_MAX_SECONDS): boolean {
  return Math.floor(seconds) > max
}

/** "7:12" (or "1:02:03") — a video length the way a phone shows it. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = String(total % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

/** Give up on reading a length after this long (a huge file on a slow
 *  phone); the server checks it again anyway. */
export const DURATION_TIMEOUT_MS = 15_000

/**
 * The length of a video file in seconds, read by the browser from the
 * file itself (only its header is read, nothing is uploaded) — or
 * ``null`` when this browser can't tell (a codec it can't open, a broken
 * file, too slow). ``null`` lets the file go up: the server reads the
 * length again and refuses a long one with the same message.
 */
export function readVideoDuration(file: File, timeoutMs = DURATION_TIMEOUT_MS): Promise<number | null> {
  if (typeof document === 'undefined' || typeof URL.createObjectURL !== 'function') {
    return Promise.resolve(null)
  }
  return new Promise((resolve) => {
    const video = document.createElement('video')
    const url = URL.createObjectURL(file)
    let settled = false
    const done = (value: number | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      video.removeAttribute('src')
      video.load()
      URL.revokeObjectURL(url)
      resolve(value)
    }
    const timer = setTimeout(() => done(null), timeoutMs)
    video.preload = 'metadata'
    video.muted = true
    video.onloadedmetadata = () => {
      const d = video.duration
      done(Number.isFinite(d) && d > 0 ? d : null)
    }
    video.onerror = () => done(null)
    video.src = url
  })
}

/** Can this browser draw ``file`` in an ``<img>``? HEIC only in Safari —
 *  never try elsewhere (a broken image per row, and wasted decodes);
 *  the row shows a placeholder and the server converts it. */
export function canPreview(file: File, check: FileCheck): boolean {
  if (check.kind !== 'photo' || check.problem === 'unsupported') return false
  if (check.mime === 'image/heic') return heicDecodes()
  return true
}

let heicCache: boolean | null = null
function heicDecodes(): boolean {
  if (heicCache === null) {
    const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent
    // Safari (macOS / iOS) draws HEIC; Chrome, Firefox and Edge don't.
    heicCache = /Safari\//.test(ua) && !/Chrome\/|Chromium\/|CriOS\/|Edg\/|Firefox\/|FxiOS\//.test(ua)
  }
  return heicCache
}

/** The file to put in the form: the original, re-typed when the browser
 *  gave it no (or a generic) type, so the server sees what it is. */
export function withType(file: File, mime: string): File {
  if (!mime || file.type === mime) return file
  return new File([file], file.name, { type: mime, lastModified: file.lastModified })
}

// ─── Drops (files and folders) ──────────────────────────────────────────

/** Minimal shape of the (non-standard but universal) entries API. */
interface Entry {
  isFile: boolean
  isDirectory: boolean
  name: string
  file?: (ok: (f: File) => void, err: (e: unknown) => void) => void
  createReader?: () => {
    readEntries: (ok: (es: Entry[]) => void, err: (e: unknown) => void) => void
  }
}

/** A folder drop is cut off past this many files, so a dropped photo
 *  library can't freeze the tab before the sheet even opens. */
export const MAX_DROP_FILES = 5000

async function walk(entry: Entry, out: File[]): Promise<void> {
  if (out.length >= MAX_DROP_FILES) return
  if (entry.isFile && entry.file) {
    // Hidden files (``.DS_Store``) are never photos.
    if (entry.name.startsWith('.')) return
    const f = await new Promise<File | null>((ok) => entry.file!(ok, () => ok(null)))
    if (f) out.push(f)
    return
  }
  if (entry.isDirectory && entry.createReader) {
    const reader = entry.createReader()
    // ``readEntries`` hands out a directory in chunks (100 in Chrome)
    // until it returns an empty one.
    for (;;) {
      const chunk = await new Promise<Entry[]>((ok) => reader.readEntries(ok, () => ok([])))
      if (chunk.length === 0) break
      for (const e of chunk) await walk(e, out)
      if (out.length >= MAX_DROP_FILES) return
    }
  }
}

/** Does this drag carry files (not a text or link drag)? */
export function dragHasFiles(dt: DataTransfer | null): boolean {
  return !!dt && Array.from(dt.types ?? []).includes('Files')
}

/** Every file in a drop, walking into dropped folders. */
export async function filesFromDrop(dt: DataTransfer): Promise<File[]> {
  const items = Array.from(dt.items ?? [])
  // Grab the entries synchronously: the DataTransfer empties after the
  // drop handler returns.
  const entries = items
    .filter(i => i.kind === 'file')
    .map(i => (i as DataTransferItem & { webkitGetAsEntry?: () => Entry | null })
      .webkitGetAsEntry?.() ?? null)
  if (entries.length === 0 || entries.some(e => e === null)) {
    return Array.from(dt.files ?? [])
  }
  const out: File[] = []
  for (const e of entries as Entry[]) await walk(e, out)
  return out
}

/** "1.9 MB" in the reader's locale. */
export function formatSize(bytes: number): string {
  const mb = bytes / (1024 * 1024)
  const fmt = (n: number, digits: number) =>
    new Intl.NumberFormat(formatLocale(), { maximumFractionDigits: digits }).format(n)
  if (mb >= 1) return `${fmt(mb, mb >= 10 ? 0 : 1)} MB`
  return `${fmt(Math.max(1, Math.round(bytes / 1024)), 0)} KB`
}
