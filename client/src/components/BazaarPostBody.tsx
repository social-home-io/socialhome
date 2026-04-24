/**
 * BazaarPostBody — inline marketplace card rendered inside a PostCard
 * when ``post.type === 'bazaar'`` (§9 / §23.15).
 *
 * Lazy-fetches the listing summary, subscribes to ``bazaar.*`` WS frames
 * to keep bid counts + countdowns live, and exposes context-aware
 * action affordances (Buy / Place bid / Make offer / Cancel / Accept).
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { BazaarOffersPanel } from './BazaarOffersPanel'
import { ImageRenderer } from './FileRenderer'
import { SaveListingButton } from './SaveListingButton'
import { showToast } from './Toast'
import { currentUser } from '@/store/auth'
import type { BazaarBid, BazaarListing } from '@/types'

const CURRENCY_FRACTION_DIGITS: Record<string, number> = {
  JPY: 0, KRW: 0, ISK: 0,
}

export function formatBazaarAmount(
  amount: number | null | undefined, currency: string,
): string {
  if (amount == null) return '—'
  const digits = CURRENCY_FRACTION_DIGITS[currency] ?? 2
  const value = digits === 0 ? amount : amount / 100
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency,
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(value)
}

function modeLabel(mode: BazaarListing['mode']): string {
  switch (mode) {
    case 'fixed':      return 'Fixed price'
    case 'offer':      return 'Offers'
    case 'bid_from':   return 'Bid from'
    case 'negotiable': return 'Negotiable'
    case 'auction':    return 'Auction'
  }
}

function formatCountdown(iso: string): string {
  const end = Date.parse(iso)
  if (Number.isNaN(end)) return ''
  const diff = end - Date.now()
  if (diff <= 0) return 'ended'
  const mins = Math.floor(diff / 60_000)
  if (mins < 60)  return `${mins}m left`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ${mins % 60}m left`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h left`
}

interface Props {
  postId: string
  onUpdated?: () => void
}

export function BazaarPostBody({ postId, onUpdated }: Props) {
  const [listing, setListing] = useState<BazaarListing | null>(null)
  const [bids, setBids] = useState<BazaarBid[]>([])
  const [busy, setBusy] = useState(false)
  const [bidAmount, setBidAmount] = useState('')
  const [offerMessage, setOfferMessage] = useState('')
  const [tick, setTick] = useState(0)  // re-render for countdown

  const me = currentUser.value?.user_id

  useEffect(() => {
    let stopped = false
    const refresh = async () => {
      try {
        const [l, bs] = await Promise.all([
          api.get(`/api/bazaar/${postId}`) as Promise<BazaarListing>,
          api.get(`/api/bazaar/${postId}/bids`) as Promise<BazaarBid[]>,
        ])
        if (stopped) return
        setListing(l)
        setBids(bs)
      } catch { /* noop — listing may not exist yet */ }
    }
    void refresh()

    const matches = (e: { data: unknown }) =>
      (e.data as { listing_post_id?: string }).listing_post_id === postId
    const off1 = ws.on('bazaar.bid_placed',        (e) => { if (matches(e)) void refresh() })
    const off2 = ws.on('bazaar.listing_closed',    (e) => { if (matches(e)) void refresh() })
    const off3 = ws.on('bazaar.listing_updated',   (e) => { if (matches(e)) void refresh() })
    const off4 = ws.on('bazaar.listing_cancelled', (e) => { if (matches(e)) void refresh() })
    const off5 = ws.on('bazaar.offer_accepted',    (e) => { if (matches(e)) void refresh() })
    const off6 = ws.on('bazaar.offer_rejected',    (e) => { if (matches(e)) void refresh() })
    const off7 = ws.on('bazaar.bid_withdrawn',     (e) => { if (matches(e)) void refresh() })

    const timer = setInterval(() => setTick(t => t + 1), 30_000)

    return () => {
      stopped = true
      off1(); off2(); off3(); off4(); off5(); off6(); off7()
      clearInterval(timer)
    }
  }, [postId])

  // Acknowledge tick in a way that satisfies eslint's exhaustive-deps.
  void tick

  if (!listing) {
    return (
      <div class="sh-bazaar-card sh-bazaar-card--loading">
        <span class="sh-muted">Loading listing…</span>
      </div>
    )
  }

  const isSeller = me === listing.seller_user_id
  const activeBids = bids.filter(b => !b.withdrawn && !b.rejected && !b.accepted)
  const highestBid = activeBids.reduce<BazaarBid | null>(
    (best, b) => best == null || b.amount > best.amount ? b : best, null,
  )
  const myBid = me
    ? activeBids.filter(b => b.bidder_user_id === me).at(-1) ?? null
    : null
  const closed = listing.status !== 'active'
  const countdown = formatCountdown(listing.end_time)

  const placeBid = async (amountCents: number, message?: string) => {
    if (busy) return
    setBusy(true)
    try {
      await api.post(`/api/bazaar/${postId}/bids`, {
        amount: amountCents,
        ...(message ? { message } : {}),
      })
      setBidAmount('')
      setOfferMessage('')
      showToast('Bid placed', 'success')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        `Could not bid: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const withdraw = async (bidId: string) => {
    if (!confirm('Withdraw this bid?')) return
    setBusy(true)
    try {
      await api.delete(`/api/bazaar/${postId}/bids/${bidId}`)
      showToast('Bid withdrawn', 'info')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        `Could not withdraw: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const acceptOffer = async (bidId: string) => {
    if (!confirm('Accept this offer? The listing will be marked sold.')) return
    setBusy(true)
    try {
      await api.post(`/api/bazaar/${postId}/bids/${bidId}/accept`)
      showToast('Offer accepted', 'success')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        `Could not accept: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const rejectOffer = async (bidId: string) => {
    const reason = prompt('Reason (optional):') ?? ''
    setBusy(true)
    try {
      await api.post(
        `/api/bazaar/${postId}/bids/${bidId}/reject`,
        reason ? { reason } : {},
      )
      showToast('Offer declined', 'info')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        `Could not reject: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const cancelListing = async () => {
    if (!confirm('Cancel this listing? Active bids will be voided.')) return
    setBusy(true)
    try {
      await api.delete(`/api/bazaar/${postId}`)
      showToast('Listing cancelled', 'info')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        `Could not cancel: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const floorCents = (() => {
    if (listing.mode !== 'auction' && listing.mode !== 'bid_from') return null
    const step = listing.step_price ?? 0
    const base = listing.start_price ?? 0
    if (highestBid) return highestBid.amount + step
    return base
  })()

  const placeOffer = async (amountCents: number, message?: string) => {
    if (busy) return
    setBusy(true)
    try {
      await api.post(`/api/bazaar/${postId}/offers`, {
        amount: amountCents,
        ...(message ? { message } : {}),
      })
      setBidAmount('')
      setOfferMessage('')
      showToast('Offer sent — the seller has been notified.', 'success')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        `Could not send offer: ${(err as Error).message ?? err}`, 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const submitBid = (e: Event) => {
    e.preventDefault()
    const n = Number(bidAmount)
    if (!Number.isFinite(n) || n <= 0) {
      showToast('Enter a valid amount', 'error')
      return
    }
    const digits = CURRENCY_FRACTION_DIGITS[listing.currency] ?? 2
    const cents = digits === 0 ? Math.round(n) : Math.round(n * 100)
    // offer / negotiable modes write to the dedicated bazaar_offers
    // table; auction / bid_from stay on the bids path.
    if (listing.mode === 'offer' || listing.mode === 'negotiable') {
      void placeOffer(cents, offerMessage.trim() || undefined)
    } else {
      void placeBid(cents, offerMessage.trim() || undefined)
    }
  }

  return (
    <div class={`sh-bazaar-card sh-bazaar-card--${listing.mode} sh-bazaar-card--${listing.status}`}>
      <div class="sh-bazaar-card-head">
        <h3 class="sh-bazaar-title">{listing.title}</h3>
        <div class="sh-bazaar-card-head-right">
          <span class={`sh-bazaar-mode-chip sh-bazaar-mode-chip--${listing.mode}`}>
            {modeLabel(listing.mode)}
          </span>
          {me && !isSeller && (
            <SaveListingButton postId={postId} />
          )}
        </div>
      </div>
      {listing.image_urls.length > 0 && (
        <div class={`sh-bazaar-gallery ${listing.image_urls.length === 1 ? 'sh-bazaar-gallery--single' : ''}`}>
          {listing.image_urls.slice(0, 5).map(url => (
            <ImageRenderer key={url} src={url} alt={listing.title} />
          ))}
        </div>
      )}
      {listing.description && (
        <p class="sh-bazaar-description">{listing.description}</p>
      )}

      <div class="sh-bazaar-price-row">
        <div class="sh-bazaar-price">
          {listing.mode === 'fixed' || listing.mode === 'negotiable'
            ? formatBazaarAmount(listing.price, listing.currency)
            : listing.mode === 'auction' || listing.mode === 'bid_from'
              ? (
                highestBid
                  ? formatBazaarAmount(highestBid.amount, listing.currency)
                  : formatBazaarAmount(listing.start_price, listing.currency)
              )
              : formatBazaarAmount(listing.price, listing.currency)}
        </div>
        <div class="sh-bazaar-countdown"
             title={new Date(listing.end_time).toLocaleString()}>
          ⏱ {countdown}
        </div>
      </div>

      <div class="sh-bazaar-meta">
        <span>
          {activeBids.length} {activeBids.length === 1 ? 'bid' : 'bids'}
        </span>
        {listing.status === 'sold' && listing.winning_price != null && (
          <span class="sh-bazaar-sold-pill">
            Sold · {formatBazaarAmount(listing.winning_price, listing.currency)}
          </span>
        )}
        {listing.status === 'expired' && (
          <span class="sh-bazaar-meta-pill">Ended without a buyer</span>
        )}
        {listing.status === 'cancelled' && (
          <span class="sh-bazaar-meta-pill">Cancelled by seller</span>
        )}
      </div>

      {!closed && me && !isSeller && (
        listing.mode === 'fixed' ? (
          <Button loading={busy}
                  onClick={() => void placeBid(listing.price ?? 0)}>
            Buy for {formatBazaarAmount(listing.price, listing.currency)}
          </Button>
        ) : (
          <form class="sh-bazaar-bid-form" onSubmit={submitBid}>
            <label class="sh-bazaar-bid-amount">
              <span>Your {listing.mode === 'offer' ? 'offer' : 'bid'}</span>
              <input type="number" step="0.01" min="0"
                     value={bidAmount}
                     placeholder={floorCents != null
                       ? formatBazaarAmount(floorCents, listing.currency)
                       : undefined}
                     onInput={(e) =>
                       setBidAmount((e.target as HTMLInputElement).value)} />
            </label>
            {(listing.mode === 'offer' || listing.mode === 'negotiable') && (
              <label>
                <span>Message (optional)</span>
                <input type="text" maxLength={280}
                       value={offerMessage}
                       onInput={(e) =>
                         setOfferMessage((e.target as HTMLInputElement).value)} />
              </label>
            )}
            <Button type="submit" loading={busy}
                    disabled={!bidAmount || Number(bidAmount) <= 0}>
              {listing.mode === 'offer' ? 'Send offer' : 'Place bid'}
            </Button>
          </form>
        )
      )}

      {!closed && myBid && !isSeller && (
        <div class="sh-bazaar-mybid">
          Your bid: <strong>
            {formatBazaarAmount(myBid.amount, listing.currency)}
          </strong>
          <button type="button" class="sh-link sh-link--danger"
                  disabled={busy}
                  onClick={() => void withdraw(myBid.id)}>
            Withdraw
          </button>
        </div>
      )}

      {/* Offer/negotiable modes use the dedicated bazaar_offers pane;
          seller sees every pending offer, buyer sees their own. */}
      {!closed && (listing.mode === 'offer' || listing.mode === 'negotiable') && (
        <BazaarOffersPanel
          listing={listing}
          currentUserId={me ?? null}
          onListingChanged={onUpdated} />
      )}

      {isSeller && (
        <SellerControls
          listing={listing}
          bids={activeBids}
          busy={busy}
          onAccept={acceptOffer}
          onReject={rejectOffer}
          onCancel={cancelListing} />
      )}
    </div>
  )
}

function SellerControls({
  listing, bids, busy, onAccept, onReject, onCancel,
}: {
  listing: BazaarListing
  bids: BazaarBid[]
  busy: boolean
  onAccept: (bidId: string) => Promise<void>
  onReject: (bidId: string) => Promise<void>
  onCancel: () => Promise<void>
}) {
  const active = listing.status === 'active'
  // offer / negotiable now use BazaarOffersPanel; keep the legacy
  // bid-list only for auction / bid_from.
  const showBids = bids.length > 0 && active &&
    (listing.mode === 'auction' || listing.mode === 'bid_from')
  return (
    <div class="sh-bazaar-seller">
      <strong class="sh-muted">You own this listing</strong>
      {showBids && (
        <ul class="sh-bazaar-incoming">
          {bids.map(b => (
            <li key={b.id}>
              <span class="sh-bazaar-incoming-amt">
                {formatBazaarAmount(b.amount, listing.currency)}
              </span>
              {b.message && (
                <span class="sh-muted">— {b.message}</span>
              )}
              <div class="sh-row" style={{ marginLeft: 'auto' }}>
                <Button variant="secondary" loading={busy}
                        onClick={() => void onReject(b.id)}>
                  Decline
                </Button>
                <Button loading={busy}
                        onClick={() => void onAccept(b.id)}>
                  Accept
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {active && (
        <Button variant="danger" loading={busy} onClick={() => void onCancel()}>
          Cancel listing
        </Button>
      )}
    </div>
  )
}
