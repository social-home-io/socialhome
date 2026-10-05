// Import token from its own module (NOT '@/store/auth') so api.ts has no
// static dependency on auth.ts — breaks the api↔auth import cycle.
import { token } from '@/store/token'
import { showToast } from '@/components/Toast'
import { accessNote } from '@/features/spaces/spaceAccess'
import { t } from '@/i18n/i18n'

// Logout handler, registered by store/auth at module load. Lets the 401 path
// clear the session without importing '@/store/auth' (which would re-form the
// api↔auth cycle, since auth imports api).
let _onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void): void {
  _onUnauthorized = fn
}

// Strip the leading ``/`` from a caller-supplied path so ``fetch``
// resolves it against ``document.baseURI`` (which the backend sets to
// the HA Supervisor ingress prefix when behind ingress, ``/``
// otherwise). An absolute ``/api/me`` would resolve against the
// document origin, bypassing ``<base href>`` and 404ing under ingress.
//
// Raw ``fetch`` / ``XMLHttpRequest`` sites outside this client (the
// gallery upload XHR, the shared ``UploadProgress`` helper) do the
// same thing inline — they just pass a relative URL with no leading
// slash. Either form works; the difference is whose code is doing the
// trim. See #303.
const _rel = (p: string): string => p.replace(/^\/+/, '')

/**
 * Read a fetch ``Response`` body as JSON when there is one, ``null``
 * otherwise. Specifically: 204 (No Content) and empty-body 200s.
 *
 * Why the gymnastics: ``res.json()`` on an empty body throws. Chrome
 * says ``"Unexpected end of JSON input"`` and we'd swallow that in
 * a generic ``catch`` upstream. Safari (mobile + desktop, both
 * WebKit) throws ``"The string did not match the expected pattern."``
 * — a stringly-typed error from ``JSON.parse('')`` that has no
 * obvious referent. That's the error a real user saw when clicking
 * Accept on a pending cross-household invite, because the
 * ``/api/remote_invites/{token}/accept`` endpoint returns 204 by
 * design (no payload to return; the accept just records state and
 * fans a federation event).
 *
 * Branch on ``status === 204`` first, then on ``content-length: 0``,
 * because Safari throws even before sniffing the body shape — we
 * have to *avoid* calling ``json()``, not catch its rejection.
 */
async function _parseJsonOrNull<T>(res: Response): Promise<T> {
  if (res.status === 204) return null as T
  const len = res.headers.get('content-length')
  if (len === '0') return null as T
  return res.json() as Promise<T>
}

/** The ``error`` object of a canonical ``{"error": {"code", "detail"}}``
 *  body, or ``null`` when the body is missing / not JSON / another shape
 *  (proxy errors, upstream 502s). Never throws. */
async function _parseErrorBody(
  res: Response,
): Promise<{ code?: unknown; detail?: unknown; [extra: string]: unknown } | null> {
  try {
    const body = await res.json() as { error?: unknown }
    if (body && typeof body === 'object'
        && body.error && typeof body.error === 'object') {
      return body.error as { code?: unknown; detail?: unknown }
    }
  } catch {
    // Non-JSON body.
  }
  return null
}

/**
 * Error thrown by ``ApiClient`` for non-2xx responses.
 *
 * Carries the HTTP status code (``e.status === 501`` to detect a
 * not-implemented endpoint, ``=== 404`` for missing data, etc.) and
 * — when the response body matches the canonical
 * ``{"error": {"code", "detail"}}`` shape from
 * :func:`socialhome.security.error_response` — the parsed ``code`` +
 * ``detail`` fields so call sites can branch on the machine-readable
 * code while showing the human-readable detail to the user.
 *
 * ``message`` defaults to the ``detail`` when present so the common
 * ``showToast(err.message)`` pattern lands the friendly text the
 * backend already provides (e.g. "Home Assistant has no picture for
 * this user.") instead of the bare ``"API 422: /api/..."`` string.
 */
