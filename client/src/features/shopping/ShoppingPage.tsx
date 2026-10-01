import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import {
  items,
  stores,
  loadShopping,
  shoppingLoaded,
  wireShoppingWs,
  addItem,
  updateItem,
  toggleItem,
  deleteItems,
  reinsertItems,
  reorderStores,
  sameName,
} from '@/store/shopping'
import type { ShoppingItem } from '@/types'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { CheckToggle } from '@/components/CheckToggle'
import { RowActionButton } from '@/components/RowActionButton'
import { QuickAddBar } from '@/components/QuickAddBar'
import { ListSkeleton } from '@/components/Skeleton'
import { LoadErrorState } from '@/components/LoadErrorState'
import { ChipRadioGroup } from '@/components/ChipRadioGroup'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { currentUser } from '@/store/auth'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { relativeDocsTime } from '@/utils/relativeTime'
import { parseItemInput } from '@/utils/shoppingParse'
import { useLoad } from '@/utils/useLoad'
import { undoableDelete, pendingDeletes } from '@/utils/undoableDelete'
import { t } from '@/i18n/i18n'
import { OrganizeSectionHeader } from '@/features/organize/shared/OrganizeSectionHeader'
import { ArchiveDivider } from '@/features/organize/shared/ArchiveDivider'
import { DropPad } from '@/features/organize/shared/DropPad'
import {
  useDragBuckets,
  composeDragHandlers,
  type DragItemProps,
  type DragBuckets,
} from '@/features/organize/shared/useDragBuckets'
import { StoreManagerDialog } from './StoreManagerDialog'

/** Key the "Group by store" toggle off ``localStorage`` so the
 *  toggle survives reloads. Default is "auto" — turn on automatically
 *  the first time the household has ≥2 distinct stores; the user can
 *  override either way and the override sticks. */
const GROUP_PREF_KEY = 'sh_shopping_group_by_store'
type GroupPref = 'auto' | 'on' | 'off'

function readGroupPref(): GroupPref {
  try {
    const v = localStorage.getItem(GROUP_PREF_KEY)
    if (v === 'on' || v === 'off') return v
  } catch {
    /* sandboxed */
  }
  return 'auto'
}

function writeGroupPref(v: GroupPref) {
  try {
    if (v === 'auto') localStorage.removeItem(GROUP_PREF_KEY)
    else localStorage.setItem(GROUP_PREF_KEY, v)
  } catch {
    /* sandboxed */
  }
}

const NO_STORE_KEY = '__no_store__'

/** Distinguishes item drags from store-header drags in the same
 *  drag-and-drop layer (one ``useDragBuckets`` instance per kind). */
const DRAG_ITEM_MIME = 'application/x-sh-shopping-item'
const DRAG_STORE_MIME = 'application/x-sh-shopping-store'

/** ``t()`` with a count: picks the ``_one`` key for exactly one.
 *  Keys reached this way (for ``i18n:check``):
 *  t('shopping.dupes_skipped') t('shopping.dupes_skipped_one')
 *  t('shopping.cleared') t('shopping.cleared_one') */
function tn(key: string, n: number, params: Record<string, string> = {}): string {
  return t(n === 1 ? `${key}_one` : key, { ...params, n: String(n) })
}

function errText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

/** Touch devices: don't pop the on-screen keyboard on arrival. */
function prefersNoAutofocus(): boolean {
  try {
    return typeof window.matchMedia === 'function'
      && window.matchMedia('(pointer: coarse)').matches
  } catch {
    return false
  }
}

