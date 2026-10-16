/**
 * SearchBar — global search (§23.2).
 *
 * v1 lists this household's people by name / username. Picking one hands
 * ``('user', user_id, row)`` to ``onSelect`` (the app shell opens their
 * person sheet) and clears the bar.
 */
import { signal } from '@preact/signals'
import { useRef } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { openPerson } from './PersonSheet'

/** The slice of a ``GET /api/users`` row the bar shows and hands on. */
export interface SearchUser {
  user_id: string
  username: string
  display_name?: string | null
  picture_url?: string | null
}

const query = signal('')
const results = signal<SearchUser[]>([])
const open = signal(false)
let debounceTimer: ReturnType<typeof setTimeout>

async function doSearch(q: string) {
  if (q.length < 2) { results.value = []; return }
  try {
    // v1: search users by name prefix via GET /api/users?q=...
    const users = await api.get('/api/users') as SearchUser[]
    const needle = q.toLowerCase()
    results.value = users.filter(u =>
      u.display_name?.toLowerCase().includes(needle) ||
      u.username?.toLowerCase().includes(needle)
    ).slice(0, 10)
  } catch {
    results.value = []
  }
}

/** The app shell's ``onSelect``: a person opens their sheet (the bar
 *  only lists this household's people today). */
export function openSearchResult(type: string, id: string, row?: SearchUser): void {
  if (type !== 'user') return
  openPerson(id, row
    ? {
        person: {
          display_name: row.display_name || row.username,
          picture_url: row.picture_url ?? null,
          username: row.username,
          is_local: true,
        },
      }
    : {})
}

interface SearchBarProps {
  onSelect?: (type: string, id: string, row?: SearchUser) => void
}

export function SearchBar({ onSelect }: SearchBarProps) {
  const handleInput = (e: Event) => {
    const val = (e.target as HTMLInputElement).value
    query.value = val
    open.value = true
    clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => doSearch(val), 300)
  }

  const root = useRef<HTMLDivElement | null>(null)
  // Close when focus leaves the bar — but not when it Tabs from the
  // input into a result (or between results), so the list is
  // keyboard-reachable. The delay lets a pointer tap land first.
  const onBlur = (e: FocusEvent) => {
    const next = e.relatedTarget as Node | null
    if (next && root.current?.contains(next)) return
    setTimeout(() => { open.value = false }, 200)
  }

  const pick = (r: SearchUser) => {
    clearTimeout(debounceTimer)
    query.value = ''
    results.value = []
    open.value = false
    onSelect?.('user', r.user_id, r)
  }

  return (
    <div
      class="sh-search"
      ref={root}
      onKeyDown={(e) => { if (e.key === 'Escape') open.value = false }}
    >
      <input class="sh-search-input" type="search"
        placeholder={`🔍 ${t('search.bar_placeholder')}`}
        aria-label={t('nav.search')}
        data-shortcut="search" aria-keyshortcuts="/"
        value={query.value} onInput={handleInput}
        onFocus={() => open.value = true}
        onBlur={onBlur} />
      {/* A popover anchored under the bar (absolutely positioned, see
        * ``.sh-search-popover``) — never a flow child of the TopBar, or
        * opening it grows the bar and shoves the page title down. */}
      {open.value && results.value.length > 0 && (
        <div class="sh-search-results sh-search-popover">
          <div class="sh-search-popover__list">
            {results.value.map(r => {
              const name = r.display_name || r.username
              return (
                <button key={r.user_id} type="button" class="sh-search-result"
                  title={`${name} @${r.username}`}
                  onClick={() => pick(r)} onBlur={onBlur}>
                  <strong class="sh-search-result__name">{name}</strong>
                  {/* The space keeps the accessible name "Lina @lina",
                    * not "Lina@lina"; the flex gap does the visual work. */}
                  {' '}
                  <span class="sh-search-result__handle sh-muted">@{r.username}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
