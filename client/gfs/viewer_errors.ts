/* Error copy shared by the GFS public viewers (highlight + moments).
 *
 * The GFS answers every non-success state of the anonymous streaming
 * routes — unknown author, unknown or withdrawn item, author's household
 * not connected, stream not brokered — with one uniform ``503``, so it
 * is not an author-presence oracle. The copy must not undo that: it
 * says "not available right now", never "offline" or "ended".
 *
 * People only ever see one of these translated messages — never the raw
 * text of an exception or a status line ("Failed to fetch", "HTTP 500").
 * The visitor's own network failing reads "Couldn't connect."; anything
 * else that went wrong is the same neutral "not available".
 */
import { t } from './viewer_i18n'


export function humanizeViewerError(msg: string | null | undefined): string {
  if (!msg) return t('error.connect')
  // 503 is the uniform reply; 404/410 can only come from a stale session
  // poll or an older GFS — same neutral copy either way.
  if (/HTTP (404|410|503)\b/.test(msg)) return t('error.unavailable')
  if (/HTTP 429\b/.test(msg) || /backpressure/i.test(msg)) return t('error.busy')
  // fetch() rejecting (Chromium / Firefox / Safari wording), a timeout or
  // a 408: the request never got an answer.
  if (/failed to fetch|networkerror|load failed|network|timed? ?out|HTTP 408\b/i.test(msg)) {
    return t('error.connect')
  }
  return t('error.unavailable')
}
