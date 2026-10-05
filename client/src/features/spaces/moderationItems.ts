/**
 * Space moderation-queue items (§4.3 "Reviewed") — the wire shape of
 * ``GET /api/spaces/{id}/moderation`` / ``.../moderation/mine`` and the
 * pure helpers the queue and the author's pending strip render with:
 * the action label ("New task", "Edit to page"), a one-line preview,
 * the field table of an edit (old → new, flagging fields that changed
 * since the member submitted) and a line diff for page bodies.
 *
 * No DOM, no signals: everything here is unit-testable on its own.
 */
import { formatLocale, t } from '@/i18n/i18n'

export type ModerationFeature = 'posts' | 'pages' | 'tasks' | 'stickies' | 'calendar'
export type ModerationAction = 'create' | 'edit' | 'delete'
export type ModerationEntity = 'post' | 'page' | 'task' | 'list' | 'sticky' | 'event'
export type ModerationStatus = 'pending' | 'approved' | 'rejected' | 'expired'

export interface ModerationItem {
  id: string
  space_id: string
  feature: ModerationFeature | string
  action: ModerationAction | string
  /** Absent on an older host (posts-only queue) — derived from ``feature``. */
  entity?: ModerationEntity | string | null
  /** ``archive`` / ``unarchive`` (a task delete), ``resolve_conflict``
   *  (a page edit), else ``null``. */
  op?: string | null
  target_id?: string | null
  submitted_by: string
  submitted_by_display?: string | null
  submitted_at: string
  expires_at: string
  status: ModerationStatus | string
  reviewed_by?: string | null
  reviewed_at?: string | null
  rejection_reason?: string | null
  /** Approved on this member household and handed to the space's host,
   *  which publishes it (federated moderation, v_43). */
  publishing?: boolean
  /** Proposed state — create: full; edit: the changed fields; delete: the row. */
  preview?: Record<string, unknown> | null
  /** Edit: OLD values of exactly the changed fields; delete: the full row. */
  snapshot?: Record<string, unknown> | null
  /** Edit: the LIVE values of those fields now (``null`` if the target is gone). */
  current?: Record<string, unknown> | null
  /** Legacy raw payload (an older host sends only this). */
  payload?: Record<string, unknown> | null
}

const ENTITY_OF_FEATURE: Record<string, ModerationEntity> = {
  posts: 'post', pages: 'page', tasks: 'task', stickies: 'sticky', calendar: 'event',
}

/** The item's entity — the server's, else the feature's main one. */
export function itemEntity(item: Pick<ModerationItem, 'entity' | 'feature'>): string {
  return item.entity || ENTITY_OF_FEATURE[item.feature] || item.feature
}

/** What the item shows: ``preview``, else the legacy ``payload``. */
export function itemPreview(item: ModerationItem): Record<string, unknown> {
  return item.preview ?? item.payload ?? {}
}

/* For ``i18n:check`` — the keys ``actionLabel`` builds:
 * t('moderation.action.post.create') t('moderation.action.post.edit') t('moderation.action.post.delete')
 * t('moderation.action.page.create') t('moderation.action.page.edit') t('moderation.action.page.delete')
 * t('moderation.action.task.create') t('moderation.action.task.edit') t('moderation.action.task.delete')
 * t('moderation.action.list.create') t('moderation.action.list.edit') t('moderation.action.list.delete')
 * t('moderation.action.sticky.create') t('moderation.action.sticky.edit') t('moderation.action.sticky.delete')
 * t('moderation.action.event.create') t('moderation.action.event.edit') t('moderation.action.event.delete')
 * t('moderation.action.task.archive') t('moderation.action.task.unarchive')
 * t('moderation.action.page.resolve_conflict') t('moderation.action.generic')
 */
