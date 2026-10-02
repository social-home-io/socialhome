/**
 * ModerationQueue — admin review UI for moderated content (§23.96/§23.97).
 *
 * Fetches `/api/spaces/{spaceId}/moderation` on mount and on every
 * ``spaceId`` change (and again, quietly, on every ``space.moderation.*``
 * frame for the space), then lets the admin approve or reject each item.
 * Rejection pops a ``RejectReasonDialog`` for the reason textarea.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { Button } from './Button'
import { showToast } from './Toast'
import { Spinner } from './Spinner'
import { openRejectReason } from './RejectReasonDialog'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { relativeDocsTime } from '@/utils/relativeTime'
import { t } from '@/i18n/i18n'

/** Friendly label for a feature/action pair so the queue doesn't
 *  read as raw enum strings. */
function actionLabel(feature: string, action: string): string {
  const f = feature.toLowerCase()
  const a = action.toLowerCase()
  if (f === 'posts' && a === 'create')   return 'New post'
  if (f === 'pages' && a === 'edit')     return 'Page edit'
  if (f === 'pages' && a === 'create')   return 'New page'
  if (f === 'gallery' && a === 'upload') return 'Gallery upload'
  if (f === 'stickies' && a === 'create') return 'New sticky'
  if (f === 'comments' && a === 'create') return 'New comment'
  return `${feature} · ${action}`
}

/** Render the queue payload as readable copy instead of raw JSON.
 *  Recognises the common shapes (post body, page diff, gallery
 *  upload caption); falls back to a "no preview" line for unknown
 *  shapes so the admin can still approve/reject by author + type. */
function PayloadPreview({ payload }: { payload: Record<string, unknown> }) {
  const content =
    typeof payload.content === 'string' ? payload.content :
    typeof payload.body    === 'string' ? payload.body :
    typeof payload.text    === 'string' ? payload.text :
    typeof payload.caption === 'string' ? payload.caption :
    null
  const title = typeof payload.title === 'string' ? payload.title : null
  const url   = typeof payload.url   === 'string' ? payload.url   : null
  if (!content && !title && !url) {
    return (
      <details class="sh-moderation-payload-details">
        <summary class="sh-muted">No preview — view raw payload</summary>
        <pre class="sh-moderation-payload">
          {JSON.stringify(payload, null, 2)}
        </pre>
      </details>
    )
  }
  return (
    <div class="sh-moderation-preview">
      {title && <strong>{title}</strong>}
      {content && (
        <p class="sh-moderation-preview-body">
          {content.length > 300 ? `${content.slice(0, 300)}…` : content}
        </p>
      )}
      {url && (
        <a class="sh-link" href={url} target="_blank" rel="noopener noreferrer">
          {url}
        </a>
      )}
    </div>
  )
}

interface QueueItem {
  id: string
  space_id: string
  feature: string
  action: string
  submitted_by: string
  payload: Record<string, unknown>
  status: string
  submitted_at: string
  expires_at: string
  rejection_reason?: string | null
}

const items = signal<QueueItem[]>([])
const loading = signal(true)
const error = signal<string | null>(null)

export function ModerationQueue({ spaceId, canApprove = true }: {
  spaceId: string
  /** False for a moderator while the space keeps posts to its admins
   *  (``posts_access`` ADMIN_ONLY, §4.3): approving would create the post,
   *  which the server refuses — rejecting stays open. */
  canApprove?: boolean
}) {
  useEffect(() => {
    // Hydrate the household roster so submitter rows render with
    // display names + avatars instead of raw user_ids.
    void loadHouseholdUsers()
    let cancelled = false
    loading.value = true
    error.value = null
    api.get(`/api/spaces/${spaceId}/moderation`)
      .then((data: QueueItem[]) => {
        if (cancelled) return
        items.value = data
      })
      .catch((e: Error) => {
        if (cancelled) return
        error.value = e.message || 'Failed to load queue'
      })
      .finally(() => {
        if (!cancelled) loading.value = false
      })
    // Live: a member submitted something, or another admin (or this
    // admin on another device) decided an item. Refetch the canonical
    // admin-only list quietly — no spinner, and a failed refresh keeps
    // the current rows rather than flipping to the error state.
    const onModerationFrame = (e: WsEvent) => {
      if ((e.data as { space_id?: string }).space_id !== spaceId) return
      api.get(`/api/spaces/${spaceId}/moderation`)
        .then((data: QueueItem[]) => { if (!cancelled) items.value = data })
        .catch(() => { /* keep the rows on screen */ })
    }
    const offs = [
      'space.moderation.queued',
      'space.moderation.approved',
      'space.moderation.rejected',
    ].map(type => ws.on(type, onModerationFrame))
    return () => {
      cancelled = true
      offs.forEach(off => off())
    }
  }, [spaceId])

  const approve = async (itemId: string) => {
    const prev = items.value
    items.value = items.value.filter(i => i.id !== itemId)
    try {
      await api.post(`/api/spaces/${spaceId}/moderation/${itemId}/approve`)
      showToast('Content approved', 'success')
    } catch (e: any) {
      items.value = prev
      showToast(e.message || 'Approval failed', 'error')
    }
  }

  const reject = (itemId: string) => {
    openRejectReason({
      title: 'Reject this submission?',
      label: 'Reason (optional — shown to the submitter)',
      onSubmit: async (reason) => {
        const prev = items.value
        items.value = items.value.filter(i => i.id !== itemId)
        try {
          await api.post(
            `/api/spaces/${spaceId}/moderation/${itemId}/reject`,
            { reason },
          )
          showToast('Content rejected', 'info')
        } catch (e: any) {
          items.value = prev
          showToast(e.message || 'Rejection failed', 'error')
        }
      },
    })
  }

  if (loading.value) return <Spinner />
  if (error.value) {
    return (
      <div class="sh-moderation" role="alert">
        <h3>Moderation queue</h3>
        <p class="sh-error">{error.value}</p>
      </div>
    )
  }

  return (
    <div class="sh-moderation">
      <h3>Moderation queue</h3>
      {!canApprove && (
        <p class="sh-access-note" role="note">
          <span aria-hidden="true">🔒</span>
          <span>{t('space.access.note.approve')}</span>
        </p>
      )}
      {items.value.length === 0 && (
        <p class="sh-muted">Nothing pending — you're all caught up.</p>
      )}
      {items.value.map(item => (
        <div key={item.id} class="sh-moderation-item">
          <div class="sh-moderation-meta">
            <span>
              <strong>{householdDisplayName(item.submitted_by)}</strong>
              <span class="sh-muted"> · {actionLabel(item.feature, item.action)}</span>
            </span>
            <time
              class="sh-muted"
              dateTime={item.submitted_at}
              title={new Date(item.submitted_at).toLocaleString()}
            >
              {relativeDocsTime(item.submitted_at)}
            </time>
          </div>
          <PayloadPreview payload={item.payload} />
          <div class="sh-moderation-actions">
            {canApprove && <Button onClick={() => approve(item.id)}>Approve</Button>}
            <Button variant="secondary" onClick={() => reject(item.id)}>
              Reject
            </Button>
          </div>
        </div>
      ))}
    </div>
  )
}


