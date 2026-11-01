/**
 * DrawResultSheet — Social Home's own sheet for a gift draw's result
 * (v_78, design §5.1 step 5, decision D13).
 *
 * "You're giving a gift to Ben (Familie Okafor)", Ben's wish list, the
 * budget and the exchange day, and "Only you can see this. Ben won't
 * know it's you." Drawn OUTSIDE the app's frame: the app asks with
 * ``draw.reveal`` (or the person taps "Your gift draw is ready") and gets
 * nothing back — the result never enters the app.
 *
 * Tap-jacking: the app decides when the sheet opens, so Done (and the
 * backdrop / Escape) ignore activation for :data:`DONE_ARM_MS` after it
 * appears — an app-timed tap can't dismiss it unread (design §8.11).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { t, locale } from '@/i18n/i18n'
import { ApiError } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { getDrawResult, type DrawResult } from '../engineBridge'
import './DrawResultSheet.css'

/** Done (and every other way out) ignores taps for this long. */
export const DONE_ARM_MS = 600

type Load =
  | { kind: 'loading' }
  | { kind: 'none' }
  | { kind: 'failed' }
  | { kind: 'ok'; result: DrawResult }

/** The exchange day in the reader's words ("Thu 24 December"). */
export function dayLabel(day: string, lang: string): string {
  const [y, m, d] = day.split('-').map(Number)
  const when = new Date(Date.UTC(y, m - 1, d))
  if (Number.isNaN(when.getTime())) return day
  return when.toLocaleDateString(lang, { weekday: 'short', day: 'numeric', month: 'long', timeZone: 'UTC' })
}

export function DrawResultSheet({ appId, sessionId, onClose }: {
  appId: string
  sessionId: string
  onClose: () => void
}) {
  const [load, setLoad] = useState<Load>({ kind: 'loading' })
  const [attempt, setAttempt] = useState(0)
  /** When the sheet appeared: nothing closes it before DONE_ARM_MS. */
  const shownAt = useRef(Date.now())

  useEffect(() => {
    let cancelled = false
    setLoad({ kind: 'loading' })
    getDrawResult(appId, sessionId).then(result => {
      if (cancelled) return
      shownAt.current = Date.now()
      setLoad({ kind: 'ok', result })
    }).catch(err => {
      if (cancelled) return
      const none = err instanceof ApiError && (err.code === 'APP_DRAW_NO_RESULT' || err.status === 404)
      setLoad({ kind: none ? 'none' : 'failed' })
    })
    return () => { cancelled = true }
  }, [appId, sessionId, attempt])

  const close = () => {
    // Tap-jacking guard: a tap before the person could read it is no choice.
    if (Date.now() - shownAt.current < DONE_ARM_MS) return
    onClose()
  }

  const r = load.kind === 'ok' ? load.result : null
  return (
    <Modal
      open
      onClose={close}
      // "Your name is ready" only when there is one to show.
      title={t(r ? 'apps.draw.result.title' : 'apps.draw.result.title_plain')}
      class="sh-draw-result"
    >
      <p class="sh-draw-result__eyebrow">{t('apps.draw.result.eyebrow')}</p>

      {load.kind === 'loading' && (
        <p class="sh-draw-result__status" role="status">{t('apps.draw.result.loading')}</p>
      )}
      {load.kind === 'none' && (
        <p class="sh-draw-result__status">{t('apps.draw.result.none')}</p>
      )}
      {load.kind === 'failed' && (
        <div class="sh-draw-result__status" role="alert">
          <p>{t('apps.draw.result.load_failed')}</p>
          <Button variant="secondary" onClick={() => setAttempt(n => n + 1)}>{t('common.try_again')}</Button>
        </div>
      )}

      {r && (
        <>
          {r.draw_name && (
            <p class="sh-draw-result__context">
              {r.again > 0 ? t('apps.draw.result.nth_draw', { name: r.draw_name, count: String(r.again + 1) }) : r.draw_name}
            </p>
          )}
          {!r.draw_name && r.again > 0 && (
            <p class="sh-draw-result__context">{t('apps.draw.result.nth_draw_plain', { count: String(r.again + 1) })}</p>
          )}
          {r.handed_over_by && (
            <p class="sh-draw-result__note" role="status">
              {t('apps.draw.result.handed_over', { leaver: r.handed_over_by, name: r.name })}
            </p>
          )}
          <p class="sh-draw-result__lead">{t('apps.draw.result.giving_to')}</p>
          <div class="sh-draw-result__who">
            <span aria-hidden="true"><Avatar name={r.name} size={56} /></span>
            <span class="sh-draw-result__names">
              <span class="sh-draw-result__name">{r.name}</span>
              <span class="sh-draw-result__home">{r.household}</span>
            </span>
          </div>
          <section class="sh-draw-result__wish" aria-label={t('apps.draw.result.wish_title', { name: r.name })}>
            <h3 class="sh-draw-result__wish-title">
              {t('apps.draw.result.wish_title', { name: r.name })}
              {r.wish_updated && <span class="sh-draw-result__updated"> · {t('apps.draw.result.wish_updated')}</span>}
            </h3>
            {r.wish
              ? <p class="sh-draw-result__wish-text">{r.wish}</p>
              : <p class="sh-draw-result__wish-text is-empty">{t('apps.draw.result.wish_empty', { name: r.name })}</p>}
          </section>
          {(r.budget || r.day) && (
            <dl class="sh-draw-result__facts">
              {r.budget && (
                <div><dt>{t('apps.draw.result.budget')}</dt><dd>{r.budget}</dd></div>
              )}
              {r.day && (
                <div><dt>{t('apps.draw.result.day')}</dt><dd>{dayLabel(r.day, locale.value)}</dd></div>
              )}
            </dl>
          )}
          <p class="sh-draw-result__private">
            <span aria-hidden="true">🔒 </span>{t('apps.draw.result.private', { name: r.name })}
          </p>
        </>
      )}

      <div class="sh-modal-actions sh-draw-result__actions">
        <Button onClick={close}>{t('apps.draw.result.done')}</Button>
      </div>
    </Modal>
  )
}