export default function ShoppingPage() {
  useTitle(t('shopping.title'))
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    wireShoppingWs()
    void loadHouseholdUsers()
  }, [])

  // Cached signals render at once on a revisit; the loader revalidates
  // behind them and again after a WebSocket reconnect.
  // A reconnect revalidation forces a fresh fetch past any in-flight
  // one; a recovered Retry hands focus to the add field (the Retry
  // button it replaces held it).
  const { state, retry } = useLoad(
    ({ reason }) => loadShopping({ force: reason === 'reconnect' }),
    {
      cached: shoppingLoaded.value,
      onRecovered: () => requestAnimationFrame(() => inputRef.current?.focus()),
    },
  )

  const [draft, setDraft] = useState('')
  const [showSuggest, setShowSuggest] = useState(false)
  const [suggestHeld, setSuggestHeld] = useState(false)
  /** Caret position inside the quick-add input. Updated on every key
   *  / click / selection change so the ``@ store`` autocomplete can
   *  scope its query to the *current* segment when the user pastes
   *  ``Milk @ Aldi, Bread @ Ba`` and is mid-edit on the second
   *  ``@`` — only the last ``@`` before the caret matters, and only
   *  when there's no ``,`` separator between it and the caret. */
  const [caretPos, setCaretPos] = useState(0)
  const [groupPref, setGroupPref] = useState<GroupPref>(readGroupPref())
  const [editingId, setEditingId] = useState<string | null>(null)
  /** Row whose rename trigger should take focus back once its editor
   *  closed from the keyboard (Enter / Escape / Save / Cancel). A blur
   *  commit leaves focus wherever the user moved it. */
  const [refocusId, setRefocusId] = useState<string | null>(null)
  const clearRefocus = useCallback(() => setRefocusId(null), [])
  /** Whether the store-catalogue manager dialog is open. */
  const [storesOpen, setStoresOpen] = useState(false)

  const itemDrag = useDragBuckets<string>({
    mime: DRAG_ITEM_MIME,
    onDrop: (id, bucket) => {
      void handleReassignStore(id, bucket === NO_STORE_KEY ? null : bucket)
    },
  })
  const storeDrag = useDragBuckets<string>({
    mime: DRAG_STORE_MIME,
    canDrop: (bucket) => bucket !== NO_STORE_KEY,
    onDrop: (name, target) => { void handleStoreHeaderDrop(name, target) },
  })

  /** ``@ store`` autocomplete context. ``null`` when the caret isn't
   *  inside a store-name suffix (no ``@`` before the caret on this
   *  comma-segment, or no stores defined yet). Otherwise carries the
   *  range to splice over on selection plus the partial query the
   *  user has typed so far (which the dropdown filters against —
   *  empty query shows every store). */
  const storeContext = useMemo(() => {
    if (stores.value.length === 0) return null
    // Constrain to the current comma-segment so we don't autocomplete
    // across a finished ``…, Bread`` boundary.
    const segStart = (draft.lastIndexOf(',', caretPos - 1) + 1) || 0
    const atIndex = draft.lastIndexOf('@', caretPos - 1)
    if (atIndex < 0 || atIndex < segStart) return null
    const between = draft.slice(atIndex + 1, caretPos)
    // The caret must be on the store-name side of the ``@`` separator
    // (no commas have been typed yet on this segment).
    if (between.includes(',')) return null
    const query = between.trim()
    return { atIndex, query }
  }, [draft, caretPos, stores.value])

  const storeMatches = useMemo(() => {
    if (!storeContext) return []
    const q = storeContext.query.toLowerCase()
    const all = stores.value.map(s => s.name)
    if (!q) return all.slice(0, 8)
    return all
      .filter(n => n.toLowerCase().includes(q))
      .slice(0, 8)
  }, [storeContext, stores.value])

  /** Splice the chosen store name into ``draft`` over the
   *  ``@…<caret>`` range so the segment ends as ``"Milk @ Aldi "``
   *  (single trailing space — sets up the next comma-separated
   *  segment without forcing the user to type another). */
  const pickStore = (name: string) => {
    if (!storeContext) return
    const before = draft.slice(0, storeContext.atIndex)
    const after = draft.slice(caretPos)
    const next = `${before}@ ${name} ${after}`
    setDraft(next)
    // Restore caret to right after the inserted store + space so the
    // user can immediately ``,`` into another item.
    const newCaret = before.length + `@ ${name} `.length
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(newCaret, newCaret)
      setCaretPos(newCaret)
    })
  }

  // Hidden-but-uncommitted deletes (Undo window) stay out of every
  // view, even if a WS frame or a revalidate brings the row back.
  const pending = pendingDeletes.value
  const visible = useMemo(
    () => items.value.filter(i => !pending.has(i.id)),
    [items.value, pending],
  )

  // Suggest re-adding any completed item by name (existing pattern).
  const pastNames = useMemo(() => {
    const names = visible
      .filter(i => i.completed)
      .map(i => (i.text || '').trim())
      .filter(Boolean)
    const seen = new Set<string>()
    const out: string[] = []
    for (const n of names.reverse()) {
      if (seen.has(n)) continue
      seen.add(n)
      out.push(n)
    }
    return out.slice(0, 12)
  }, [visible])

  const handleQuickAdd = async () => {
    const raw = draft.trim()
    if (!raw) return
    // Comma-split first, then ``@``-split each segment so the user
    // can batch in one go: ``Milk @ Aldi, Bread @ Bakery, Eggs``.
    const parts = raw
      .split(',')
      .map(s => parseItemInput(s))
      .filter(p => p.text)
    if (parts.length === 0) return
    const existing = new Set(
      visible
        .filter(i => !i.completed)
        .map(i => (i.text || '').toLowerCase()),
    )
    const dupes: string[] = []
    try {
      for (const { text, store } of parts) {
        if (existing.has(text.toLowerCase())) {
          dupes.push(text)
          continue
        }
        await addItem(text, store)
        existing.add(text.toLowerCase())
      }
      setDraft('')
      setShowSuggest(false)
      if (dupes.length && parts.length === dupes.length) {
        showToast(t('shopping.all_dupes'), 'info')
      } else if (dupes.length) {
        showToast(tn('shopping.dupes_skipped', dupes.length), 'info')
      }
    } catch (err: unknown) {
      showToast(t('shopping.error.add', { error: errText(err) }), 'error')
    }
  }

  const addSuggestion = async (name: string) => {
    try {
      await addItem(name)
      inputRef.current?.focus()
    } catch (err: unknown) {
      showToast(t('shopping.error.add', { error: errText(err) }), 'error')
    }
  }

  const handleToggle = async (id: string, completed: boolean) => {
    try {
      await toggleItem(id, !completed)
    } catch (err: unknown) {
      showToast(t('shopping.error.update', { error: errText(err) }), 'error')
    }
  }

  /** Instant delete with Undo: the row hides now, the DELETE goes out
   *  when the toast expires. */
  const handleDelete = (item: ShoppingItem) => {
    const snapshot = [item]
    undoableDelete({
      ids: [item.id],
      message: t('shopping.deleted', { text: item.text }),
      // ``deleteItems`` treats a 404 as gone and only drops gone ids.
      commit: ({ keepalive }) => deleteItems([item.id], { keepalive }),
      restore: () => reinsertItems(snapshot),
      onUndone: () => focusItemCheck(item.id),
    })
  }

  /** "Clear all" bought items, with Undo. On expiry it deletes exactly
   *  the ids hidden now — not the atomic ``clear-completed``, which
   *  would also take items ticked off during the Undo window. */
  const handleClearCompleted = () => {
    const done = visible.filter(i => i.completed)
    if (done.length === 0) return
    const ids = done.map(i => i.id)
    undoableDelete({
      ids,
      message: tn('shopping.cleared', ids.length),
      commit: ({ keepalive }) => deleteItems(ids, { keepalive }),
      restore: () => reinsertItems(done),
      onUndone: () => focusItemCheck(ids[0]),
    })
    inputRef.current?.focus()
  }

  /** Rename-only save path. The store assignment lives in the row's
   *  ``StorePicker`` so this entry point doesn't need a second field.
   *  ``refocus`` is set when the edit ended from the keyboard. */
  const handleEditSave = async (item: ShoppingItem, nextText: string, refocus: boolean) => {
    const trimmedText = nextText.trim()
    if (!trimmedText || trimmedText === item.text) {
      setEditingId(null)
      if (refocus) setRefocusId(item.id)
      return
    }
    try {
      await updateItem(item.id, { text: trimmedText })
      setEditingId(null)
      if (refocus) setRefocusId(item.id)
    } catch (err: unknown) {
      showToast(t('shopping.error.update', { error: errText(err) }), 'error')
    }
  }

  const handleEditCancel = (item: ShoppingItem, refocus: boolean) => {
    setEditingId(null)
    if (refocus) setRefocusId(item.id)
  }

  /** Reassign an item to a different store (or clear the store).
   *  Drives both the StorePicker popover and the drag-and-drop path
   *  in GroupedView. ``null`` clears the store ("No store"). */
  const handleReassignStore = async (
    id: string,
    nextStore: string | null,
  ) => {
    try {
      await updateItem(id, { store: nextStore })
    } catch (err: unknown) {
      showToast(t('shopping.error.reassign', { error: errText(err) }), 'error')
    }
  }

  const handleMoveStore = async (name: string, delta: number) => {
    const order = stores.value.map(s => s.name)
    const i = order.indexOf(name)
    if (i < 0) return
    const j = i + delta
    if (j < 0 || j >= order.length) return
    const next = order.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    try {
      await reorderStores(next)
    } catch (err: unknown) {
      showToast(t('shopping.error.reorder', { error: errText(err) }), 'error')
    }
  }

  const handleStoreHeaderDrop = async (dragged: string, target: string) => {
    if (dragged === target) return
    const order = stores.value.map(s => s.name)
    const from = order.indexOf(dragged)
    const to = order.indexOf(target)
    if (from < 0 || to < 0) return
    const next = order.slice()
    next.splice(from, 1)
    next.splice(to, 0, dragged)
    try {
      await reorderStores(next)
    } catch (err: unknown) {
      showToast(t('shopping.error.reorder', { error: errText(err) }), 'error')
    }
  }

  // Autofocus once the list is on screen so keyboard flow ("open page
  // → start typing → Enter → repeat") works without an extra click.
  // Skipped on touch, where it would pop the keyboard over the list.
  const autofocused = useRef(false)
  useEffect(() => {
    if (state !== 'ready' || autofocused.current) return
    autofocused.current = true
    // Never steal focus the user already placed somewhere.
    const idle = !document.activeElement || document.activeElement === document.body
    if (idle && !prefersNoAutofocus()) inputRef.current?.focus()
  }, [state])

  const active    = visible.filter(i => !i.completed)
  const completed = visible.filter(i =>  i.completed)
  const me        = currentUser.value

  const userNameById = (uid: string): string =>
    me?.user_id === uid ? t('shopping.you') : householdDisplayName(uid)

  // Group rendering kicks in when the catalogue has ≥2 stores OR the
  // user explicitly toggled it on. ``auto`` (default) flips ON as
  // soon as a second store appears; the explicit ``on`` / ``off``
  // overrides stick.
  const distinctStores = useMemo(() => {
    const s = new Set<string>()
    for (const i of visible) if (i.store) s.add(i.store)
    return s
  }, [visible])
  const grouped =
    groupPref === 'on' ||
    (groupPref === 'auto' && (stores.value.length >= 2 || distinctStores.size >= 2))

  const setGroupedPref = (next: 'on' | 'off') => {
    setGroupPref(next)
    writeGroupPref(next)
  }

  const storeNames = stores.value.map(s => s.name)

  const header = (
    <OrganizeSectionHeader
      // The top bar already shows the page title; keep it for screen
      // readers so the store sections (h3) sit under an h2.
      title={t('shopping.title')}
      hideTitle
      counts={state === 'ready' ? [
        { label: t('shopping.counts.to_buy', { n: String(active.length) }), tone: 'open' },
        { label: t('shopping.counts.done', { n: String(completed.length) }), tone: 'done' },
      ] : []}
    >
      {state === 'ready' && (stores.value.length > 0 || distinctStores.size > 0) && (
        <ChipRadioGroup<'on' | 'off'>
          variant="segmented"
          ariaLabel={t('shopping.view.label')}
          value={grouped ? 'on' : 'off'}
          onChange={setGroupedPref}
          options={[
            { value: 'on', label: t('shopping.view.grouped') },
            { value: 'off', label: t('shopping.view.list') },
          ]}
        />
      )}
      {/* Always rendered once loaded — with zero items (or zero ACTIVE
       *  items) there is no item row to hang store management off. */}
      {state === 'ready' && (
        <button
          type="button"
          class="sh-chip sh-shopping-stores-btn"
          aria-haspopup="dialog"
          aria-expanded={storesOpen}
          title={t('shopping.stores.button_title')}
          onClick={() => setStoresOpen(true)}
        >
          <span aria-hidden="true">🏪</span> {t('shopping.stores.button')}
        </button>
      )}
    </OrganizeSectionHeader>
  )

  if (state !== 'ready') {
    return (
      <div class="sh-shopping">
        {header}
        {state === 'error'
          ? <LoadErrorState message={t('shopping.load_failed')} onRetry={retry} />
          : <ListSkeleton variant="list" rows={6} label={t('shopping.loading')} />}
      </div>
    )
  }

  const viewProps: ViewProps = {
    active,
    completed,
    editingId,
    refocusId,
    onRefocused: clearRefocus,
    onEditStart: setEditingId,
    onEditCancel: handleEditCancel,
    onEditSave: handleEditSave,
    onToggle: handleToggle,
    onDelete: handleDelete,
    onClearCompleted: handleClearCompleted,
    onReassignStore: handleReassignStore,
    userNameById,
    storeNames,
  }

  return (
    <div class="sh-shopping">
      {header}

      <StoreManagerDialog
        open={storesOpen}
        onClose={() => setStoresOpen(false)}
      />

      <QuickAddBar
        class="sh-shopping-add"
        value={draft}
        inputRef={inputRef}
        placeholder={t('shopping.placeholder')}
        inputLabel={t('shopping.input_label')}
        submitLabel={t('shopping.add')}
        onSubmit={() => { void handleQuickAdd() }}
        onValueChange={(value, el) => {
          setDraft(value)
          setCaretPos(el.selectionStart ?? value.length)
        }}
        inputProps={{
          name: 'text',
          onKeyUp: (e) => {
            const el = e.currentTarget as HTMLInputElement
            setCaretPos(el.selectionStart ?? el.value.length)
          },
          onClick: (e) => {
            const el = e.currentTarget as HTMLInputElement
            setCaretPos(el.selectionStart ?? el.value.length)
          },
          onFocus: (e) => {
            const el = e.currentTarget as HTMLInputElement
            setCaretPos(el.selectionStart ?? el.value.length)
            setShowSuggest(true)
          },
          onBlur: () => {
            setTimeout(() => {
              if (!suggestHeld) setShowSuggest(false)
            }, 120)
          },
        }}
      />

      {/* Store-name autocomplete takes priority over the re-add chips
       *  whenever the user is in a ``@ …<caret>`` context. Both share
       *  the ``.sh-shopping-suggest`` shell so blur handling (the
       *  suggestHeld latch) stays uniform. A group of plain buttons —
       *  not a listbox, which would promise arrow-key options. */}
      {storeMatches.length > 0 ? (
        <div
          class="sh-shopping-suggest" role="group"
          aria-label={t('shopping.suggest.store_label')}
          onMouseDown={() => setSuggestHeld(true)}
          onMouseUp={() => setSuggestHeld(false)}
        >
          <span class="sh-shopping-suggest__label" aria-hidden="true">
            {t('shopping.suggest.store')}
          </span>
          {storeMatches.map((name) => (
            <button
              key={name}
              type="button"
              class="sh-chip"
              onMouseDown={(e) => e.preventDefault()}
              // Touch (iOS/WKWebView) suppresses the synthetic ``click``
              // when the preceding ``mousedown`` is preventDefault'd, so a
              // tap would do nothing — the store autocomplete looked dead
              // on mobile. Pick on ``touchend`` and cancel the would-be
              // click so it never double-fires; ``onClick`` still covers
              // mouse + keyboard (Enter/Space).
              onTouchEnd={(e) => {
                e.preventDefault()
                pickStore(name)
              }}
              onClick={() => pickStore(name)}
            >
              {name}
            </button>
          ))}
        </div>
      ) : (
        showSuggest && pastNames.length > 0 && (
          <div
            class="sh-shopping-suggest" role="group"
            aria-label={t('shopping.suggest.recent_label')}
            onMouseDown={() => setSuggestHeld(true)}
            onMouseUp={() => setSuggestHeld(false)}
          >
            <span class="sh-shopping-suggest__label" aria-hidden="true">
              {t('shopping.suggest.recent')}
            </span>
            {pastNames.map((name) => (
              <button
                key={name}
                type="button"
                class="sh-chip"
                onMouseDown={(e) => e.preventDefault()}
                // See the store chips above — touch needs an explicit
                // ``touchend`` pick (and a cancelled click) so the
                // re-add suggestion isn't dead on mobile.
                onTouchEnd={(e) => {
                  e.preventDefault()
                  void addSuggestion(name)
                }}
                onClick={() => void addSuggestion(name)}
              >
                {name}
              </button>
            ))}
          </div>
        )
      )}

      {visible.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🛒</div>
          <h3>{t('shopping.empty.title')}</h3>
          <p>{t('shopping.empty.body')}</p>
          <div class="sh-empty-state__cta-row">
            <Button onClick={() => inputRef.current?.focus()}>
              {t('shopping.empty.cta')}
            </Button>
          </div>
        </div>
      ) : grouped ? (
        <GroupedView
          {...viewProps}
          itemDrag={itemDrag}
          storeDrag={storeDrag}
          onMoveStore={handleMoveStore}
        />
      ) : (
        <FlatView {...viewProps} />
      )}
    </div>
  )
}