/**
 * ContentReportsList — admin review for user reports (§23.97).
 *
 * Lists pending reports from ``/api/admin/reports`` and lets the admin
 * resolve them. Appears inside the AdminPage moderation tab.
 */
interface ReportRow {
  id: string
  target_type: string
  target_id: string
  reporter_user_id: string
  reporter_instance_id: string | null
  category: string
  notes: string | null
  status: string
  created_at: string
}

const reports = signal<ReportRow[]>([])
const reportsLoading = signal(true)
const reportsError = signal<string | null>(null)

export function ContentReportsList() {
  useEffect(() => {
    let cancelled = false
    reportsLoading.value = true
    reportsError.value = null
    api.get('/api/admin/reports')
      .then((data: ReportRow[]) => {
        if (!cancelled) reports.value = data
      })
      .catch((e: Error) => {
        if (!cancelled) reportsError.value = e.message || 'Failed to load reports'
      })
      .finally(() => {
        if (!cancelled) reportsLoading.value = false
      })
    return () => {
      cancelled = true
    }
  }, [])

  const resolve = async (id: string, dismissed = false) => {
    const prev = reports.value
    reports.value = reports.value.filter(r => r.id !== id)
    try {
      await api.post(`/api/admin/reports/${id}/resolve`, { dismissed })
      showToast(dismissed ? 'Report dismissed' : 'Report resolved', 'success')
    } catch (e: any) {
      reports.value = prev
      showToast(e.message || 'Resolve failed', 'error')
    }
  }

  if (reportsLoading.value) return <Spinner />
  if (reportsError.value) {
    return (
      <div class="sh-reports" role="alert">
        <h3>Content reports</h3>
        <p class="sh-error">{reportsError.value}</p>
      </div>
    )
  }

  return (
    <div class="sh-reports">
      <h3>Content reports</h3>
      {reports.value.length === 0 && (
        <p class="sh-muted">No pending reports.</p>
      )}
      {reports.value.map(r => {
        // Friendly category labels — the wire enum is uppercase
        // snake_case but admins want sentence-cased categories.
        const categoryLabel = ((c: string) => {
          switch (c) {
            case 'spam':           return 'Spam'
            case 'harassment':     return 'Harassment'
            case 'inappropriate':  return 'Inappropriate content'
            case 'misinformation': return 'Misinformation'
            case 'other':          return 'Other'
            default:               return c
          }
        })(r.category)
        const reporterName = householdDisplayName(r.reporter_user_id)
        return (
          <div key={r.id} class="sh-report-row">
            <div class="sh-report-meta">
              <strong>{categoryLabel}</strong>
              <span class="sh-muted">
                {' · '}{r.target_type === 'user' ? 'on a user' : 'on a post'}
                {' · reported by '}{reporterName}
              </span>
              {r.reporter_instance_id && (
                <span class="sh-badge sh-badge--peer"
                      title={`Report mirrored from peer ${r.reporter_instance_id}`}>
                  from peer
                </span>
              )}
              <time
                class="sh-muted"
                dateTime={r.created_at}
                title={new Date(r.created_at).toLocaleString()}
              >
                {relativeDocsTime(r.created_at)}
              </time>
            </div>
            {r.notes && <p class="sh-muted">“{r.notes}”</p>}
            <div class="sh-form-actions">
              <Button onClick={() => resolve(r.id)}>Resolve</Button>
              <Button variant="secondary" onClick={() => resolve(r.id, true)}>
                Dismiss
              </Button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
