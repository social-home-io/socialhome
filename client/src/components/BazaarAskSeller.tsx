/**
 * "Ask the seller" on a listing (v_73).
 *
 * * :func:`AskSellerButton` — a buyer's one button. The server picks the
 *   path (``POST /api/bazaar/{id}/ask``): the normal DM when the seller is
 *   in this household or a paired one, else the private listing thread.
 *   Either way the button opens the conversation.
 * * :func:`ListingQuestions` — the seller's list of threads about the
 *   listing ("Questions"), each with the buyer's name and unread count.
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { ws } from '@/ws'
import { t, isOne } from '@/i18n/i18n'
import type { BazaarAskResult, BazaarThreadRow } from '@/types'
import { Button } from './Button'
import { showToast } from './Toast'

export function AskSellerButton({ postId, afterSale = false }: {
  postId: string
  /** The listing sold to the viewer: the button is for arranging pickup. */
  afterSale?: boolean
}) {
  const loc = useLocation()
  const [busy, setBusy] = useState(false)
  const ask = async () => {
    if (busy) return
    setBusy(true)
    try {
      const res = await api.post(`/api/bazaar/${postId}/ask`) as BazaarAskResult
      loc.route(`/dms/${res.conversation_id}`)
    } catch (err: unknown) {
      showToast((err as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div class="sh-bazaar-ask">
      <Button variant="secondary" loading={busy} onClick={() => void ask()}>
        <span aria-hidden="true">💬</span>{' '}
        {afterSale ? t('bazaar.ask.message_seller') : t('bazaar.ask.button')}
      </Button>
    </div>
  )
}

export function ListingQuestions({ postId }: { postId: string }) {
  const [rows, setRows] = useState<BazaarThreadRow[] | null>(null)
  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const got = await api.get(`/api/bazaar/${postId}/threads`) as BazaarThreadRow[]
        if (!stopped) setRows(Array.isArray(got) ? got : [])
      } catch {
        if (!stopped) setRows([])
      }
    }
    void load()
    // A new question (or answer) moves the unread counts.
    const off = ws.on('dm.message', () => { void load() })
    return () => { stopped = true; off() }
  }, [postId])
  if (!rows || rows.length === 0) return null
  return (
    <section class="sh-bazaar-questions" aria-label={t('bazaar.ask.conversations_title')}>
      <h4 class="sh-bazaar-questions__title">
        {t('bazaar.ask.conversations_title')}
        <span class="sh-badge">{rows.length}</span>
      </h4>
      <ul class="sh-bazaar-questions__list">
        {rows.map(r => (
          <li key={r.conversation_id}>
            <a class="sh-bazaar-questions__row" href={addBase(`/dms/${r.conversation_id}`)}>
              <span class="sh-bazaar-questions__name">
                {r.other_display_name || t('bazaar.ask.someone')}
              </span>
              {r.unread > 0 && (
                <span class="sh-dm-unread"
                      aria-label={t(isOne(r.unread) ? 'dms.unread_count_one' : 'dms.unread_count',
                        { n: String(r.unread) })}>
                  {r.unread > 99 ? '99+' : r.unread}
                </span>
              )}
              {!r.writable && (
                <span class="sh-muted sh-bazaar-questions__closed">
                  {t('bazaar.ask.closed_short')}
                </span>
              )}
            </a>
          </li>
        ))}
      </ul>
    </section>
  )
}
