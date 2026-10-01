/**
 * StickyBoardPage — the sticky-note board (§19 / §23.24 / §23.61).
 *
 * One component for the household board (Organize → Stickies) and a
 * space board (``spaceId``); each reads its own per-scope store
 * (``store/stickies.ts``), so the two never mix.
 *
 * Wide screens: a free-position board. ``position_x`` / ``position_y``
 * are stored in a 1000 × 700 board space; a note is 18 % × 20 % of the
 * board (with a minimum size, kept inside the board by CSS ``min()``).
 * Moving is done by the note's grip only — the board itself never
 * captures touch, so swiping over it scrolls the page:
 *
 * - drag the grip (mouse, pen or touch; the PATCH is sent on release).
 *   A tap on the grip (no movement past the drag threshold) opens the
 *   editor like the body does;
 * - or focus the grip and use the arrow keys (10 units, Shift 50). One
 *   PATCH is sent after a short pause or when focus leaves; Escape
 *   puts the note back. Each step is announced (``aria-live``).
 *
 * Narrow boards (the board itself under ``CANVAS_MIN_PX``, measured
 * with a ResizeObserver; the < 640 px viewport query is the first
 * guess): a two-column grid in reading order (top-to-bottom in 10 %
 * row bands, then left-to-right). No moving — stored positions are left
 * untouched.
 *
 * Tapping / clicking a note's body opens the edit dialog.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { Button } from '@/components/Button'
import { ListSkeleton } from '@/components/Skeleton'
import { LoadErrorState } from '@/components/LoadErrorState'
import { showToast } from '@/components/Toast'
import {
  STICKY_COLORS,
  openCreateStickyDialog,
  openEditStickyDialog,
} from '@/components/StickyDialog'
import { stickyStoreFor, type StickyRow, type StickyStore } from '@/store/stickies'
import { isOne } from '@/store/tasks'
import { t } from '@/i18n/i18n'
import { useLoad } from '@/utils/useLoad'
import { OrganizeSectionHeader } from '@/features/organize/shared/OrganizeSectionHeader'
import { useNarrow } from '@/features/timetable/useNarrow'
import { inkClass, stickyBackground } from './ink'

/** Normalised board space (positions are stored in these units). */
export const BOARD_W = 1000
export const BOARD_H = 700
/** A note's nominal size in board units (18 % × 20 %). */
const NOTE_W = BOARD_W * 0.18
const NOTE_H = BOARD_H * 0.2

const STEP = 10
const STEP_BIG = 50
/** Idle time before a keyboard move is sent. */
const KEY_COMMIT_MS = 500
const LABEL_MAX = 60
/** Pointer travel (px) before a press on the grip becomes a drag. */
const MOUSE_SLOP_PX = 4
const TOUCH_SLOP_PX = 8
/** A touch held this long without moving is a long-press, not a tap. */
const LONG_PRESS_MS = 500
/** The click a browser sends after a handled pointer release arrives
 *  within this window; a later click is a fresh (keyboard) one. */
const CLICK_AFTER_POINTER_MS = 800

type DragEnd = 'commit' | 'revert'
/** Narrowest board (px) that shows the free-position canvas; notes
 *  are then at least ~150 px wide (see ``--sh-sticky-w``). Below it the
 *  board shows the reading-order grid. ``CANVAS_SLACK`` is hysteresis,
 *  so a scrollbar appearing with the switch can't flip it back. */
export const CANVAS_MIN_PX = 760
const CANVAS_SLACK = 16

export interface StickyBoardPageProps {
  spaceId?: string
}

/** First ~60 characters of a note, on one line, for labels. */
export function noteSnippet(content: string, max = LABEL_MAX): string {
  const one = content.trim().replace(/\s+/g, ' ')
  return one.length > max ? `${one.slice(0, max).trimEnd()}…` : one
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v))
}

/** How far a note may go, in board units, given its rendered size
 *  (its CSS minimum can make it larger than 18 % × 20 %). */
