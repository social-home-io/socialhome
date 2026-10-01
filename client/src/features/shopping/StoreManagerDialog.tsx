import { useEffect, useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import {
  items,
  stores,
  createStore,
  renameStore,
  deleteStore,
  reorderStores,
} from '@/store/shopping'
import { t } from '@/i18n/i18n'

/** Drag payload for a store row inside this dialog. Scoped to the
 *  dialog so it can never be confused with the page's item /
 *  section-header drags (``application/x-sh-shopping-item`` and
 *  ``…-store``), which share the same document. */
const DRAG_MIME = 'application/x-sh-store-manager'

interface StoreManagerDialogProps {
  open: boolean
  onClose: () => void
}

/** "1 item" / "3 items", translated.
 *  Keys reached dynamically (for ``i18n:check``): t('shopping.stores.items')
 *  t('shopping.stores.items_one') t('shopping.error.reorder')
 *  t('shopping.error.rename') t('shopping.error.delete_store')
 *  t('shopping.error.add_store') */
function itemCount(n: number): string {
  return t(n === 1 ? 'shopping.stores.items_one' : 'shopping.stores.items', { n: String(n) })
}

/** Store catalogue manager (§23.120).
 *
 *  The ONE place a household renames, reorders, deletes or adds a
 *  shop. Rename/delete used to live only inside an item row's 📍
 *  popover, which meant an empty list — or a list whose items were
 *  all bought — offered no way to manage stores at all. The page
 *  header opens this dialog unconditionally instead.
 *
 *  Reads the ``items`` / ``stores`` signals directly: ``loadShopping``
 *  already fetches items with ``include_completed=true``, so the
 *  per-store count is derived, never fetched.
 */
export function StoreManagerDialog({ open, onClose }: StoreManagerDialogProps) {
  /** Name of the store currently in inline-rename mode, or null. */
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  /** Whether the "+ Add store" input is showing. */
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  /** Store name being dragged for reorder, or null. */
  const [dragName, setDragName] = useState<string | null>(null)

  const renameRef = useRef<HTMLInputElement | null>(null)
  const addRef = useRef<HTMLInputElement | null>(null)

  // Trip order is the catalogue's ``sort_order`` — the sequence the
  // household walks the shops in. Computed inline rather than
  // memoised: the signal read has to happen during render for the
  // dialog to re-render on a reorder, and a household's shop list is
  // a handful of rows.
  const ordered = [...stores.value].sort((a, b) => a.sort_order - b.sort_order)

  /** Items sitting at ``name`` — completed ones included, because the
   *  count describes the store, not the remaining shopping. */
  const countFor = (name: string): number =>
    items.value.filter(
      i => (i.store ?? '').toLowerCase() === name.toLowerCase(),
    ).length

  // Reset every transient sub-state when the dialog closes so the
  // next open starts clean (no half-typed rename hanging around).
  useEffect(() => {
    if (!open) {
      setRenaming(null)
      setRenameDraft('')
      setAdding(false)
      setNewName('')
      setDragName(null)
    }
  }, [open])

  useEffect(() => {
    if (renaming) {
      renameRef.current?.focus()
      renameRef.current?.select()
    }
  }, [renaming])

  useEffect(() => {
    if (adding) addRef.current?.focus()
  }, [adding])

  const fail = (key: string, err: unknown) =>
    showToast(t(key, { error: String((err as Error)?.message ?? err) }), 'error')

  const commitOrder = async (next: string[]) => {
    try {
      await reorderStores(next)
    } catch (err: unknown) {
      fail('shopping.error.reorder', err)
    }
  }

  /** Keyboard-operable reorder. The ⋮⋮ drag handle is pointer-only,
   *  so ▲/▼ is not a nicety — it's the accessible path. */
  const nudge = (name: string, delta: number) => {
    const order = ordered.map(s => s.name)
    const i = order.indexOf(name)
    const j = i + delta
    if (i < 0 || j < 0 || j >= order.length) return
    const next = order.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    void commitOrder(next)
  }

  const dropOn = (target: string) => {
    const from = dragName
    setDragName(null)
    if (!from || from === target) return
    const order = ordered.map(s => s.name)
    const i = order.indexOf(from)
    const j = order.indexOf(target)
    if (i < 0 || j < 0) return
    const next = order.slice()
    next.splice(i, 1)
    next.splice(j, 0, from)
    void commitOrder(next)
  }

  const startRename = (name: string) => {
    setAdding(false)
    setRenaming(name)
    setRenameDraft(name)
  }

  const cancelRename = () => {
    setRenaming(null)
    setRenameDraft('')
  }

  const saveRename = async () => {
    const from = renaming
    const to = renameDraft.trim()
    if (!from || !to || to === from) {
      cancelRename()
      return
    }
    // A name that matches ANOTHER row case-insensitively is a merge,
    // not a rename — the server folds the two stores together. Say so
    // before it happens; the moved items are the user's real concern.
    const target = ordered.find(
      s => s.name !== from && s.name.toLowerCase() === to.toLowerCase(),
    )
    if (target) {
      const n = countFor(from)
      const ok = await confirmDialog(
        t('shopping.stores.merge_confirm', {
          target: target.name, from, count: itemCount(n),
        }),
        { destructive: true, confirmLabel: t('shopping.stores.merge') },
      )
      if (!ok) return
    }
    try {
      const result = await renameStore(from, to)
      cancelRename()
      if (result.merged) {
        // ``new_name`` is the spelling that SURVIVED — on a merge
        // that's the target's casing, not what the user typed.
        showToast(
          t('shopping.stores.merged', {
            name: result.new_name, count: itemCount(result.moved_items),
          }),
          'info',
        )
      } else {
        showToast(t('shopping.stores.renamed', { name: result.new_name }), 'info')
      }
    } catch (err: unknown) {
      fail('shopping.error.rename', err)
    }
  }

  const removeStore = async (name: string) => {
    const n = countFor(name)
    const ok = await confirmDialog(
      n > 0
        ? t('shopping.stores.delete_confirm', { name, count: itemCount(n) })
        : t('shopping.stores.delete_confirm_empty', { name }),
      { destructive: true, confirmLabel: t('common.delete') },
    )
    if (!ok) return
    try {
      await deleteStore(name)
    } catch (err: unknown) {
      fail('shopping.error.delete_store', err)
    }
  }

  const saveNew = async () => {
    const name = newName.trim()
    if (!name) return
    // ``POST /api/shopping/stores`` is idempotent case-insensitively,
    // so adding an existing name succeeds silently — which reads as a
    // broken button. Detect it up front and say what happened.
    const existed = stores.value.some(
      s => s.name.toLowerCase() === name.toLowerCase(),
    )
    try {
      const store = await createStore(name)
      setNewName('')
      setAdding(false)
      if (existed) {
        showToast(t('shopping.stores.exists', { name: store.name }), 'info')
      }
    } catch (err: unknown) {
      fail('shopping.error.add_store', err)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={t('shopping.stores.title')}>
      {/* Only meaningful once there is an order to speak of — the
        * empty state below already explains what stores are for. */}
      {ordered.length > 0 && (
        <p class="sh-muted sh-store-manager__hint">
          {t('shopping.stores.hint')}
        </p>
      )}

      {ordered.length === 0 ? (
        <div class="sh-empty-state sh-store-manager__empty">
          <div aria-hidden="true">🏪</div>
          <h3>{t('shopping.stores.empty_title')}</h3>
          <p>{t('shopping.stores.empty_body')}</p>
        </div>
      ) : (
        <ul class="sh-store-manager">
          {ordered.map((s, idx) => {
            const name = s.name
            const n = countFor(name)
            const isRenaming = renaming === name
            return (
              <li
                key={name}
                class={
                  'sh-store-manager__row '
                  + (dragName === name ? 'sh-store-manager__row--dragging' : '')
                }
                onDragOver={(e) => {
                  if (e.dataTransfer?.types.includes(DRAG_MIME)) {
                    e.preventDefault()
                  }
                }}
                onDrop={(e) => {
                  if (!e.dataTransfer?.types.includes(DRAG_MIME)) return
                  e.preventDefault()
                  dropOn(name)
                }}
              >
                <span
                  class="sh-store-manager__drag"
                  aria-hidden="true"
                  title={t('shopping.drag_reorder')}
                  draggable={!isRenaming}
                  onDragStart={(e) => {
                    e.dataTransfer?.setData(DRAG_MIME, name)
                    if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
                    setDragName(name)
                  }}
                  onDragEnd={() => setDragName(null)}
                >
                  ⋮⋮
                </span>

                <div class="sh-store-manager__nudge" role="group" aria-label={t('shopping.reorder_group', { name })}>
                  <button
                    type="button"
                    class="sh-store-manager__nudge-btn"
                    aria-label={t('shopping.move_up', { name })}
                    disabled={idx === 0}
                    onClick={() => nudge(name, -1)}
                  >
                    ▲
                  </button>
                  <button
                    type="button"
                    class="sh-store-manager__nudge-btn"
                    aria-label={t('shopping.move_down', { name })}
                    disabled={idx === ordered.length - 1}
                    onClick={() => nudge(name, +1)}
                  >
                    ▼
                  </button>
                </div>

                {isRenaming ? (
                  <div class="sh-store-manager__edit">
                    <input
                      ref={renameRef}
                      type="text"
                      class="sh-store-manager__input"
                      aria-label={t('shopping.stores.rename_input', { name })}
                      value={renameDraft}
                      onInput={(e) =>
                        setRenameDraft((e.target as HTMLInputElement).value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          void saveRename()
                        } else if (e.key === 'Escape') {
                          // Swallow so the Modal's Escape handler
                          // doesn't close the whole dialog — one step
                          // back at a time.
                          e.preventDefault()
                          e.stopPropagation()
                          cancelRename()
                        }
                      }}
                    />
                    <Button
                      type="button"
                      onClick={() => void saveRename()}
                      disabled={!renameDraft.trim()}
                    >
                      {t('common.save')}
                    </Button>
                    <button
                      type="button"
                      class="sh-link sh-store-manager__cancel"
                      onClick={cancelRename}
                    >
                      {t('common.cancel')}
                    </button>
                  </div>
                ) : (
                  <>
                    <span class="sh-store-manager__name">{name}</span>
                    <span class="sh-store-manager__count">
                      {itemCount(n)}
                    </span>
                    <div class="sh-store-manager__actions">
                      <button
                        type="button"
                        class="sh-store-manager__action"
                        aria-label={t('shopping.stores.rename_label', { name })}
                        title={t('shopping.stores.rename_label', { name })}
                        onClick={() => startRename(name)}
                      >
                        {t('shopping.stores.rename')}
                      </button>
                      <button
                        type="button"
                        class="sh-store-manager__action sh-store-manager__action--danger"
                        aria-label={t('shopping.stores.delete_label', { name })}
                        title={t('shopping.stores.delete_label', { name })}
                        onClick={() => void removeStore(name)}
                      >
                        {t('common.delete')}
                      </button>
                    </div>
                  </>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {adding ? (
        <div class="sh-store-manager__add">
          <input
            ref={addRef}
            type="text"
            class="sh-store-manager__add-input"
            placeholder={t('shopping.stores.placeholder')}
            aria-label={t('shopping.stores.new_label')}
            value={newName}
            onInput={(e) => setNewName((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                void saveNew()
              } else if (e.key === 'Escape') {
                e.preventDefault()
                e.stopPropagation()
                setAdding(false)
                setNewName('')
              }
            }}
          />
          <Button type="button" onClick={() => void saveNew()} disabled={!newName.trim()}>
            {t('common.save')}
          </Button>
          <button
            type="button"
            class="sh-link sh-store-manager__cancel"
            onClick={() => { setAdding(false); setNewName('') }}
          >
            {t('common.cancel')}
          </button>
        </div>
      ) : (
        <button
          type="button"
          /* With an empty catalogue this is the ONLY thing to do here, so
           * it carries the primary weight; once there are rows to manage
           * it steps back and lets them lead.
           *
           * Deliberately a plain <button>, not <Button>: that component
           * spreads {...props} AFTER its own class=, so passing a class
           * REPLACES `sh-btn sh-btn--{variant}` rather than extending it,
           * and `variant` would silently do nothing. */
          class={
            'sh-btn sh-store-manager__addbtn '
            + (ordered.length === 0 ? 'sh-btn--primary' : 'sh-btn--secondary')
          }
          onClick={() => { cancelRename(); setAdding(true) }}
        >
          {t('shopping.stores.add')}
        </button>
      )}
    </Modal>
  )
}

export default StoreManagerDialog