// ─── Flat / ungrouped rendering (existing single-list layout) ──────────

interface ViewProps {
  active: ShoppingItem[]
  completed: ShoppingItem[]
  editingId: string | null
  refocusId: string | null
  onRefocused: () => void
  onEditStart: (id: string) => void
  onEditCancel: (item: ShoppingItem, refocus: boolean) => void
  onEditSave: (item: ShoppingItem, text: string, refocus: boolean) => void
  onToggle: (id: string, completed: boolean) => void
  onDelete: (item: ShoppingItem) => void
  onClearCompleted: () => void
  onReassignStore: (id: string, nextStore: string | null) => void
  userNameById: (uid: string) => string
  storeNames: string[]
}

function rowProps(props: ViewProps, item: ShoppingItem, done: boolean) {
  return {
    item,
    done,
    isEditing: props.editingId === item.id,
    refocus: props.refocusId === item.id,
    onRefocused: props.onRefocused,
    onEditStart: () => props.onEditStart(item.id),
    onEditCancel: (refocus: boolean) => props.onEditCancel(item, refocus),
    onEditSave: (text: string, refocus: boolean) => props.onEditSave(item, text, refocus),
    onToggle: () => props.onToggle(item.id, item.completed),
    onDelete: () => props.onDelete(item),
    onReassignStore: (s: string | null) => props.onReassignStore(item.id, s),
    userNameById: props.userNameById,
    storeNames: props.storeNames,
  }
}

