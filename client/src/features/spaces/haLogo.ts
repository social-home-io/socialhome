/**
 * The Home Assistant logo as a ready-made space picture.
 *
 * The logo is bundled with the SPA (``assets/home-assistant/icon.png``,
 * the official 512 px icon from home-assistant/brands — see the
 * ``NOTICE.txt`` beside it for source and terms), never fetched from a
 * third party. Choosing it uploads those bytes through the ordinary space
 * icon upload (``POST /api/spaces/{id}/icon``), exactly like a picture the
 * person picked: the server re-encodes it to the 256 px WebP every space
 * icon is, and other households receive it as that space's picture — no
 * special value travels between households.
 *
 * Vite resolves the import against the module URL (``base: './'``), so
 * the asset is fetched under the ingress prefix in every mode.
 */
import haLogoUrl from '@/assets/home-assistant/icon.png'

export { haLogoUrl }

/** The bundled logo as a ``File``, ready for a ``FormData`` upload. */
export async function haLogoFile(): Promise<File> {
  const res = await fetch(haLogoUrl)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const blob = await res.blob()
  return new File([blob], 'home-assistant.png', { type: 'image/png' })
}
