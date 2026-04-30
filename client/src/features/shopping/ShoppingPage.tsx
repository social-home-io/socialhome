import { useEffect, useMemo, useRef, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import {
  items,
  loadShopping,
  wireShoppingWs,
  addItem,
  toggleItem,
  deleteItem,
  clearCompleted,
} from '@/store/shopping'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'

const loading = signal(true)

function _relativeTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60)    return 'just now'
  if (s < 3600)  return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return `${Math.floor(s / 86400)} d ago`
}

export default function ShoppingPage() {
  useTitle('Shopping')
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    wireShoppingWs()
    void loadHouseholdUsers()
    loadShopping().then(() => { loading.value = false })
  }, [])

  const [draft, setDraft] = useState('')
  const [showSuggest, setShowSuggest] = useState(false)
  const [suggestHeld, setSuggestHeld] = useState(false)

  // §23.129.2 quick-add: the ``draft`` input accepts a comma-separated
  // list. On submit we split, dedupe against the current live list,
  // and post each as a separate item. "Past items" suggestions surface
  // completed names so users can re-add with one tap.
  const pastNames = useMemo(() => {
    const names = items.value
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
  }, [items.value])

  const handleQuickAdd = async (e: Event) => {
    e.preventDefault()
    const raw = draft.trim()
    if (!raw) return
    const parts = raw.split(',').map(s => s.trim()).filter(Boolean)
    if (parts.length === 0) return
    const existing = new Set(
      items.value
        .filter(i => !i.completed)
        .map(i => (i.text || '').toLowerCase()),
    )
    const dupes: string[] = []
    try {
      for (const name of parts) {
        if (existing.has(name.toLowerCase())) {
          dupes.push(name)
          continue
        }
        await addItem(name)
        existing.add(name.toLowerCase())
      }
      setDraft('')
      setShowSuggest(false)
      if (dupes.length && parts.length === dupes.length) {
        showToast('All items are already on the list', 'info')
      } else if (dupes.length) {
        showToast(`${dupes.length} duplicate skipped`, 'info')
      }
    } catch (err: unknown) {
      showToast(`Add failed: ${(err as Error)?.message ?? err}`, 'error')
      // Keep the draft so the user can retry without retyping.
    }
  }

  const addSuggestion = async (name: string) => {
    try {
      await addItem(name)
      inputRef.current?.focus()
    } catch (err: unknown) {
      showToast(`Add failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleToggle = async (id: string, completed: boolean) => {
    try {
      await toggleItem(id, !completed)
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await deleteItem(id)
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  const handleClearCompleted = async () => {
    if (!confirm('Clear all completed items? This cannot be undone.')) return
    try {
      await clearCompleted()
    } catch (err: unknown) {
      showToast(`Clear failed: ${(err as Error)?.message ?? err}`, 'error')
    }
  }

  // Autofocus on mount so keyboard flow ("open page → start typing →
  // Enter → repeat") works without an extra click.
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  if (loading.value) return <Spinner />

  const active    = items.value.filter(i => !i.completed)
  const completed = items.value.filter(i =>  i.completed)
  const me        = currentUser.value

  const userNameById = (uid: string): string => {
    if (me?.user_id === uid) return 'you'
    const found = householdUsers.value.get(uid)
    return found?.display_name || found?.username || uid.slice(0, 6)
  }

  return (
    <div class="sh-shopping">
      <div class="sh-page-header">
        <span class="sh-muted">
          {active.length} to buy · {completed.length} done
        </span>
      </div>
      <form onSubmit={handleQuickAdd} class="sh-shopping-add">
        <input
          ref={inputRef}
          name="text"
          value={draft}
          placeholder="Add one — or paste several separated by commas…"
          autoComplete="off"
          onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
          onFocus={() => setShowSuggest(true)}
          onBlur={() => {
            // Defer hide so a pointer-down on a suggestion chip below
            // can register before the dropdown disappears.
            setTimeout(() => {
              if (!suggestHeld) setShowSuggest(false)
            }, 120)
          }}
          aria-label="New shopping item"
        />
        <Button type="submit" disabled={!draft.trim()}>Add</Button>
      </form>

      {showSuggest && pastNames.length > 0 && (
        <div
          class="sh-shopping-suggest" role="listbox"
          onMouseDown={() => setSuggestHeld(true)}
          onMouseUp={() => setSuggestHeld(false)}
        >
          <span class="sh-muted">Re-add recent:</span>
          {pastNames.map((name) => (
            <button
              key={name}
              type="button"
              class="sh-chip"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => void addSuggestion(name)}
            >
              {name}
            </button>
          ))}
        </div>
      )}

      {items.value.length === 0 ? (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }}>🛒</div>
          <h3>Your list is empty</h3>
          <p>Type an item above. Paste multiple, separated by commas.</p>
        </div>
      ) : (
        <>
          <ul class="sh-shopping-list sh-list-card">
            {active.map(item => (
              <li key={item.id} class="sh-shopping-item">
                <label class="sh-shopping-item__main">
                  <input
                    type="checkbox"
                    checked={false}
                    onChange={() => handleToggle(item.id, item.completed)}
                    aria-label={`Mark ${item.text} as bought`}
                  />
                  <span class="sh-shopping-item__text">{item.text}</span>
                </label>
                <div
                  class="sh-shopping-item__meta"
                  title={
                    item.created_at
                      ? `Added ${_relativeTime(item.created_at)}`
                      : undefined
                  }
                >
                  {item.created_by && (
                    <span>+ {userNameById(item.created_by)}</span>
                  )}
                </div>
                <button
                  type="button"
                  class="sh-shopping-item__delete"
                  aria-label={`Delete ${item.text}`}
                  title="Delete"
                  onClick={() => void handleDelete(item.id)}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>

          {completed.length > 0 && (
            <>
              <div class="sh-shopping-divider">
                <span>Already bought ({completed.length})</span>
                <button
                  type="button"
                  class="sh-link"
                  onClick={() => void handleClearCompleted()}
                >
                  Clear all
                </button>
              </div>
              <ul class="sh-shopping-list sh-list-card sh-list-card--moss sh-shopping-list--done">
                {completed.map(item => (
                  <li key={item.id} class="sh-shopping-item sh-item--done">
                    <label class="sh-shopping-item__main">
                      <input
                        type="checkbox"
                        checked={true}
                        onChange={() => handleToggle(item.id, item.completed)}
                        aria-label={`Put ${item.text} back on the list`}
                      />
                      <span class="sh-shopping-item__text">{item.text}</span>
                    </label>
                    <div class="sh-shopping-item__meta">
                      {item.created_by && (
                        <span>+ {userNameById(item.created_by)}</span>
                      )}
                    </div>
                    <button
                      type="button"
                      class="sh-shopping-item__delete"
                      aria-label={`Delete ${item.text}`}
                      onClick={() => void handleDelete(item.id)}
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  )
}