function DoneTrailer(props: ViewProps) {
  if (props.completed.length === 0) return null
  return (
    <>
      <ArchiveDivider
        class="sh-shopping-divider"
        label={t('shopping.done_heading', { n: String(props.completed.length) })}
        actionLabel={t('shopping.clear_all')}
        actionAriaLabel={t('shopping.clear_all_label')}
        onAction={props.onClearCompleted}
      />
      <ul class="sh-shopping-list sh-list-card sh-list-card--moss sh-shopping-list--done">
        {props.completed.map(i => <ItemRow key={i.id} {...rowProps(props, i, true)} />)}
      </ul>
    </>
  )
}

function FlatView(props: ViewProps) {
  return (
    <>
      {props.active.length > 0 && (
        <ul class="sh-shopping-list sh-list-card">
          {props.active.map(i => <ItemRow key={i.id} {...rowProps(props, i, false)} />)}
        </ul>
      )}
      <DoneTrailer {...props} />
    </>
  )
}

// ─── Grouped-by-store rendering ────────────────────────────────────────

interface GroupedProps extends ViewProps {
  itemDrag: DragBuckets<string>
  storeDrag: DragBuckets<string>
  onMoveStore: (name: string, delta: number) => void
}

function GroupedView(props: GroupedProps) {
  const { itemDrag, storeDrag } = props
  // Build an ordered list of section keys. Catalogue stores first
  // (in their sort_order), then the synthetic "No store" bucket.
  const sections: { key: string; label: string; draggable: boolean }[] = [
    ...stores.value.map((s) => ({
      key: s.name,
      label: s.name,
      draggable: true,
    })),
    { key: NO_STORE_KEY, label: t('shopping.no_store'), draggable: false },
  ]
  const dragging = itemDrag.draggingId !== null

  return (
    <>
      {sections.map((section, idx) => {
        const itemsHere = props.active.filter((i) =>
          section.key === NO_STORE_KEY
            ? !i.store
            // Fold the same way the server does — an item that slips
            // out of step with its catalogue row must not render in NO
            // section at all.
            : sameName(i.store, section.key),
        )
        // Hide a section with no ACTIVE items unless an item drag is
        // in flight — then every section is a visible drop target.
        if (itemsHere.length === 0 && !dragging) return null
        const isFirst = idx === 0
        const isLast = idx === stores.value.length - 1 // before "No store"
        const isDropTarget = itemDrag.overBucket === section.key
        return (
          <section
            key={section.key}
            aria-labelledby={`sh-shopping-group-${idx}`}
            class={
              'sh-shopping-group ' +
              (storeDrag.draggingId === section.key ? 'sh-shopping-group--dragging ' : '') +
              (isDropTarget ? 'sh-shopping-group--drop-target ' : '') +
              (section.key === NO_STORE_KEY ? 'sh-shopping-group--unassigned' : '')
            }
            {...composeDragHandlers(
              itemDrag.bucketProps(section.key),
              storeDrag.bucketProps(section.key),
            )}
          >
            <header
              class="sh-shopping-group__header"
              {...(section.draggable ? storeDrag.itemProps(section.key) : {})}
            >
              {section.draggable && (
                <span
                  class="sh-shopping-group__drag"
                  aria-hidden="true"
                  title={t('shopping.drag_reorder')}
                >
                  ⋮⋮
                </span>
              )}
              <h3 class="sh-shopping-group__name" id={`sh-shopping-group-${idx}`}>
                {section.label}
              </h3>
              <span class="sh-shopping-group__count">
                {itemsHere.length}
              </span>
              {section.draggable && (
                <div
                  class="sh-shopping-group__nudge" role="group"
                  aria-label={t('shopping.reorder_group', { name: section.label })}
                >
                  <button
                    type="button"
                    class="sh-shopping-group__nudge-btn"
                    aria-label={t('shopping.move_up', { name: section.label })}
                    disabled={isFirst}
                    onClick={() => props.onMoveStore(section.key, -1)}
                  >
                    <span aria-hidden="true">▲</span>
                  </button>
                  <button
                    type="button"
                    class="sh-shopping-group__nudge-btn"
                    aria-label={t('shopping.move_down', { name: section.label })}
                    disabled={isLast}
                    onClick={() => props.onMoveStore(section.key, +1)}
                  >
                    <span aria-hidden="true">▼</span>
                  </button>
                </div>
              )}
            </header>

            {itemsHere.length === 0 && dragging && (
              <DropPad
                label={t('organize.drop_here', { name: section.label })}
                active={isDropTarget}
              />
            )}

            {itemsHere.length > 0 && (
              <ul class="sh-shopping-list sh-list-card">
                {itemsHere.map((item) => (
                  <ItemRow
                    key={item.id}
                    {...rowProps(props, item, false)}
                    compact={true}
                    dragProps={itemDrag.itemProps(item.id)}
                  />
                ))}
              </ul>
            )}
          </section>
        )
      })}
      {/* Global "Already bought" trailer — done items from every store
       *  in one quiet pile at the bottom, like the flat view. */}
      <DoneTrailer {...props} />
    </>
  )
}

