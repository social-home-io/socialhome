/**
 * Timetable store: REST wrappers keep ``timetables`` in step with the
 * server's answer; version conflicts refetch + toast; orphan 409s ask
 * for confirmation and retry with the flag; WS frames upsert / remove
 * household timetables and ignore space ones.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Timetable, TimetableEntry } from '@/types'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a),
      post: (...a: unknown[]) => apiPost(...a),
      patch: (...a: unknown[]) => apiPatch(...a),
      put: (...a: unknown[]) => apiPut(...a),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})

const handlers: Record<string, (e: { type: string; data: Record<string, unknown> }) => void> = {}
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string; data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...a) }))

import { ApiError } from '@/api'
import {
  timetables, selectedId, loaded, loadTimetables, createTimetable, patchHeader,
  addEntry, patchEntry, deleteEntry, replaceEntries, copyDay,
  deleteTimetable, duplicateTimetable, wireTimetablesWs,
} from './timetables'

export function entry(id: string, weekday: number, start: string, end: string,
  extra: Partial<TimetableEntry> = {}): TimetableEntry {
  return {
    id, weekday, start, end, kind: 'lesson', label: null, title: null, room: null,
    teacher: null, note: null, color: null, icon: null, ...extra,
  }
}

function tt(id: string, version = 1, extra: Partial<Timetable> = {}): Timetable {
  return {
    schema: 1, id, name: `TT ${id}`, created_by: 'u1', created_at: '', updated_at: '',
    updated_by: null, version, week_start: 0, tz: 'UTC', color: null,
    days: [0, 1, 2, 3, 4],
    defaults: { lesson_minutes: 45, gap_minutes: 5, day_start: '08:00' },
    entries: [], overrides: [],
    validity: { valid_from: null, valid_until: null, excluded_weeks: [] },
    assignees: ['u1'], active_this_week: true, valid_today: true, ...extra,
  }
}

function apiError(status: number, code: string, extra: Record<string, unknown> = {}) {
  return new ApiError(status, '/api/timetables', { code, detail: 'x', ...extra })
}

wireTimetablesWs()

beforeEach(() => {
  timetables.value = []
  selectedId.value = null
  loaded.value = false
  for (const f of [apiGet, apiPost, apiPatch, apiPut, apiDelete, showToast, confirmDialog]) {
    f.mockReset()
  }
})

describe('loading + create', () => {
  it('loads the household timetables', async () => {
    apiGet.mockResolvedValue({ timetables: [tt('a'), tt('b')] })
    await loadTimetables()
    expect(apiGet).toHaveBeenCalledWith('/api/timetables')
    expect(timetables.value.map(x => x.id)).toEqual(['a', 'b'])
    expect(loaded.value).toBe(true)
  })

  it('create appends the created timetable', async () => {
    timetables.value = [tt('a')]
    apiPost.mockResolvedValue({ timetable: tt('b') })
    const created = await createTimetable({ name: 'B', template: 'school' })
    expect(apiPost).toHaveBeenCalledWith('/api/timetables', { name: 'B', template: 'school' })
    expect(created.id).toBe('b')
    expect(timetables.value.map(x => x.id)).toEqual(['a', 'b'])
  })

  it('duplicate appends the copy', async () => {
    timetables.value = [tt('a')]
    apiPost.mockResolvedValue({ timetable: tt('c') })
    await duplicateTimetable('a')
    expect(apiPost).toHaveBeenCalledWith('/api/timetables/a/duplicate', {})
    expect(timetables.value.map(x => x.id)).toEqual(['a', 'c'])
  })

  it('delete removes the timetable and clears the selection', async () => {
    timetables.value = [tt('a'), tt('b')]
    selectedId.value = 'a'
    apiDelete.mockResolvedValue(undefined)
    await deleteTimetable('a')
    expect(apiDelete).toHaveBeenCalledWith('/api/timetables/a')
    expect(timetables.value.map(x => x.id)).toEqual(['b'])
    expect(selectedId.value).toBeNull()
  })
})

describe('mutations send the version and replace the stored timetable', () => {
  it('patchHeader', async () => {
    timetables.value = [tt('a', 3)]
    apiPatch.mockResolvedValue({ timetable: tt('a', 4, { name: 'New' }) })
    const out = await patchHeader('a', { name: 'New' })
    expect(apiPatch).toHaveBeenCalledWith('/api/timetables/a', { version: 3, name: 'New' })
    expect(out?.version).toBe(4)
    expect(timetables.value[0].name).toBe('New')
  })

  it('addEntry / patchEntry / replaceEntries', async () => {
    timetables.value = [tt('a', 1)]
    apiPost.mockResolvedValueOnce({ timetable: tt('a', 2) })
    await addEntry('a', { weekday: 0, start: '08:00', end: '08:45', kind: 'lesson' })
    expect(apiPost).toHaveBeenLastCalledWith('/api/timetables/a/entries',
      { version: 1, weekday: 0, start: '08:00', end: '08:45', kind: 'lesson' })

    apiPatch.mockResolvedValueOnce({ timetable: tt('a', 3) })
    await patchEntry('a', 'e1', { title: 'Mathe' })
    expect(apiPatch).toHaveBeenLastCalledWith('/api/timetables/a/entries/e1',
      { version: 2, title: 'Mathe' })

    apiPut.mockResolvedValueOnce({ timetable: tt('a', 4) })
    const list = [entry('e1', 0, '08:00', '08:45')]
    await replaceEntries('a', list)
    expect(apiPut).toHaveBeenLastCalledWith('/api/timetables/a/entries',
      { version: 3, entries: list })

    expect(timetables.value[0].version).toBe(4)
  })

  it('deleteEntry passes ?version= and offers an undo that restores the entries', async () => {
    const before = [entry('e1', 0, '08:00', '08:45'), entry('e2', 0, '08:50', '09:35')]
    timetables.value = [tt('a', 2, { entries: before })]
    apiDelete.mockResolvedValue({ timetable: tt('a', 3, { entries: [before[1]] }) })
    await deleteEntry('a', 'e1')
    expect(apiDelete).toHaveBeenCalledWith('/api/timetables/a/entries/e1?version=2')
    expect(timetables.value[0].version).toBe(3)
    const [, , opts] = showToast.mock.calls[0]
    expect(opts.action.label).toBe('Undo')

    apiPut.mockResolvedValue({ timetable: tt('a', 4, { entries: before }) })
    opts.action.onClick()
    await vi.waitFor(() => expect(apiPut).toHaveBeenCalledWith(
      '/api/timetables/a/entries', { version: 3, entries: before }))
  })
})

describe('409 TIMETABLE_CONFLICT', () => {
  it('refetches the timetable and toasts that it was reloaded', async () => {
    timetables.value = [tt('a', 1)]
    apiPatch.mockRejectedValue(apiError(409, 'TIMETABLE_CONFLICT', { current_version: 2 }))
    apiGet.mockResolvedValue({ timetable: tt('a', 2, { name: 'Theirs' }) })
    const out = await patchHeader('a', { name: 'Mine' })
    expect(out).toBeNull()
    expect(apiGet).toHaveBeenCalledWith('/api/timetables/a')
    expect(timetables.value[0].name).toBe('Theirs')
    expect(showToast).toHaveBeenCalledWith(
      'Someone else just changed this timetable — reloaded', 'info')
  })

  it('rethrows other errors (e.g. a 422 the dialog shows inline)', async () => {
    timetables.value = [tt('a', 1)]
    apiPatch.mockRejectedValue(apiError(422, 'UNPROCESSABLE'))
    await expect(patchHeader('a', { name: '' })).rejects.toBeInstanceOf(ApiError)
  })
})

describe('orphan 409s confirm, then retry with the flag', () => {
  it('DAYS_ORPHAN_ENTRIES → confirm → drop_orphans', async () => {
    const entries = Array.from({ length: 6 }, (_, i) =>
      entry(`s${i}`, 5, `0${8 + i}:00`, `0${8 + i}:45`))
    timetables.value = [tt('a', 1, { days: [0, 1, 2, 3, 4, 5], entries })]
    apiPatch
      .mockRejectedValueOnce(apiError(409, 'DAYS_ORPHAN_ENTRIES', { count: 6 }))
      .mockResolvedValueOnce({ timetable: tt('a', 2) })
    confirmDialog.mockResolvedValue(true)
    const out = await patchHeader('a', { days: [0, 1, 2, 3, 4] })
    expect(confirmDialog.mock.calls[0][0]).toBe('Remove 6 lessons on Saturday?')
    expect(apiPatch).toHaveBeenLastCalledWith('/api/timetables/a',
      { version: 1, days: [0, 1, 2, 3, 4], drop_orphans: true })
    expect(out?.version).toBe(2)
  })

  it('a declined confirm sends nothing more', async () => {
    timetables.value = [tt('a', 1)]
    apiPatch.mockRejectedValueOnce(apiError(409, 'DAYS_ORPHAN_ENTRIES', { count: 1 }))
    confirmDialog.mockResolvedValue(false)
    const out = await patchHeader('a', { days: [0] })
    expect(out).toBeNull()
    expect(apiPatch).toHaveBeenCalledTimes(1)
  })

  it('DAY_HAS_ENTRIES on copy → confirm → replace', async () => {
    timetables.value = [tt('a', 1)]
    apiPost
      .mockRejectedValueOnce(apiError(409, 'DAY_HAS_ENTRIES', { count: 3 }))
      .mockResolvedValueOnce({ timetable: tt('a', 2) })
    confirmDialog.mockResolvedValue(true)
    await copyDay('a', 0, [1])
    expect(confirmDialog.mock.calls[0][0]).toBe('Replace 3 lessons on Tuesday?')
    expect(apiPost).toHaveBeenLastCalledWith('/api/timetables/a/days/0/copy',
      { version: 1, to: [1], with_subjects: true, replace: true })
  })
})

describe('WebSocket frames', () => {
  it('timetable.changed upserts a newer version, keeping the computed flags', () => {
    timetables.value = [tt('a', 1, { active_this_week: false })]
    const wire = tt('a', 2, { name: 'Renamed' })
    delete wire.active_this_week
    delete wire.valid_today
    handlers['timetable.changed']({ type: 'timetable.changed',
      data: { type: 'timetable.changed', space_id: null, timetable: wire } })
    expect(timetables.value[0].name).toBe('Renamed')
    expect(timetables.value[0].active_this_week).toBe(false)
  })

  it('ignores a stale frame (our own mutation already applied it)', () => {
    timetables.value = [tt('a', 5, { name: 'Current' })]
    handlers['timetable.changed']({ type: 'timetable.changed',
      data: { space_id: null, timetable: tt('a', 4, { name: 'Old' }) } })
    expect(timetables.value[0].name).toBe('Current')
  })

  it('a new household timetable is fetched for its computed flags', async () => {
    apiGet.mockResolvedValue({ timetable: tt('n', 1) })
    handlers['timetable.changed']({ type: 'timetable.changed',
      data: { space_id: null, timetable: tt('n', 1) } })
    expect(timetables.value.map(x => x.id)).toEqual(['n'])
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/timetables/n'))
  })

  it('timetable.deleted removes it and clears the selection', () => {
    timetables.value = [tt('a'), tt('b')]
    selectedId.value = 'b'
    handlers['timetable.deleted']({ type: 'timetable.deleted',
      data: { timetable_id: 'b', space_id: null } })
    expect(timetables.value.map(x => x.id)).toEqual(['a'])
    expect(selectedId.value).toBeNull()
  })

  it('ignores space-scoped frames', () => {
    timetables.value = [tt('a', 1)]
    handlers['timetable.changed']({ type: 'timetable.changed',
      data: { space_id: 's1', timetable: tt('a', 9, { name: 'Space' }) } })
    handlers['timetable.deleted']({ type: 'timetable.deleted',
      data: { timetable_id: 'a', space_id: 's1' } })
    expect(timetables.value).toHaveLength(1)
    expect(timetables.value[0].name).toBe('TT a')
  })
})

describe('review fixes', () => {
  it('a caller-supplied baseVersion is sent instead of the stored one (dialog snapshot → 409)', async () => {
    timetables.value = [tt('a', 5)]
    apiPatch.mockRejectedValue(apiError(409, 'TIMETABLE_CONFLICT', { current_version: 5 }))
    apiGet.mockResolvedValue({ timetable: tt('a', 5) })
    const out = await patchEntry('a', 'e1', { title: 'X' }, { baseVersion: 3 })
    expect(apiPatch).toHaveBeenCalledWith('/api/timetables/a/entries/e1', { version: 3, title: 'X' })
    expect(out).toBeNull()
    expect(showToast).toHaveBeenCalledWith(
      'Someone else just changed this timetable — reloaded', 'info')
  })

  it('undo sends the version of the mutation that offered it, so a later change 409s', async () => {
    const before = [entry('e1', 0, '08:00', '08:45'), entry('e2', 0, '08:50', '09:35')]
    timetables.value = [tt('a', 2, { entries: before })]
    apiDelete.mockResolvedValue({ timetable: tt('a', 3, { entries: [before[1]] }) })
    await deleteEntry('a', 'e1')
    const opts = showToast.mock.calls[0][2]
    // Someone else saves in between (WS frame → v4).
    handlers['timetable.changed']({ type: 'timetable.changed',
      data: { space_id: null, timetable: tt('a', 4, { entries: [before[1]] }) } })
    apiPut.mockRejectedValue(apiError(409, 'TIMETABLE_CONFLICT', { current_version: 4 }))
    apiGet.mockResolvedValue({ timetable: tt('a', 4) })
    opts.action.onClick()
    await vi.waitFor(() => expect(apiPut).toHaveBeenCalledWith(
      '/api/timetables/a/entries', { version: 3, entries: before }))
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'Someone else just changed this timetable — reloaded', 'info'))
  })

  it('an out-of-order (older) response never overwrites a newer stored copy', async () => {
    timetables.value = [tt('a', 1)]
    let resolveSlow: (v: unknown) => void = () => {}
    apiPatch
      .mockImplementationOnce(() => new Promise(r => { resolveSlow = r }))
      .mockResolvedValueOnce({ timetable: tt('a', 3, { name: 'Newest' }) })
    const slow = patchHeader('a', { name: 'Slow' })
    await patchHeader('a', { color: 'teal' })
    resolveSlow({ timetable: tt('a', 2, { name: 'Slow' }) })
    const out = await slow
    expect(timetables.value[0].name).toBe('Newest')
    expect(out?.version).toBe(3)
  })

  it('deleteEntry refetches when the server answers without a body', async () => {
    timetables.value = [tt('a', 2, { entries: [entry('e1', 0, '08:00', '08:45')] })]
    apiDelete.mockResolvedValue(null)
    apiGet.mockResolvedValue({ timetable: tt('a', 3) })
    const out = await deleteEntry('a', 'e1')
    expect(apiGet).toHaveBeenCalledWith('/api/timetables/a')
    expect(out?.version).toBe(3)
  })

  it('encodes ids in URL paths', async () => {
    timetables.value = [tt('a b', 1)]
    apiPatch.mockResolvedValue({ timetable: tt('a b', 2) })
    await patchEntry('a b', 'e/1', { title: 'X' })
    expect(apiPatch).toHaveBeenCalledWith('/api/timetables/a%20b/entries/e%2F1', { version: 1, title: 'X' })
  })
})
