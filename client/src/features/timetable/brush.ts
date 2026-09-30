/**
 * Brush mode ("🖌 Fill") — the fastest way to fill in subjects.
 *
 * With a brush picked in the brush bar (a subject: title + icon +
 * colour, or the eraser) a click / tap on a lesson block applies it
 * instead of opening the EntryDialog. On desktop a pointer drag across
 * blocks paints every block the pointer passes and commits them as ONE
 * ``PUT …/entries`` on pointerup (one Undo); a touch drag scrolls the
 * page instead (the browser cancels the pointer), so phones paint tap
 * by tap. Breaks are never brushed. Commits run one after another so
 * quick taps never race each other's version.
 *
 * State is module-level signals so every block (Periods, Timeline, the
 * phone day view) reads it without prop drilling.
 */
import { signal } from '@preact/signals'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { replaceEntries, timetables, type EntryInput } from '@/store/timetables'
import type { Timetable, TimetableColor, TimetableEntry } from '@/types'
import { normalizeSubject } from './colors'
import { focusGrid } from './focus'

export interface PaintBrush {
  kind: 'paint'
  title: string
  icon: string | null
  color: TimetableColor | null
  room: string | null
  teacher: string | null
}
export type Brush = PaintBrush | { kind: 'erase' }

export interface BrushState {
  timetableId: string
  active: Brush | null
}

/** ``null`` = brush mode off. */
export const brushState = signal<BrushState | null>(null)
/** Ids painted by the pointer stroke in progress (for the live tint). */
export const strokeIds = signal<ReadonlySet<string>>(new Set())

export function brushOn(timetableId: string): boolean {
  return brushState.value?.timetableId === timetableId
}

export function startBrush(tt: Timetable): void {
  brushState.value = { timetableId: tt.id, active: brushSubjects(tt)[0] ?? null }
}

/** "+ New subject" dialog open, and the subjects made there that
 *  aren't on the plan yet (per timetable) — brush-session state. */
export const newSubjectOpen = signal(false)
export const extraSubjects = signal<Record<string, PaintBrush[]>>({})

/** Leave brush mode and drop every bit of session state (also what a
 *  test's teardown calls, so nothing leaks into the next test). */
export function stopBrush(): void {
  brushState.value = null
  strokeIds.value = new Set()
  newSubjectOpen.value = false
  extraSubjects.value = {}
  document.removeEventListener('pointerup', onUp)
  document.removeEventListener('pointercancel', onCancel)
  stroke = null
  swallowClick = false
  queue = Promise.resolve()
}

export function pickBrush(active: Brush | null): void {
  const cur = brushState.value
  if (cur) brushState.value = { ...cur, active }
}

/** The timetable's subjects as brushes — one per (normalised) title,
 *  in first-appearance order by day and time, carrying that lesson's
 *  icon, explicit colour, room and teacher. */
export function brushSubjects(tt: Timetable): PaintBrush[] {
  const seen = new Set<string>()
  const out: PaintBrush[] = []
  const sorted = [...tt.entries].sort((a, b) => a.weekday - b.weekday || a.start.localeCompare(b.start))
  for (const e of sorted) {
    if (e.kind !== 'lesson') continue
    const title = e.title?.trim()
    if (!title) continue
    const key = normalizeSubject(title)
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ kind: 'paint', title, icon: e.icon, color: e.color, room: e.room, teacher: e.teacher })
  }
  return out
}

export function sameBrush(a: Brush | null, b: Brush | null): boolean {
  if (!a || !b) return a === b
  if (a.kind === 'erase' || b.kind === 'erase') return a.kind === b.kind
  return normalizeSubject(a.title) === normalizeSubject(b.title)
}

/** "Fill with Mathe" / "Clear". */
export function brushLabel(b: Brush): string {
  return b.kind === 'erase' ? t('timetable.brush.erase_action')
    : t('timetable.brush.fill_with', { title: b.title })
}

/** ``entry`` with the brush applied; ``null`` when nothing would
 *  change (a break, or the lesson already looks like that). Room and
 *  teacher are only filled where the lesson has none. */
