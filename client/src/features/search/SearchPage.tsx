/**
 * SearchPage — full-text search over posts / spaces / people / pages /
 * messages (spec §23.2).
 *
 * Hits ``GET /api/search?q=&type=&space_id=&limit=`` and renders the
 * snippet (which the backend wraps in ``<mark>`` tags via FTS5
 * ``snippet()``) as text — see :func:`snippetParts`.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

interface Hit {
  scope: string
  ref_id: string
  space_id: string | null
  title: string
  snippet: string
}

interface SearchResponse {
  hits: Hit[]
  counts: Record<string, number>
}

const query = signal('')
/** spec's high-level filter ``type``. '' means All (no filter). */
const activeType = signal<string>('')
const hits = signal<Hit[]>([])
const counts = signal<Record<string, number>>({})
const loading = signal(false)
const loadingMore = signal(false)
const lastQuery = signal('')
/** Page size — mirrors the server default so "load more" returns a
 *  full batch unless the caller is at the end of results. */
const PAGE_SIZE = 20
/** True while the last request returned a full page (so there may be
 *  more). Reset on each fresh query. */
const hasMore = signal(false)

/** Filter chips per spec §23.2.3. */
/** Filter chips; ``label`` is a translation key, looked up at render. */
const FILTERS: { type: string, label: string }[] = [
  { type: '',         label: 'search.scope.all'      },
  { type: 'posts',    label: 'search.filter.posts'   },
  { type: 'people',   label: 'search.filter.people'  },
  { type: 'spaces',   label: 'search.filter.spaces'  },
  { type: 'pages',    label: 'search.scope.page'     },
  { type: 'messages', label: 'search.filter.dms'     },
]

/** Result-kind label keys, looked up at render. */
const SCOPE_LABELS: Record<string, string> = {
  post:       'search.scope.post',
  space_post: 'search.hit.space_post',
  message:    'dms.direct_message',
  page:       'search.hit.page',
  user:       'search.hit.person',
  space:      'search.hit.space',
}

/** Sum of the underlying scopes each filter covers (for chip counts). */
function countsFor(type: string, all: Record<string, number>): number {
  if (type === '')         return Object.values(all).reduce((a, b) => a + b, 0)
  if (type === 'posts')    return (all.post ?? 0) + (all.space_post ?? 0)
  if (type === 'people')   return all.user ?? 0
  if (type === 'spaces')   return all.space ?? 0
  if (type === 'pages')    return all.page ?? 0
  if (type === 'messages') return all.message ?? 0
  return 0
}

function emptyMessage(type: string, q: string): string {
  switch (type) {
    case 'posts':    return t('search.empty.posts', { query: q })
    case 'people':   return t('search.empty.people', { query: q })
    case 'spaces':   return t('search.empty.spaces', { query: q })
    case 'pages':    return t('search.empty.pages', { query: q })
    case 'messages': return t('search.empty.messages', { query: q })
    default:         return t('search.empty.all', { query: q })
  }
}

/** Split an FTS5 ``snippet()`` into text runs, flagging the ones the
 *  server wrapped in ``<mark>…</mark>``.
 *
 *  ``snippet()`` copies the indexed body verbatim — only the
 *  ``<mark>`` delimiters are server-generated, the text between them
 *  is whatever a post / DM / page author (possibly on another
 *  household) typed, markup included. So the snippet is rendered as
 *  Preact text nodes, never via ``innerHTML``. A literal ``<mark>``
 *  typed by an author is indistinguishable from a delimiter and gets
 *  highlighted — harmless, it carries no attributes. */
function snippetParts(snippet: string): { text: string, mark: boolean }[] {
  const out: { text: string, mark: boolean }[] = []
  const re = /<mark>([\s\S]*?)<\/mark>/g
  let last = 0
  for (let m = re.exec(snippet); m !== null; m = re.exec(snippet)) {
    if (m.index > last) out.push({ text: snippet.slice(last, m.index), mark: false })
    out.push({ text: m[1], mark: true })
    last = m.index + m[0].length
  }
  if (last < snippet.length) out.push({ text: snippet.slice(last), mark: false })
  return out
}