export class ApiError extends Error {
  /** Machine-readable code from ``{"error": {"code": ...}}`` (e.g.
   *  ``"UNPROCESSABLE"``, ``"NOT_IMPLEMENTED"``). ``null`` if the
   *  response body wasn't in the canonical shape. */
  public readonly code: string | null
  /** Human-readable string safe to display in the UI. ``null`` if the
   *  body had no ``detail`` field. */
  public readonly detail: string | null
  /** Every other field of the ``error`` object — the machine-readable
   *  hints ``error_response(extra=…)`` merges in (``count``,
   *  ``current_version``, ``section``…). ``{}`` when there are none. */
  public readonly extra: Readonly<Record<string, unknown>>

  constructor(
    public readonly status: number,
    public readonly path: string,
    parsed?: { code?: unknown; detail?: unknown; [extra: string]: unknown } | null,
  ) {
    const code = typeof parsed?.code === 'string' ? parsed.code : null
    const detail = typeof parsed?.detail === 'string' ? parsed.detail : null
    // Prefer the backend's detail string as the ``Error.message`` so
    // call sites that do ``err.message`` (which is most of them — toast
    // strings, status banners) light up with the friendly text.
    // Fall back to the historic ``"API <status>: <path>"`` shape so a
    // body-less / non-JSON error still produces an actionable string
    // rather than ``""`` or ``undefined``.
    super(detail || `API ${status}: ${path}`)
    this.name = 'ApiError'
    this.code = code
    this.detail = detail
    this.extra = Object.fromEntries(
      Object.entries(parsed ?? {}).filter(([k]) => k !== 'code' && k !== 'detail'),
    )
  }
}

/** Per-request options shared by the JSON verbs. */
export interface RequestOptions {
  /** ``fetch``'s ``keepalive``: the request outlives the page — for the
   *  best-effort flush on ``pagehide`` / tab hidden (``sendBeacon``
   *  can't carry the ``Authorization`` header). */
  keepalive?: boolean
}

function extra(opts?: RequestOptions): RequestInit {
  return opts?.keepalive ? { keepalive: true } : {}
}

class ApiClient {
  private headers(): HeadersInit {
    return {
      'Content-Type': 'application/json',
      ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
    }
  }

  /** Centralised handler for ``fetch`` responses. Surface a "Session
   *  expired" toast before logging the user out so the page doesn't
   *  silently wipe to the login form. Same path applies to every
   *  verb (GET/POST/PUT/PATCH/DELETE/upload) so an expired token is
   *  handled identically no matter which method tripped the 401.
   *
   *  A 401 with **no token attached** is a different beast: it
   *  usually means an ingress-mode cold-start probe (haos) couldn't
   *  authenticate via the Supervisor headers. The App shell renders
   *  a dedicated "Ingress auth failed" page for that — we stay quiet
   *  here so the user doesn't see a misleading "Session expired"
   *  toast for a session that never existed. */
  private async _handle(res: Response, path: string): Promise<Response> {
    if (res.status === 401) {
      if (token.value !== null && !ApiClient._loggingOut) {
        ApiClient._loggingOut = true
        showToast(t('api.session_expired'), 'info')
        // Call the registered logout (auth registers it at module load) rather
        // than importing '@/store/auth' here — that import would re-form the
        // api↔auth cycle (auth already imports api). DI keeps the graph acyclic.
        _onUnauthorized?.()
      }
      // A real ``ApiError`` carrying the status + parsed code, so callers
      // can branch on ``err.status === 401`` (the login form shows its
      // translated "invalid credentials" copy) — a bare ``Error`` here
      // lost the status and leaked the English "Unauthorized" text.
      throw new ApiError(401, path, await _parseErrorBody(res))
    }
    if (!res.ok) {
      // Try to parse the canonical ``{"error": {"code", "detail"}}``
      // body so the thrown ``ApiError`` carries the friendly detail
      // the backend already provides. Falls back to a body-less
      // ``ApiError`` when the response isn't JSON (proxy errors,
      // upstream 502s, etc.) — callers still get the historic
      // ``"API <status>: <path>"`` message in that case.
      let parsed = await _parseErrorBody(res)
      if (parsed) {
        if (parsed.code === 'ACCESS_ADMIN_ONLY') {
          // §4.3: an ADMIN_ONLY feature refused the write. Every
          // surface toasts ``err.message`` — give it the translated
          // "only admins can …" note for that feature.
          const feature = (parsed as { feature?: unknown }).feature
          parsed = { ...parsed, detail: accessNote(String(feature ?? '')) }
        } else if (parsed.code === 'HOST_UNREACHABLE') {
          // A forward to the space's host went nowhere — nothing was
          // queued, so never say "sent".
          const reason = (parsed as { reason?: unknown }).reason
          parsed = {
            ...parsed,
            detail: reason === 'unknown_host'
              ? t('space.host.unknown')
              : t('space.host.unreachable'),
          }
        } else if (parsed.code === 'HOST_TOO_OLD') {
          // The space's host is too old for what this household asked:
          // v_43 review submissions, or a v_47 forwarded role change
          // (``feature: 'role_change'``) — the toast says why, translated.
          const feature = (parsed as { feature?: unknown }).feature
          parsed = {
            ...parsed,
            detail: feature === 'role_change'
              ? t('space.member.role_host_too_old')
              : t('moderation.error.host_too_old'),
          }
          }
      }
      throw new ApiError(res.status, path, parsed)
    }
    return res
  }

