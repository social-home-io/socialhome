/**
 * ``cssUrl`` — a data-derived URL as a CSS ``url()`` value.
 *
 * ``url(${u})`` unquoted lets a URL containing ``)`` or ``,`` close
 * the function and append another background layer (a tracking
 * beacon). Quoting and escaping the string keeps it one token.
 */

/** ``url("…")`` with ``"``, ``\`` and line breaks escaped as CSS hex
 *  escapes. */
export function cssUrl(u: string): string {
  const body = u.replace(
    /["\\\n\r\f]/g,
    (c) => `\\${c.charCodeAt(0).toString(16)} `,
  )
  return `url("${body}")`
}
