/**
 * Household timetables store (school "Stundenplan").
 *
 * Every mutation carries the stored ``version`` (the server's
 * compare-and-swap) and replaces the stored timetable with the one in
 * the response, so the UI never merges locally. Error handling that
 * every caller wants is centralised here:
 *
 *  * 409 ``TIMETABLE_CONFLICT`` — someone else saved first: refetch the
 *    timetable, toast "reloaded", resolve ``null`` (the caller keeps
 *    its dialog open; a retry reads the fresh version).
 *  * 409 ``DAYS_ORPHAN_ENTRIES`` / ``DAY_HAS_ENTRIES`` — the change
 *    would drop entries: ask via ``confirmDialog`` and retry with
 *    ``drop_orphans`` / ``replace``; a decline resolves ``null``.
 *  * anything else (422 validation…) is rethrown for the caller to
 *    show inline.
 *
 * WS: ``timetable.changed`` carries the bare wire dict (no computed
 * ``active_this_week`` / ``valid_today``) — a newer version is merged
 * in keeping the previous flags; an unknown timetable is refetched for
 * them. Frames with a ``space_id`` belong to space timetables and are
 * ignored here.
 */
import { signal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { ws } from '@/ws'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'
import { editableFrom } from '@/features/timetable/dates'
import { weekdayName } from '@/features/timetable/time'
import type {
  ResolvedWeek, Timetable, TimetableEntry, TimetableOverride, TimetableValidity,
} from '@/types'

export const timetables = signal<Timetable[]>([])
export const selectedId = signal<string | null>(null)
export const loaded = signal(false)

type TimetableBody = { timetable: Timetable }

/** Fields of a new entry (the server mints the id). */
export type NewEntry = Partial<Omit<TimetableEntry, 'id'>>
  & Pick<TimetableEntry, 'weekday' | 'start' | 'end'>
/** An element of a replace-all: existing entries keep their id. */
export type EntryInput = Omit<TimetableEntry, 'id'> & { id?: string }
/** One slot of a generated day (the server mints ids, forces the weekday). */
export type SlotInput = Partial<Omit<TimetableEntry, 'id' | 'weekday'>>
  & Pick<TimetableEntry, 'start' | 'end'>
/** Fields of a new override (the server mints the id). */
export type OverrideInput = Partial<Omit<TimetableOverride, 'id'>>
  & Pick<TimetableOverride, 'date' | 'kind'>

export interface CreateTimetableBody {
  name: string
  template?: 'school' | 'empty'
  week_start?: 0 | 6
  days?: number[]
  tz?: string
  assignees?: string[]
  color?: string | null
}

export type HeaderPatch = Partial<Pick<Timetable,
  'name' | 'color' | 'week_start' | 'tz' | 'days' | 'assignees' | 'defaults'>>

/** Options every versioned mutation takes. ``baseVersion`` is the
 *  version the caller's edit is based on — an open dialog passes the
 *  version it snapshotted at mount, so a change that landed meanwhile
 *  409s (refetch + "reloaded") instead of being silently overwritten. */
export interface MutateOpts {
  baseVersion?: number
}

/** Undo toast for a replace-all. ``onUndone`` runs after the undo
 *  saved (e.g. to move focus back to the grid). */
export interface UndoOpts {
  message: string
  onUndone?: () => void
}

const enc = encodeURIComponent
const path = (id: string, rest = '') => `/api/timetables/${enc(id)}${rest}`

function find(id: string): Timetable {
  const tt = timetables.value.find(x => x.id === id)
  if (!tt) throw new Error(`unknown timetable ${id}`)
  return tt
}

/** Insert or replace ``tt`` — unless the stored copy is newer (an
 *  out-of-order response or a WS frame that beat it), which wins.
 *  Flags missing on ``tt`` (a WS wire dict) keep their old value. */
function store(tt: Timetable): Timetable {
  const prev = timetables.value.find(x => x.id === tt.id)
  if (prev && prev.version > tt.version) return prev
  const merged: Timetable = {
    ...tt,
    active_this_week: tt.active_this_week ?? prev?.active_this_week,
    valid_today: tt.valid_today ?? prev?.valid_today,
  }
  timetables.value = prev
    ? timetables.value.map(x => x.id === tt.id ? merged : x)
    : [...timetables.value, merged]
  return merged
}

function drop(id: string) {
  timetables.value = timetables.value.filter(x => x.id !== id)
  if (selectedId.value === id) selectedId.value = null
}

function isCode(e: unknown, ...codes: string[]): e is ApiError {
  return e instanceof ApiError && e.code !== null && codes.includes(e.code)
}

function countOf(e: ApiError): number {
  const n = e.extra.count
  return typeof n === 'number' ? n : 0
}

/** "Saturday" / "Saturday, Sunday" */
function dayList(days: readonly number[]): string {
  return days.map(d => weekdayName(d, 'long')).join(', ')
}

export async function loadTimetables(): Promise<void> {
  wireTimetablesWs()
  const body = await api.get('/api/timetables') as { timetables: Timetable[] }
  timetables.value = body.timetables
  loaded.value = true
}

/** GET one timetable into the store; ``null`` when it is gone (404). */
export async function refetchTimetable(id: string): Promise<Timetable | null> {
  try {
    const body = await api.get(path(id)) as TimetableBody
    return store(body.timetable)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      drop(id)
      return null
    }
    throw e
  }
}