// ─── Item row + inline edit ────────────────────────────────────────────

interface RowProps {
  item: ShoppingItem
  done: boolean
  isEditing: boolean
  /** Focus the rename trigger now (its editor just closed by key). */
  refocus: boolean
  onRefocused: () => void
  onEditStart: () => void
  onEditCancel: (refocus: boolean) => void
  onEditSave: (text: string, refocus: boolean) => void
  onToggle: () => void
  onDelete: () => void
  onReassignStore: (nextStore: string | null) => void
  /** When ``true`` the store pill renders icon-only (no store name) —
   *  used in the grouped view, where the section header already names
   *  the store. The icon still opens the picker, so touch users keep
   *  a reassign target even though drag is desktop-only. */
  compact?: boolean
  userNameById: (uid: string) => string
  storeNames: string[]
  /** Grouped-view active rows only: drag onto another store section. */
  dragProps?: DragItemProps
}

/** After an Undo, put focus on the restored row's check (next frame,
 *  once it has re-rendered). */
function focusItemCheck(id: string) {
  requestAnimationFrame(() => {
    const row = [...document.querySelectorAll<HTMLElement>('.sh-shopping-item[data-item-id]')]
      .find(li => li.dataset.itemId === id)
    row?.querySelector<HTMLElement>('.sh-check-toggle')?.focus()
  })
}

