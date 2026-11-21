/**
 * SyncSection — "Sync" in a space's settings: always says whether this
 * household has everything from the space (up to date, catching up,
 * behind, host offline, not synced yet), when it was last up to date and
 * what of ours still waits to go out — plus **Sync now**, which asks the
 * household hosting the space for what's new right away (the small
 * incremental sync of one round; ``POST /api/spaces/{id}/sync``). The
 * outcome arrives as the ``space.sync_state`` frame.
 */
import { useState } from 'preact/hooks'
import { ApiError, api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import {
  syncLastOkLine, syncStateBody, syncStateLabel, syncWaitingLine, useSpaceSync,
} from '../spaceSync'

export function SyncSection({ spaceId, sync, hostedHere }: {
  spaceId: string
  /** ``GET /api/spaces/{id}`` ``sync``. */
  sync: unknown
  /** This household hosts the space: always up to date. */
  hostedHere: boolean
}) {
  const live = useSpaceSync(spaceId, sync)
  const [asking, setAsking] = useState(false)
  if (!live) return null

  const syncNow = async () => {
    setAsking(true)
    try {
      await api.post(`/api/spaces/${spaceId}/sync`)
      showToast(t('space.sync.asked'), 'success')
    } catch (err) {
      showToast(
        err instanceof ApiError && err.status === 429
          ? t('space.sync.rate_limited')
          : t('space.sync.failed'),
        'error',
      )
    } finally {
      setAsking(false)
    }
  }

  const lastOk = hostedHere ? null : syncLastOkLine(live)
  const waiting = syncWaitingLine(live)
  return (
    <div class="sh-ss-card sh-sync-card" data-testid="space-sync-section">
      <p class={`sh-sync-card__state sh-sync-card__state--${live.state}`}>
        <span class="sh-sync-chip__dot" aria-hidden="true" />
        <strong>{syncStateLabel(live.state)}</strong>
      </p>
      <p class="sh-muted sh-sync-card__body">{syncStateBody(live.state, hostedHere)}</p>
      {(lastOk || waiting) && (
        <ul class="sh-sync-card__facts">
          {lastOk && <li>{lastOk}</li>}
          {waiting && <li>{waiting}</li>}
        </ul>
      )}
      <div class="sh-sync-card__actions">
        <Button variant="secondary" onClick={() => void syncNow()} loading={asking}
                data-testid="space-sync-now">
          {t('space.sync.now')}
        </Button>
      </div>
    </div>
  )
}
