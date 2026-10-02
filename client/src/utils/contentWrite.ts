/**
 * Space content writes that may be held for review (§4.3 "Reviewed").
 *
 * When a space feature is MODERATED, a member's new item — or their
 * edit / delete of someone else's — isn't saved: the host answers
 * **202** ``{queued: true, item_id, feature, action, entity, target_id}``
 * instead of the usual 200 / 201 / 204. That body is NOT the created or
 * updated object, so every write surface runs its response through
 * here:
 *
 * ```ts
 * const res = await contentWrite<Post>(api.post(url, body), { spaceId })
 * if (res.queued) return            // toast shown, pending strip refreshed
 * use(res.data)                     // the normal 2xx body
 * ```
 *
 * A queued answer shows the "Submitted for review — a moderator will
 * look at it" toast and refetches the author's pending items
 * (``store/moderationMine``). The ``api`` client parses a 202's JSON body
 * like any 2xx, so the ``queued: true`` marker is what tells the two
 * apart — no status plumbing needed.
 */
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { refreshModerationMine } from '@/store/moderationMine'

/** The body of a 202 for a write held for review. */
export interface QueuedWrite {
  queued: true
  item_id: string
  feature: string
  action: string
  entity?: string | null
  target_id?: string | null
}

export type ContentWriteResult<T> =
  | { queued: false; data: T }
  | { queued: true; item: QueuedWrite }

export interface ContentWriteOptions {
  /** The space written to — its pending strip is refreshed on a queue. */
  spaceId?: string | null
  /** ``false`` leaves the toast to the caller. Default ``true``. */
  toast?: boolean
}

/** Is ``body`` a "held for review" answer? */
export function isQueuedWrite(body: unknown): body is QueuedWrite {
  return !!body && typeof body === 'object' && (body as { queued?: unknown }).queued === true
}

/** Tell the author their write waits for a moderator, and refresh
 *  their pending items. */
export function announceQueued(opts: ContentWriteOptions = {}): void {
  if (opts.toast !== false) showToast(t('moderation.submitted'), 'info')
  if (opts.spaceId) void refreshModerationMine(opts.spaceId)
}

/** Classify an already-received body (see ``contentWrite``). */
export function classifyWrite<T>(body: unknown, opts: ContentWriteOptions = {}): ContentWriteResult<T> {
  if (isQueuedWrite(body)) {
    announceQueued(opts)
    return { queued: true, item: body }
  }
  return { queued: false, data: body as T }
}

/** Await a space write and classify its answer: ``{queued: true}``
 *  (toast + pending-strip refresh done) or ``{queued: false, data}``.
 *  Errors propagate unchanged. */
export async function contentWrite<T>(
  request: Promise<unknown>, opts: ContentWriteOptions = {},
): Promise<ContentWriteResult<T>> {
  return classifyWrite<T>(await request, opts)
}
