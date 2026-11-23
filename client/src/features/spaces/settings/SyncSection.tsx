/**
 * SyncSection — "Is everything here?" in a space's settings: one plain
 * heading with an icon (Everything's here · Getting the latest… · Some
 * posts may be missing · {Host} is away · Getting the space ready), one
 * sentence under it, and **Check now** — the small incremental sync of
 * one round, right away (``POST /api/spaces/{id}/sync``). Its outcome
 * arrives as the ``space.sync_state`` frame, which updates the heading.
 *
 * Check now is hidden on the household hosting the space, and disabled
 * with a visible reason (``aria-describedby``) after the server said "not
 * yet" (its ``retry_after_s``). It stays enabled while the host is away: a
 * check asks the host again, and finds it back. The plumbing
 * — signed page, parts that differ, signer, outbox, last sync — sits in a
 * collapsed **Details** for space admins only.
 */
import { useEffect, useState } from 'preact/hooks'
import { ApiError, api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { formatLocale, t } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import {
  SYNC_ICON, parseSpaceSync, syncBody, syncDetailLines, syncHeading, syncView, useSpaceSync,
  type SyncBody,
} from '../spaceSync'

/** The full local date and time of *iso*, for a ``<time>``'s tooltip. */
function fullDate(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleString(formatLocale(), { dateStyle: 'full', timeStyle: 'short' })
}

/** "Mon 21 Nov, 07:03" in the UI language — a date, never a bare time. */
function shortDateTime(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleString(formatLocale(), {
      weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
    })
}

/** Sentence case: a host name or fallback may start the sentence. */
function capitalised(text: string): string {
  return text ? text.charAt(0).toLocaleUpperCase() + text.slice(1) : text
}

function Body({ body }: { body: SyncBody }) {
  const text = capitalised(body.text)
  if (!body.time) return <p class="sh-muted sh-sync-card__body">{text}</p>
  const [before, after = ''] = text.split('{time}')
  const { iso, style } = body.time
  const shown = style === 'since' ? shortDateTime(iso) : relativeDocsTime(iso)
  return (
    <p class="sh-muted sh-sync-card__body">
      {before}
      <time dateTime={iso} title={fullDate(iso)}>
        {/* A sentence may start with its time ("Vor 12 Min. geprüft …"). */}
        {before ? shown : capitalised(shown)}
      </time>
      {after}
    </p>
  )
}

/** Whole minutes until *until* (ms epoch), at least 1. */
const minutesLeft = (until: number) => Math.max(1, Math.ceil((until - Date.now()) / 60_000))

export function SyncSection({ spaceId, sync, hostedHere, hostName, isAdmin }: {
  spaceId: string
  /** ``GET /api/spaces/{id}`` ``sync``. */
  sync: unknown
  /** This household hosts the space: always everything, no Check now. */
  hostedHere: boolean
  /** The host household's name, ``null`` when not known ("the host"). */
  hostName: string | null
  /** A space owner / admin: sees the Details. */
  isAdmin: boolean
}) {
  const live = useSpaceSync(spaceId, sync)
  const [asking, setAsking] = useState(false)
  /** When Check now works again: the detail's ``next_check_at`` (so a
   *  reload knows), else the server's ``retry_after_s`` on a 429. */
  const nextCheckAt = parseSpaceSync(sync)?.next_check_at ?? null
  const [blockedUntil, setBlockedUntil] = useState<number | null>(null)
  useEffect(() => {
    const at = nextCheckAt ? Date.parse(nextCheckAt) : NaN
    if (at > Date.now()) setBlockedUntil(at)
  }, [nextCheckAt])
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (blockedUntil === null) return
    const left = blockedUntil - Date.now()
    if (left <= 0) { setBlockedUntil(null); return }
    // Tick each minute for the countdown; the button comes back at the end.
    const timer = setTimeout(() => setNow(Date.now()), Math.min(left, 60_000))
    return () => clearTimeout(timer)
  }, [blockedUntil, now])
  if (!live) return null

  const view = syncView(live, hostedHere)
  const reasonId = `space-sync-why-${spaceId}`
  const reason = blockedUntil !== null
    ? t('space.sync.check_again_in', { m: String(minutesLeft(blockedUntil)) })
    : null

  const checkNow = async () => {
    setAsking(true)
    try {
      await api.post(`/api/spaces/${spaceId}/sync`)
      showToast(t('space.sync.checking'), 'info')
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        const s = Number(err.extra.retry_after_s)
        setBlockedUntil(Date.now() + (Number.isFinite(s) && s > 0 ? s : 60) * 1000)
      } else {
        showToast(t('space.sync.check_failed'), 'error')
      }
    } finally {
      setAsking(false)
    }
  }

  const details = isAdmin && !hostedHere ? syncDetailLines(live) : []
  return (
    <div class="sh-ss-card sh-sync-card" data-testid="space-sync-section" data-view={view}>
      <p class={`sh-sync-card__state sh-sync-card__state--${view}`}>
        <span class="sh-sync-card__icon" aria-hidden="true">{SYNC_ICON[view]}</span>
        <strong>{syncHeading(view, hostName)}</strong>
      </p>
      <Body body={syncBody(view, live, hostName, hostedHere)} />
      {!hostedHere && (
        <div class="sh-sync-card__actions">
          <Button variant="secondary" onClick={() => void checkNow()} loading={asking}
                  disabled={reason !== null}
                  aria-describedby={reason ? reasonId : undefined}
                  data-testid="space-sync-now">
            {t('space.sync.check_now')}
          </Button>
          {reason && (
            <p class="sh-muted sh-sync-card__why" id={reasonId} data-testid="space-sync-why">
              {reason}
            </p>
          )}
        </div>
      )}
      {details.length > 0 && (
        <details class="sh-sync-card__details" data-testid="space-sync-details">
          <summary>{t('space.sync.details')}</summary>
          <ul class="sh-sync-card__facts">
            {details.map(line => <li key={line}>{line}</li>)}
          </ul>
        </details>
      )}
    </div>
  )
}