function bounds(note: HTMLElement | null, board: HTMLElement | null): { maxX: number; maxY: number } {
  const bw = board?.clientWidth ?? 0
  const bh = board?.clientHeight ?? 0
  const w = bw > 0 && note ? (note.offsetWidth / bw) * BOARD_W : NOTE_W
  const h = bh > 0 && note ? (note.offsetHeight / bh) * BOARD_H : NOTE_H
  return { maxX: Math.max(0, BOARD_W - w), maxY: Math.max(0, BOARD_H - h) }
}

/** Height of a reading-order row, in board units (10 % of the board):
 *  notes whose tops are this close read as one row, left to right. */
const ROW_BAND = BOARD_H * 0.1

/** Top-to-bottom (by row band), then left-to-right — the order the
 *  phone grid shows the board in. */
export function readingOrder(a: StickyRow, b: StickyRow): number {
  return Math.floor(a.position_y / ROW_BAND) - Math.floor(b.position_y / ROW_BAND)
    || a.position_x - b.position_x
    || a.position_y - b.position_y
}

/** ``sh-sticky`` plus the light-ink switch for a dark colour (the
 *  text, grip and focus ring follow the ink tokens it sets). */
function noteClass(color: string, dragging = false): string {
  return ['sh-sticky', dragging && 'sh-sticky--dragging', inkClass(color)].filter(Boolean).join(' ')
}

function pct(v: number, of: number): string {
  return `${(v / of) * 100}%`
}

function errText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

/** Is the board too narrow for the canvas? Measured from the board's
 *  own width (sidebar, space chrome and split views all shrink it);
 *  until the first measurement the viewport query is the guess, so a
 *  phone never flashes the canvas. */
function useBoardNarrow(ref: { current: HTMLElement | null }, guess: boolean): boolean {
  const [measured, setMeasured] = useState<boolean | null>(null)
  const guessRef = useRef(guess)
  guessRef.current = guess
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const width = entries[entries.length - 1]?.contentRect.width ?? 0
      if (width <= 0) return // hidden (e.g. an inactive tab)
      setMeasured((prev) => {
        const wasNarrow = prev ?? guessRef.current
        return wasNarrow ? width < CANVAS_MIN_PX + CANVAS_SLACK : width < CANVAS_MIN_PX
      })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return measured ?? guess
}

/** The top-bar title — only for the household board; a space board
 *  sits under the space's own title. */
function HouseholdTitle() {
  useTitle(t('stickies.title'))
  return null
}