/** Run a versioned mutation. ``call`` gets the version to send: the
 *  caller's ``baseVersion``, else the stored one. A response without a
 *  body is healed by a refetch. */
async function run(
  id: string,
  call: (version: number) => Promise<TimetableBody | null>,
  opts: MutateOpts = {},
): Promise<Timetable | null> {
  try {
    const body = await call(opts.baseVersion ?? find(id).version)
    return body ? store(body.timetable) : await refetchTimetable(id)
  } catch (e) {
    if (isCode(e, 'TIMETABLE_CONFLICT')) {
      await refetchTimetable(id)
      showToast(t('timetable.conflict_reloaded'), 'info')
      return null
    }
    throw e
  }
}

/** ``run`` with the orphan-confirm retry (``force`` = the flag). */
async function runConfirming(
  id: string,
  call: (version: number, force: boolean) => Promise<TimetableBody | null>,
  message: (count: number) => string,
  confirmLabel: string,
  opts: MutateOpts = {},
): Promise<Timetable | null> {
  try {
    return await run(id, v => call(v, false), opts)
  } catch (e) {
    if (!isCode(e, 'DAYS_ORPHAN_ENTRIES', 'DAY_HAS_ENTRIES')) throw e
    const ok = await confirmDialog(message(countOf(e)), {
      title: t('timetable.confirm.title'),
      confirmLabel,
      destructive: true,
    })
    if (!ok) return null
    return run(id, v => call(v, true), opts)
  }
}

function lessonsMessage(key: string, count: number, days: readonly number[]): string {
  return t(count === 1 ? `${key}_one` : key, { count: String(count), days: dayList(days) })
}

export async function createTimetable(body: CreateTimetableBody): Promise<Timetable> {
  const res = await api.post('/api/timetables', body) as TimetableBody
  return store(res.timetable)
}

export async function duplicateTimetable(id: string, name?: string): Promise<Timetable> {
  const res = await api.post(path(id, '/duplicate'), name ? { name } : {}) as TimetableBody
  return store(res.timetable)
}

export async function deleteTimetable(id: string): Promise<void> {
  await api.delete(path(id))
  drop(id)
}

