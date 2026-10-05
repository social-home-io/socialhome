/**
 * safeHref — the one gate for a data-derived ``href`` (or a ``src`` used
 * as a link target). A URL that came from the server or another
 * household — a DM file's ``media_url``, a space quick link, a lightbox
 * download — must never become ``javascript:`` / ``data:`` / ``vbscript:``
 * in an anchor: one click would run script with the viewer's session.
 *
 * Allowed:
 *   * ``http:`` / ``https:`` (rebuilt by :func:`safeWebUrl`, so no
 *     embedded credentials);
 *   * an app path (``/spaces/…``) — one leading slash, never ``//`` or
 *     ``/\`` (protocol-relative to another host);
 *   * a base-relative media / API path (``api/…``) — the shape every
 *     local upload carries, which resolves against ``<base href>`` under
 *     ingress;
 *   * ``mailto:``;
 *   * ``blob:`` only when the caller opts in (``{ blob: true }``) for an
 *     object URL it minted itself (e.g. a lightbox preview).
 *
 * Anything else returns ``undefined`` so Preact omits the attribute.
 * The scheme is read the way a browser reads it: tabs / newlines are
 * dropped anywhere and leading control chars / spaces are ignored, so
 * ``java\tscript:`` is still ``javascript:``.
 */
import { safeWebUrl } from './linkPreview'

// Browsers strip ASCII tab / LF / CR everywhere, then leading and
// trailing C0 controls + space, before parsing the scheme.
// eslint-disable-next-line no-control-regex
const _STRIPPED = /[\t\n\r]/g
// eslint-disable-next-line no-control-regex
const _EDGE = /^[\u0000- ]+|[\u0000- ]+$/g
const _SCHEME = /^([a-z][a-z0-9+.-]*):/i

export interface SafeHrefOptions {
  /** Also allow ``blob:`` object URLs. */
  blob?: boolean
}

export function safeHref(
  raw: string | null | undefined,
  opts: SafeHrefOptions = {},
): string | undefined {
  if (typeof raw !== 'string') return undefined
  const value = raw.replace(_STRIPPED, '').replace(_EDGE, '')
  if (!value) return undefined
  const scheme = _SCHEME.exec(value)?.[1].toLowerCase()
  if (scheme === 'http' || scheme === 'https') return safeWebUrl(value) ?? undefined
  if (scheme === 'mailto') return value
  if (scheme === 'blob') return opts.blob ? value : undefined
  if (scheme !== undefined) return undefined
  if (value.startsWith('/')) {
    return value.startsWith('//') || value.startsWith('/\\') ? undefined : value
  }
  if (value.startsWith('api/')) return value
  return undefined
}
