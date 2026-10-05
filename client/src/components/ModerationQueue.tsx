/**
 * ModerationQueue — the space's review queue (§4.3 "Reviewed",
 * §23.96/§23.97), for its content authority (owner / admin / moderator)
 * on the host.
 *
 * Fetches ``GET /api/spaces/{spaceId}/moderation`` on mount, on every
 * ``spaceId`` change and again, quietly, on every ``space.moderation.*``
 * frame for the space. Every feature queues here — posts, pages, tasks
 * (and lists), sticky notes, calendar events — as a create, an edit or a
 * delete:
 *
 * - **create** — a preview card of the proposed item;
 * - **edit** — old → new: a line diff for a page body, a field table for
 *   everything else, flagging a field someone changed after the member
 *   submitted (``current`` ≠ ``snapshot``);
 * - **delete** — the row that would go.
 *
 * Approve may answer ``409 STALE`` (the page changed since): a dialog
 * shows what's there now and offers "Approve anyway" (``{force: true}``).
 * ``410 TARGET_GONE`` / ``409 ALREADY_DECIDED`` / ``409
 * FEATURE_UNAVAILABLE`` toast and refresh. Reject asks for an optional
 * reason (≤ 500 chars) the submitter sees.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { Spinner } from './Spinner'
import { openRejectReason } from './RejectReasonDialog'
import { formatBazaarAmount } from './bazaarFormat'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { relativeDocsTime } from '@/utils/relativeTime'
import {
  actionLabel,
  expiryLabel,
  fieldDiffRows,
  fieldLabel,
  formatFieldValue,
  itemEntity,
  itemPreview,
  lineDiff,
  sameValue,
  type ModerationItem,
} from '@/features/spaces/moderationItems'
import { formatLocale, t } from '@/i18n/i18n'

export type { ModerationItem } from '@/features/spaces/moderationItems'

const QUEUE_FRAMES = [
  'space.moderation.queued',
  'space.moderation.approved',
  'space.moderation.rejected',
  'space.moderation.expired',
] as const

const items = signal<ModerationItem[]>([])
const loading = signal(true)
const error = signal<string | null>(null)

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

function clip(text: string, max = 300): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/** A sticky colour fit for an inline style — hex only, so a crafted
 *  value can't smuggle a ``url(...)``. */
function safeColor(v: unknown): string | null {
  return typeof v === 'string' && /^#[0-9a-f]{3,8}$/i.test(v) ? v : null
}

const nameOf = (uid: string) => householdDisplayName(uid)

/* ── Preview cards ─────────────────────────────────────────────────── */

function PostPreview({ p }: { p: Record<string, unknown> }) {
  const content = str(p.content)
  const images = Array.isArray(p.image_urls) ? p.image_urls.length : 0
  const poll = p.poll as { question?: unknown; options?: unknown } | undefined
  const schedule = p.schedule as { title?: unknown; slots?: unknown } | undefined
  const bazaar = p.bazaar as Record<string, unknown> | undefined
  const location = p.location as { label?: unknown } | undefined
  const linkPreview = p.link_preview as { url?: unknown; title?: unknown } | undefined
  const priceCents = typeof bazaar?.price === 'number' ? bazaar.price
    : typeof bazaar?.start_price === 'number' ? bazaar.start_price : null
  const currency = str(bazaar?.currency) ?? 'EUR'
  return (
    <>
      {bazaar && (
        <p class="sh-moderation-preview__line">
          <span aria-hidden="true">🛍 </span>
          <strong>{str(bazaar.title) ?? t('moderation.preview.listing')}</strong>
          {priceCents !== null && <> · {safeAmount(priceCents, currency)}</>}
        </p>
      )}
      {content && <p class="sh-moderation-preview-body">{clip(content)}</p>}
      {images > 0 && (
        <p class="sh-muted sh-moderation-preview__line">
          {t('moderation.preview.images', { n: String(images) })}
        </p>
      )}
      {poll && (
        <div class="sh-moderation-preview__line">
          <strong>📊 {str(poll.question) ?? ''}</strong>
          {Array.isArray(poll.options) && (
            <ul class="sh-moderation-preview__options">
              {poll.options.map((o, i) => <li key={i}>{String(o)}</li>)}
            </ul>
          )}
        </div>
      )}
      {schedule && (
        <p class="sh-moderation-preview__line">
          <strong>🗓 {str(schedule.title) ?? ''}</strong>
          {Array.isArray(schedule.slots) && (
            <span class="sh-muted"> · {t('moderation.preview.slots', { n: String(schedule.slots.length) })}</span>
          )}
        </p>
      )}
      {location && str(location.label) && (
        <p class="sh-muted sh-moderation-preview__line">📍 {str(location.label)}</p>
      )}
      {linkPreview && str(linkPreview.url) && (
        <p class="sh-muted sh-moderation-preview__line sh-moderation-preview__url">
          🔗 {str(linkPreview.title) ?? str(linkPreview.url)}
        </p>
      )}
    </>
  )
}