export default function StickyBoardPage({ spaceId }: StickyBoardPageProps) {
  const scope = spaceId ?? null
  const store = stickyStoreFor(scope)
  const viewportNarrow = useNarrow()
  const boardRef = useRef<HTMLDivElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const narrow = useBoardNarrow(wrapRef, viewportNarrow)
  const [dragging, setDragging] = useState<string | null>(null)
  const [liveMsg, setLiveMsg] = useState('')
  // The same text twice is not re-read by screen readers: alternate a
  // zero-width space so a repeated "Already at the edge" is heard again.
  const announce = (msg: string) =>
    setLiveMsg(prev => (prev === msg ? `${msg}\u200B` : msg))

  // The store revalidates itself after a reconnect; the hook's own
  // reconnect run then shares that request (no ``force``) and also
  // recovers a board whose first load had failed.
  const { state, retry } = useLoad(() => store.load(), {
    cached: store.loaded.value,
    key: scope ?? '',
  })

  const notes = store.visible.value
  const n = notes.length

  const addSticky = () => {
    // Cycle the palette so a run of new notes isn't all yellow, and
    // spread them over the board instead of stacking.
    const color = STICKY_COLORS[n % STICKY_COLORS.length].hex
    const position_x = Math.round(40 + Math.random() * (BOARD_W - NOTE_W - 80))
    const position_y = Math.round(40 + Math.random() * (BOARD_H - NOTE_H - 80))
    openCreateStickyDialog(scope, { x: position_x, y: position_y }, color)
  }

  const header = (
    <OrganizeSectionHeader
      title={t('stickies.title')}
      hideTitle
      counts={state === 'ready' && n > 0
        ? [{ label: t(isOne(n) ? 'stickies.count_one' : 'stickies.count', { n: String(n) }) }]
        : []}
    >
      {state === 'ready' && n > 0 && (
        <Button data-sticky-add onClick={addSticky}>
          + {t('stickies.add')}
        </Button>
      )}
    </OrganizeSectionHeader>
  )

  let body
  if (state === 'error') {
    body = <LoadErrorState message={t('stickies.load_failed')} onRetry={retry} />
  } else if (state === 'loading') {
    body = <ListSkeleton variant="sticky" rows={6} label={t('stickies.loading')} />
  } else if (n === 0) {
    body = (
      <div class="sh-empty-state">
        <div aria-hidden="true">📝</div>
        <h3>{t('stickies.empty.title')}</h3>
        <p>{t(scope ? 'stickies.empty.body_space' : 'stickies.empty.body')}</p>
        <div class="sh-empty-state__cta-row">
          <Button data-sticky-add onClick={addSticky}>
            {t('stickies.empty.cta')}
          </Button>
        </div>
      </div>
    )
  } else if (narrow) {
    const ordered = [...notes].sort(readingOrder)
    body = (
      <div class="sh-sticky-grid">
        {ordered.map(s => (
          <div key={s.id} class={noteClass(s.color)} data-sticky-id={s.id}
               style={{ background: stickyBackground(s.color) }}>
            <NoteBody sticky={s} scope={scope} />
          </div>
        ))}
      </div>
    )
  } else {
    body = (
      <div
        ref={boardRef}
        class="sh-sticky-canvas"
        style={{ aspectRatio: `${BOARD_W} / ${BOARD_H}` }}
      >
        {notes.map(s => (
          <BoardNote
            key={s.id}
            sticky={s}
            scope={scope}
            store={store}
            boardRef={boardRef}
            dragging={dragging === s.id}
            setDragging={setDragging}
            announce={announce}
          />
        ))}
      </div>
    )
  }

  return (
    <div class="sh-sticky-board" ref={wrapRef}>
      {!scope && <HouseholdTitle />}
      {header}
      {body}
      <span id={`sh-sticky-hint-${scope ?? 'home'}`} class="sr-only">{t('stickies.move_hint')}</span>
      <div class="sr-only" aria-live="polite">{liveMsg}</div>
    </div>
  )
}

function NoteBody({ sticky, scope }: { sticky: StickyRow; scope: string | null }) {
  return (
    <button
      type="button"
      class="sh-sticky__body"
      aria-label={t('stickies.edit_label', { text: noteSnippet(sticky.content) })}
      onClick={() => openEditStickyDialog(sticky, scope)}
    >
      <span class="sh-sticky__text">{sticky.content}</span>
    </button>
  )
}

interface BoardNoteProps {
  sticky: StickyRow
  scope: string | null
  store: StickyStore
  boardRef: { current: HTMLDivElement | null }
  dragging: boolean
  setDragging: (id: string | null) => void
  announce: (msg: string) => void
}

