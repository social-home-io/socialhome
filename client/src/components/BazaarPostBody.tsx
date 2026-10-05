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
import { ProtectedNotice, isRestricted } from './ProtectedNotice'
import { BazaarOffersPanel } from './BazaarOffersPanel'
import { ImageRenderer } from './FileRenderer'
import { SaveListingButton } from './SaveListingButton'
import { showToast } from './Toast'
import { currentUser } from '@/store/auth'
import type { BazaarBid, BazaarListing, BazaarOffer } from '@/types'
import { confirmDialog } from '@/components/confirm'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { CURRENCY_FRACTION_DIGITS, formatBazaarAmount } from './bazaarFormat'

// Re-exported so existing ``import { formatBazaarAmount } from
// '@/components/BazaarPostBody'`` call sites keep working. The helper now lives
// in ./bazaarFormat to break the BazaarPostBody↔BazaarOffersPanel import cycle.
export { formatBazaarAmount } from './bazaarFormat'

function modeLabel(mode: BazaarListing['mode']): string {
  switch (mode) {
    case 'fixed':      return t('bazaar.card.mode.fixed')
    case 'offer':      return t('bazaar.card.mode.offer')
    case 'bid_from':   return t('bazaar.card.mode.bid_from')
    case 'negotiable': return t('bazaar.card.mode.negotiable')
    case 'auction':    return t('bazaar.card.mode.auction')
  }
}

function formatCountdown(iso: string): string {
  const end = Date.parse(iso)
  if (Number.isNaN(end)) return ''
  const diff = end - Date.now()
  if (diff <= 0) return t('bazaar.card.ended')
  const mins = Math.floor(diff / 60_000)
  if (mins < 60)  return t('bazaar.card.left_minutes', { m: String(mins) })
  const hours = Math.floor(mins / 60)
  if (hours < 24) return t('bazaar.card.left_hours', { h: String(hours), m: String(mins % 60) })
  const days = Math.floor(hours / 24)
  return t('bazaar.card.left_days', { d: String(days), h: String(hours % 24) })
}

interface Props {
  postId: string
  onUpdated?: () => void
}