function safeAmount(cents: number, currency: string): string {
  try { return formatBazaarAmount(cents, currency) } catch { return String(cents) }
}

const TASK_FIELDS = ['status', 'due_date', 'priority', 'assignees', 'labels'] as const
const EVENT_FIELDS = ['start', 'end', 'all_day', 'location', 'capacity', 'rrule'] as const

/** A compact "field: value" list for the fields that are set. */
function FieldList({ p, fields }: { p: Record<string, unknown>; fields: readonly string[] }) {
  const shown = fields.filter((f) => {
    const v = p[f]
    return v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && v.length === 0)
      && !(f === 'all_day' && v === false)
  })
  if (shown.length === 0) return null
  return (
    <dl class="sh-moderation-fields">
      {shown.map(f => (
        <div key={f} class="sh-moderation-fields__row">
          <dt>{fieldLabel(f)}</dt>
          <dd>{formatFieldValue(f, p[f], nameOf)}</dd>
        </div>
      ))}
    </dl>
  )
}

/** What a created item would be (or a deleted one was). */
export function PreviewCard({ entity, p }: { entity: string; p: Record<string, unknown> }) {
  let body
  switch (entity) {
    case 'post':
      body = <PostPreview p={p} />
      break
    case 'page': {
      const content = str(p.content)
      body = (
        <>
          <strong>{str(p.title) ?? t('moderation.preview.untitled')}</strong>
          {content && <p class="sh-moderation-preview-body">{clip(content)}</p>}
        </>
      )
      break
    }
    case 'task': {
      const description = str(p.description)
      body = (
        <>
          <strong>{str(p.title) ?? ''}</strong>
          {description && <p class="sh-moderation-preview-body">{clip(description, 200)}</p>}
          <FieldList p={p} fields={TASK_FIELDS} />
        </>
      )
      break
    }
    case 'list':
      body = <strong>{str(p.name) ?? ''}</strong>
      break
    case 'sticky': {
      const color = safeColor(p.color)
      body = (
        <p class="sh-moderation-preview-body">
          {color && <span class="sh-chip-swatch" style={{ background: color }} aria-hidden="true" />}
          {clip(str(p.content) ?? '')}
        </p>
      )
      break
    }
    case 'event': {
      const description = str(p.description)
      body = (
        <>
          <strong>{str(p.summary) ?? str(p.title) ?? ''}</strong>
          <FieldList p={p} fields={EVENT_FIELDS} />
          {description && <p class="sh-moderation-preview-body">{clip(description, 200)}</p>}
        </>
      )
      break
    }
    default:
      body = null
  }
  const empty = !body || Object.keys(p).length === 0
  if (empty) {
    return (
      <details class="sh-moderation-payload-details">
        <summary class="sh-muted">{t('moderation.preview.none')}</summary>
        <pre class="sh-moderation-payload">{JSON.stringify(p, null, 2)}</pre>
      </details>
    )
  }
  return <div class="sh-moderation-preview">{body}</div>
}