  /** Latched flag so a burst of failed calls during logout doesn't
   *  fan out into a stack of "Session expired" toasts. Reset on the
   *  next successful auth (handled by the auth store on token set). */
  private static _loggingOut = false
  static resetLoggedOut(): void { ApiClient._loggingOut = false }

  async get<T = any>(path: string, params?: Record<string, string>): Promise<T> {
    const url = params ? `${path}?${new URLSearchParams(params)}` : path
    const res = await this._handle(
      await fetch(_rel(url), { headers: this.headers() }),
      path,
    )
    return res.json()
  }

  async post<T = any>(path: string, body?: unknown, opts?: RequestOptions): Promise<T> {
    const res = await this._handle(
      await fetch(_rel(path), {
        ...extra(opts),
        method: 'POST', headers: this.headers(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
      path,
    )
    return _parseJsonOrNull<T>(res)
  }

  async put<T = any>(path: string, body?: unknown, opts?: RequestOptions): Promise<T> {
    const res = await this._handle(
      await fetch(_rel(path), {
        ...extra(opts),
        method: 'PUT', headers: this.headers(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
      path,
    )
    return _parseJsonOrNull<T>(res)
  }

  async patch<T = any>(path: string, body?: unknown, opts?: RequestOptions): Promise<T> {
    const res = await this._handle(
      await fetch(_rel(path), {
        ...extra(opts),
        method: 'PATCH', headers: this.headers(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
      path,
    )
    return _parseJsonOrNull<T>(res)
  }

  /** Resolves to the parsed JSON body when the response is JSON (the
   *  timetable routes answer a DELETE with the updated resource),
   *  ``null`` otherwise — callers that don't care simply ignore it. */
  async delete<T = void>(path: string, opts?: RequestOptions): Promise<T> {
    const res = await this._handle(
      await fetch(_rel(path), { ...extra(opts), method: 'DELETE', headers: this.headers() }),
      path,
    )
    // Only a JSON response is parsed (older DELETE routes answer with
    // an empty / plain body); a malformed JSON body still rejects.
    if (!(res.headers.get('content-type') ?? '').includes('json')) return null as T
    return _parseJsonOrNull<T>(res)
  }

  /** POST a raw (non-JSON) body with an explicit ``Content-Type`` — an
   *  ``.ics`` file as ``text/calendar``, an image as ``image/jpeg``.
   *  Same base-relative URL + 401 handling as every other verb, so a
   *  file upload never needs a hand-rolled ``fetch``. */
  async postRaw<T = any>(path: string, body: BodyInit, contentType: string): Promise<T> {
    const res = await this._handle(
      await fetch(_rel(path), {
        method: 'POST',
        headers: {
          'Content-Type': contentType,
          ...(token.value ? { Authorization: `Bearer ${token.value}` } : {}),
        },
        body,
      }),
      path,
    )
    return _parseJsonOrNull<T>(res)
  }

  async upload<T = any>(path: string, body: FormData): Promise<T> {
    const headers: HeadersInit = token.value
      ? { Authorization: `Bearer ${token.value}` }
      : {}
    const res = await this._handle(
      await fetch(_rel(path), {
        method: 'POST',
        headers,
        body,
      }),
      path,
    )
    return res.json()
  }
}

export const api = new ApiClient()
export const _resetApiLoggedOut = ApiClient.resetLoggedOut