function BoardNote({
  sticky, scope, store, boardRef, dragging, setDragging, announce,
}: BoardNoteProps) {
  const noteRef = useRef<HTMLDivElement | null>(null)
  /** Start position of a keyboard move not sent yet. */
  const pending = useRef<{ x: number; y: number } | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** The pointer press / drag in progress on the grip, if any. */
  const drag = useRef<{ pointerId: number; end: (how: DragEnd) => void } | null>(null)
  /** When a pointer press on the grip was last handled (on release) —
   *  the click the browser sends after it must not act again. */
  const pointerHandledAt = useRef(0)
  const id = sticky.id

  const clearTimer = () => {
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = null
  }

  const commit = (from: { x: number; y: number }) => {
    store.commitMove(id, from).catch((err: unknown) => {
      showToast(t('stickies.error.move', { error: errText(err) }), 'error')
    })
  }

  const flushKeyboardMove = () => {
    clearTimer()
    const from = pending.current
    if (!from) return
    pending.current = null
    store.releasePosition(id)
    commit(from)
  }

  // Unmount (leaving the board, or it turning into the grid): send a
  // pending keyboard move; cancel a drag in progress (revert, clean up).
  const unmountRef = useRef(() => {})
  unmountRef.current = () => {
    drag.current?.end('revert')
    flushKeyboardMove()
  }
  useEffect(() => () => unmountRef.current(), [])

  const onGripKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      if (!pending.current) return
      e.preventDefault()
      e.stopPropagation()
      clearTimer()
      store.moveLocal(id, pending.current.x, pending.current.y)
      pending.current = null
      store.releasePosition(id)
      announce(t('stickies.move_cancelled'))
      return
    }
    const step = e.shiftKey ? STEP_BIG : STEP
    let dx = 0
    let dy = 0
    switch (e.key) {
      case 'ArrowLeft': dx = -step; break
      case 'ArrowRight': dx = step; break
      case 'ArrowUp': dy = -step; break
      case 'ArrowDown': dy = step; break
      default: return
    }
    e.preventDefault()
    const cur = store.find(id)
    if (!cur || drag.current) return
    const { maxX, maxY } = bounds(noteRef.current, boardRef.current)
    // Start from where the note is DRAWN: CSS pulls a note stored past
    // the edge inwards, so its stored value may exceed the bound.
    const baseX = Math.min(cur.position_x, maxX)
    const baseY = Math.min(cur.position_y, maxY)
    const x = clamp(baseX + dx, 0, maxX)
    const y = clamp(baseY + dy, 0, maxY)
    if (x === baseX && y === baseY) {
      announce(t('stickies.at_edge'))
      return
    }
    if (!pending.current) {
      pending.current = { x: cur.position_x, y: cur.position_y }
      store.holdPosition(id)
    }
    store.moveLocal(id, x, y)
    announce(t('stickies.moved', {
      x: String(Math.round((x / BOARD_W) * 100)),
      y: String(Math.round((y / BOARD_H) * 100)),
    }))
    clearTimer()
    timer.current = setTimeout(flushKeyboardMove, KEY_COMMIT_MS)
  }

  /** Pointer on the grip. Nothing moves until the pointer travels past
   *  the drag threshold; then it's a drag (local moves, one PATCH on
   *  release). A release before that is a tap and opens the editor,
   *  like the body — unless it was a touch long-press.
   *
   *  The drag ends on pointerup (commit), pointercancel (revert — the
   *  browser took the gesture over), lostpointercapture or a mouse move
   *  with no button held (the release never reached us: commit), and
   *  on unmount (revert). Only the pointer that started it counts. */
  const onGripPointerDown = (e: PointerEvent) => {
    if (e.button !== 0 || drag.current) return
    const grip = e.currentTarget as HTMLElement
    const board = boardRef.current
    const note = noteRef.current
    if (!board || !note) return
    e.preventDefault()
    flushKeyboardMove()
    const pointerId = e.pointerId
    try { grip.setPointerCapture?.(pointerId) } catch { /* pointer already gone */ }
    const touch = e.pointerType !== 'mouse'
    const threshold = touch ? TOUCH_SLOP_PX : MOUSE_SLOP_PX
    const startX = e.clientX
    const startY = e.clientY
    const startedAt = Date.now()
    let dragged = false
    // Board origin and scale from its padding box (inside the border),
    // where the notes are positioned.
    const rect = board.getBoundingClientRect()
    const originX = rect.left + board.clientLeft
    const originY = rect.top + board.clientTop
    const scaleX = BOARD_W / (board.clientWidth || rect.width)
    const scaleY = BOARD_H / (board.clientHeight || rect.height)
    // Pointer offset from the note's RENDERED top-left (CSS may have
    // pulled a note at the edge inwards).
    const noteRect = note.getBoundingClientRect()
    const offX = e.clientX - noteRect.left
    const offY = e.clientY - noteRect.top
    const { maxX, maxY } = bounds(note, board)
    const from = { x: sticky.position_x, y: sticky.position_y }
    store.holdPosition(id)

    const mine = (ev: Event) => {
      const pid = (ev as PointerEvent).pointerId
      return pid === undefined || pid === pointerId
    }
    const onMove = (ev: PointerEvent) => {
      if (!mine(ev)) return
      if (ev.pointerType === 'mouse' && ev.buttons === 0) {
        end('commit')
        return
      }
      if (!dragged) {
        if (Math.hypot(ev.clientX - startX, ev.clientY - startY) < threshold) return
        dragged = true
        setDragging(id)
      }
      const x = clamp((ev.clientX - originX - offX) * scaleX, 0, maxX)
      const y = clamp((ev.clientY - originY - offY) * scaleY, 0, maxY)
      store.moveLocal(id, Math.round(x), Math.round(y))
    }
    const onUp = (ev: PointerEvent) => {
      if (!mine(ev)) return
      const wasDrag = dragged
      end('commit')
      pointerHandledAt.current = Date.now()
      const longPress = touch && Date.now() - startedAt >= LONG_PRESS_MS
      if (!wasDrag && !longPress) openEditStickyDialog(sticky, scope)
    }
    const onCancel = (ev: PointerEvent) => {
      if (mine(ev)) end('revert')
    }
    const onLost = (ev: Event) => {
      if (mine(ev)) end('commit')
    }
    const state = { pointerId, end: (how: DragEnd) => end(how) }
    function end(how: DragEnd) {
      if (drag.current !== state) return
      drag.current = null
      grip.removeEventListener('pointermove', onMove)
      grip.removeEventListener('pointerup', onUp)
      grip.removeEventListener('pointercancel', onCancel)
      grip.removeEventListener('lostpointercapture', onLost)
      try { grip.releasePointerCapture?.(pointerId) } catch { /* already released */ }
      store.releasePosition(id)
      if (!dragged) return
      setDragging(null)
      if (how === 'commit') commit(from)
      else store.moveLocal(id, from.x, from.y)
    }
    drag.current = state
    grip.addEventListener('pointermove', onMove)
    grip.addEventListener('pointerup', onUp)
    grip.addEventListener('pointercancel', onCancel)
    grip.addEventListener('lostpointercapture', onLost)
  }

  /** A click the pointer path already handled (tap → editor, or the
   *  end of a drag) is ignored; any other click — Enter / Space on the
   *  focused grip — opens the editor. */
  const onGripClick = () => {
    const handled = Date.now() - pointerHandledAt.current < CLICK_AFTER_POINTER_MS
    pointerHandledAt.current = 0
    if (!handled) openEditStickyDialog(sticky, scope)
  }

  return (
    <div
      ref={noteRef}
      class={noteClass(sticky.color, dragging)}
      data-sticky-id={id}
      style={{
        background: stickyBackground(sticky.color),
        '--sh-sticky-x': pct(sticky.position_x, BOARD_W),
        '--sh-sticky-y': pct(sticky.position_y, BOARD_H),
      }}
    >
      <button
        type="button"
        class="sh-sticky__grip"
        aria-label={t('stickies.move_label', { text: noteSnippet(sticky.content, 40) })}
        aria-describedby={`sh-sticky-hint-${scope ?? 'home'}`}
        onPointerDown={onGripPointerDown}
        onClick={onGripClick}
        onKeyDown={onGripKeyDown}
        onBlur={flushKeyboardMove}
      >
        <span aria-hidden="true">⠿</span>
      </button>
      <NoteBody sticky={sticky} scope={scope} />
    </div>
  )
}
