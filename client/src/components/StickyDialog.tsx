/**
 * StickyDialog — focused create / edit dialog for sticky notes (§19).
 *
 * Replaces the inline ``prompt()`` / ``await confirmDialog()`` flow on
 * ``StickyBoardPage``. Mirrors the pattern of
 * :class:`CalendarEventDialog`: a single global signal drives open
 * state; ``openCreateStickyDialog(spaceId?)`` and
 * ``openEditStickyDialog(sticky, spaceId?)`` flip it; the dialog
 * lives once at the App root next to the other global dialogs.
 *
 * Form surface:
 *   • Content — multiline textarea (stickies are 1–500 chars; the
 *     server caps but the UI also enforces).
 *   • Colour — 6-swatch picker matching the hardcoded palette in
 *     ``StickyBoardPage``. Click a swatch to pick; the active one
 *     gets a terracotta ring.
 *   • Delete button (edit mode only) — destructive secondary, with
 *     a one-step confirmation via ``await confirmDialog()``.
 *
 * Position is left to the board: new stickies pick a randomised
 * mid-board slot in :func:`StickyBoardPage`'s open call (so a flood
 * of new notes doesn't stack); existing stickies keep their drag-set
 * coordinates untouched on edit.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { stickies, trackHouseholdSticky, type StickyRow } from '@/store/stickies'
import { confirmDialog } from '@/components/confirm'

/** Sticky-note swatch palette — same six colours the board cycles
 *  through on quick-create, exposed here so users can override the
 *  auto-pick. */
export const STICKY_COLORS = [
  '#FFF9B1', // soft yellow (default)
  '#FFB3B3', // coral
  '#B3FFB3', // mint
  '#B3D4FF', // sky
  '#E8B3FF', // lilac
  '#FFD4B3', // peach
] as const

const CONTENT_MAX = 500

const open = signal(false)
const editingId = signal<string | null>(null)
const scopeSpaceId = signal<string | null>(null)
/** Position the new sticky should land at — set by the caller so the
 *  caller can spread successive new stickies across the board rather
 *  than stacking them. Edit mode reads the existing values from the
 *  store and leaves them untouched. */
const newPosition = signal<{ x: number; y: number }>({ x: 0, y: 0 })

const content = signal('')
const color = signal<string>(STICKY_COLORS[0])
const submitting = signal(false)

function reset(): void {
  editingId.value = null
  content.value = ''
  color.value = STICKY_COLORS[0]
  submitting.value = false
}

/** Open the dialog in create mode. ``spaceId`` is ``null`` for the
 *  household board, the space id for the per-space board. */
export function openCreateStickyDialog(
  spaceId: string | null,
  position?: { x: number; y: number },
  defaultColor?: string,
): void {
  reset()
  scopeSpaceId.value = spaceId
  newPosition.value = position ?? { x: 0, y: 0 }
  if (defaultColor && (STICKY_COLORS as readonly string[]).includes(defaultColor)) {
    color.value = defaultColor
  }
  open.value = true
}

/** Open the dialog in edit mode for an existing sticky. Pre-fills
 *  content + colour; submit PATCHes those fields, leaves position
 *  alone. */
export function openEditStickyDialog(
  sticky: StickyRow,
  spaceId: string | null,
): void {
  reset()
  editingId.value = sticky.id
  scopeSpaceId.value = spaceId
  content.value = sticky.content
  color.value = sticky.color
  open.value = true
}

function endpointBase(spaceId: string | null): string {
  return spaceId ? `/api/spaces/${spaceId}/stickies` : '/api/stickies'
}

