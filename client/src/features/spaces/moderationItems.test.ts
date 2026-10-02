/**
 * Moderation-queue item helpers: action labels, previews, the edit field
 * table (with "changed since submitted"), the page line diff and the
 * expiry countdown.
 */
import { describe, it, expect } from 'vitest'
import {
  actionLabel, expiryLabel, fieldDiffRows, formatFieldValue, itemEntity, lineDiff, previewText,
  MAX_DIFF_LINES, type ModerationItem,
} from './moderationItems'

const base: ModerationItem = {
  id: 'i1', space_id: 's1', feature: 'tasks', action: 'edit', entity: 'task', op: null,
  target_id: 't1', submitted_by: 'u2', submitted_at: '2026-10-01T00:00:00Z',
  expires_at: '2026-10-08T00:00:00Z', status: 'pending',
}

describe('actionLabel', () => {
  it('names the entity and the action', () => {
    expect(actionLabel({ ...base, action: 'create' })).toBe('New task')
    expect(actionLabel({ ...base, entity: 'page', feature: 'pages' })).toBe('Edit to page')
    expect(actionLabel({ ...base, entity: 'sticky', feature: 'stickies', action: 'delete' }))
      .toBe('Delete sticky note')
    expect(actionLabel({ ...base, entity: 'list', action: 'create' })).toBe('New task list')
  })

  it('prefers the op (archive / resolve_conflict)', () => {
    expect(actionLabel({ ...base, action: 'delete', op: 'archive' })).toBe('Archive task')
    expect(actionLabel({ ...base, entity: 'page', op: 'resolve_conflict' }))
      .toBe('Page conflict resolution')
  })

  it('an older host without entity falls back to the feature', () => {
    expect(itemEntity({ feature: 'posts', entity: undefined })).toBe('post')
    expect(actionLabel({ ...base, feature: 'posts', entity: undefined, action: 'create' })).toBe('New post')
    expect(actionLabel({ ...base, feature: 'gallery', entity: undefined, action: 'upload' }))
      .toBe('gallery · upload')
  })
})

describe('previewText', () => {
  it('reads the title / content, the snapshot for a delete', () => {
    expect(previewText({ ...base, action: 'create', preview: { title: 'Buy milk' } })).toBe('Buy milk')
    expect(previewText({ ...base, action: 'delete', snapshot: { title: 'Old' }, preview: {} })).toBe('Old')
    expect(previewText({ ...base, feature: 'posts', preview: { content: 'a '.repeat(100) } }, 10))
      .toBe('a a a a a …')
    expect(previewText({ ...base, preview: { bazaar: { title: 'Bike' }, content: 'x' } })).toBe('Bike')
  })

  it('falls back to the legacy payload', () => {
    expect(previewText({ ...base, preview: undefined, payload: { content: 'legacy' } })).toBe('legacy')
  })
})

describe('fieldDiffRows', () => {
  it('lists the changed fields old → new and flags one changed since', () => {
    const rows = fieldDiffRows({
      ...base,
      preview: { title: 'New title', status: 'done' },
      snapshot: { title: 'Old title', status: 'todo' },
      current: { title: 'Old title', status: 'in_progress' },
    })
    expect(rows).toEqual([
      { field: 'title', before: 'Old title', after: 'New title' },
      { field: 'status', before: 'todo', after: 'done', changedSince: 'in_progress' },
    ])
  })

  it('skips fields the caller diffs otherwise', () => {
    const rows = fieldDiffRows({ ...base, preview: { title: 'b', content: 'x' }, snapshot: { title: 'a' } }, ['content'])
    expect(rows.map(r => r.field)).toEqual(['title'])
  })
})

describe('formatFieldValue', () => {
  it('formats empties, lists, booleans and task statuses', () => {
    expect(formatFieldValue('due_date', null)).toBe('—')
    expect(formatFieldValue('labels', ['a', 'b'])).toBe('a, b')
    expect(formatFieldValue('assignees', ['u1'], () => 'Lena')).toBe('Lena')
    expect(formatFieldValue('all_day', true)).toBe('Yes')
    expect(formatFieldValue('status', 'in_progress')).toBe('In progress')
    expect(formatFieldValue('location', { lat: 1, lon: 2, label: 'Park' })).toBe('Park')
  })
})

describe('lineDiff', () => {
  it('marks removed and added lines around the common ones', () => {
    expect(lineDiff('a\nb\nc', 'a\nB\nc\nd')).toEqual([
      { kind: 'same', text: 'a' },
      { kind: 'del', text: 'b' },
      { kind: 'add', text: 'B' },
      { kind: 'same', text: 'c' },
      { kind: 'add', text: 'd' },
    ])
  })

  it('gives up (null) on very long bodies', () => {
    const big = Array.from({ length: MAX_DIFF_LINES + 1 }, (_, i) => String(i)).join('\n')
    expect(lineDiff(big, 'x')).toBeNull()
  })
})

describe('expiryLabel', () => {
  const now = Date.parse('2026-10-05T00:00:00Z')
  it('counts down in days, hours, then says expired', () => {
    expect(expiryLabel('2026-10-08T00:00:00Z', now)).toBe('Expires in 3 days')
    expect(expiryLabel('2026-10-05T05:00:00Z', now)).toBe('Expires in 5 hours')
    expect(expiryLabel('2026-10-04T00:00:00Z', now)).toBe('Expired')
  })

  it('reads a naive SQLite UTC stamp as UTC', () => {
    expect(expiryLabel('2026-10-08 00:00:00', now)).toBe('Expires in 3 days')
  })
})
