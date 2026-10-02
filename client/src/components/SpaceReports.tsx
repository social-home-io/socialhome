/**
 * SpaceReports — the space's content reports, for its content authority
 * (owner / admin / moderator), under the Moderation tab.
 *
 * Fetches ``GET /api/spaces/{spaceId}/reports`` (pending only). Each row
 * shows what was reported (kind + a short preview), why, by whom and
 * when, and offers:
 *
 * - **Show** — opens the tab holding the item (``onOpenTab``);
 * - **Remove post** — posts only: the ordinary moderator delete
 *   (``DELETE /api/spaces/{id}/posts/{post_id}``), then resolves;
 * - **Resolve** — "I've dealt with it";
 * - **Dismiss** — the report is unfounded.
 *
 * Household admins don't see these: a space's reports belong to the
 * space's moderators (the server answers 403 to anyone else).
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { confirmDialog } from './confirm'
import { relativeDocsTime } from '@/utils/relativeTime'
import { t } from '@/i18n/i18n'
import type { SpaceTab } from './SpaceSubHeader'

/** One row of ``GET /api/spaces/{id}/reports``. */
export interface SpaceReportRow {
  id: string
  target_type: string
  target_id: string
  reporter_user_id: string
  reporter_instance_id: string | null
  reporter_name: string | null
  target_name?: string | null
  target_preview: string | null
  target_gone: boolean
  /** The viewer is the report's subject and the space's sole authority:
   *  the reporter is hidden and only Dismiss is offered. */
  anonymous?: boolean
  dismiss_only?: boolean
  category: string
  notes: string | null
  status: string
  created_at: string
  space_id: string
}

/** The space tab that holds each kind of reported item. */
export const REPORT_TARGET_TAB: Record<string, SpaceTab> = {
  post: 'feed',
  comment: 'feed',
  page: 'pages',
  task: 'tasks',
  sticky: 'stickies',
  calendar_event: 'calendar',
  gallery_item: 'gallery',
  user: 'members',
}

// Dynamic keys (for i18n greps): reports.kind.post, reports.kind.comment,
// reports.kind.page, reports.kind.task, reports.kind.sticky,
// reports.kind.calendar_event, reports.kind.gallery_item, reports.kind.user,
// reports.place.feed, reports.place.pages, reports.place.tasks,
// reports.place.stickies, reports.place.calendar, reports.place.gallery,
// reports.place.members, report.category.spam, report.category.harassment,
// report.category.inappropriate, report.category.misinformation,
// report.category.other.
const KINDS = new Set(Object.keys(REPORT_TARGET_TAB))
const CATEGORIES = new Set(['spam', 'harassment', 'inappropriate', 'misinformation', 'other'])

export function reportKindLabel(kind: string): string {
  return KINDS.has(kind) ? t(`reports.kind.${kind}`) : kind
}

export function reportCategoryLabel(category: string): string {
  return CATEGORIES.has(category) ? t(`report.category.${category}`) : category
}

const rows = signal<SpaceReportRow[]>([])
const loading = signal(true)
const error = signal<string | null>(null)
const busy = signal<string | null>(null)

async function load(spaceId: string, quiet = false): Promise<void> {
  if (!quiet) loading.value = true
  error.value = null
  try {
    rows.value = await api.get(`/api/spaces/${encodeURIComponent(spaceId)}/reports`) as SpaceReportRow[]
  } catch (e: unknown) {
    error.value = (e as Error)?.message || t('reports.load_failed')
  } finally {
    loading.value = false
  }
}