/** After the focused row disappears (deleted), hand focus to a
 *  neighbour's delete button — or the quick-add input — so keyboard
 *  users aren't dropped back at the top of the document. */
function focusNeighbour(li: HTMLElement | null) {
  const next = (li?.nextElementSibling ?? li?.previousElementSibling) as HTMLElement | null
  requestAnimationFrame(() => {
    const target = next?.isConnected
      ? next.querySelector<HTMLElement>('.sh-shopping-item__delete')
      : document.querySelector<HTMLElement>('.sh-shopping-add input')
    target?.focus()
  })
}

function ItemRow(props: RowProps) {
  const { item, done, refocus, isEditing, onRefocused } = props
  const textRef = useRef<HTMLButtonElement | null>(null)
  const rowRef = useRef<HTMLLIElement | null>(null)

  useEffect(() => {
    if (!refocus || isEditing) return
    textRef.current?.focus()
    onRefocused()
  }, [refocus, isEditing, onRefocused])

  if (isEditing) return (
    <li class="sh-shopping-item sh-shopping-item--edit">
      <EditRow
        initialText={item.text}
        onSave={props.onEditSave}
        onCancel={props.onEditCancel}
      />
    </li>
  )

  return (
    <li
      ref={rowRef}
      data-item-id={item.id}
      // ``sh-row-reveal-host``: the ✕ (RowActionButton reveal) shows
      // while this row is hovered or holds focus.
      class={'sh-shopping-item sh-row-reveal-host' + (done ? ' sh-item--done' : '')}
      {...(props.dragProps ?? { draggable: false })}
    >
      {/* Standalone checkbox — never wrapping the row content, so a
       *  click on the text / pill / delete can't tick the item off. */}
      <CheckToggle
        class="sh-shopping-item__check"
        checked={done}
        label={t('shopping.check_label', { text: item.text })}
        onChange={props.onToggle}
      />
      {done ? (
        <span class="sh-shopping-item__text">{item.text}</span>
      ) : (
        <button
          ref={textRef}
          type="button"
          class="sh-shopping-item__text"
          aria-label={t('shopping.rename_label', { text: item.text })}
          title={t('shopping.rename_title')}
          onClick={props.onEditStart}
        >
          {item.text}
        </button>
      )}
      {!done && (
        <StorePicker
          currentStore={item.store ?? null}
          storeNames={props.storeNames}
          compact={props.compact}
          onPick={props.onReassignStore}
        />
      )}
      <div
        class="sh-shopping-item__meta"
        title={
          item.created_at
            ? t('shopping.added_at', { when: relativeDocsTime(item.created_at) })
            : undefined
        }
      >
        {item.created_by && (
          <span>{t('shopping.added_by', { name: props.userNameById(item.created_by) })}</span>
        )}
      </div>
      <RowActionButton
        class="sh-shopping-item__delete"
        label={t('shopping.delete_label', { text: item.text })}
        icon="✕"
        danger
        reveal
        onClick={(e) => {
          const li = rowRef.current
          props.onDelete()
          // Keyboard activation (Enter / Space reports ``detail === 0``).
          if (e.detail === 0) focusNeighbour(li)
        }}
      />
    </li>
  )
}

