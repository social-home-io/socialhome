/**
 * ListingThreadCard — the listing an "Ask the seller" thread is about,
 * shown at the top of the thread (v_73).
 *
 * The card names the listing (photo, title, price, state) and links back
 * to it, says who sees the conversation, and — once the listing ended —
 * how long it stays open. The seller can block the buyer from here.
 */
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { confirmDialog } from '@/components/confirm'
import { formatBazaarAmount } from '@/components/bazaarFormat'
import { showToast } from '@/components/Toast'
import { t, formatLocale } from '@/i18n/i18n'
import type { ListingThreadInfo } from '@/types'

function statusLabel(status: ListingThreadInfo['status']): string {
  switch (status) {
    case 'active':    return t('bazaar.ask.status_active')
    case 'sold':      return t('bazaar.ask.status_sold')
    case 'expired':   return t('bazaar.ask.status_expired')
    case 'cancelled': return t('bazaar.ask.status_cancelled')
  }
}

/** The price the card shows: what it sold for, else its price, else the
 *  auction's starting price. */
export function listingThreadPrice(card: ListingThreadInfo): string {
  const amount = card.status === 'sold' && card.winning_price != null
    ? card.winning_price
    : (card.price ?? card.start_price)
  return formatBazaarAmount(amount, card.currency)
}

interface Props {
  card: ListingThreadInfo
  /** The other person's name, when the roster has it. */
  otherName: string | null
  /** The seller blocked the buyer: leave the thread. */
  onBlocked?: () => void
}

export function ListingThreadCard({ card, otherName, onBlocked }: Props) {
  const name = otherName || t('bazaar.ask.someone')
  const photo = card.image_urls[0]
  const closesAt = card.closes_at ? new Date(card.closes_at) : null
  const block = async () => {
    if (!await confirmDialog(t('bazaar.ask.block_confirm', { name }), { destructive: true })) return
    try {
      await api.post('/api/blocks', { user_id: card.other_user_id })
      showToast(t('bazaar.ask.blocked', { name }), 'info')
      onBlocked?.()
    } catch (err: unknown) {
      showToast((err as Error).message, 'error')
    }
  }
  return (
    <section class="sh-listing-thread-card" aria-label={t('bazaar.ask.card_label')}>
      {photo
        ? <img class="sh-listing-thread-card__img" src={photo} alt="" loading="lazy" />
        : <span class="sh-listing-thread-card__img sh-listing-thread-card__img--none"
                aria-hidden="true">🛍</span>}
      <div class="sh-listing-thread-card__body">
        <a class="sh-listing-thread-card__title"
           href={addBase(`/bazaar?listing=${encodeURIComponent(card.post_id)}`)}>
          {card.title}
        </a>
        <span class="sh-listing-thread-card__meta">
          <span class="sh-listing-thread-card__price">{listingThreadPrice(card)}</span>
          <span class={`sh-listing-thread-card__status sh-listing-thread-card__status--${card.status}`}>
            {statusLabel(card.status)}
          </span>
        </span>
        <span class="sh-muted sh-listing-thread-card__who">
          {card.role === 'seller'
            ? t('bazaar.ask.seller_line', { name })
            : t('bazaar.ask.buyer_line')}
        </span>
        {card.writable && closesAt && (
          <span class="sh-muted sh-listing-thread-card__open">
            {t('bazaar.ask.open_until', {
              date: closesAt.toLocaleDateString(formatLocale(), { day: 'numeric', month: 'long' }),
            })}
          </span>
        )}
        {card.role === 'seller' && (
          <button type="button" class="sh-link sh-link--danger sh-listing-thread-card__block"
                  onClick={() => void block()}>
            {t('bazaar.ask.block', { name })}
          </button>
        )}
      </div>
    </section>
  )
}