/** PATCH the header. Removing days that still hold entries asks first. */
export function patchHeader(
  id: string, fields: HeaderPatch, opts: MutateOpts = {},
): Promise<Timetable | null> {
  const removed = fields.days
    ? find(id).days.filter(d => !fields.days!.includes(d)
        && find(id).entries.some(e => e.weekday === d))
    : []
  return runConfirming(
    id,
    (version, force) => api.patch(path(id), {
      version, ...fields, ...(force ? { drop_orphans: true } : {}),
    }),
    count => lessonsMessage('timetable.confirm.drop_days', count, removed),
    t('timetable.confirm.remove'),
    opts,
  )
}

export function addEntry(
  id: string, fields: NewEntry, opts: MutateOpts = {},
): Promise<Timetable | null> {
  return run(id, version => api.post(path(id, '/entries'), { version, ...fields }), opts)
}

export async function patchEntry(
  id: string, entryId: string, fields: Partial<Omit<TimetableEntry, 'id'>>,
  opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  const previous = find(id).entries
  const out = await run(id, version => api.patch(path(id, `/entries/${enc(entryId)}`), {
    version, ...fields,
  }), opts)
  if (out && opts.undo) offerEntriesUndo(id, previous, out.version, opts.undo)
  return out
}

/** PUT the whole entry list in one atomic call. With ``undo`` a toast
 *  offers to put the previous list back. */
export async function replaceEntries(
  id: string,
  entries: EntryInput[],
  opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  const previous = find(id).entries
  const out = await run(id, version => api.put(path(id, '/entries'), { version, entries }), opts)
  if (out && opts.undo) offerEntriesUndo(id, previous, out.version, opts.undo)
  return out
}

/** Undo = PUT the previous entries, based on the version the mutation
 *  produced — if anyone saved since, it 409s instead of clobbering.
 *  Exported for callers that chain several mutations (the day builder:
 *  generate + copy) under one Undo. */
export function offerEntriesUndo(
  id: string, previous: TimetableEntry[], version: number, undo: UndoOpts,
) {
  showToast(undo.message, 'info', {
    action: {
      label: t('timetable.undo'),
      onClick: () => {
        replaceEntries(id, previous, { baseVersion: version })
          .then(out => { if (out) undo.onUndone?.() })
          .catch((e: unknown) => showToast((e as Error).message, 'error'))
      },
    },
  })
}

/** DELETE one entry, then offer an undo (re-adds it via PUT). */
export async function deleteEntry(
  id: string, entryId: string, opts: MutateOpts & { onUndone?: () => void } = {},
): Promise<Timetable | null> {
  const previous = find(id).entries
  const out = await run(id, version => api.delete<TimetableBody | null>(
    path(id, `/entries/${enc(entryId)}?version=${version}`),
  ), opts)
  if (out) {
    offerEntriesUndo(id, previous, out.version, {
      message: t('timetable.entry.deleted'), onUndone: opts.onUndone,
    })
  }
  return out
}

/** Copy a day onto others; replacing non-empty days asks first. */
export async function copyDay(
  id: string, weekday: number, to: number[], withSubjects = true,
  opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  const previous = find(id).entries
  const out = await runConfirming(
    id,
    (version, force) => api.post(path(id, `/days/${weekday}/copy`), {
      version, to, with_subjects: withSubjects,
      ...(force ? { replace: true } : {}),
    }),
    count => {
      const busy = to.filter(d => find(id).entries.some(e => e.weekday === d))
      return lessonsMessage('timetable.confirm.replace_days', count, busy.length ? busy : to)
    },
    t('timetable.confirm.replace'),
    opts,
  )
  if (out && opts.undo) offerEntriesUndo(id, previous, out.version, opts.undo)
  return out
}

/** Set a day's entries from ``slots`` (the day builder). A day that
 *  already has entries asks "Replace N lessons on Monday?" first. */
export async function generateDay(
  id: string, weekday: number, slots: SlotInput[],
  opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  const previous = find(id).entries
  const out = await runConfirming(
    id,
    (version, force) => api.post(path(id, `/days/${weekday}/generate`), {
      version, slots, ...(force ? { replace: true } : {}),
    }),
    count => lessonsMessage('timetable.confirm.replace_days', count, [weekday]),
    t('timetable.confirm.replace'),
    opts,
  )
  if (out && opts.undo) offerEntriesUndo(id, previous, out.version, opts.undo)
  return out
}