/* ── Edit diffs ────────────────────────────────────────────────────── */

/** Old → new, line by line (a page body). */
export function TextDiff({ before, after }: { before: string; after: string }) {
  const lines = lineDiff(before, after)
  if (!lines) {
    return (
      <div class="sh-moderation-diff sh-moderation-diff--split">
        <p class="sh-muted">{t('moderation.diff.before')}</p>
        <pre class="sh-moderation-diff__text">{before}</pre>
        <p class="sh-muted">{t('moderation.diff.after')}</p>
        <pre class="sh-moderation-diff__text">{after}</pre>
      </div>
    )
  }
  return (
    <pre class="sh-moderation-diff" aria-label={t('moderation.diff.label')}>
      {lines.map((l, i) => (
        <div key={i} class={`sh-moderation-diff__line sh-moderation-diff__line--${l.kind}`}>
          <span class="sh-moderation-diff__mark" aria-hidden="true">
            {l.kind === 'add' ? '+' : l.kind === 'del' ? '−' : ' '}
          </span>
          {l.kind !== 'same' && (
            <span class="sr-only">
              {l.kind === 'add' ? t('moderation.diff.added') : t('moderation.diff.removed')}
            </span>
          )}
          {l.text || ' '}
        </div>
      ))}
    </pre>
  )
}

/** Old → new per field; a field changed since the member submitted is
 *  flagged with its value now. */