export function applyBrush(entry: TimetableEntry, b: Brush): TimetableEntry | null {
  if (entry.kind !== 'lesson') return null
  const next: TimetableEntry = b.kind === 'erase'
    ? { ...entry, title: null, icon: null, color: null, room: null, teacher: null }
    : {
        ...entry,
        title: b.title,
        icon: b.icon,
        color: b.color,
        room: entry.room ?? b.room,
        teacher: entry.teacher ?? b.teacher,
      }
  return JSON.stringify(next) === JSON.stringify(entry) ? null : next
}

let queue: Promise<unknown> = Promise.resolve()

/** Apply the active brush to ``ids`` as one PUT with one Undo. */
export function commitBrush(timetableId: string, ids: readonly string[]): Promise<unknown> {
  const b = brushState.value?.timetableId === timetableId ? brushState.value.active : null
  if (!b || ids.length === 0) return Promise.resolve()
  const run = async () => {
    const tt = timetables.value.find(x => x.id === timetableId)
    if (!tt) return
    const wanted = new Set(ids)
    let changed = 0
    const list: EntryInput[] = tt.entries.map(e => {
      if (!wanted.has(e.id)) return e
      const next = applyBrush(e, b)
      if (!next) return e
      changed += 1
      return next
    })
    if (changed === 0) return
    const message = b.kind === 'erase'
      ? t(changed === 1 ? 'timetable.brush.cleared_one' : 'timetable.brush.cleared', { n: String(changed) })
      : t(changed === 1 ? 'timetable.brush.filled_one' : 'timetable.brush.filled',
          { n: String(changed), title: b.title })
    await replaceEntries(timetableId, list, {
      undo: { message, onUndone: () => focusGrid(timetableId) },
    })
  }
  queue = queue.then(run).catch((e: unknown) => showToast((e as Error).message, 'error'))
  return queue
}

// ─── Pointer strokes ─────────────────────────────────────────────────

interface Stroke {
  timetableId: string
  ids: string[]
  x: number
  y: number
  moved: boolean
  /** Blocks crossed (a merged double lesson is one block, two ids). */
  blocks: number
}

let stroke: Stroke | null = null
/** A pointer stroke just committed — swallow the click that follows. */
let swallowClick = false

/** Moving further than this on one block is a swipe (the phone day
 *  view changes day), not a tap. */
const TAP_SLOP_PX = 10

function onUp(e: PointerEvent) {
  document.removeEventListener('pointerup', onUp)
  document.removeEventListener('pointercancel', onCancel)
  const s = stroke
  stroke = null
  strokeIds.value = new Set()
  if (!s) return
  const far = Math.hypot(e.clientX - s.x, e.clientY - s.y) > TAP_SLOP_PX
  swallowClick = true
  // A click may not follow at all (the pointer ended elsewhere).
  setTimeout(() => { swallowClick = false }, 0)
  if (s.blocks === 1 && far) return
  void commitBrush(s.timetableId, s.ids)
}

function onCancel() {
  document.removeEventListener('pointerup', onUp)
  document.removeEventListener('pointercancel', onCancel)
  stroke = null
  strokeIds.value = new Set()
}

export function strokeStart(
  timetableId: string, entry: TimetableEntry, ids: readonly string[], e: PointerEvent,
): void {
  if (e.button !== 0 || entry.kind !== 'lesson') return
  stroke = { timetableId, ids: [...ids], x: e.clientX, y: e.clientY, moved: false, blocks: 1 }
  strokeIds.value = new Set(ids)
  document.addEventListener('pointerup', onUp)
  document.addEventListener('pointercancel', onCancel)
}

export function strokeEnter(entry: TimetableEntry, ids: readonly string[] = [entry.id]): void {
  const s = stroke
  if (!s || entry.kind !== 'lesson' || s.ids.includes(entry.id)) return
  s.ids.push(...ids)
  s.blocks += 1
  s.moved = true
  strokeIds.value = new Set(s.ids)
}

/** Click on a block in brush mode: a keyboard Enter / Space (or a
 *  click no pointer stroke handled) applies the brush to that block. */
export function brushClick(timetableId: string, ids: readonly string[]): void {
  if (swallowClick) { swallowClick = false; return }
  void commitBrush(timetableId, ids)
}

/** Screen-reader prefix for a block in brush mode. */
export function brushAriaPrefix(entry: TimetableEntry): string | null {
  const b = brushState.value?.active
  if (!b || entry.kind !== 'lesson') return null
  return brushLabel(b)
}
