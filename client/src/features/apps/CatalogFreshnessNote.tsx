/**
 * CatalogFreshnessNote — the honest line on the Apps page when the app
 * list (the catalog) couldn't be reached.
 *
 * "All apps are up to date" is only true after the app list answered. When
 * the latest check failed, the server serves the last list it held and says
 * so (``stale`` + ``checked_at``); this strip says "Couldn't reach the app
 * list — showing what we knew at <time>" instead, with a retry for admins.
 * Nothing renders while the app list answers normally.
 *
 * Keys: apps.catalog_stale apps.catalog_unreachable
 */
import { formatLocale, t } from '@/i18n/i18n'
import { catalogFreshness, catalogReached, type CatalogFreshness } from '@/store/apps'
import { Button } from '@/components/Button'

/** ``checked_at`` in the viewer's language ("29 Oct 2026, 08:15"), or
 *  ``null`` when unknown or unreadable. */
export function formatCheckedAt(iso: string | null): string | null {
  if (!iso) return null
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return null
  return at.toLocaleString(formatLocale(), { dateStyle: 'medium', timeStyle: 'short' })
}

/** What to say when the app list wasn't reached; ``null`` when it was. */
export function catalogFreshnessText(f: CatalogFreshness): string | null {
  if (catalogReached(f)) return null
  const time = formatCheckedAt(f.checkedAt)
  return time ? t('apps.catalog_stale', { time }) : t('apps.catalog_unreachable')
}

export function CatalogFreshnessNote({ onRetry, retrying = false }: {
  /** Shown as "Retry" when given (admins: a check hits the network). */
  onRetry?: () => void
  retrying?: boolean
}) {
  const text = catalogFreshnessText(catalogFreshness.value)
  if (!text) return null
  return (
    <div class="sh-apps-stale" role="status">
      <p class="sh-apps-stale__text">{text}</p>
      {onRetry && (
        <Button variant="secondary" loading={retrying} onClick={onRetry}>
          {t('common.retry')}
        </Button>
      )}
    </div>
  )
}