export function FieldDiffTable({ item, skip = [] }: { item: ModerationItem; skip?: readonly string[] }) {
  const rows = fieldDiffRows(item, skip)
  if (rows.length === 0) return null
  return (
    <table class="sh-moderation-table">
      <thead>
        <tr>
          <th scope="col">{t('moderation.diff.field')}</th>
          <th scope="col">{t('moderation.diff.before')}</th>
          <th scope="col">{t('moderation.diff.after')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(r => (
          <tr key={r.field} class={r.changedSince !== undefined ? 'sh-moderation-table__row--stale' : ''}>
            <th scope="row">{fieldLabel(r.field)}</th>
            <td class="sh-moderation-table__old">{formatFieldValue(r.field, r.before, nameOf)}</td>
            <td class="sh-moderation-table__new">
              {formatFieldValue(r.field, r.after, nameOf)}
              {r.changedSince !== undefined && (
                <span class="sh-moderation-table__since">
                  {t('moderation.diff.changed_since', {
                    value: formatFieldValue(r.field, r.changedSince, nameOf),
                  })}
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function EditBody({ item }: { item: ModerationItem }) {
  const entity = itemEntity(item)
  const after = itemPreview(item)
  const before = item.snapshot ?? {}
  const gone = item.current === null && item.snapshot !== undefined && item.snapshot !== null
  const textual = entity === 'page' && typeof after.content === 'string'
  return (
    <div class="sh-moderation-edit">
      {gone && <p class="sh-access-note" role="note">{t('moderation.diff.target_gone')}</p>}
      <FieldDiffTable item={item} skip={textual ? ['content'] : []} />
      {textual && (
        <TextDiff
          before={typeof before.content === 'string' ? before.content : ''}
          after={after.content as string}
        />
      )}
    </div>
  )
}

function ItemBody({ item }: { item: ModerationItem }) {
  const entity = itemEntity(item)
  if (item.action === 'edit') return <EditBody item={item} />
  if (item.action === 'delete') {
    return (
      <div class="sh-moderation-delete">
        <p class="sh-muted sh-moderation-delete__lead">{t('moderation.delete_lead')}</p>
        <PreviewCard entity={entity} p={item.snapshot ?? itemPreview(item)} />
      </div>
    )
  }
  return <PreviewCard entity={entity} p={itemPreview(item)} />
}

/* ── The queue ─────────────────────────────────────────────────────── */

interface StaleState {
  item: ModerationItem
  current: string | null
  proposed: string | null
}

function errCode(e: unknown): string | null {
  return e instanceof ApiError ? e.code : ((e as { code?: unknown } | null)?.code as string | null) ?? null
}

function errStatus(e: unknown): number | null {
  const s = (e as { status?: unknown } | null)?.status
  return typeof s === 'number' ? s : null
}

function textOf(v: unknown): string | null {
  if (typeof v === 'string') return v
  if (v && typeof v === 'object') {
    const c = (v as { content?: unknown }).content
    if (typeof c === 'string') return c
  }
  return null
}

export function ModerationQueue({ spaceId, canApprove = true }: {
  spaceId: string
  /** May the viewer approve an item of ``feature``? ``false`` for a
   *  moderator where the feature is kept to admins (``*_access``
   *  ADMIN_ONLY, §4.3): approving creates the item as the approver,
   *  which the server refuses — rejecting stays open. A boolean applies
   *  to every feature. */
  canApprove?: boolean | ((feature: string) => boolean)
}) {
  const [stale, setStale] = useState<StaleState | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  // Re-render the expiry countdowns now and then.
  const [, setTick] = useState(0)

  const refresh = () =>
    api.get(`/api/spaces/${spaceId}/moderation`)
      .then((data: ModerationItem[]) => { items.value = data })
      .catch(() => { /* keep the rows on screen */ })

  useEffect(() => {
    // Hydrate the household roster so submitter rows render with
    // display names instead of raw user_ids.
    void loadHouseholdUsers()
    let cancelled = false
    loading.value = true
    error.value = null
    api.get(`/api/spaces/${spaceId}/moderation`)
      .then((data: ModerationItem[]) => {
        if (cancelled) return
        items.value = data
      })
      .catch((e: Error) => {
        if (cancelled) return
        error.value = e.message || t('moderation.error.load')
      })
      .finally(() => {
        if (!cancelled) loading.value = false
      })
    // Live: a member submitted something, or another moderator (or this
    // one on another device) decided an item, or one expired. Refetch
    // quietly — no spinner, and a failed refresh keeps the current rows.
    const onModerationFrame = (e: WsEvent) => {
      if ((e.data as { space_id?: string }).space_id !== spaceId) return
      api.get(`/api/spaces/${spaceId}/moderation`)
        .then((data: ModerationItem[]) => { if (!cancelled) items.value = data })
        .catch(() => { /* keep the rows on screen */ })
    }
    const offs = QUEUE_FRAMES.map(type => ws.on(type, onModerationFrame))
    const timer = setInterval(() => setTick(n => n + 1), 60_000)
    return () => {
      cancelled = true
      offs.forEach(off => off())
      clearInterval(timer)
    }
  }, [spaceId])

  const approvable = (item: ModerationItem) =>
    typeof canApprove === 'function' ? canApprove(item.feature) : canApprove

  const approve = async (item: ModerationItem, force = false) => {
    if (busyId) return
    setBusyId(item.id)
    const prev = items.value
    items.value = items.value.filter(i => i.id !== item.id)
    try {
      const url = `/api/spaces/${spaceId}/moderation/${item.id}/approve`
      let res = await api.post<{ complete?: boolean; status?: string }>(
        url, force ? { force: true } : {},
      )
      if (res?.status === 'publishing') {
        // A member household: the host publishes it from its own copy.
        // The card stays, showing the state, until the decision arrives.
        items.value = prev.map(i => (i.id === item.id ? { ...i, publishing: true } : i))
        showToast(t('moderation.publishing_toast'), 'success')
        return
      }
      if (res?.complete === false) {
        // Published, but a part (a poll, a listing) didn't save: approving
        // an approved item again finishes it (idempotent on the host).
        try {
          res = await api.post<{ complete?: boolean }>(url, {})
        } catch { /* fall through to the warning */ }
      }
      if (res?.complete === false) {
        showToast(t('moderation.approved_incomplete'), 'error')
      } else {
        showToast(t('moderation.approved'), 'success')
      }
    } catch (e: unknown) {
      const code = errCode(e)
      const status = errStatus(e)
      if (status === 409 && code === 'STALE') {
        items.value = prev
        const extra = e instanceof ApiError ? e.extra : {}
        setStale({
          item,
          current: textOf(extra.current),
          proposed: textOf(extra.proposed) ?? textOf(itemPreview(item)),
        })
        return
      }
      if (code === 'EXPIRED') {
        showToast(t('moderation.error.expired'), 'info')
        void refresh()
        return
      }
      if (status === 410 || code === 'TARGET_GONE') {
        showToast(t('moderation.error.target_gone'), 'info')
        void refresh()
        return
      }
      if (code === 'IN_PROGRESS') {
        // Another moderator (or a double-click) is approving it right now.
        showToast(t('moderation.error.in_progress'), 'info')
        void refresh()
        return
      }
      if (code === 'ALREADY_DECIDED') {
        showToast(t('moderation.error.already_decided'), 'info')
        void refresh()
        return
      }
      if (code === 'FEATURE_UNAVAILABLE') {
        items.value = prev
        showToast(t('moderation.error.feature_unavailable'), 'error')
        void refresh()
        return
      }
      items.value = prev
      showToast((e as Error)?.message || t('moderation.error.approve'), 'error')
    } finally {
      setBusyId(null)
    }
  }

  const reject = (item: ModerationItem) => {
    openRejectReason({
      title: t('moderation.reject.title'),
      label: t('moderation.reject.label'),
      onSubmit: async (reason) => {
        const prev = items.value
        items.value = items.value.filter(i => i.id !== item.id)
        try {
          await api.post(
            `/api/spaces/${spaceId}/moderation/${item.id}/reject`,
            { reason: reason.slice(0, 500) },
          )
          showToast(t('moderation.rejected'), 'info')
        } catch (e: unknown) {
          if (errCode(e) === 'ALREADY_DECIDED') {
            showToast(t('moderation.error.already_decided'), 'info')
            void refresh()
            return
          }
          items.value = prev
          showToast((e as Error)?.message || t('moderation.error.reject'), 'error')
        }
      },
    })
  }

  if (loading.value) return <Spinner />
  if (error.value) {
    return (
      <div class="sh-moderation" role="alert">
        <h3>{t('moderation.heading')}</h3>
        <p class="sh-error">{error.value}</p>
      </div>
    )
  }

  return (
    <div class="sh-moderation">
      <h3>{t('moderation.heading')}</h3>
      {items.value.length === 0 && (
        <p class="sh-muted">{t('moderation.empty')}</p>
      )}
      {items.value.map((item) => {
        const canApproveItem = approvable(item)
        const submitter = item.submitted_by_display || householdDisplayName(item.submitted_by)
        const expiry = item.expires_at ? expiryLabel(item.expires_at) : ''
        return (
          <article key={item.id} class="sh-moderation-item" aria-label={`${actionLabel(item)} — ${submitter}`}>
            <div class="sh-moderation-meta">
              <span>
                <strong>{submitter}</strong>
                <span class="sh-muted"> · {actionLabel(item)}</span>
              </span>
              <span class="sh-moderation-meta__times">
                <time
                  class="sh-muted"
                  dateTime={item.submitted_at}
                  title={new Date(item.submitted_at).toLocaleString(formatLocale())}
                >
                  {relativeDocsTime(item.submitted_at)}
                </time>
                {expiry && (
                  <span class="sh-badge sh-moderation-meta__expiry" title={new Date(item.expires_at).toLocaleString(formatLocale())}>
                    {expiry}
                  </span>
                )}
              </span>
            </div>
            <ItemBody item={item} />
            {item.publishing && (
              <p class="sh-access-note" role="status">
                <span aria-hidden="true">⏳</span>
                <span>
                  <strong>{t('moderation.publishing')}</strong>
                  {' '}{t('moderation.publishing_hint')}
                </span>
              </p>
            )}
            {!item.publishing && !canApproveItem && (
              <p class="sh-access-note" role="note">
                <span aria-hidden="true">🔒</span>
                <span>
                  {item.feature === 'posts'
                    ? t('space.access.note.approve')
                    : t('moderation.note.approve_admin_only')}
                </span>
              </p>
            )}
            {!item.publishing && <div class="sh-moderation-actions">
              {canApproveItem && (
                <Button onClick={() => approve(item)} loading={busyId === item.id}>
                  {t('moderation.approve')}
                </Button>
              )}
              <Button variant="secondary" onClick={() => reject(item)}>
                {t('moderation.reject')}
              </Button>
            </div>}
          </article>
        )
      })}
      <Modal
        open={stale !== null}
        onClose={() => setStale(null)}
        title={t('moderation.stale.title')}
      >
        {stale && (
          <div class="sh-moderation-stale">
            <p>{t('moderation.stale.body')}</p>
            {stale.current !== null && stale.proposed !== null
              && !sameValue(stale.current, stale.proposed) && (
              <>
                <p class="sh-muted">{t('moderation.stale.diff_lead')}</p>
                <TextDiff before={stale.current} after={stale.proposed} />
              </>
            )}
            <div class="sh-form-actions">
              <Button variant="secondary" onClick={() => setStale(null)}>
                {t('moderation.stale.cancel')}
              </Button>
              <Button
                onClick={() => {
                  const target = stale.item
                  setStale(null)
                  void approve(target, true)
                }}
              >
                {t('moderation.stale.approve_anyway')}
              </Button>
            </div>
          </div>
        )}
      </Modal>
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
        if (!cancelled) reportsError.value = e.message || t('reports.load_failed')
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
      showToast(dismissed ? t('reports.dismissed') : t('reports.resolved'), 'success')
    } catch (e: any) {
      reports.value = prev
      showToast(e.message || t('admin.reports.resolve_failed'), 'error')
    }
  }

  if (reportsLoading.value) return <Spinner />
  if (reportsError.value) {
    return (
      <div class="sh-reports" role="alert">
        <h3>{t('admin.reports.title')}</h3>
        <p class="sh-error">{reportsError.value}</p>
      </div>
    )
  }

  return (
    <div class="sh-reports">
      <h3>{t('admin.reports.title')}</h3>
      {reports.value.length === 0 && (
        <p class="sh-muted">{t('admin.reports.empty')}</p>
      )}
      {reports.value.map(r => {
        // Friendly category labels — the wire enum is uppercase
        // snake_case but admins want sentence-cased categories.
        const categoryLabel = ((c: string) => {
          switch (c) {
            case 'spam':           return t('report.category.spam')
            case 'harassment':     return t('report.category.harassment')
            case 'inappropriate':  return t('report.category.inappropriate')
            case 'misinformation': return t('report.category.misinformation')
            case 'other':          return t('report.category.other')
            default:               return c
          }
        })(r.category)
        const reporterName = householdDisplayName(r.reporter_user_id)
        return (
          <div key={r.id} class="sh-report-row">
            <div class="sh-report-meta">
              <strong>{categoryLabel}</strong>
              <span class="sh-muted">
                {' · '}{r.target_type === 'user'
                  ? t('admin.reports.on_user')
                  : t('admin.reports.on_post')}
                {' · '}{t('admin.reports.reported_by', { name: reporterName })}
              </span>
              {r.reporter_instance_id && (
                <span class="sh-badge sh-badge--peer"
                      title={t('admin.reports.from_other_title')}>
                  {t('admin.reports.from_other')}
                </span>
              )}
              <time
                class="sh-muted"
                dateTime={r.created_at}
                title={new Date(r.created_at).toLocaleString(formatLocale())}
              >
                {relativeDocsTime(r.created_at)}
              </time>
            </div>
            {r.notes && <p class="sh-muted">“{r.notes}”</p>}
            <div class="sh-form-actions">
              <Button onClick={() => resolve(r.id)}>{t('reports.resolve')}</Button>
              <Button variant="secondary" onClick={() => resolve(r.id, true)}>
                {t('reports.dismiss')}
              </Button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