export function BazaarPostBody({ postId, onUpdated }: Props) {
  const [listing, setListing] = useState<BazaarListing | null>(null)
  const [bids, setBids] = useState<BazaarBid[]>([])
  // Offers (offer / negotiable modes) live in a separate table from bids.
  // Fetched here only to drive the "N offers" activity count; the
  // interactive list lives in BazaarOffersPanel. The API scopes the rows
  // per viewer (seller → all, buyer → own), so the count is privacy-safe.
  const [offers, setOffers] = useState<BazaarOffer[]>([])
  const [busy, setBusy] = useState(false)
  const [bidAmount, setBidAmount] = useState('')
  const [offerMessage, setOfferMessage] = useState('')
  const [tick, setTick] = useState(0)  // re-render for countdown

  const me = currentUser.value?.user_id
  // Mirror PollUI / ScheduleUI's load-state machine — when the
  // listing endpoint 404s (post created with type=bazaar but no
  // listing envelope yet) the previous bare-catch left the renderer
  // stuck on "Loading listing…" forever.  Surface a calmer
  // placeholder for the missing case instead.
  const [loadState, setLoadState] = useState<'loading' | 'missing' | 'error' | 'ok'>('loading')

  useEffect(() => {
    let stopped = false
    const refresh = async () => {
      try {
        const [l, bs, os] = await Promise.all([
          api.get(`/api/bazaar/${postId}`) as Promise<BazaarListing>,
          api.get(`/api/bazaar/${postId}/bids`) as Promise<BazaarBid[]>,
          // Returns [] for non-offer listings; scoped per viewer by the API.
          (api.get(`/api/bazaar/${postId}/offers`) as Promise<BazaarOffer[]>)
            .catch(() => [] as BazaarOffer[]),
        ])
        if (stopped) return
        setListing(l)
        setBids(bs)
        setOffers(os)
        setLoadState('ok')
      } catch (err: unknown) {
        if (stopped) return
        const status = (err as { status?: number })?.status
        const msg = (err as Error)?.message ?? ''
        if (status === 404 || msg.includes('404')) {
          setLoadState('missing')
        } else {
          setLoadState('error')
        }
      }
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

  if (loadState === 'loading') {
    return (
      <div class="sh-bazaar-card sh-bazaar-card--loading">
        <span class="sh-muted">{t('bazaar.card.loading')}</span>
      </div>
    )
  }
  if (loadState === 'missing') {
    return (
      <div class="sh-bazaar-card sh-bazaar-card--missing">
        <span class="sh-muted">
          🛍 {t('bazaar.card.missing')}
        </span>
      </div>
    )
  }
  if (loadState === 'error' || !listing) {
    return (
      <div class="sh-bazaar-card sh-bazaar-card--error">
        <span class="sh-muted">{t('bazaar.card.load_error')}</span>
      </div>
    )
  }

  const isSeller = me === listing.seller_user_id
  const activeBids = bids.filter(b => !b.withdrawn && !b.rejected && !b.accepted)
  const pendingOffers = offers.filter(o => o.status === 'pending')
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
      showToast(t('bazaar.card.bid_placed'), 'success')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.error.bid', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const withdraw = async (bidId: string) => {
    if (!await confirmDialog(t('bazaar.card.confirm_withdraw_bid'), { destructive: true })) return
    setBusy(true)
    try {
      await api.delete(`/api/bazaar/${postId}/bids/${bidId}`)
      showToast(t('bazaar.card.bid_withdrawn'), 'info')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.error.withdraw', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const acceptOffer = async (bidId: string) => {
    if (!await confirmDialog(t('bazaar.card.confirm_accept'))) return
    setBusy(true)
    try {
      await api.post(`/api/bazaar/${postId}/bids/${bidId}/accept`)
      showToast(t('bazaar.card.offer_accepted'), 'success')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.error.accept', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const rejectOffer = async (bidId: string) => {
    const reason = prompt(t('bazaar.card.reason_prompt')) ?? ''
    setBusy(true)
    try {
      await api.post(
        `/api/bazaar/${postId}/bids/${bidId}/reject`,
        reason ? { reason } : {},
      )
      showToast(t('bazaar.card.offer_declined'), 'info')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.error.decline', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const cancelListing = async () => {
    if (!await confirmDialog(t('bazaar.card.confirm_cancel'), { destructive: true })) return
    setBusy(true)
    try {
      await api.delete(`/api/bazaar/${postId}`)
      showToast(t('bazaar.card.cancelled_toast'), 'info')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.error.cancel', { error: String((err as Error).message ?? err) }), 'error',
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
      showToast(t('bazaar.card.offer_sent'), 'success')
      onUpdated?.()
    } catch (err: unknown) {
      showToast(
        t('bazaar.error.offer', { error: String((err as Error).message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const submitBid = (e: Event) => {
    e.preventDefault()
    const n = Number(bidAmount)
    if (!Number.isFinite(n) || n <= 0) {
      showToast(t('bazaar.card.invalid_amount'), 'error')
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
             title={new Date(listing.end_time).toLocaleString(formatLocale())}>
          ⏱ {countdown}
        </div>
      </div>

      <div class="sh-bazaar-meta">
        {/* Activity-count copy varies by listing mode: auction / bid-from
         *  modes count *bids*; offer / negotiable count *offers*; fixed
         *  has no buyer activity at all so the line is suppressed.
         *  Previously every mode read "N bids", which was wrong for
         *  fixed (no bids exist) and confusing for offers. */}
        {(listing.mode === 'auction' || listing.mode === 'bid_from') && (
          <span>
            {t(isOne(activeBids.length) ? 'bazaar.card.bids_one' : 'bazaar.card.bids', { n: String(activeBids.length) })}
          </span>
        )}
        {(listing.mode === 'offer' || listing.mode === 'negotiable') && (
          <span>
            {t(isOne(pendingOffers.length) ? 'bazaar.card.offers_one' : 'bazaar.card.offers', { n: String(pendingOffers.length) })}
          </span>
        )}
        {/* listing.mode === 'fixed' → no activity row; the price + countdown
         *  carry enough information.  ``status`` pills below still render. */}
        {listing.status === 'sold' && listing.winning_price != null && (
          <span class="sh-bazaar-sold-pill">
            {t('bazaar.card.sold', { price: formatBazaarAmount(listing.winning_price, listing.currency) })}
          </span>
        )}
        {listing.status === 'expired' && (
          <span class="sh-bazaar-meta-pill">{t('bazaar.card.expired')}</span>
        )}
        {listing.status === 'cancelled' && (
          <span class="sh-bazaar-meta-pill">{t('bazaar.card.cancelled')}</span>
        )}
      </div>

      {!closed && me && !isSeller && isRestricted('bazaar') && (
        <ProtectedNotice capability="bazaar" />
      )}
      {!closed && me && !isSeller && !isRestricted('bazaar') && (
        listing.mode === 'fixed' ? (
          <Button loading={busy}
                  onClick={() => void placeBid(listing.price ?? 0)}>
            {t('bazaar.card.buy_for', { price: formatBazaarAmount(listing.price, listing.currency) })}
          </Button>
        ) : (
          <form class="sh-bazaar-bid-form" onSubmit={submitBid}>
            <label class="sh-bazaar-bid-amount">
              <span>{listing.mode === 'offer' ? t('bazaar.card.your_offer') : t('bazaar.card.your_bid')}</span>
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
                <span>{t('bazaar.card.message')}</span>
                <input type="text" maxLength={280}
                       value={offerMessage}
                       onInput={(e) =>
                         setOfferMessage((e.target as HTMLInputElement).value)} />
              </label>
            )}
            <Button type="submit" loading={busy}
                    disabled={!bidAmount || Number(bidAmount) <= 0}>
              {listing.mode === 'offer' ? t('bazaar.card.send_offer') : t('bazaar.place_bid_submit')}
            </Button>
          </form>
        )
      )}

      {!closed && myBid && !isSeller && (
        <div class="sh-bazaar-mybid">
          {t('bazaar.card.your_bid_is')} <strong>
            {formatBazaarAmount(myBid.amount, listing.currency)}
          </strong>
          <button type="button" class="sh-link sh-link--danger"
                  disabled={busy}
                  onClick={() => void withdraw(myBid.id)}>
            {t('bazaar.withdraw')}
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
      <strong class="sh-muted">{t('bazaar.card.yours')}</strong>
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
                  {t('bazaar.decline')}
                </Button>
                <Button loading={busy}
                        onClick={() => void onAccept(b.id)}>
                  {t('bazaar.accept')}
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {active && (
        <Button variant="danger" loading={busy} onClick={() => void onCancel()}>
          {t('bazaar.card.cancel_listing')}
        </Button>
      )}
    </div>
  )
}
