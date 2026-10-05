/**
 * PagesView — the Markdown wiki (§23.58 / §23.72): list, viewer, editor,
 * history. Rendered by the household Pages tab and by a space's Pages tab;
 * a {@link PageScope} says which routes it uses and what the viewer may do.
 *
 * Viewer:
 *   - Renders Markdown via {@link MarkdownView} with a sticky table of
 *     contents on desktop, and an "Edited by … · <when>" byline.
 *   - "History" opens the version drawer with an inline diff.
 *   - "Edit" is disabled while another user holds the household lock.
 *
 * Editor:
 *   - Inline title input + split-pane Markdown source / live preview on
 *     desktop; tab toggle on mobile. Toolbar with keyboard shortcuts.
 *   - Autosaves (title and body together) 1.5 s after the last keystroke;
 *     the status pill says "Saving… / Saved ✓ / Save failed".
 *   - Household only: takes an edit lock on open, refreshes it every 30 s
 *     and releases it on close. Space pages have no lock routes.
 *   - A 409 (someone saved meanwhile) opens the side-by-side conflict view:
 *     keep mine (saved over the newer version), keep theirs, or merge by
 *     hand.
 *   - Host-sequenced space pages (v_48): the space's host household orders
 *     every version. On a member household a save is stored here at once
 *     and proposed to the host — until the host answers, a "Saved here ·
 *     waiting for {host}" pill shows (viewer and editor) and the list card
 *     says "Not yet shared". WS ``page.sequenced`` refetches; a refusal
 *     shows a notice with its reason — ``gone`` (deleted at the host)
 *     offers "Save as new page".
 *   - Federated conflicts: edits the host could not merge are kept as
 *     versions beside the page. The viewer (and the editor) shows a banner
 *     — Resolve for writers, a note for everyone else; editing stays
 *     possible meanwhile. The resolve view (``PageConflictView``) keeps one
 *     version, or merges by hand in the editor; either posts
 *     ``resolve-conflict`` with the versions the user saw (``sides``). A WS
 *     ``page.conflict`` with ``federated`` refetches.
 *   - Held for review (§4.3 "Reviewed", ``scope.reviewed``): no autosave —
 *     every save would queue another item — one "Submit for review" sends
 *     the edit; the 202 toasts and refreshes the pending strip
 *     (``contentWrite``).
 *   - The viewer of a space page with the viewer's own edit or delete
 *     still waiting for a moderator says so ("Your edit to this page is
 *     waiting for review") — the pending strip only shows on the list.
 *     Read from ``store/moderationMine``, so it clears on the decision.
 */