/** Inline rename. Enter / Save commits, Escape / Cancel backs out,
 *  and clicking or tabbing away commits too (an unchanged or blank
 *  name just closes) — the edit is never silently thrown away. */
function EditRow({
  initialText,
  onSave,
  onCancel,
}: {
  initialText: string
  onSave: (text: string, refocus: boolean) => void
  onCancel: (refocus: boolean) => void
}) {
  const [text, setText] = useState(initialText)
  const textRef = useRef<HTMLInputElement | null>(null)
  /** Set once the edit is resolved so a late focusout can't fire a
   *  second save after Enter / Escape. */
  const doneRef = useRef(false)
  useEffect(() => {
    textRef.current?.focus()
    textRef.current?.select()
  }, [])
  const submit = (refocus: boolean) => {
    if (doneRef.current) return
    doneRef.current = true
    onSave(text, refocus)
    // A failed save keeps the editor open — allow another try.
    requestAnimationFrame(() => { doneRef.current = false })
  }
  const cancel = (refocus: boolean) => {
    if (doneRef.current) return
    doneRef.current = true
    onCancel(refocus)
  }
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      submit(true)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      cancel(true)
    }
  }
  return (
    <div
      class="sh-shopping-edit"
      onFocusOut={(e) => {
        const next = e.relatedTarget as Node | null
        if (next && (e.currentTarget as HTMLElement).contains(next)) return
        submit(false)
      }}
    >
      <input
        ref={textRef}
        type="text"
        value={text}
        aria-label={t('shopping.edit.label')}
        onInput={(e) => setText((e.target as HTMLInputElement).value)}
        onKeyDown={onKey}
        class="sh-shopping-edit__text"
      />
      <Button
        type="button"
        // Keep focus in the input so its focusout doesn't save first
        // (Safari never focuses a clicked button).
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => submit(true)}
        disabled={!text.trim()}
      >
        {t('common.save')}
      </Button>
      <button
        type="button"
        class="sh-link sh-shopping-edit__cancel"
        // Keep the input's focusout from committing before the click.
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => cancel(true)}
      >
        {t('common.cancel')}
      </button>
    </div>
  )
}