export function SpaceReports({
  spaceId,
  onOpenTab,
}: {
  spaceId: string
  onOpenTab?: (tab: SpaceTab) => void
}) {
  useEffect(() => {
    rows.value = []
    void load(spaceId)
  }, [spaceId])

  const decide = async (r: SpaceReportRow, dismissed: boolean) => {
    if (busy.value) return
    busy.value = r.id
    const prev = rows.value
    rows.value = rows.value.filter((x) => x.id !== r.id)
    try {
      await api.post(
        `/api/spaces/${encodeURIComponent(spaceId)}/reports/${encodeURIComponent(r.id)}/resolve`,
        { dismissed },
      )
      showToast(t(dismissed ? 'reports.dismissed' : 'reports.resolved'), 'success')
    } catch (e: unknown) {
      rows.value = prev
      showToast((e as Error)?.message || t('reports.action_failed'), 'error')
    } finally {
      busy.value = null
    }
  }

  const removePost = async (r: SpaceReportRow) => {
    if (busy.value) return
    const ok = await confirmDialog(t('reports.remove_post_confirm'), {
      title: t('reports.remove_post'),
      confirmLabel: t('reports.remove_post'),
      cancelLabel: t('reports.cancel'),
      destructive: true,
    })
    if (!ok) return
    busy.value = r.id
    try {
      await api.delete(
        `/api/spaces/${encodeURIComponent(spaceId)}/posts/${encodeURIComponent(r.target_id)}`,
      )
    } catch (e: unknown) {
      busy.value = null
      showToast((e as Error)?.message || t('reports.action_failed'), 'error')
      return
    }
    busy.value = null
    await decide(r, false)
  }

  if (loading.value) return <Spinner />

  return (
    <section class="sh-space-reports" aria-labelledby="sh-space-reports-h">
      <h3 id="sh-space-reports-h">{t('reports.heading')}</h3>
      <p class="sh-muted sh-space-reports__hint">{t('reports.hint')}</p>
      {error.value && (
        <div role="alert" class="sh-space-reports__error">
          <p class="sh-error">{error.value}</p>
          <Button variant="secondary" onClick={() => void load(spaceId)}>
            {t('reports.retry')}
          </Button>
        </div>
      )}
      {!error.value && rows.value.length === 0 && (
        <p class="sh-muted">{t('reports.empty')}</p>
      )}
      {rows.value.map((r) => {
        const kind = reportKindLabel(r.target_type)
        const reporter = r.anonymous
          ? t('reports.anonymous')
          : (r.reporter_name || t('reports.someone'))
        const tab = REPORT_TARGET_TAB[r.target_type]
        const subject = r.target_type === 'user'
          ? (r.target_name || t('reports.a_member'))
          : r.target_preview
        return (
          <article key={r.id} class="sh-moderation-item sh-space-report"
                   aria-label={t('reports.row_label', { kind, reporter })}>
            <div class="sh-moderation-meta">
              <span>
                <strong>{kind}</strong>
                <span class="sh-muted">
                  {' · '}{t('reports.reported_as', {
                    category: reportCategoryLabel(r.category),
                    reporter,
                  })}
                </span>
              </span>
              <time
                class="sh-muted"
                dateTime={r.created_at}
                title={new Date(r.created_at).toLocaleString()}
              >
                {relativeDocsTime(r.created_at)}
              </time>
            </div>
            {r.target_gone ? (
              <p class="sh-access-note" role="note">
                {t(r.target_type === 'user' ? 'reports.member_gone' : 'reports.target_gone')}
              </p>
            ) : subject ? (
              <div class="sh-moderation-preview">
                <p class="sh-moderation-preview-body">{subject}</p>
              </div>
            ) : null}
            {r.anonymous && (
              <p class="sh-access-note" role="note">{t('reports.about_you')}</p>
            )}
            {/* Never for an anonymous row: their words could name them
             *  (the server sends none either). */}
            {r.notes && !r.anonymous && (
              <p class="sh-space-report__notes">
                <span class="sh-muted">{t('reports.notes_label')} </span>
                “{r.notes}”
              </p>
            )}
            <div class="sh-moderation-actions">
              {tab && onOpenTab && !r.target_gone && (
                <Button variant="secondary" onClick={() => onOpenTab(tab)}>
                  {t('reports.show_in', { place: t(`reports.place.${tab}`) })}
                </Button>
              )}
              {r.target_type === 'post' && !r.target_gone && !r.dismiss_only && (
                <Button
                  variant="danger"
                  disabled={busy.value !== null}
                  onClick={() => void removePost(r)}
                >
                  {t('reports.remove_post')}
                </Button>
              )}
              <Button
                variant="secondary"
                disabled={busy.value !== null}
                onClick={() => void decide(r, true)}
              >
                {t('reports.dismiss')}
              </Button>
              {!r.dismiss_only && (
                <Button
                  disabled={busy.value !== null}
                  onClick={() => void decide(r, false)}
                >
                  {t('reports.resolve')}
                </Button>
              )}
            </div>
          </article>
        )
      })}
    </section>
  )
}