import { useEffect, useRef } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import type { ComponentChildren } from 'preact'
import { api } from '@/api'
import { ws } from '@/ws'
import { formatLocale, t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { openReport } from '@/components/ReportDialog'
import { Button } from '@/components/Button'
import {
  MarkdownToolbar,
  useMarkdownShortcuts,
} from '@/components/MarkdownToolbar'
import { MarkdownView } from '@/components/MarkdownView'
import { NewPageDialog } from '@/components/NewPageDialog'
import { PageHistoryDrawer } from '@/components/PageHistoryDrawer'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { pendingMine } from '@/store/moderationMine'
import { contentWrite } from '@/utils/contentWrite'
import { extractHeadings } from '@/utils/markdown'
import { normaliseTimestamp, relativeDocsTime } from '@/utils/relativeTime'
import type { EditLock, Page } from '@/types'
import type { PageScope } from './scope'
import { PageConflictView, conflictMarkers, type ConflictPanel } from './PageConflictView'

interface ConflictData {
  mine: string
  theirs: string
  theirs_by: string
}

type SaveStatus = 'idle' | 'saving' | 'saved' | 'error'

/** Why the space's host refused this household's edit (WS
 *  ``page.sequenced``). */
type RefusalReason = 'access' | 'archived' | 'gone' | 'rate_limited' | 'bad_base'

const REFUSAL_REASONS: readonly RefusalReason[] = [
  'access', 'archived', 'gone', 'rate_limited', 'bad_base',
]
type SaveOutcome = 'saved' | 'queued' | 'conflict' | 'error'

/** A server timestamp as epoch ms — naive SQLite stamps are UTC. */
function tsMs(iso: string): number {
  return Date.parse(normaliseTimestamp(iso))
}

/** A server timestamp for a tooltip, in the viewer's locale. */
function tsTitle(iso: string): string {
  return new Date(tsMs(iso)).toLocaleString(formatLocale())
}

/** Autosave debounce after the last keystroke (title or body). */
const AUTOSAVE_MS = 1500

function errMessage(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

/** The ``ApiError.code`` of a failed request (``STALE``…), ``null`` for
 *  anything else. */
function errCode(err: unknown): string | null {
  const code = (err as { code?: unknown } | null)?.code
  return typeof code === 'string' ? code : null
}

function isStale(err: unknown): boolean {
  const e = err as { status?: unknown; message?: unknown } | null
  return e?.status === 409
    || String(e?.message ?? '').toLowerCase().includes('stale')
}

export function PagesView({ scope, header }: {
  scope: PageScope
  /** Above the list (an access note, the pending-review strip). */
  header?: ComponentChildren
}) {
  const pages = useSignal<Page[]>([])
  const viewing = useSignal<Page | null>(null)
  const editing = useSignal(false)
  const editContent = useSignal('')
  const editTitle = useSignal('')
  const loading = useSignal(true)
  const loadFailed = useSignal(false)
  const editLock = useSignal<EditLock | null>(null)
  const conflict = useSignal<ConflictData | null>(null)
  const showNew = useSignal(false)
  const showHistory = useSignal(false)
  const mobileView = useSignal<'edit' | 'preview'>('edit')
  const saveStatus = useSignal<SaveStatus>('idle')
  const submitting = useSignal(false)
  // Federated conflict resolve view: open (with the unsaved draft, if a
  // save ran into the conflict) or ``null``.
  const resolveView = useSignal<{ draft: string | null } | null>(null)
  // Merging a federated conflict by hand in the editor: the versions the
  // user saw. Saving then posts the resolution instead of a PATCH.
  const resolvingSides = useSignal<string[] | null>(null)
  const resolving = useSignal(false)
  // The host refused this household's last edit of a page (v_48).
  const refusal = useSignal<{ pageId: string; reason: RefusalReason } | null>(null)

  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const saveTimer = useRef<number | null>(null)
  const heartbeatTimer = useRef<number | null>(null)
  // The save on the wire, if any: a later save waits for it and is then
  // based on its answer — two saves racing would 409 against each other.
  const inFlight = useRef<Promise<SaveOutcome> | null>(null)
  // Latest-request-wins for ``viewPage``.
  const viewSeq = useRef(0)
  // The scope of the latest render, for the unmount flush.
  const scopeRef = useRef(scope)
  scopeRef.current = scope

  const itemPath = (id: string) => `${scope.base}/${id}`
  const tabIds = `sh-page-editor-${scope.key.replace(/[^a-z0-9-]/gi, '-')}`
  const reviewed = (p: Page | null) => !!p && scope.reviewed(p)

  /** Refresh the list without the loading state (a WS nudge). */
  const refreshList = () => {
    api.get(scope.base).then((rows: Page[]) => { pages.value = rows }).catch(() => {})
  }

  /** Re-read the open page (its conflict state included). */
  const refetchViewing = async (): Promise<Page | null> => {
    const open = viewing.value
    if (!open) return null
    try {
      const latest = await api.get(itemPath(open.id)) as Page
      if (viewing.value?.id !== open.id) return null
      viewing.value = latest
      pages.value = pages.value.map(p => p.id === latest.id
        ? { ...p, ...latest, in_conflict: !!latest.conflict } : p)
      return latest
    } catch {
      return null
    }
  }

  const loadList = () => {
    loading.value = true
    loadFailed.value = false
    api.get(scope.base).then((rows: Page[]) => {
      pages.value = rows
    }).catch(() => {
      loadFailed.value = true
    }).finally(() => { loading.value = false })
  }

  useEffect(() => {
    scope.loadPeople()
    loadList()

    const offLock = ws.on('page.editing', (evt) => {
      if (!scope.locks) return
      const data = evt.data as unknown as EditLock & { page_id: string }
      if (viewing.value && data.page_id === viewing.value.id) {
        editLock.value = {
          locked_by: data.locked_by,
          locked_at: data.locked_at ?? null,
          lock_expires_at: data.lock_expires_at ?? null,
        }
      }
    })
    const offUnlock = ws.on('page.editing_done', (evt) => {
      const data = evt.data as { page_id: string }
      if (viewing.value?.id === data.page_id) editLock.value = null
    })
    const offConflict = ws.on('page.conflict', (evt) => {
      const data = evt.data as {
        page_id: string; theirs: string; theirs_by: string; federated?: boolean;
      }
      if (data.federated) {
        // Another household's edit opened (or grew) a conflict here. An
        // open editor keeps going — its banner offers the resolution.
        refreshList()
        if (viewing.value?.id === data.page_id) void refetchViewing()
        return
      }
      if (
        viewing.value?.id === data.page_id
        && editing.value
        && !conflict.value
      ) {
        conflict.value = {
          mine: editContent.value,
          theirs: data.theirs,
          theirs_by: scope.nameOf(data.theirs_by),
        }
      }
    })
    const offSequenced = ws.on('page.sequenced', (evt) => {
      const data = evt.data as {
        page_id: string; outcome?: string; reason?: string | null;
      }
      // The host answered this household's edit: the pill clears, or the
      // refusal is explained.
      refreshList()
      const open = viewing.value?.id === data.page_id
      if (open) void refetchViewing()
      if (data.outcome === 'refused' && open) {
        const reason = REFUSAL_REASONS.find(r => r === data.reason) ?? 'access'
        refusal.value = { pageId: data.page_id, reason }
      } else if (refusal.value?.pageId === data.page_id) {
        refusal.value = null
      }
    })
    return () => {
      offLock(); offUnlock(); offConflict(); offSequenced()
      clearSaveTimer()
      stopHeartbeat()
      flushOnUnmount()
    }
  }, [scope.key]) // eslint-disable-line react-hooks/exhaustive-deps

  useMarkdownShortcuts(textareaRef, (s) => {
    editContent.value = s
    scheduleAutosave()
  })

  // ─── Saving ──────────────────────────────────────────────────────────

  const clearSaveTimer = () => {
    if (saveTimer.current !== null) {
      window.clearTimeout(saveTimer.current)
      saveTimer.current = null
    }
  }

  /** What the editor would change: the body, and the title when it was
   *  edited (an empty title is never sent — the field keeps the old one). */
  const pendingPatch = (page: Page): Record<string, unknown> | null => {
    const patch: Record<string, unknown> = {}
    if (editContent.value !== page.content) patch.content = editContent.value
    const title = editTitle.value.trim()
    if (title && title !== page.title) patch.title = title
    return Object.keys(patch).length ? patch : null
  }

  /** Save the editor's changes. ``'saved'`` (or nothing to save),
   *  ``'queued'`` (held for review), ``'conflict'`` or ``'error'``.
   *  Saves run one at a time: this waits for one already on the wire,
   *  then works out what is still unsaved against its answer. */
  const doSave = async (): Promise<SaveOutcome> => {
    clearSaveTimer()
    while (inFlight.current) await inFlight.current.catch(() => 'error')
    const run = saveNow()
    inFlight.current = run
    try {
      return await run
    } finally {
      if (inFlight.current === run) inFlight.current = null
    }
  }

  const saveNow = async (): Promise<SaveOutcome> => {
    const page = viewing.value
    if (!page || !editing.value) return 'error'
    if (conflict.value) return 'conflict'    // don't thrash while resolving
    if (resolvingSides.value) {
      const ok = await resolveFederated({
        resolution: 'merged_content', content: editContent.value,
      })
      return ok
    }
    const patch = pendingPatch(page)
    if (!patch) return 'saved'
    saveStatus.value = 'saving'
    try {
      const res = await contentWrite<Page>(
        api.patch(itemPath(page.id), { ...patch, base_updated_at: page.updated_at }),
        { spaceId: scope.spaceId },
      )
      if (res.queued) {
        saveStatus.value = 'idle'
        return 'queued'
      }
      const updated = res.data
      // The PATCH answer carries no conflict detail — keep the open one.
      viewing.value = { ...updated, conflict: updated.conflict ?? page.conflict ?? null }
      pages.value = pages.value.map(p => p.id === updated.id
        ? { ...p, ...updated, in_conflict: p.in_conflict } : p)
      saveStatus.value = 'saved'
      window.setTimeout(() => {
        if (saveStatus.value === 'saved') saveStatus.value = 'idle'
      }, 2000)
      return 'saved'
    } catch (err: unknown) {
      if (isStale(err)) {
        saveStatus.value = 'idle'
        await surfaceConflict()
        return 'conflict'
      }
      saveStatus.value = 'error'
      showToast(t('pages.save_failed_detail', { error: errMessage(err) }), 'error')
      return 'error'
    }
  }

  const scheduleAutosave = () => {
    // Held for review: one explicit submit, never an autosave. Merging a
    // federated conflict by hand: one explicit save of the resolution.
    if (reviewed(viewing.value) || resolvingSides.value) return
    clearSaveTimer()
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null
      void doSave()
    }, AUTOSAVE_MS)
  }

  /** Someone saved since this editor loaded the page: fetch theirs and
   *  open the side-by-side view. ``viewing`` becomes their version, so
   *  the next save is based on it. */
  const surfaceConflict = async () => {
    const page = viewing.value
    if (!page) return
    try {
      const latest = await api.get(itemPath(page.id)) as Page
      conflict.value = {
        mine: editContent.value,
        theirs: latest.content,
        theirs_by: scope.nameOf(latest.last_editor_user_id || latest.created_by),
      }
      viewing.value = latest
    } catch {
      saveStatus.value = 'error'
    }
  }

  // ─── Lock lifecycle (household only) ─────────────────────────────────

  const stopHeartbeat = () => {
    if (heartbeatTimer.current !== null) {
      window.clearInterval(heartbeatTimer.current)
      heartbeatTimer.current = null
    }
  }

  const acquireAndHeartbeat = async (pageId: string) => {
    if (!scope.locks) return
    try {
      await api.post(`${itemPath(pageId)}/lock`, {})
    } catch { /* best-effort — another editor may already hold it */ }
    stopHeartbeat()
    heartbeatTimer.current = window.setInterval(() => {
      api.post(`${itemPath(pageId)}/lock/refresh`, {}).catch(() => {})
    }, 30_000)
  }

  const releaseLock = (pageId: string, opts?: { keepalive: true }) => {
    if (!scopeRef.current.locks) return
    stopHeartbeat()
    const path = `${scopeRef.current.base}/${pageId}/lock`
    ;(opts ? api.delete(path, opts) : api.delete(path)).catch(() => {})
  }

  /** Leaving with the editor open (tab switch, navigation): best-effort
   *  save of what's unsaved — not an edit held for review, which only
   *  goes out on an explicit submit — after any save on the wire, then
   *  release the household lock. ``keepalive`` so it outlives the page
   *  (as ``undoableDelete``'s flush). */
  const flushOnUnmount = () => {
    const open = viewing.value
    if (!open || !editing.value) return
    const scopeNow = scopeRef.current
    const send = () => {
      const page = viewing.value ?? open
      const patch = conflict.value || resolvingSides.value || resolveView.value
        || scopeNow.reviewed(page) ? null : pendingPatch(page)
      const save = patch
        ? api.patch(`${scopeNow.base}/${page.id}`,
          { ...patch, base_updated_at: page.updated_at }, { keepalive: true }).catch(() => {})
        : Promise.resolve()
      void save.finally(() => releaseLock(page.id, { keepalive: true }))
    }
    if (inFlight.current) void inFlight.current.finally(send)
    else send()
  }

  // ─── Flow actions ────────────────────────────────────────────────────

  const openEditor = (page: Page) => {
    viewing.value = page
    editContent.value = page.content
    editTitle.value = page.title
    editing.value = true
    conflict.value = null
    resolveView.value = null
    resolvingSides.value = null
    saveStatus.value = 'idle'
    mobileView.value = 'edit'
    void acquireAndHeartbeat(page.id)
  }

  const createPage = async (title: string, content: string): Promise<Page | null> => {
    try {
      const res = await contentWrite<Page>(
        api.post(scope.base, { title, content }),
        { spaceId: scope.spaceId },
      )
      showNew.value = false
      if (res.queued) return null
      const page = res.data
      pages.value = [page, ...pages.value]
      openEditor(page)
      return page
    } catch (err: unknown) {
      showToast(t('pages.create_failed', { error: errMessage(err) }), 'error')
      return null
    }
  }

  /** The host no longer has this page (deleted there): keep the words as a
   *  new page of this space. */
  const saveAsNewPage = async (page: Page) => {
    const created = await createPage(page.title, page.content)
    if (created) {
      refusal.value = null
      showToast(t('pages.refused.saved_as_new'), 'info')
    }
  }

  const viewPage = async (id: string) => {
    const seq = ++viewSeq.current
    try {
      const page = await api.get(itemPath(id)) as Page
      if (seq !== viewSeq.current) return   // a later open won
      viewing.value = page
      editing.value = false
      editLock.value = null
      conflict.value = null
      resolveView.value = null
      resolvingSides.value = null
      showHistory.value = false
      if (scope.locks) {
        try {
          const lock = await api.get(`${itemPath(id)}/lock`) as EditLock | null
          if (lock && seq === viewSeq.current) editLock.value = lock
        } catch { /* noop */ }
      }
    } catch (err: unknown) {
      if (seq === viewSeq.current) showToast(errMessage(err), 'error')
    }
  }

  const closeEditor = () => {
    const page = viewing.value
    if (!page) return
    clearSaveTimer()
    releaseLock(page.id)
    editing.value = false
    saveStatus.value = 'idle'
    conflict.value = null
    resolvingSides.value = null
  }

  /** "Save & close" / "Submit for review": close only once it's stored
   *  (or queued) — a conflict or an error keeps the editor open. */
  const saveAndClose = async () => {
    if (submitting.value) return
    submitting.value = true
    try {
      const outcome = await doSave()
      if (outcome === 'saved' || outcome === 'queued') closeEditor()
    } finally {
      submitting.value = false
    }
  }

  /** "Close" — unsaved changes autosave first; a held-for-review edit
   *  that wasn't submitted asks before it's dropped. */
  const close = async () => {
    const page = viewing.value
    if (!page) return
    if (resolvingSides.value) {
      if (!await confirmDialog(t('pages.discard_confirm'), { destructive: true })) return
      closeEditor()
      return
    }
    if (pendingPatch(page)) {
      if (reviewed(page)) {
        if (!await confirmDialog(t('pages.discard_confirm'), { destructive: true })) return
      } else {
        const outcome = await doSave()
        if (outcome === 'conflict' || outcome === 'error') return
      }
    }
    closeEditor()
  }

  const deletePage = async () => {
    const page = viewing.value
    if (!page) return
    if (!await confirmDialog(t('pages.delete_confirm', { title: page.title }), { destructive: true })) return
    try {
      const res = await contentWrite<unknown>(
        api.delete(itemPath(page.id)), { spaceId: scope.spaceId },
      )
      if (res.queued) return
      pages.value = pages.value.filter(p => p.id !== page.id)
      viewing.value = null
      showToast(t('pages.deleted'), 'info')
    } catch (err: unknown) {
      showToast(t('pages.delete_failed', { error: errMessage(err) }), 'error')
    }
  }

  const resolveConflict = (choice: 'mine' | 'theirs' | 'merge') => {
    const c = conflict.value
    if (!c) return
    if (choice === 'mine') {
      editContent.value = c.mine
    } else if (choice === 'theirs') {
      editContent.value = c.theirs
    } else {
      editContent.value =
        `<<<<<<< ${t('pages.conflict.marker_mine')}\n${c.mine}\n=======\n${c.theirs}\n>>>>>>> ${c.theirs_by}\n`
    }
    conflict.value = null
    if (choice === 'mine' && !reviewed(viewing.value)) {
      // ``viewing`` is their version now, so this save goes over it. (An
      // edit held for review waits for its explicit submit instead.)
      void doSave()
      showToast(t('pages.resolved_mine'), 'info')
    } else if (choice === 'mine') {
      showToast(t('pages.conflict.mine_review'), 'info')
    } else {
      showToast(choice === 'theirs' ? t('pages.resolved_theirs') : t('pages.conflict.merge_ready'), 'info')
    }
  }

  // ─── Federated conflicts (v_48) ──────────────────────────────────────

  /** Post a resolution of the open federated conflict, naming the
   *  versions the user saw. ``'saved'`` (or queued for review: the view
   *  closes, the toast says so), ``'conflict'`` when the versions changed
   *  meanwhile (refetched — pick again), ``'error'``. */
  const resolveFederated = async (
    body: { resolution: 'side'; side: string } | { resolution: 'merged_content'; content: string },
  ): Promise<SaveOutcome> => {
    const page = viewing.value
    const sides = page?.conflict?.sides.map(s => s.hash)
    if (!page || !sides || resolving.value) return 'error'
    resolving.value = true
    try {
      const res = await contentWrite<{ ok: boolean; page?: Page }>(
        api.post(`${itemPath(page.id)}/resolve-conflict`, { ...body, sides }),
        { spaceId: scope.spaceId },
      )
      resolveView.value = null
      if (editing.value) closeEditor()
      if (res.queued) return 'queued'
      // On a member household the resolution goes to the host first.
      showToast(res.data.page?.pending
        ? t('pages.pending.resolution_sent', { host: scope.hostName() })
        : t('pages.conflict.federated_resolved'), 'info')
      await refetchViewing()
      return 'saved'
    } catch (err: unknown) {
      if (errCode(err) === 'STALE') {
        showToast(t('pages.conflict.federated_stale'), 'info')
        await refetchViewing()
        if (editing.value && resolvingSides.value) {
          // Back to the versions as they are now.
          closeEditor()
          resolveView.value = { draft: null }
        }
        return 'conflict'
      }
      showToast(t('pages.save_failed_detail', { error: errMessage(err) }), 'error')
      return 'error'
    } finally {
      resolving.value = false
    }
  }

  /** "Merge by hand": every version (and the draft) into the editor. */
  const mergeFederatedByHand = () => {
    const page = viewing.value
    const c = page?.conflict
    if (!page || !c) return
    const draft = resolveView.value?.draft ?? null
    const versions = c.sides.map(s => ({
      label: scope.nameOf(s.by), content: s.content,
    }))
    if (draft !== null) versions.push({ label: t('pages.conflict.federated_draft'), content: draft })
    editTitle.value = page.title
    editContent.value = conflictMarkers(versions)
    resolvingSides.value = c.sides.map(s => s.hash)
    resolveView.value = null
    conflict.value = null
    editing.value = true
    saveStatus.value = 'idle'
    mobileView.value = 'edit'
    showToast(t('pages.conflict.merge_ready'), 'info')
  }

  if (loading.value && pages.value.length === 0 && !viewing.value) return <Spinner />

  // ─── Render ──────────────────────────────────────────────────────────

  // 1. Conflict views take precedence over everything else.
  if (viewing.value && resolveView.value && viewing.value.conflict) {
    const page = viewing.value
    const c = page.conflict!
    const draft = resolveView.value.draft
    const panels: ConflictPanel[] = c.sides.map(side => ({
      key: side.hash,
      heading: t('pages.conflict.federated_by', { user: scope.nameOf(side.by) }),
      meta: (
        <>
          <time dateTime={side.at} title={tsTitle(side.at)}>{relativeDocsTime(side.at)}</time>
          {side.hash === c.current_hash && <> · {t('pages.conflict.federated_shown')}</>}
        </>
      ),
      content: side.content,
      keepLabel: t('pages.conflict.federated_keep'),
      shown: side.hash === c.current_hash,
      onKeep: () => { void resolveFederated({ resolution: 'side', side: side.hash }) },
    }))
    if (draft !== null) {
      panels.push({
        key: 'draft',
        heading: t('pages.conflict.federated_draft'),
        meta: t('pages.conflict.federated_draft_note'),
        content: draft,
        keepLabel: t('pages.conflict.federated_keep_draft'),
        onKeep: () => { void resolveFederated({ resolution: 'merged_content', content: draft }) },
      })
    }
    return (
      <PageConflictView
        testId="page-federated-conflict"
        title={t('pages.conflict_title', { title: page.title })}
        message={t('pages.conflict.federated_message', { count: String(c.sides.length) })}
        panels={panels}
        busy={resolving.value}
        onMerge={mergeFederatedByHand}
        onCancel={() => {
          void (async () => {
            if (draft !== null && draft !== page.content
              && !await confirmDialog(t('pages.discard_confirm'), { destructive: true })) return
            resolveView.value = null
            if (editing.value) closeEditor()
          })()
        }}
      />
    )
  }

  if (viewing.value && editing.value && conflict.value) {
    const c = conflict.value
    return (
      <PageConflictView
        title={t('pages.conflict_title', { title: viewing.value.title })}
        message={t('pages.conflict_message', { user: c.theirs_by })}
        panels={[
          {
            key: 'mine', heading: t('pages.your_version'), content: c.mine,
            keepLabel: t('pages.keep_mine'), onKeep: () => resolveConflict('mine'),
          },
          {
            key: 'theirs', heading: t('pages.their_version'), content: c.theirs,
            keepLabel: t('pages.keep_theirs'), onKeep: () => resolveConflict('theirs'),
          },
        ]}
        onMerge={() => resolveConflict('merge')}
      />
    )
  }

  // 2. Editor.
  if (viewing.value && editing.value) {
    const page = viewing.value
    const forReview = reviewed(page)
    const merging = !!resolvingSides.value
    const status = saveStatus.value
    const statusLabel =
      status === 'saving' ? t('pages.status.saving') :
      status === 'saved' ? t('pages.status.saved') :
      status === 'error' ? t('pages.status.error') : ''
    return (
      <div class="sh-page-editor">
        <div class="sh-page-header">
          <input
            class="sh-page-editor-title"
            aria-label={t('pages.title_label')}
            value={editTitle.value}
            maxLength={200}
            onInput={(e) => {
              editTitle.value = (e.target as HTMLInputElement).value
              scheduleAutosave()
            }}
          />
          <div class="sh-row sh-page-editor-actions">
            {statusLabel && (
              status === 'error' ? (
                <button
                  type="button"
                  class={`sh-page-editor-status sh-page-editor-status--${status}`}
                  onClick={() => void doSave()}
                >
                  {statusLabel}
                </button>
              ) : (
                <span
                  class={`sh-page-editor-status sh-page-editor-status--${status}`}
                  role="status"
                >
                  {statusLabel}
                </span>
              )
            )}
            {page.pending && status !== 'saving' && (
              <PendingPill host={scope.hostName()} />
            )}
            <Button variant="secondary" onClick={() => void close()}>{t('common.close')}</Button>
            <Button loading={submitting.value || resolving.value} onClick={() => void saveAndClose()}>
              {merging ? t('pages.conflict.federated_save_merge')
                : forReview ? t('pages.submit_review') : t('pages.save_close')}
            </Button>
          </div>
        </div>
        {merging && (
          <p class="sh-page-review-note" role="note">{t('pages.conflict.federated_merge_note')}</p>
        )}
        {!merging && page.conflict && (
          <ConflictBanner
            count={page.conflict.sides.length}
            canWrite={scope.canWrite}
            onResolve={() => { resolveView.value = { draft: editContent.value } }}
          />
        )}
        {refusal.value?.pageId === page.id && (
          <RefusalNotice
            reason={refusal.value.reason}
            host={scope.hostName()}
            onSaveAsNew={() => void saveAsNewPage({
              ...page, title: editTitle.value.trim() || page.title, content: editContent.value,
            })}
            onDismiss={() => { refusal.value = null }}
          />
        )}
        {forReview && (
          <p class="sh-page-review-note" role="note">{t('pages.review_note')}</p>
        )}

        <MarkdownToolbar
          textareaRef={textareaRef}
          onUpdate={(s) => { editContent.value = s; scheduleAutosave() }}
        />

        <div class="sh-page-editor-mobile-tabs" role="tablist">
          <button
            type="button" role="tab" id={`${tabIds}-edit-tab`}
            aria-selected={mobileView.value === 'edit'}
            aria-controls={`${tabIds}-source`}
            onClick={() => { mobileView.value = 'edit' }}
          >{t('pages.tab.edit')}</button>
          <button
            type="button" role="tab" id={`${tabIds}-preview-tab`}
            aria-selected={mobileView.value === 'preview'}
            aria-controls={`${tabIds}-preview`}
            onClick={() => { mobileView.value = 'preview' }}
          >{t('pages.tab.preview')}</button>
        </div>

        <div class="sh-page-editor-panes">
          <textarea
            ref={textareaRef}
            id={`${tabIds}-source`}
            value={editContent.value}
            class={mobileView.value === 'preview' ? 'sh-page-editor-hidden-mobile' : ''}
            onInput={(e) => {
              editContent.value = (e.target as HTMLTextAreaElement).value
              scheduleAutosave()
            }}
            aria-label={t('pages.source_label')}
            placeholder={t('pages.source_placeholder')}
          />
          <div
            id={`${tabIds}-preview`}
            class={`sh-page-editor-preview ${mobileView.value === 'edit' ? 'sh-page-editor-hidden-mobile' : ''}`}
            aria-label={t('pages.preview_label')}
          >
            <MarkdownView src={editContent.value} live />
          </div>
        </div>
      </div>
    )
  }

  // 3. Viewer.
  if (viewing.value) {
    const page = viewing.value
    const headings = extractHeadings(page.content)
    const editorUid = page.last_editor_user_id || page.created_by
    const editedAt = page.last_edited_at || page.updated_at
    const lockedByOther = !!editLock.value
    // The viewer's own edit / delete of this page waiting for review.
    const mine = scope.spaceId
      ? pendingMine(scope.spaceId, 'pages').filter(
        i => i.target_id === page.id && i.action !== 'create',
      )
      : []
    const pendingDelete = mine.some(i => i.action === 'delete')
    const pendingEdit = mine.some(i => i.action !== 'delete')
    return (
      <>
        <div class="sh-page-viewer">
          <div class="sh-page-header">
            <div>
              <h1 class="sh-page-viewer-title">{page.title}</h1>
              <div class="sh-page-viewer-byline">
                {t('pages.edited_by', { user: scope.nameOf(editorUid) })} ·{' '}
                <time
                  dateTime={editedAt}
                  title={tsTitle(editedAt)}
                >
                  {relativeDocsTime(editedAt)}
                </time>
              </div>
              {page.pending && <PendingPill host={scope.hostName()} />}
            </div>
            <div class="sh-row sh-page-viewer-actions">
              <Button
                variant="secondary"
                onClick={() => { viewSeq.current++; viewing.value = null; editLock.value = null }}
              >{t('pages.back')}</Button>
              <Button variant="secondary" onClick={() => { showHistory.value = true }}>
                {t('pages.history')}
              </Button>
              {scope.canWrite && (
                <>
                  <Button onClick={() => openEditor(page)} disabled={lockedByOther}>
                    {t('pages.edit')}
                  </Button>
                  <Button variant="danger" onClick={() => void deletePage()}>
                    {t('common.delete')}
                  </Button>
                </>
              )}
              {scope.spaceId && page.created_by !== currentUser.value?.user_id && (
                // A space page goes to the space's moderators.
                <Button
                  variant="ghost"
                  onClick={() => openReport('page', page.id, scope.spaceId)}
                >
                  {t('report.action')}
                </Button>
              )}
            </div>
          </div>
          {(pendingEdit || pendingDelete) && (
            <p
              class="sh-page-review-note sh-page-review-note--pending"
              role="status"
              data-testid="page-pending-review"
            >
              <span class="sh-badge sh-badge--pending">{t('moderation.mine.badge')}</span>
              <span>
                {pendingEdit && t('pages.pending_edit_mine')}
                {pendingEdit && pendingDelete && ' '}
                {pendingDelete && t('pages.pending_delete_mine')}
              </span>
            </p>
          )}
          {scope.canWrite && reviewed(page) && !pendingEdit && !pendingDelete && (
            <p class="sh-page-review-note" role="note">{t('pages.review_note')}</p>
          )}
          {page.conflict && (
            <ConflictBanner
              count={page.conflict.sides.length}
              canWrite={scope.canWrite}
              onResolve={() => { resolveView.value = { draft: null } }}
            />
          )}
          {refusal.value?.pageId === page.id && (
            <RefusalNotice
              reason={refusal.value.reason}
              host={scope.hostName()}
              onSaveAsNew={() => void saveAsNewPage(page)}
              onDismiss={() => { refusal.value = null }}
            />
          )}
          {editLock.value && (
            <div class="sh-edit-lock-banner" role="alert">
              <span aria-hidden="true">🔒</span>{' '}
              {t('pages.editing_banner', { user: scope.nameOf(editLock.value.locked_by) })}
            </div>
          )}
          <div class="sh-page-viewer-layout">
            {page.content
              ? <MarkdownView src={page.content} />
              : (
                <div class="sh-empty-state">
                  <p>{t('pages.empty_body')}</p>
                  {scope.canWrite && <p>{t('pages.empty_body_hint')}</p>}
                </div>
              )}
            {headings.length > 0 && (
              <nav class="sh-page-toc" aria-label={t('pages.toc')}>
                <h4>{t('pages.toc')}</h4>
                <ul>
                  {headings.map(h => (
                    <li key={h.slug} class={`sh-toc-depth-${h.depth}`}>
                      <a href={`#${h.slug}`}>{h.text}</a>
                    </li>
                  ))}
                </ul>
              </nav>
            )}
          </div>
        </div>
        <PageHistoryDrawer
          versionsUrl={`${itemPath(page.id)}/versions`}
          revertUrl={scope.canRevert ? `${itemPath(page.id)}/revert` : null}
          revertNote={scope.revertNote()}
          nameOf={scope.nameOf}
          currentContent={page.content}
          open={showHistory.value}
          onClose={() => { showHistory.value = false }}
          onRestored={(c) => {
            if (viewing.value) viewing.value = { ...viewing.value, content: c }
            void viewPage(page.id)
          }}
        />
      </>
    )
  }

  // 4. Index.
  // "X pages · last edited 5h" tells at a glance whether the wiki is alive
  // or stale — from the freshest ``updated_at`` (no reliance on order).
  const newestUpdate = pages.value.reduce<string | null>((acc, p) => {
    if (!acc) return p.updated_at
    return tsMs(p.updated_at) > tsMs(acc) ? p.updated_at : acc
  }, null)
  const n = pages.value.length
  return (
    <div class="sh-pages">
      {header}
      <header class="sh-pages-hero">
        <div class="sh-pages-hero-headline">
          {n === 1 ? t('pages.count_one') : t('pages.count', { count: String(n) })}
          {newestUpdate && (
            <>
              {' · '}
              <span class="sh-muted">
                {t('pages.last_edited', { when: relativeDocsTime(newestUpdate) })}
              </span>
            </>
          )}
        </div>
        {scope.canWrite && (
          <div class="sh-pages-hero-actions">
            <Button onClick={() => { showNew.value = true }}>+ {t('pages.new_page')}</Button>
          </div>
        )}
      </header>
      {loadFailed.value ? (
        <div class="sh-empty-state" role="alert">
          <p>{t('pages.load_failed')}</p>
          <Button variant="secondary" onClick={loadList}>{t('common.retry')}</Button>
        </div>
      ) : n === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">📝</div>
          <h3>{t('pages.no_pages')}</h3>
          <p>{scope.spaceId ? t('pages.empty_space') : t('pages.empty_household')}</p>
          {scope.canWrite && (
            <Button onClick={() => { showNew.value = true }}>
              + {t('pages.create_first')}
            </Button>
          )}
          {scope.canWrite && <MarkdownBasics />}
        </div>
      ) : (
        <>
          <div class="sh-pages-list">
            {pages.value.map(p => (
              <button
                key={p.id}
                type="button"
                class="sh-page-card"
                onClick={() => void viewPage(p.id)}
              >
                <div class="sh-page-card-main">
                  <strong>{p.title}</strong>
                  {p.in_conflict && (
                    <span class="sh-badge sh-badge--conflict">
                      {t('pages.conflict.federated_badge')}
                    </span>
                  )}
                  {p.pending && (
                    <span class="sh-badge sh-badge--pending" data-testid="page-not-shared">
                      {t('pages.pending.badge')}
                    </span>
                  )}
                  {/* A ~160-char snippet of the body with the Markdown
                   *  noise (and a leading H1 repeating the title) stripped,
                   *  so it reads as body copy rather than source. */}
                  <span class="sh-page-card-snippet sh-muted">
                    {snippetFromMarkdown(p.content, p.title)}
                  </span>
                  {p.last_editor_user_id && (
                    <span class="sh-page-card-byline sh-muted">
                      {t('pages.edited_by', { user: scope.nameOf(p.last_editor_user_id) })}
                    </span>
                  )}
                </div>
                <time
                  class="sh-page-card-when sh-muted"
                  dateTime={p.updated_at}
                  title={tsTitle(p.updated_at)}
                >
                  {relativeDocsTime(p.updated_at)}
                </time>
              </button>
            ))}
          </div>
          {scope.canWrite && <MarkdownBasics />}
        </>
      )}

      <NewPageDialog
        open={showNew.value}
        reviewed={scope.reviewedCreate}
        onCancel={() => { showNew.value = false }}
        onCreate={async (title, content) => { await createPage(title, content) }}
      />
    </div>
  )
}