export function StickyDialog() {
  // Autofocus the textarea on open so the user can just start typing.
  // We also pre-select the existing text in edit mode so a quick
  // overwrite "just works" without manually selecting first.
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => {
    if (!open.value) return
    requestAnimationFrame(() => {
      const el = textareaRef.current
      if (!el) return
      el.focus()
      if (editingId.value) el.select()
    })
  }, [open.value, editingId.value])

  const submit = async (e: Event) => {
    e.preventDefault()
    const trimmed = content.value.trim()
    if (!trimmed || submitting.value) return
    submitting.value = true
    try {
      const sid = editingId.value
      const base = endpointBase(scopeSpaceId.value)
      if (sid) {
        const updated = await api.patch(`${base}/${sid}`, {
          content: trimmed,
          color: color.value,
        }) as StickyRow
        // Optimistic merge — WS will follow up but we want immediate
        // feedback so the closing animation reads as "saved", not
        // "queued".
        stickies.value = stickies.value.map(s =>
          s.id === sid ? { ...s, ...updated } : s,
        )
        showToast('Sticky updated', 'success')
      } else {
        const row = await api.post(base, {
          content: trimmed,
          color: color.value,
          position_x: newPosition.value.x,
          position_y: newPosition.value.y,
        }) as StickyRow
        if (!stickies.value.some(s => s.id === row.id)) {
          stickies.value = [...stickies.value, row]
        }
        if (scopeSpaceId.value === null) trackHouseholdSticky(row.id, true)
        showToast('Sticky added', 'success')
      }
      open.value = false
    } catch (err: unknown) {
      showToast(
        `${editingId.value ? 'Update' : 'Add'} failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    } finally {
      submitting.value = false
    }
  }

  const handleDelete = async () => {
    const sid = editingId.value
    if (!sid) return
    if (!await confirmDialog('Delete this sticky?', { destructive: true })) return
    submitting.value = true
    try {
      await api.delete(`${endpointBase(scopeSpaceId.value)}/${sid}`)
      stickies.value = stickies.value.filter(s => s.id !== sid)
      if (scopeSpaceId.value === null) trackHouseholdSticky(sid, false)
      open.value = false
      showToast('Sticky deleted', 'info')
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      submitting.value = false
    }
  }

  if (!open.value) return null

  const isEdit = !!editingId.value
  const remaining = CONTENT_MAX - content.value.length

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title={isEdit ? 'Edit sticky' : 'New sticky'}
    >
      <form class="sh-form sh-sticky-dialog" onSubmit={submit}>
        <label>
          Note
          <textarea
            ref={textareaRef}
            class="sh-sticky-dialog-textarea"
            value={content.value}
            maxLength={CONTENT_MAX}
            rows={5}
            style={{ background: color.value }}
            placeholder="What do you want to pin?"
            onInput={(e) => {
              content.value = (e.target as HTMLTextAreaElement).value
            }}
            onKeyDown={(e) => {
              // Cmd/Ctrl-Enter submits; plain Enter inserts a newline
              // — sticky notes are short enough that "newline by
              // default" beats "submit by default".
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault()
                void submit(new Event('submit'))
              }
            }}
            required
          />
          <span class="sh-sticky-dialog-counter sh-muted">
            {remaining} characters left
          </span>
        </label>

        <fieldset class="sh-sticky-dialog-colors" aria-label="Sticky colour">
          <legend class="sh-muted">Colour</legend>
          <div class="sh-sticky-dialog-swatches" role="radiogroup">
            {STICKY_COLORS.map(c => (
              <button
                key={c}
                type="button"
                role="radio"
                aria-checked={color.value === c}
                aria-label={`Use ${c}`}
                class={
                  color.value === c
                    ? 'sh-sticky-dialog-swatch sh-sticky-dialog-swatch--active'
                    : 'sh-sticky-dialog-swatch'
                }
                style={{ background: c }}
                onClick={() => { color.value = c }}
              />
            ))}
          </div>
        </fieldset>

        <div class="sh-form-actions sh-sticky-dialog-actions">
          {isEdit && (
            <Button
              variant="danger"
              type="button"
              onClick={() => void handleDelete()}
              disabled={submitting.value}
            >
              Delete
            </Button>
          )}
          <span style={{ flex: 1 }} />
          <Button
            variant="secondary"
            type="button"
            onClick={() => { open.value = false }}
            disabled={submitting.value}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            loading={submitting.value}
            disabled={!content.value.trim()}
          >
            {isEdit ? 'Save' : 'Add'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
