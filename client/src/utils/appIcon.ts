/**
 * Which app icon sources the SPA will put in an `<img>`.
 *
 * Only a self-contained `data:image/…` URI. A remote `http(s)` icon from
 * the operator's catalog or an app manifest would make every viewer's
 * browser fetch from that host (and the SPA CSP's `img-src` has no
 * `https:` any more, so it would not load anyway). A relative path (e.g.
 * `"icon.svg"`) would resolve against the SPA origin and 404 — app bundles
 * are served from a sandboxed opaque origin. Either way the caller renders
 * its placeholder instead of a broken image. The published catalog ships
 * its icons as `data:` SVGs.
 */
export function safeIconSrc(icon: string | null | undefined): string | null {
  if (!icon) return null
  return /^data:image\//i.test(icon) ? icon : null
}