const ACTION_KEYS = new Set([
  'post.create', 'post.edit', 'post.delete',
  'page.create', 'page.edit', 'page.delete', 'page.resolve_conflict',
  'task.create', 'task.edit', 'task.delete', 'task.archive', 'task.unarchive',
  'list.create', 'list.edit', 'list.delete',
  'sticky.create', 'sticky.edit', 'sticky.delete',
  'event.create', 'event.edit', 'event.delete',
])

/** "New task", "Edit to page", "Delete sticky note", "Archive task"… */
export function actionLabel(item: Pick<ModerationItem, 'entity' | 'feature' | 'action' | 'op'>): string {
  const entity = itemEntity(item)
  const withOp = item.op ? `${entity}.${item.op}` : null
  const key = withOp && ACTION_KEYS.has(withOp) ? withOp : `${entity}.${item.action}`
  if (ACTION_KEYS.has(key)) return t(`moderation.action.${key}`)
  return t('moderation.action.generic', { feature: item.feature, action: item.action })
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

function clip(text: string, max: number): string {
  const one = text.trim().replace(/\s+/g, ' ')
  return one.length > max ? `${one.slice(0, max)}…` : one
}

/** A one-line description of what the item is about — the title /
 *  content of the proposed (or, for a delete, the doomed) row. */
export function previewText(item: ModerationItem, max = 80): string {
  const p = item.action === 'delete' ? (item.snapshot ?? itemPreview(item)) : itemPreview(item)
  const bazaar = p.bazaar as Record<string, unknown> | undefined
  const poll = p.poll as Record<string, unknown> | undefined
  const schedule = p.schedule as Record<string, unknown> | undefined
  const text = str(bazaar?.title) ?? str(p.title) ?? str(p.summary) ?? str(p.name)
    ?? str(p.content) ?? str(poll?.question) ?? str(schedule?.title)
    ?? str(p.body) ?? str(p.text) ?? str(p.caption)
  return text ? clip(text, max) : ''
}

/* ── Field table (task / sticky / event / page-title edits) ────────── */

/* For ``i18n:check``:
 * t('moderation.field.title') t('moderation.field.description') t('moderation.field.status')
 * t('moderation.field.due_date') t('moderation.field.assignees') t('moderation.field.priority')
 * t('moderation.field.labels') t('moderation.field.list_id') t('moderation.field.name')
 * t('moderation.field.content') t('moderation.field.color') t('moderation.field.summary')
 * t('moderation.field.start') t('moderation.field.end') t('moderation.field.all_day')
 * t('moderation.field.location') t('moderation.field.capacity') t('moderation.field.rrule')
 * t('moderation.field.cover_url') t('moderation.field.cover_image_url')
 */
const FIELD_KEYS = new Set([
  'title', 'description', 'status', 'due_date', 'assignees', 'priority', 'labels',
  'list_id', 'name', 'content', 'color', 'summary', 'start', 'end', 'all_day',
  'location', 'capacity', 'rrule', 'cover_url', 'cover_image_url',
])

/** The field's name in the UI language (the raw name if unknown). */
export function fieldLabel(field: string): string {
  return FIELD_KEYS.has(field) ? t(`moderation.field.${field}`) : field
}

/** Same value? (Deep, for arrays / objects.) */
export function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if ((a === null || a === undefined || a === '') && (b === null || b === undefined || b === '')) {
    return true
  }
  try {
    return JSON.stringify(a) === JSON.stringify(b)
  } catch {
    return false
  }
}

export interface FieldDiffRow {
  field: string
  before: unknown
  after: unknown
  /** The live value now, when it differs from ``before`` — someone
   *  changed the field after the member submitted. ``undefined`` when it
   *  didn't (or the host sent no ``current``). */
  changedSince?: unknown
}

/** Fields of an edit, old → new. ``preview`` holds the changed fields,
 *  ``snapshot`` their old values, ``current`` their values now. Only
 *  fields in ``preview`` are listed; ``skip`` leaves some out (a page's
 *  ``content`` gets a text diff instead). */
