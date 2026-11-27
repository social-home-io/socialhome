/**
 * SpaceUnsentBanner — "Couldn't send to {space}" (v_97).
 *
 * In a GFS space every change waits in this household's durable queue
 * until each of the space's GFSs took it (owner 2026-11-27: retry, never a
 * fallback). What could not go for good is the AUTHOR's to see: the GFS
 * refused it, it was too large, too much was waiting already, or preparing
 * it failed. The banner lists why and offers **Try again** (when any item
 * kept its bytes) and **Discard**. Only the viewer's own items
 * (``GET /api/spaces/{id}/unsent``); it reloads on the ``space.unsent``
 * frame for this space. Nothing shows while there is nothing to say.
 */
import { useCallback, useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t, isOne } from '@/i18n/i18n'
import { ws } from '@/ws'
import { showToast } from '@/components/Toast'
import { Button } from '@/components/Button'

export interface UnsentItem {
  item_id: string
  event_type: string
  error: 'refused' | 'too_large' | 'queue_full' | 'seal_failed' | string
  created_at: string
  retryable: boolean
  count: number
}

const REASONS = ['refused', 'too_large', 'queue_full', 'seal_failed'] as const

/** One line per reason, in a fixed order: "The GFS refused 2." */
export function unsentReasons(items: UnsentItem[]): string[] {
  const counts = new Map<string, number>()
  for (const it of items) {
    const reason = (REASONS as readonly string[]).includes(it.error) ? it.error : 'refused'
    counts.set(reason, (counts.get(reason) ?? 0) + Math.max(1, it.count))
  }
  return REASONS.filter(r => counts.has(r)).map(r => {
    const n = counts.get(r) ?? 0
    return t(isOne(n) ? `space.unsent.reason.${r}_one` : `space.unsent.reason.${r}`, { n: String(n) })
  })
}

export function SpaceUnsentBanner({ spaceId, spaceName }: {
  spaceId: string
  spaceName: string
}) {
  const [items, setItems] = useState<UnsentItem[]>([])
  const [busy, setBusy] = useState(false)
  const url = `/api/spaces/${encodeURIComponent(spaceId)}/unsent`

  const load = useCallback(async () => {
    try {
      const out = await api.get(url) as { items?: UnsentItem[] }
      setItems(Array.isArray(out?.items) ? out.items : [])
    } catch {
      // Nothing to say when it can't be read: the next frame tries again.
    }
  }, [url])

  useEffect(() => { void load() }, [load])
  useEffect(() => ws.on('space.unsent', (e) => {
    if ((e.data as { space_id?: string })?.space_id === spaceId) void load()
  }), [spaceId, load])

  if (items.length === 0) return null
  const retryable = items.some(i => i.retryable)

  const act = async (kind: 'retry' | 'discard') => {
    setBusy(true)
    try {
      if (kind === 'retry') await api.post(url)
      else await api.delete(url)
      await load()
    } catch {
      showToast(t('space.unsent.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div class="sh-unsent-banner" role="alert" data-testid="space-unsent-banner">
      <span class="sh-unsent-banner__icon" aria-hidden="true">⚠</span>
      <div class="sh-unsent-banner__text">
        <strong>{t('space.unsent.title', { space: spaceName })}</strong>
        {unsentReasons(items).map(line => <span key={line}>{line}</span>)}
      </div>
      <div class="sh-unsent-banner__actions">
        {retryable && (
          <Button type="button" loading={busy} data-testid="space-unsent-retry"
                  onClick={() => { void act('retry') }}>
            {t('common.try_again')}
          </Button>
        )}
        <Button type="button" variant="secondary" disabled={busy}
                data-testid="space-unsent-discard"
                onClick={() => { void act('discard') }}>
          {t('draft.discard')}
        </Button>
      </div>
    </div>
  )
}