/** PUT the validity (valid from / until, holiday weeks). */
export function setValidity(
  id: string, validity: TimetableValidity, opts: MutateOpts = {},
): Promise<Timetable | null> {
  return run(id, version => api.put(path(id, '/validity'), { version, ...validity }), opts)
}

// ─── Week mode: resolved weeks + per-date overrides ──────────────────

/** The resolved week (overrides applied) containing ``date``. */
export async function fetchWeek(id: string, date: string): Promise<ResolvedWeek> {
  const body = await api.get(path(id, `/weeks/${enc(date)}`)) as { week: ResolvedWeek }
  return body.week
}

/** Run an override mutation; with ``undo`` a toast offers to put the
 *  overrides it touched back the way they were. */
async function runOverrides(
  id: string,
  call: (version: number) => Promise<TimetableBody | null>,
  opts: MutateOpts & { undo?: UndoOpts },
): Promise<Timetable | null> {
  const previous = find(id).overrides
  const out = await run(id, call, opts)
  if (out && opts.undo) offerOverridesUndo(id, previous, out.version, opts.undo)
  return out
}

export function addOverride(
  id: string, fields: OverrideInput, opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  return runOverrides(id, version => api.post(path(id, '/overrides'), { version, ...fields }), opts)
}

export function patchOverride(
  id: string, overrideId: string, fields: Partial<Omit<TimetableOverride, 'id'>>,
  opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  return runOverrides(id, version => api.patch(path(id, `/overrides/${enc(overrideId)}`), {
    version, ...fields,
  }), opts)
}

export function deleteOverride(
  id: string, overrideId: string, opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  return runOverrides(id, version => api.delete<TimetableBody | null>(
    path(id, `/overrides/${enc(overrideId)}?version=${version}`),
  ), opts)
}

/** Drop every override in the week containing ``date``. */
export function clearWeek(
  id: string, date: string, opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  return runOverrides(id, version => api.delete<TimetableBody | null>(
    path(id, `/weeks/${enc(date)}/overrides?version=${version}`),
  ), opts)
}

const withoutId = ({ id: _id, ...rest }: TimetableOverride) => rest

/** Undo for override mutations: diff the overrides now against
 *  ``previous`` and replay the difference — DELETE the new ones, PATCH
 *  changed ones back, re-POST removed ones — as one sequence, each call
 *  based on the version the one before produced. There is no atomic
 *  "replace all overrides" endpoint, so when anyone saved since the
 *  mutation (the version moved) the undo is refused up front instead
 *  of half-applying over their change. */
function offerOverridesUndo(
  id: string, previous: TimetableOverride[], version: number, undo: UndoOpts,
) {
  showToast(undo.message, 'info', {
    action: {
      label: t('timetable.undo'),
      onClick: () => {
        restoreOverrides(id, previous, version)
          .then(out => { if (out) undo.onUndone?.() })
          .catch((e: unknown) => showToast((e as Error).message, 'error'))
      },
    },
  })
}

async function restoreOverrides(
  id: string, previous: TimetableOverride[], version: number,
): Promise<Timetable | null> {
  const now = find(id)
  if (now.version !== version) {
    showToast(t('timetable.undo_unavailable'), 'error')
    return null
  }
  // The backend refuses overrides dated > 14 days ago — never try to
  // bring those back (it would 422 halfway through the replay).
  const oldest = editableFrom(now)
  const before = new Map(previous.map(o => [o.id, o]))
  const after = new Map(now.overrides.map(o => [o.id, o]))
  const steps: Step[] = []
  for (const o of now.overrides) {
    if (!before.has(o.id)) {
      steps.push(v => api.delete(path(id, `/overrides/${enc(o.id)}?version=${v}`)))
    }
  }
  for (const o of previous) {
    const cur = after.get(o.id)
    if (cur && JSON.stringify(cur) !== JSON.stringify(o) && o.date >= oldest) {
      steps.push(v => api.patch(path(id, `/overrides/${enc(o.id)}`), { version: v, ...withoutId(o) }))
    }
  }
  for (const o of previous) {
    if (!after.has(o.id) && o.date >= oldest) {
      steps.push(v => api.post(path(id, '/overrides'), { version: v, ...withoutId(o) }))
    }
  }
  return runSteps(id, steps, version)
}

