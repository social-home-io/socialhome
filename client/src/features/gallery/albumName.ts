/**
 * An album's shown name. Two albums carry a name the server can't say in
 * the reader's language, so the app says it instead:
 *
 * - the auto "Posts" album (``is_system``) — one English name on the
 *   server;
 * - the household's default album while nobody renamed it
 *   (``default_name``) — stored in the first uploader's language, shown
 *   as each reader's own word for "Photos".
 */
import { t } from '@/i18n/i18n'

export interface NamedAlbum {
  name: string
  is_system?: boolean
  /** The household's default album, still under its default name. */
  default_name?: boolean
}

export function albumDisplayName(a: NamedAlbum): string {
  if (a.is_system) return t('gallery.system_name')
  if (a.default_name) return t('gallery.upload.default_album')
  return a.name
}