export default function SearchPage() {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const q = params.get('q') || ''
    const type = params.get('type') || ''
    if (q) {
      query.value = q
      activeType.value = type
      void runSearch()
    }
  }, [])

  const q = query.value.trim()
  const tooShort = q.length > 0 && q.length < 2

  return (
    <div class="sh-search-page">
      <h2>{t('search.title')}</h2>
      <form
        onSubmit={(e) => { e.preventDefault(); void runSearch() }}
        class="sh-row"
      >
        <input
          type="search"
          placeholder={t('search.page_placeholder')}
          value={query.value}
          onInput={(e) => (query.value = (e.target as HTMLInputElement).value)}
          autoFocus
        />
        <Button type="submit">{t('search.submit')}</Button>
      </form>

      <div class="sh-filter-chips" role="tablist">
        {FILTERS.map(f => {
          const c = countsFor(f.type, counts.value)
          const isActive = activeType.value === f.type
          return (
            <button
              key={f.type}
              type="button"
              role="tab"
              aria-selected={isActive}
              class={`sh-chip ${isActive ? 'sh-chip-active' : ''}`}
              onClick={() => {
                activeType.value = f.type
                void runSearch()
              }}
            >
              {t(f.label)}{c > 0 && <span class="sh-chip-count">{c}</span>}
            </button>
          )
        })}
      </div>

      {loading.value && <Spinner />}

      {!loading.value && tooShort && (
        <p class="sh-muted">{t('search.keep_typing')}</p>
      )}

      {!loading.value && !lastQuery.value && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🔎</div>
          <h3>{t('search.intro_title')}</h3>
          <p>{t('search.intro_body')}</p>
        </div>
      )}

      {!loading.value && !tooShort && lastQuery.value && hits.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🗂️</div>
          <h3>{emptyMessage(activeType.value, lastQuery.value)}</h3>
          <p>{t('search.empty_hint')}</p>
        </div>
      )}

      <ul class="sh-search-results">
        {hits.value.map(h => (
          <li key={`${h.scope}:${h.ref_id}`} class="sh-search-hit sh-card">
            <header class="sh-row sh-justify-between">
              <strong>{SCOPE_LABELS[h.scope] ? t(SCOPE_LABELS[h.scope]) : h.scope}</strong>
              {h.space_id && <span class="sh-muted">{h.space_id}</span>}
            </header>
            {h.title && <h4>{h.title}</h4>}
            <p class="sh-snippet">
              {snippetParts(h.snippet).map((part, i) => (
                part.mark ? <mark key={i}>{part.text}</mark> : part.text
              ))}
            </p>
          </li>
        ))}
      </ul>

      {hasMore.value && hits.value.length > 0 && (
        <div class="sh-row sh-justify-center">
          <Button onClick={() => void loadMore()} loading={loadingMore.value}>
            {t('feed.load_more')}
          </Button>
        </div>
      )}
    </div>
  )
}

async function runSearch() {
  const q = query.value.trim()
  if (q.length < 2) {
    hits.value = []
    counts.value = {}
    hasMore.value = false
    lastQuery.value = q
    return
  }
  loading.value = true
  lastQuery.value = q
  try {
    const params = new URLSearchParams({ q, limit: String(PAGE_SIZE) })
    if (activeType.value) params.set('type', activeType.value)
    const body = await api.get(`/api/search?${params.toString()}`) as SearchResponse
    hits.value = body.hits
    counts.value = body.counts ?? {}
    hasMore.value = body.hits.length === PAGE_SIZE
  } catch (err: unknown) {
    showToast(t('search.failed', { error: String((err as Error)?.message ?? err) }), 'error')
    hits.value = []
    counts.value = {}
    hasMore.value = false
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  const q = lastQuery.value
  if (q.length < 2 || !hasMore.value) return
  loadingMore.value = true
  try {
    const params = new URLSearchParams({
      q,
      limit:  String(PAGE_SIZE),
      offset: String(hits.value.length),
    })
    if (activeType.value) params.set('type', activeType.value)
    const body = await api.get(`/api/search?${params.toString()}`) as SearchResponse
    hits.value = [...hits.value, ...body.hits]
    hasMore.value = body.hits.length === PAGE_SIZE
  } catch (err: unknown) {
    showToast(t('search.failed', { error: String((err as Error)?.message ?? err) }), 'error')
  } finally {
    loadingMore.value = false
  }
}
