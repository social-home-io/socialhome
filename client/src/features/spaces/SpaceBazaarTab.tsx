/**
 * SpaceBazaarTab — the space's marketplace, surfaced as a first-class
 * tab (§23.15) alongside Feed / Calendar / Gallery.
 *
 * Listings live here, not in the feed: creating one defaults to NOT
 * posting in the feed (the "Also announce in the space feed" checkbox in
 * BazaarCreateDialog is opt-in). The tab browses every listing in the
 * space via ``GET /api/spaces/{id}/bazaar`` and opens the full
 * BazaarPostBody detail inline on click — the same grid → detail UX as
 * the household-wide Bazaar page.
 */
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { BazaarSkeleton } from '@/components/Skeleton'
import { BazaarPostBody } from '@/components/BazaarPostBody'
import { BazaarCard } from '@/features/bazaar/BazaarPage'
import { BazaarCreateDialog, openBazaarCreate } from '@/components/BazaarCreateDialog'
import { AccessNote } from './AccessNote'
import type { BazaarListing } from '@/types'

const listings = signal<BazaarListing[]>([])
const selected = signal<BazaarListing | null>(null)
const loading = signal(true)

export async function loadSpaceBazaar(spaceId: string) {
  loading.value = true
  try {
    listings.value = await api.get(
      `/api/spaces/${spaceId}/bazaar`,
    ) as BazaarListing[]
  } catch {
    listings.value = []
  } finally {
    loading.value = false
  }
}

export function SpaceBazaarTab({ spaceId, canSell = true }: {
  spaceId: string
  /** False while the space keeps posts to its admins for the viewer
   *  (``posts_access`` ADMIN_ONLY, §4.3): a listing is a post. */
  canSell?: boolean
}) {
  useEffect(() => {
    selected.value = null
    void loadSpaceBazaar(spaceId)
    // Live updates: any listing-set change in this space refetches.
    const refresh = () => { void loadSpaceBazaar(spaceId) }
    const offs = [
      ws.on('bazaar.listing_created', refresh),
      ws.on('bazaar.listing_updated', refresh),
      ws.on('bazaar.listing_cancelled', refresh),
      ws.on('bazaar.listing_closed', refresh),
    ]
    return () => { offs.forEach((off) => off()) }
  }, [spaceId])

  // The create dialog is module-signal driven; mount one instance here so
  // ``openBazaarCreate`` from this tab actually renders (the household
  // BazaarPage mounts its own — only one route is live at a time).
  const dialog = (
    <BazaarCreateDialog onCreated={() => { void loadSpaceBazaar(spaceId) }} />
  )

  if (selected.value) {
    return (
      <div class="sh-bazaar-detail">
        {dialog}
        <Button variant="secondary" onClick={() => { selected.value = null }}>
          ← Back to listings
        </Button>
        <BazaarPostBody postId={selected.value.post_id} />
      </div>
    )
  }

  return (
    <div class="sh-space-bazaar">
      {dialog}
      <div class="sh-space-bazaar-header">
        <p class="sh-muted" style={{ margin: 0 }}>
          Items members are sharing or selling in this space.
        </p>
        {canSell && !isRestricted('bazaar') && (
          <Button onClick={() => openBazaarCreate(spaceId)}>+ New listing</Button>
        )}
      </div>
      {!canSell && <AccessNote feature="posts" />}
      {isRestricted('bazaar') && <ProtectedNotice capability="bazaar" />}
      {loading.value ? (
        <BazaarSkeleton />
      ) : listings.value.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🛍</div>
          <h3>Nothing listed yet</h3>
          {canSell && (
            <p class="sh-muted">
              Be the first to list something. New listings stay in this tab —
              tick “Also announce in the space feed” if you want a feed post too.
            </p>
          )}
          {canSell && !isRestricted('bazaar') && (
            <Button onClick={() => openBazaarCreate(spaceId)}>+ New listing</Button>
          )}
        </div>
      ) : (
        <div class="sh-bazaar-grid">
          {listings.value.map((l) => (
            <BazaarCard
              key={l.post_id}
              listing={l}
              onOpen={() => { selected.value = l }}
            />
          ))}
        </div>
      )}
    </div>
  )
}