// ─── Store picker (row pill → OverflowMenu) ────────────────────────────

interface StorePickerProps {
  /** Current store on the item, or ``null`` for "No store". */
  currentStore: string | null
  /** Household catalogue. The picker also offers "+ New store…". */
  storeNames: string[]
  onPick: (next: string | null) => void
  /** Icon-only pill (no store-name label) — used in the grouped view
   *  where the section header already names the store. */
  compact?: boolean
}

/** One-tap reassign affordance on the item row.
 *
 *  The pill opens the shared ``OverflowMenu`` (arrow keys, Escape
 *  returns focus to the pill, opens on the current store) with the
 *  catalogue, "No store" and "+ New store…". The last swaps to a small
 *  inline name-entry panel — Save creates the store *and* assigns the
 *  item in one round-trip (the server upserts unknown store names);
 *  Escape / Back return to the list. Rename / delete live in the
 *  header's Stores dialog. On ≤640 px both dock as a bottom sheet. */
function StorePicker({
  currentStore,
  storeNames,
  onPick,
  compact,
}: StorePickerProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const newInputRef = useRef<HTMLInputElement | null>(null)

  const trigger = () =>
    wrapRef.current?.querySelector<HTMLElement>('button[aria-haspopup="menu"]')

  const closeAdding = (refocus: boolean) => {
    setAdding(false)
    setNewName('')
    if (refocus) trigger()?.focus()
  }

  const backToList = () => {
    setAdding(false)
    setNewName('')
    setMenuOpen(true)
  }

  useEffect(() => {
    if (adding) newInputRef.current?.focus()
  }, [adding])

  // Outside press closes the new-store panel.
  useEffect(() => {
    if (!adding) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current?.contains(e.target as Node)) return
      setAdding(false)
      setNewName('')
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [adding])

  const pick = (next: string | null) => {
    if (next !== currentStore) onPick(next)
  }

  const saveNew = () => {
    const trimmed = newName.trim()
    if (!trimmed) return
    onPick(trimmed)
    closeAdding(true)
  }

  const optClass = (current: boolean) =>
    'sh-shopping-store-picker__opt'
    + (current ? ' sh-shopping-store-picker__opt--current' : '')

  const menuItems: MenuItem[] = [
    ...storeNames.map((name) => ({
      key: `store:${name}`,
      label: name,
      checked: sameName(currentStore, name),
      class: optClass(sameName(currentStore, name)),
      onSelect: () => pick(name),
    })),
    {
      key: '__none__',
      label: t('shopping.no_store'),
      checked: currentStore === null,
      class: optClass(currentStore === null) + ' sh-shopping-store-picker__opt--none',
      onSelect: () => pick(null),
    },
    {
      key: '__new__',
      label: t('shopping.picker.new'),
      class: 'sh-shopping-store-picker__opt sh-shopping-store-picker__opt--new',
      onSelect: () => setAdding(true),
    },
  ]

  const label = currentStore || t('shopping.picker.set')
  const ariaLabel = currentStore
    ? t('shopping.picker.label_current', { name: currentStore })
    : t('shopping.picker.set')

  return (
    <div class="sh-shopping-store-picker" ref={wrapRef}>
      <OverflowMenu
        label={ariaLabel}
        title={currentStore
          ? t('shopping.picker.title_current', { name: currentStore })
          : t('shopping.picker.title_empty')}
        items={menuItems}
        bareTrigger
        wrapClass="sh-shopping-store-picker__wrap"
        menuClass="sh-shopping-store-picker__menu"
        triggerClass={
          'sh-shopping-store-pill'
          + (currentStore ? '' : ' sh-shopping-store-pill--empty')
          + (compact ? ' sh-shopping-store-pill--compact' : '')
        }
        open={menuOpen}
        onOpenChange={setMenuOpen}
      >
        <span aria-hidden="true">📍</span>
        {!compact && <span>{label}</span>}
        <span aria-hidden="true" class="sh-shopping-store-pill__chev">▾</span>
      </OverflowMenu>
      {adding && (
        <div
          class="sh-shopping-store-picker__menu sh-shopping-store-picker__new"
          role="dialog"
          aria-label={t('shopping.picker.new_dialog')}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault()
              e.stopPropagation()
              backToList()
            }
          }}
        >
          <button
            type="button"
            class="sh-shopping-store-picker__back"
            onClick={backToList}
          >
            {t('shopping.picker.back')}
          </button>
          <input
            ref={newInputRef}
            type="text"
            class="sh-shopping-store-picker__new-input"
            placeholder={t('shopping.stores.placeholder')}
            aria-label={t('shopping.stores.new_label')}
            value={newName}
            onInput={(e) => setNewName((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                saveNew()
              }
            }}
          />
          <Button
            type="button"
            onClick={saveNew}
            disabled={!newName.trim()}
          >
            {t('common.save')}
          </Button>
        </div>
      )}
    </div>
  )
}