/** Strip enough Markdown noise from the page body that its first ~160
 *  characters read as body copy in the card snippet. Small and lossy on
 *  purpose — this is not a Markdown renderer. */
export function snippetFromMarkdown(src: string, title?: string): string {
  if (!src) return t('pages.empty_page')
  const cleaned = src
    .replace(/^#+\s*/gm, '')        // ATX heading marks
    .replace(/^[-*]\s+/gm, '')      // bullet lists
    .replace(/^\d+\.\s+/gm, '')     // numbered lists
    .replace(/[*_`]+/g, '')         // emphasis + inline code marks
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1') // images → alt
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')  // [text](url) → text
  // Drop the leading line if it repeats the page title.
  const lines = cleaned.split('\n')
  if (title && lines.length > 0
      && lines[0].trim().toLowerCase() === title.trim().toLowerCase()) {
    lines.shift()
  }
  const flat = lines.join(' ').replace(/\s+/g, ' ').trim()
  if (!flat) return t('pages.empty_page')
  return flat.length > 160 ? `${flat.slice(0, 160)}…` : flat
}

/** Markdown cheatsheet — collapsed by default; under the list and in the
 *  empty state for anyone who can write. */
function MarkdownBasics() {
  return (
    <details class="sh-pages-help">
      <summary class="sh-muted">{t('pages.md.title')}</summary>
      <ul>
        <li><code># Heading</code> → {t('pages.md.heading')}</li>
        <li><code>**bold**</code> → <strong>{t('pages.md.bold')}</strong></li>
        <li><code>*italic*</code> → <em>{t('pages.md.italic')}</em></li>
        <li><code>[link](https://…)</code> — {t('pages.md.link')}</li>
        <li><code>- item</code> / <code>1. item</code> — {t('pages.md.lists')}</li>
        <li><code>![alt](url)</code> — {t('pages.md.image')}</li>
        <li><code>[[Page Title]]</code> — {t('pages.md.page_link')}</li>
      </ul>
    </details>
  )
}


/** "Saved here · waiting for {host}" — this household's edit of a space
 *  page that the host has not sequenced yet (v_48). */
function PendingPill({ host }: { host: string }) {
  return (
    <span
      class="sh-page-pending-pill"
      role="status"
      data-testid="page-pending-host"
      title={t('pages.pending.hint', { host })}
    >
      {t('pages.pending.pill', { host })}
    </span>
  )
}

/** Concurrent edits the host could not merge — kept beside the page. */
function ConflictBanner({ count, canWrite, onResolve }: {
  count: number
  canWrite: boolean
  onResolve: () => void
}) {
  return (
    <div
      class="sh-conflict-banner sh-page-conflict-banner"
      role="status"
      data-testid="page-conflict-banner"
    >
      <div>
        <strong>{t('pages.conflict.federated_banner')}</strong>{' '}
        <span>
          {canWrite
            ? t('pages.conflict.federated_banner_hint', { count: String(count) })
            : t('pages.conflict.federated_read_only')}
        </span>
      </div>
      {canWrite && (
        <Button onClick={onResolve}>{t('pages.conflict.federated_resolve')}</Button>
      )}
    </div>
  )
}

function refusalText(reason: RefusalReason): string {
  switch (reason) {
    case 'access': return t('pages.refused.access')
    case 'archived': return t('pages.refused.archived')
    case 'gone': return t('pages.refused.gone')
    case 'rate_limited': return t('pages.refused.rate_limited')
    case 'bad_base': return t('pages.refused.bad_base')
  }
}

/** The host refused this household's edit — why, and what to do now. */
function RefusalNotice({ reason, host, onSaveAsNew, onDismiss }: {
  reason: RefusalReason
  host: string
  onSaveAsNew: () => void
  onDismiss: () => void
}) {
  return (
    <div
      class="sh-page-refusal"
      role="alert"
      data-testid="page-refusal"
    >
      <p>
        <strong>{t('pages.refused.title', { host })}</strong>{' '}
        <span>{refusalText(reason)}</span>
      </p>
      <div class="sh-row sh-page-refusal-actions">
        {reason === 'gone' && (
          <Button onClick={onSaveAsNew}>{t('pages.refused.save_as_new')}</Button>
        )}
        <Button variant="secondary" onClick={onDismiss}>{t('pages.refused.dismiss')}</Button>
      </div>
    </div>
  )
}
