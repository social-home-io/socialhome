/**
 * Grid keyboard navigation for the Periods table and the Timeline — one
 * Tab stop for the whole grid (a roving ``tabindex`` over its blocks),
 * then:
 *
 *  * ↑ / ↓ — previous / next block of the same day;
 *  * ← / → — the block on the neighbouring day nearest in start time;
 *  * Home / End — first / last block of the day;
 *  * Enter / Space — the block's own click (open, or apply the brush);
 *  * Delete — remove a lesson (the store's Undo toast); breaks are
 *    removed only via the dialog;
 *  * Ctrl/⌘+C / Ctrl/⌘+V — copy a block, paste its subject onto the
 *    focused lesson.
 *
 * Items are the elements marked ``data-tt-nav`` with ``data-day`` (a
 * weekday, or ``all`` for a Periods break band spanning every day) and
 * ``data-start`` (minutes). The hook owns their ``tabindex`` and
 * re-normalises it after every render, so a re-rendered grid keeps its
 * single Tab stop on the block the user was last on.
 */
import { useLayoutEffect, useRef } from 'preact/hooks'

/** ``ids``: the focused block's entries — two or more for a merged
 *  double lesson (``data-run-ids``), which acts as one. */
export interface GridNavHandlers {
  onDelete?: (ids: string[], focusNext: (id: string | null) => void) => void
  onCopy?: (entryId: string) => void
  onPaste?: (ids: string[]) => void
}

interface Item {
  el: HTMLElement
  day: number | 'all'
  start: number
  key: string
}

const keyOf = (el: HTMLElement) =>
  el.dataset.entryId ?? `${el.dataset.day}:${el.dataset.start}:${el.dataset.kind ?? ''}`

function readItems(root: HTMLElement): Item[] {
  return Array.from(root.querySelectorAll<HTMLElement>('[data-tt-nav]')).map(el => ({
    el,
    day: el.dataset.day === 'all' ? 'all' : Number(el.dataset.day),
    start: Number(el.dataset.start),
    key: keyOf(el),
  }))
}

export function useGridNav(
  days: readonly number[],
  handlers: GridNavHandlers = {},
) {
  const ref = useRef<HTMLDivElement | null>(null)
  const active = useRef<string | null>(null)
  const lastDay = useRef<number | null>(null)
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers

  useLayoutEffect(() => {
    const root = ref.current
    if (!root) return
    const items = readItems(root)
    const current = items.find(i => i.key === active.current) ?? items[0]
    for (const i of items) i.el.tabIndex = i === current ? 0 : -1
  })

  const focusItem = (item: Item | undefined) => {
    if (!item) return
    const root = ref.current
    if (!root) return
    for (const i of readItems(root)) i.el.tabIndex = i.key === item.key ? 0 : -1
    active.current = item.key
    if (item.day !== 'all') lastDay.current = item.day
    item.el.focus()
  }

  const onFocusIn = (e: FocusEvent) => {
    const el = (e.target as HTMLElement).closest?.<HTMLElement>('[data-tt-nav]')
    if (!el || !ref.current?.contains(el)) return
    active.current = keyOf(el)
    if (el.dataset.day !== 'all') lastDay.current = Number(el.dataset.day)
    for (const i of readItems(ref.current)) i.el.tabIndex = i.el === el ? 0 : -1
  }

  const onKeyDown = (e: KeyboardEvent) => {
    const root = ref.current
    const el = (e.target as HTMLElement).closest?.<HTMLElement>('[data-tt-nav]')
    if (!root || !el || !root.contains(el)) return
    const items = readItems(root)
    const cur = items.find(i => i.el === el)
    if (!cur) return
    const day = cur.day === 'all' ? (lastDay.current ?? days[0]) : cur.day
    const ofDay = (d: number) => items
      .filter(i => i.day === 'all' || i.day === d)
      .sort((a, b) => a.start - b.start)
    const mod = e.ctrlKey || e.metaKey
    const entryId = el.dataset.entryId
    const ids = el.dataset.runIds ? el.dataset.runIds.split(' ') : entryId ? [entryId] : []

    if (mod && (e.key === 'c' || e.key === 'C')) {
      if (entryId && handlersRef.current.onCopy) {
        e.preventDefault()
        handlersRef.current.onCopy(entryId)
      }
      return
    }
    if (mod && (e.key === 'v' || e.key === 'V')) {
      if (entryId && el.dataset.kind === 'lesson' && handlersRef.current.onPaste) {
        e.preventDefault()
        handlersRef.current.onPaste(ids)
      }
      return
    }
    if (mod || e.altKey) return

    switch (e.key) {
      case 'ArrowUp':
      case 'ArrowDown': {
        const list = ofDay(day)
        const i = list.indexOf(cur)
        const next = list[i + (e.key === 'ArrowDown' ? 1 : -1)]
        e.preventDefault()
        // A band keeps the column we came from for the next ← / →.
        if (next) { focusItem(next); lastDay.current = day }
        return
      }
      case 'ArrowLeft':
      case 'ArrowRight': {
        const di = days.indexOf(day) + (e.key === 'ArrowRight' ? 1 : -1)
        e.preventDefault()
        if (di < 0 || di >= days.length) return
        const target = days[di]
        const list = ofDay(target)
        if (list.length === 0) return
        const nearest = list.reduce((best, i) =>
          Math.abs(i.start - cur.start) < Math.abs(best.start - cur.start) ? i : best)
        focusItem(nearest)
        lastDay.current = target
        return
      }
      case 'Home':
      case 'End': {
        const list = ofDay(day)
        e.preventDefault()
        focusItem(e.key === 'Home' ? list[0] : list[list.length - 1])
        return
      }
      case 'Delete': {
        if (!entryId || el.dataset.kind !== 'lesson' || !handlersRef.current.onDelete) return
        e.preventDefault()
        const list = ofDay(day)
        const i = list.indexOf(cur)
        const neighbour = list[i + 1] ?? list[i - 1]
        handlersRef.current.onDelete(ids, (id) => {
          const again = readItems(root)
          const land = again.find(x => x.key === (id ?? neighbour?.key)) ?? again[0]
          focusItem(land)
        })
        return
      }
    }
  }

  return { ref, onKeyDown, onFocusIn }
}
