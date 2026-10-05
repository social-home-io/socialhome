/**
 * BazaarPage — marketplace browse + seller hub (§9 / §23.15 / §23.25).
 *
 * Layout:
 *   - filter tabs (All / Mine / Won)
 *   - search box
 *   - responsive grid of listing cards
 *   - click a card → full <BazaarPostBody/> detail
 *
 * Keeps listings in a signal and subscribes to every ``bazaar.*`` WS
 * frame so the grid updates live — no manual refresh needed.
 */
import { useEffect } from 'preact/hooks'
import { t, tValue } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { BazaarSkeleton } from '@/components/Skeleton'
import { BazaarPostBody, formatBazaarAmount } from '@/components/BazaarPostBody'
import { BazaarSellerDashboard } from '@/components/BazaarSellerDashboard'
import {
  BazaarCreateDialog,
  openBazaarCreate,
} from '@/components/BazaarCreateDialog'
import { currentUser } from '@/store/auth'
import type { BazaarListing, BazaarStatus } from '@/types'

type BazaarTab = 'all' | 'mine' | 'saved' | 'won'

const listings = signal<BazaarListing[]>([])
const myListings = signal<BazaarListing[]>([])
const savedListings = signal<BazaarListing[]>([])
const loading = signal(true)
const activeTab = signal<BazaarTab>('all')
const statusFilter = signal<'active' | BazaarStatus | 'all'>('active')
const search = signal('')
const selected = signal<BazaarListing | null>(null)

async function reloadActive() {
  listings.value = await api.get('/api/bazaar') as BazaarListing[]
}

async function reloadMine() {
  myListings.value = await api.get(
    '/api/bazaar', { seller: 'me' },
  ) as BazaarListing[]
}

async function reloadSaved() {
  const body = await api.get(
    '/api/me/bazaar/saved',
  ) as { saved: Array<{ post_id: string; saved_at: string }> }
  const fetched = await Promise.all(
    body.saved.map(entry =>
      api.get(`/api/bazaar/${entry.post_id}`).catch(() => null),
    ),
  )
  savedListings.value = fetched.filter(
    (l): l is BazaarListing => l !== null && typeof l === 'object',
  )
}

export default function BazaarPage() {
  useTitle(t('nav.bazaar'))
  const me = currentUser.value?.user_id

  useEffect(() => {
    void Promise.all([
      reloadActive(),
      me ? reloadMine() : Promise.resolve(),
      me ? reloadSaved() : Promise.resolve(),
    ]).finally(() => { loading.value = false })

    const refresh = () => {
      void reloadActive()
      if (currentUser.value) {
        void reloadMine()
        void reloadSaved()
      }
    }
    const offs = [
      ws.on('bazaar.listing_created',   refresh),
      ws.on('bazaar.listing_updated',   refresh),
      ws.on('bazaar.listing_cancelled', refresh),
      ws.on('bazaar.listing_closed',    refresh),
      ws.on('bazaar.offer_accepted',    refresh),
      ws.on('bazaar.bid_placed',        refresh),
    ]
    return () => offs.forEach(o => o())
  }, [])

  if (loading.value) return <BazaarSkeleton />

  const visible = buildVisibleList(
    activeTab.value, statusFilter.value, search.value.trim().toLowerCase(),
    listings.value, myListings.value, savedListings.value, me ?? '',
  )

  return (
    <div class="sh-bazaar">
      <div class="sh-page-header">
        {/* A protected account sees the notice once: here on the list, or
         *  in the open listing where "Buy" would be — never both. */}
        {!isRestricted('bazaar')
          ? <Button onClick={() => openBazaarCreate()}>+ {t('bazaar.new_listing')}</Button>
          : !selected.value && <ProtectedNotice capability="bazaar" />}
      </div>

      <div class="sh-bazaar-filters">
        <nav class="sh-bazaar-tabs" role="tablist">
          {(['all', 'mine', 'saved', 'won'] as BazaarTab[]).map(tab => (
            <button
              key={tab}
              type="button"
              role="tab"
              aria-selected={activeTab.value === tab}
              class={activeTab.value === tab ? 'sh-tab sh-tab--active' : 'sh-tab'}
              onClick={() => {
                activeTab.value = tab
                selected.value = null
              }}
            >
              {tab === 'all' ? t('bazaar.tab.all')
                : tab === 'mine' ? t('bazaar.tab.mine')
                : tab === 'saved' ? `♥ ${t('bazaar.tab.saved')}`
                : t('bazaar.tab.won')}
            </button>
          ))}
        </nav>
        <div class="sh-row" style={{ gap: 'var(--sh-space-sm)' }}>
          <input type="search" placeholder={t('bazaar.search_placeholder')}
                 class="sh-bazaar-search"
                 value={search.value} aria-label={t('bazaar.search_aria')}
                 onInput={(e) => search.value = (e.target as HTMLInputElement).value} />
          {activeTab.value === 'mine' && (
            <select class="sh-bazaar-status-filter"
                    value={statusFilter.value}
                    onChange={(e) =>
                      statusFilter.value =
                        (e.target as HTMLSelectElement).value as typeof statusFilter.value}>
              <option value="all">{t('bazaar.status.all')}</option>
              <option value="active">{t('bazaar.status.active')}</option>
              <option value="sold">{t('bazaar.status.sold')}</option>
              <option value="expired">{t('bazaar.status.expired')}</option>
              <option value="cancelled">{t('bazaar.status.cancelled')}</option>
            </select>
          )}
        </div>
      </div>

      {activeTab.value === 'mine' && (
        <BazaarSellerDashboard listings={myListings.value}
                               onChanged={() => { void reloadMine() }} />
      )}

      {selected.value ? (
        <div class="sh-bazaar-detail">
          <Button variant="secondary"
                  onClick={() => { selected.value = null }}>
            ← {t('bazaar.back')}
          </Button>
          <BazaarPostBody postId={selected.value.post_id} />
        </div>
      ) : (
        <>
          {visible.length === 0 && (
            <EmptyState tab={activeTab.value} />
          )}
          <div class="sh-bazaar-grid">
            {visible.map(l => (
              <BazaarCard key={l.post_id} listing={l}
                onOpen={() => { selected.value = l }} />
            ))}
          </div>
        </>
      )}

      <BazaarCreateDialog onCreated={() => {
        void reloadActive()
        void reloadMine()
      }} />
    </div>
  )
}

