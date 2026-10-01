/**
 * undoableDelete — "delete now, with Undo" as a DEFERRED commit.
 *
 * The item disappears at once and a toast offers Undo; the server
 * DELETE is only sent once that toast expires (its timer ran out, or
 * newer toasts pushed it off the stack). Undo therefore never has to
 * re-create anything — re-creating would mint a new id and drop the
 * comments, attachments and authorship hanging off the old one.
 *
 * ``pendingDeletes`` holds every id that is hidden but not yet
 * committed. Views filter on it, so a WS frame or a refetch that
 * brings the row back meanwhile can't show it. Ids are ref-counted:
 * two overlapping entries for the same id (delete the row, then
 * "Clear all" while its toast is up) can't un-hide each other.
 *
 * - A 404 on commit counts as success: someone else already removed it.
 * - Any other failure restores the item and shows an error toast.
 * - Best effort on leaving: when the page is hidden (``pagehide``, or
 *   the tab goes to the background) every pending entry commits at
 *   once with ``keepalive`` fetches and its toast is dismissed. The
 *   browser may still drop a request — nothing here can guarantee it.
 */
import { signal } from '@preact/signals'
import { showToast, dismissToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

export interface CommitOptions {
  /** Set by the leave-the-page flush: send with ``fetch`` keepalive and
   *  without batching, the page may be gone a moment later. */
  keepalive?: boolean
}

export interface UndoableDeleteOptions {
  /** Ids hidden while the delete is pending (see ``pendingDeletes``). */
  ids: readonly string[]
  /** Toast text, e.g. "Deleted Milk". */
  message: string
  /** Sends the real delete. Runs once, when the Undo window closes. */
  commit: (opts: CommitOptions) => Promise<unknown>
  /** Extra local hiding beyond ``pendingDeletes`` (optional). */
  hide?: () => void
  /** Undo / failed-commit path: bring the item back (optional). */
  restore?: () => void
  /** Runs after the user pressed Undo (and ``restore``) — e.g. to put
   *  focus back on the restored row. Not called on a failed commit. */
  onUndone?: () => void
}

const counts = new Map<string, number>()

/** Ids hidden by an uncommitted ``undoableDelete``. */
export const pendingDeletes = signal<ReadonlySet<string>>(new Set())

function publish() {
  pendingDeletes.value = new Set(counts.keys())
}

function mark(ids: readonly string[]) {
  for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1)
  publish()
}

function unmark(ids: readonly string[]) {
  for (const id of ids) {
    const n = (counts.get(id) ?? 0) - 1
    if (n > 0) counts.set(id, n)
    else counts.delete(id)
  }
  publish()
}

interface Entry {
  ids: readonly string[]
  commit: (opts: CommitOptions) => Promise<unknown>
  restore?: () => void
  onUndone?: () => void
  toastId: number
  /** ``true`` once undone or committed — later triggers no-op. */
  settled: boolean
}

const entries = new Set<Entry>()

function isNotFound(err: unknown): boolean {
  return (err as { status?: unknown } | null)?.status === 404
}

async function runCommit(entry: Entry, opts: CommitOptions = {}): Promise<void> {
  if (entry.settled) return
  entry.settled = true
  entries.delete(entry)
  try {
    await entry.commit(opts)
  } catch (err) {
    if (!isNotFound(err)) {
      entry.restore?.()
      showToast(
        t('organize.undo.commit_failed', {
          error: String((err as Error)?.message ?? err),
        }),
        'error',
      )
    }
  } finally {
    unmark(entry.ids)
  }
}

function undo(entry: Entry) {
  if (entry.settled) return
  entry.settled = true
  entries.delete(entry)
  unmark(entry.ids)
  entry.restore?.()
  entry.onUndone?.()
}

export function undoableDelete(opts: UndoableDeleteOptions): void {
  const entry: Entry = {
    ids: opts.ids,
    commit: opts.commit,
    restore: opts.restore,
    onUndone: opts.onUndone,
    toastId: -1,
    settled: false,
  }
  entries.add(entry)
  mark(opts.ids)
  opts.hide?.()
  entry.toastId = showToast(opts.message, 'info', {
    action: { label: t('common.undo'), onClick: () => undo(entry) },
    onExpire: () => { void runCommit(entry) },
  })
}

/** Commit every pending delete now — best effort, fire-and-forget,
 *  with keepalive requests — and drop their now-meaningless toasts. */
export function flushPendingDeletes(): void {
  for (const entry of [...entries]) {
    dismissToast(entry.toastId)
    void runCommit(entry, { keepalive: true })
  }
}

/** Forget every pending entry without committing (logout, tests). */
export function resetPendingDeletes(): void {
  for (const entry of entries) dismissToast(entry.toastId)
  entries.clear()
  counts.clear()
  publish()
}

if (typeof window !== 'undefined') {
  window.addEventListener('pagehide', flushPendingDeletes)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushPendingDeletes()
  })
}