type Step = (version: number) => Promise<TimetableBody | null>

/** Run ``steps`` one after another, each based on the version the one
 *  before produced. The first step fails like any mutation (a 409
 *  reloads; anything else is rethrown). A step after the first failing
 *  leaves a half-applied sequence: stop, refetch, and say so. */
async function runSteps(id: string, steps: Step[], version: number): Promise<Timetable | null> {
  let out: Timetable | null = find(id)
  let v = version
  for (let i = 0; i < steps.length; i++) {
    if (i === 0) {
      out = await run(id, steps[0], { baseVersion: v })
      if (!out) return null
    } else {
      try {
        const body = await steps[i](v)
        out = body ? store(body.timetable) : await refetchTimetable(id)
        if (!out) return null
      } catch {
        await refetchTimetable(id).catch(() => null)
        showToast(t('timetable.partly_applied'), 'error')
        return null
      }
    }
    v = out.version
  }
  return out
}

/** One override change of a sequence (``applyOverrides``). */
export type OverrideStep =
  | { op: 'add'; fields: OverrideInput }
  | { op: 'patch'; overrideId: string; fields: Partial<Omit<TimetableOverride, 'id'>> }
  | { op: 'delete'; overrideId: string }

/** Several override changes as one action — e.g. cancelling both
 *  halves of a double lesson — run in sequence under ONE Undo. There is
 *  no batch endpoint; each call is based on the previous one's version. */
export async function applyOverrides(
  id: string, ops: OverrideStep[], opts: MutateOpts & { undo?: UndoOpts } = {},
): Promise<Timetable | null> {
  const previous = find(id).overrides
  const steps: Step[] = ops.map(o => (v: number) => o.op === 'add'
    ? api.post(path(id, '/overrides'), { version: v, ...o.fields })
    : o.op === 'patch'
      ? api.patch(path(id, `/overrides/${enc(o.overrideId)}`), { version: v, ...o.fields })
      : api.delete(path(id, `/overrides/${enc(o.overrideId)}?version=${v}`)))
  const out = await runSteps(id, steps, opts.baseVersion ?? find(id).version)
  if (out && opts.undo && ops.length > 0) offerOverridesUndo(id, previous, out.version, opts.undo)
  return out
}

// ─── WebSocket ───────────────────────────────────────────────────────

let _wired = false

export function wireTimetablesWs(): void {
  if (_wired) return
  _wired = true
  ws.on('timetable.changed', (e) => {
    const d = e.data as { space_id?: string | null; timetable?: Timetable }
    if (d.space_id != null || !d.timetable) return
    const prev = timetables.value.find(x => x.id === d.timetable!.id)
    if (prev && prev.version >= d.timetable.version) return
    store(d.timetable)
    // The frame has no computed flags — a timetable we haven't seen
    // (or whose validity moved) needs the REST view for them.
    if (!prev || JSON.stringify(prev.validity) !== JSON.stringify(d.timetable.validity)) {
      void refetchTimetable(d.timetable.id).catch(() => { /* next load heals it */ })
    }
  })
  ws.on('timetable.deleted', (e) => {
    const d = e.data as { space_id?: string | null; timetable_id?: string }
    if (d.space_id != null || !d.timetable_id) return
    drop(d.timetable_id)
  })
}
