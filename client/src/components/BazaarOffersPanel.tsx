/**
 * BazaarOffersPanel — fixed-price / negotiable offer surface (§23.23).
 *
 * Two UX roles:
 *   - **Seller** sees every offer on the listing with accept/reject
 *     buttons. Accepting one flips the listing to sold and auto-
 *     rejects the rest.
 *   - **Buyer** sees only their own offer (if any) with a withdraw
 *     button while pending.
 *
 * Kept separate from :mod:`BazaarPostBody` so the feed card can host
 * it without ballooning — the panel fetches its own data and cleans
 * up its own listeners.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { showToast } from './Toast'
import { formatBazaarAmount } from './BazaarPostBody'
import type { BazaarListing, BazaarOffer } from '@/types'

interface Props {
  listing: BazaarListing
  currentUserId: string | null
  /** Called after an offer flip that affects listing state (accept)
   *  so the parent can refetch the listing row. */
  onListingChanged?: () => void
}

export function BazaarOffersPanel({
  listing, currentUserId, onListingChanged,
}: Props) {
  const [offers, setOffers] = useState<BazaarOffer[]>([])
  const [busy, setBusy] = useState(false)

  const isSeller = currentUserId === listing.seller_user_id
  const canShow = listing.mode === 'offer' || listing.mode === 'negotiable'

  useEffect(() => {
    if (!canShow) return
    let stopped = false
    const refresh = async () => {
      try {
        const body = await api.get(
          `/api/bazaar/${listing.post_id}/offers`,
        ) as BazaarOffer[]
        if (!stopped) setOffers(body)
      } catch { /* 404 when no offers yet is fine */ }
    }
    void refresh()
    const matches = (e: { data: unknown }) =>
      (e.data as { listing_post_id?: string }).listing_post_id
        === listing.post_id
    const off1 = ws.on('bazaar.bid_placed', (e) => {
      if (matches(e)) void refresh()
    })
    const off2 = ws.on('bazaar.offer_accepted', (e) => {
      if (matches(e)) void refresh()
    })
    const off3 = ws.on('bazaar.offer_rejected', (e) => {
      if (matches(e)) void refresh()
    })
    return () => { stopped = true; off1(); off2(); off3() }
  }, [listing.post_id, canShow])

  if (!canShow) return null

  const accept = async (offer: BazaarOffer) => {
    const amountStr = formatBazaarAmount(offer.amount, listing.currency)
    if (!confirm(
      `Accept this ${amountStr} offer? The listing will be marked sold ` +
      'and every other pending offer on this listing will be auto-rejected.',
    )) return
    setBusy(true)
    try {
      await api.post(
        `/api/bazaar/${listing.post_id}/offers/${offer.id}/accept`,
        {},
      )
      showToast('Offer accepted — listing is sold.', 'success')
      onListingChanged?.()
    } catch (err: unknown) {
      showToast(
        `Could not accept: ${(err as Error)?.message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const reject = async (offer: BazaarOffer) => {
    const reason = prompt(
      'Optional reason (shown to the buyer):',
    ) ?? ''
    setBusy(true)
    try {
      await api.post(
        `/api/bazaar/${listing.post_id}/offers/${offer.id}/reject`,
        reason.trim() ? { reason: reason.trim() } : {},
      )
      showToast('Offer declined.', 'info')
    } catch (err: unknown) {
      showToast(
        `Could not reject: ${(err as Error)?.message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const withdraw = async (offer: BazaarOffer) => {
    if (!confirm('Withdraw your offer?')) return
    setBusy(true)
    try {
      await api.delete(
        `/api/bazaar/${listing.post_id}/offers/${offer.id}`,
      )
      showToast('Offer withdrawn.', 'info')
    } catch (err: unknown) {
      showToast(
        `Could not withdraw: ${(err as Error)?.message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const pending = offers.filter(o => o.status === 'pending')

  if (isSeller) {
    if (pending.length === 0) {
      return (
        <p class="sh-bazaar-offers-empty sh-muted">
          No pending offers yet.
        </p>
      )
    }
    return (
      <ul class="sh-bazaar-offers-list" role="list">
        {pending.map(o => (
          <li key={o.id} class="sh-bazaar-offers-row">
            <div class="sh-bazaar-offers-amount">
              {formatBazaarAmount(o.amount, listing.currency)}
            </div>
            {o.message && (
              <div class="sh-bazaar-offers-message sh-muted">
                “{o.message}”
              </div>
            )}
            <div class="sh-bazaar-offers-actions">
              <Button variant="secondary"
                      loading={busy}
                      onClick={() => void reject(o)}>
                Decline
              </Button>
              <Button loading={busy}
                      onClick={() => void accept(o)}>
                Accept
              </Button>
            </div>
          </li>
        ))}
      </ul>
    )
  }

  // Non-seller view — show the caller's own pending offer (if any).
  const mine = offers.filter(o => o.offerer_user_id === currentUserId)
  if (mine.length === 0) return null
  const newest = mine[0]
  return (
    <div class={`sh-bazaar-offers-own sh-bazaar-offers-own--${newest.status}`}>
      <span>
        Your offer: <strong>
          {formatBazaarAmount(newest.amount, listing.currency)}
        </strong>
        {' · '}
        <span class={`sh-bazaar-offers-status-pill sh-bazaar-offers-status-pill--${newest.status}`}>
          {newest.status}
        </span>
      </span>
      {newest.status === 'pending' && (
        <button type="button"
                class="sh-link sh-link--danger"
                disabled={busy}
                onClick={() => void withdraw(newest)}>
          Withdraw
        </button>
      )}
    </div>
  )
}
