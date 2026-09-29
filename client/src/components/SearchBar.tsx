/**
 * SearchBar — global search (§23.2).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

const query = signal('')
const results = signal<any[]>([])
const open = signal(false)
let debounceTimer: ReturnType<typeof setTimeout>

async function doSearch(q: string) {
  if (q.length < 2) { results.value = []; return }
  try {
    // v1: search users by name prefix via GET /api/users?q=...
    const users = await api.get('/api/users')
    results.value = users.filter((u: any) =>
      u.display_name?.toLowerCase().includes(q.toLowerCase()) ||
      u.username?.toLowerCase().includes(q.toLowerCase())
    ).slice(0, 10)
  } catch {
    results.value = []
  }
}

interface SearchBarProps {
  onSelect?: (type: string, id: string) => void
}

export function SearchBar({ onSelect }: SearchBarProps) {
  const handleInput = (e: Event) => {
    const val = (e.target as HTMLInputElement).value
    query.value = val
    open.value = true
    clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => doSearch(val), 300)
  }

  return (
    <div class="sh-search">
      <input class="sh-search-input" type="search" placeholder="🔍 Search..."
        data-shortcut="search" aria-keyshortcuts="/"
        value={query.value} onInput={handleInput}
        onFocus={() => open.value = true}
        onBlur={() => setTimeout(() => open.value = false, 200)} />
      {open.value && results.value.length > 0 && (
        <div class="sh-search-results">
          {results.value.map((r: any) => (
            <button key={r.user_id || r.id} class="sh-search-result"
              onClick={() => onSelect?.('user', r.user_id)}>
              <strong>{r.display_name || r.name}</strong>
              <span class="sh-muted">@{r.username}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
