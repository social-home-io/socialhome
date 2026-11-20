/**
 * The GFS address a "connect to this GFS" link aims the connect flow at
 * (``/connections?connect_gfs=…``, from a space's GFS notice). The flow
 * only links to that address and shows its host — the pairing itself
 * still needs the code from the GFS's own page — so the address is held
 * to a plain ``http(s)`` origin + path: no credentials, query or fragment.
 */
export function gfsConnectTarget(raw: string | null | undefined): string | null {
  if (!raw) return null
  try {
    const url = new URL(raw)
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null
    if (url.username || url.password || url.search || url.hash) return null
    return url.href.replace(/\/+$/, '')
  } catch {
    return null
  }
}
