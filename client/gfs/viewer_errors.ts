/* Error copy shared by the GFS public viewers (highlight + moments).
 *
 * The GFS answers every non-success state of the anonymous streaming
 * routes — unknown author, unknown or withdrawn item, author's household
 * not connected, stream not brokered — with one uniform ``503``, so it
 * is not an author-presence oracle. The copy must not undo that: it
 * says "not available right now", never "offline" or "ended".
 */

export const UNAVAILABLE_COPY = 'This isn’t available right now.'

const BUSY_COPY = 'Too many viewers — try again in a minute.'

export function humanizeViewerError(msg: string | null | undefined): string {
  if (!msg) return 'Couldn’t connect.'
  // 503 is the uniform reply; 404/410 can only come from a stale session
  // poll or an older GFS — same neutral copy either way.
  if (/HTTP (404|410|503)\b/.test(msg)) return UNAVAILABLE_COPY
  if (/HTTP 429\b/.test(msg) || /backpressure/i.test(msg)) return BUSY_COPY
  return msg
}