function buildVisibleList(
  tab: BazaarTab,
  statusFilterValue: 'active' | BazaarStatus | 'all',
  query: string,
  active: BazaarListing[],
  mine: BazaarListing[],
  saved: BazaarListing[],
  meUserId: string,
): BazaarListing[] {
  let source: BazaarListing[]
  if (tab === 'mine') {
    source = statusFilterValue === 'all'
      ? mine
      : mine.filter(l => l.status === statusFilterValue)
  } else if (tab === 'saved') {
    source = saved
  } else if (tab === 'won') {
    source = mine.length > 0
      ? active.concat(mine).filter(l => l.winner_user_id === meUserId)
      : active.filter(l => l.winner_user_id === meUserId)
  } else {
    source = active
  }
  // dedupe by post_id
  const seen = new Set<string>()
  const unique = source.filter(l => {
    if (seen.has(l.post_id)) return false
    seen.add(l.post_id)
    return true
  })
  if (!query) return unique
  return unique.filter(l =>
    l.title.toLowerCase().includes(query) ||
    (l.description ?? '').toLowerCase().includes(query),
  )
}

function EmptyState({ tab }: { tab: BazaarTab }) {
  const [icon, heading, body] = tab === 'mine'
    ? ['🛒', t('bazaar.empty.mine_title'), t('bazaar.empty.mine_body')]
    : tab === 'saved'
      ? ['♡', t('bazaar.empty.saved_title'), t('bazaar.empty.saved_body')]
      : tab === 'won'
        ? ['🎉', t('bazaar.empty.won_title'), t('bazaar.empty.won_body')]
        : ['🛍️', t('bazaar.empty.all_title'), t('bazaar.empty.all_body')]
  return (
    <div class="sh-empty-state">
      <div aria-hidden="true">{icon}</div>
      <h3>{heading}</h3>
      <p>{body}</p>
      {!isRestricted('bazaar') && (
        <Button onClick={() => openBazaarCreate()}>+ {t('bazaar.empty.create')}</Button>
      )}
    </div>
  )
}

export function BazaarCard({
  listing, onOpen,
}: {
  listing: BazaarListing
  onOpen: () => void
}) {
  return (
    <button type="button"
            class={`sh-bazaar-tile sh-bazaar-tile--${listing.status}`}
            onClick={onOpen}>
      <div class="sh-bazaar-tile-image">
        {listing.image_urls[0]
          ? <img src={listing.image_urls[0]} alt={listing.title} loading="lazy" />
          : <span class="sh-bazaar-tile-placeholder">🛍</span>}
        {listing.status !== 'active' && (
          <span class={`sh-bazaar-tile-badge sh-bazaar-tile-badge--${listing.status}`}>
            {tValue('bazaar.status', listing.status)}
          </span>
        )}
      </div>
      <div class="sh-bazaar-tile-body">
        <strong class="sh-bazaar-tile-title">{listing.title}</strong>
        <div class="sh-bazaar-tile-price">
          {listing.status === 'sold' && listing.winning_price != null
            ? formatBazaarAmount(listing.winning_price, listing.currency)
            : listing.mode === 'auction' || listing.mode === 'bid_from'
              ? formatBazaarAmount(listing.start_price, listing.currency)
              : formatBazaarAmount(listing.price, listing.currency)}
        </div>
        <span class={`sh-bazaar-mode-chip sh-bazaar-mode-chip--${listing.mode}`}>
          {tValue('bazaar.card.mode', listing.mode)}
        </span>
      </div>
    </button>
  )
}