export function fieldDiffRows(item: ModerationItem, skip: readonly string[] = []): FieldDiffRow[] {
  const after = itemPreview(item)
  const before = item.snapshot ?? {}
  const current = item.current ?? null
  return Object.keys(after)
    .filter(f => !skip.includes(f))
    .map((field) => {
      const row: FieldDiffRow = { field, before: before[field], after: after[field] }
      if (current && field in current && !sameValue(current[field], before[field])) {
        row.changedSince = current[field]
      }
      return row
    })
}

/** A value for a table cell: ``—`` for empty, lists joined, booleans as
 *  yes / no, a task status by its name. ``nameOf`` resolves user ids
 *  (``assignees``). */
export function formatFieldValue(
  field: string, value: unknown, nameOf: (uid: string) => string = (u) => u,
): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) {
    if (value.length === 0) return '—'
    const parts = value.map(v => (field === 'assignees' && typeof v === 'string') ? nameOf(v) : fmtScalar(v))
    return parts.join(', ')
  }
  if (typeof value === 'boolean') return value ? t('moderation.value.yes') : t('moderation.value.no')
  if (field === 'status' && typeof value === 'string'
      && ['todo', 'in_progress', 'done'].includes(value)) {
    // For ``i18n:check``: t('tasks.status.todo') t('tasks.status.in_progress') t('tasks.status.done')
    return t(`tasks.status.${value}`)
  }
  if ((field === 'start' || field === 'end') && typeof value === 'string') {
    const d = new Date(value)
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleString(formatLocale(), { dateStyle: 'medium', timeStyle: 'short' })
    }
  }
  return fmtScalar(value)
}

function fmtScalar(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') {
    const o = v as Record<string, unknown>
    // A location ``{lat, lon, label}``.
    if (typeof o.label === 'string' && o.label) return o.label
    try { return JSON.stringify(v) } catch { return String(v) }
  }
  return String(v)
}

/* ── Line diff (page bodies) ───────────────────────────────────────── */

export interface DiffLine {
  kind: 'same' | 'add' | 'del'
  text: string
}

/** Above this many lines on either side the diff is skipped (the LCS
 *  table is O(n·m)) and the caller shows old and new in full. */
export const MAX_DIFF_LINES = 600

/** A line diff of ``before`` → ``after`` (longest common subsequence).
 *  ``null`` when either side is too long to diff. */
export function lineDiff(before: string, after: string): DiffLine[] | null {
  const a = before.split('\n')
  const b = after.split('\n')
  if (a.length > MAX_DIFF_LINES || b.length > MAX_DIFF_LINES) return null
  const n = a.length
  const m = b.length
  // lcs[i][j] = LCS length of a[i:] and b[j:].
  const lcs: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: 'same', text: a[i] }); i++; j++ }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { out.push({ kind: 'del', text: a[i] }); i++ }
    else { out.push({ kind: 'add', text: b[j] }); j++ }
  }
  while (i < n) out.push({ kind: 'del', text: a[i++] })
  while (j < m) out.push({ kind: 'add', text: b[j++] })
  return out
}

/* ── Expiry ────────────────────────────────────────────────────────── */

/** "Expires in 3 days" / "Expires in 5 hours" in the UI language;
 *  "Expired" once past. ``now`` is for tests. */
export function expiryLabel(iso: string, now: number = Date.now()): string {
  const at = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso.replace(' ', 'T')}Z`)
  if (Number.isNaN(at)) return ''
  const ms = at - now
  if (ms <= 0) return t('moderation.expired')
  const hours = ms / 3_600_000
  let when: string
  try {
    const rtf = new Intl.RelativeTimeFormat(formatLocale(), { numeric: 'always' })
    when = hours < 1
      ? rtf.format(Math.max(1, Math.round(ms / 60_000)), 'minute')
      : hours < 23.5
        ? rtf.format(Math.round(hours), 'hour')
        : rtf.format(Math.round(hours / 24), 'day')
  } catch {
    when = `${Math.round(hours / 24)}d`
  }
  return t('moderation.expires_in', { when })
}
