/**
 * uploadErrors — friendlier message for upload failures.
 *
 * Composer / DM / gallery / highlight / bazaar all surface raw exception
 * text on upload failures today (e.g. ``"Upload failed: TypeError:
 * Failed to fetch"``). That reads as "the app is broken" rather than
 * "your file is too big" or "we lost the network". Map the common
 * shapes to user-facing copy here so every caller can share the same
 * lookup.
 *
 * The classifier is heuristic: it inspects the error message + an
 * optional HTTP status (callers that throw ``new Error("Upload failed
 * (413)…")`` get richer matches). Falls back to a generic line if
 * nothing matches.
 */

import { t } from '@/i18n/i18n'

/** An upload the server (or the client's own size guard) refused, with
 *  its HTTP status and — when the body was the canonical
 *  ``{"error": {"code": …}}`` shape — its stable error code. Throw this
 *  instead of an ``Error`` carrying the raw response text: that text is
 *  JSON and English, never something to show a person. */
export class UploadHttpError extends Error {
  readonly status: number
  readonly code: string | null
  /** The refusal's flat ``error.params`` (e.g. a video's length). */
  readonly params: Record<string, unknown>
  constructor(status: number, code: string | null = null, params: Record<string, unknown> = {}) {
    super(`Upload failed (${status})${code ? `: ${code}` : ''}`)
    this.name = 'UploadHttpError'
    this.status = status
    this.code = code
    this.params = params
  }
}

/** Build an :class:`UploadHttpError` from an XHR's status + body. */
export function uploadHttpError(status: number, responseText: string): UploadHttpError {
  let code: string | null = null
  let params: Record<string, unknown> = {}
  try {
    const body = JSON.parse(responseText) as { error?: unknown }
    const err = body?.error as { code?: unknown; params?: unknown } | undefined
    if (err && typeof err === 'object' && typeof err.code === 'string') code = err.code
    if (err && typeof err === 'object' && err.params && typeof err.params === 'object') {
      params = err.params as Record<string, unknown>
    }
  } catch {
    code = null
  }
  return new UploadHttpError(status, code, params)
}

/** Error codes that mean "this file can't be used here". */
const BAD_FILE_CODES = new Set(['IMAGE_UNREADABLE', 'UNPROCESSABLE', 'UNSUPPORTED_MEDIA_TYPE'])

export interface UploadErrorContext {
  /** Optional file the user picked — surfaces in size/type messages. */
  file?: File
}

export function describeUploadError(
  err: unknown,
  ctx: UploadErrorContext = {},
): string {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  const fileName = ctx.file?.name ?? ''
  const sizeMb = ctx.file ? Math.round(ctx.file.size / (1024 * 1024)) : null
  // Prefer name+size in the lead so the user knows *which* upload
  // failed when they're picking several files in a row.
  const lead = fileName ? t('upload.lead_file', { name: fileName }) : t('upload.lead')

  // Network failures: TypeError on Failed to fetch / NetworkError /
  // explicit "Network error" string from the gallery's xhr path.
  if (/network|failed to fetch|err_network|networkerror/i.test(raw)) {
    return t('upload.network', { lead })
  }

  // Status-coded errors. The gallery xhr throws "Upload failed
  // (413): …"; api.ts throws "API 413: <path>".
  // ``UploadHttpError`` carries its status and stable code directly.
  const coded = err instanceof UploadHttpError ? err : null
  const statusMatch = raw.match(/\b(4\d\d|5\d\d)\b/)
  const status = coded ? coded.status : statusMatch ? Number(statusMatch[1]) : null
  const code = coded?.code ?? null

  if (code === 'STORAGE_FULL' || status === 507) {
    return t('upload.storage_full', { lead })
  }
  if (code === 'PAYLOAD_TOO_LARGE') {
    const detail = sizeMb != null ? ` (${sizeMb} MB)` : ''
    return t('upload.too_large', { lead: lead + detail })
  }
  if ((code !== null && BAD_FILE_CODES.has(code)) || status === 422) {
    return t('upload.bad_type', { lead })
  }

  if (status === 401 || status === 403) {
    return t('upload.forbidden', { lead })
  }
  if (status === 413) {
    const detail = sizeMb != null ? ` (${sizeMb} MB)` : ''
    return t('upload.too_large', { lead: lead + detail })
  }
  if (status === 415) {
    return t('upload.bad_type', { lead })
  }
  if (status === 429) {
    return t('upload.rate_limited', { lead })
  }
  if (status && status >= 500) {
    return t('upload.server', { lead })
  }

  // Last-ditch: include the raw message but trim noise — never a raw
  // response body (JSON / English server text) or a bare status line.
  if (coded || /[{}]/.test(raw)) return t('upload.server', { lead })
  const trimmed = raw.replace(/^Error:\s*/i, '').slice(0, 140)
  return trimmed ? `${lead} — ${trimmed}` : `${lead}.`
}
