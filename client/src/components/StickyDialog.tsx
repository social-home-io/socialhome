/**
 * StickyDialog — create / edit dialog for sticky notes (§19).
 *
 * One global instance at the App root, driven by signals:
 * ``openCreateStickyDialog(spaceId, position, colour)`` and
 * ``openEditStickyDialog(sticky, spaceId)``. Every write goes through
 * the scope's store (``stickyStoreFor``), so a space note never lands
 * on the household board.
 *
 * Form surface:
 *   • Note — multiline textarea (1–500 chars), tinted with the chosen
 *     colour so it previews the saved note.
 *   • Colour — a radiogroup of named colours (``ChipRadioGroup`` with a
 *     swatch dot). Stored values stay hex; a hex outside the palette
 *     (e.g. from an older client) shows as "Custom".
 *   • Delete (edit mode) — closes the dialog at once and offers Undo;
 *     the DELETE is sent when the toast expires.
 *
 * Position is the board's job: new notes get a spread-out slot from
 * ``StickyBoardPage``; edits leave position untouched.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { ChipRadioGroup, type ChipRadioOption } from './ChipRadioGroup'
import { stickyStoreFor, type StickyRow } from '@/store/stickies'
import { isOne } from '@/store/tasks'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { openReport } from './ReportDialog'
import { inkClass, stickyBackground } from '@/features/stickies/ink'

export interface StickyColor {
  hex: string
  /** ``stickies.color.<key>`` names it. */
  key: 'yellow' | 'coral' | 'mint' | 'sky' | 'lilac' | 'peach'
}

/** The palette the board cycles through on create; the dialog lets the
 *  user pick another. Hex values are stored as-is (user data). */
export const STICKY_COLORS: readonly StickyColor[] = [
  { hex: '#FFF9B1', key: 'yellow' },
  { hex: '#FFB3B3', key: 'coral' },
  { hex: '#B3FFB3', key: 'mint' },
  { hex: '#B3D4FF', key: 'sky' },
  { hex: '#E8B3FF', key: 'lilac' },
  { hex: '#FFD4B3', key: 'peach' },
] as const

/** The palette entry for a stored hex (case-insensitive), if any. */
export function paletteColor(hex: string): StickyColor | undefined {
  const h = hex.toUpperCase()
  return STICKY_COLORS.find(c => c.hex === h)
}

const CONTENT_MAX = 500

const open = signal(false)
const editingId = signal<string | null>(null)
const scopeSpaceId = signal<string | null>(null)
/** Who wrote the note being edited (a space note by someone else can be
 *  reported to the space's moderators). */
const editingAuthor = signal<string | null>(null)
/** Where a new note lands — the board spreads new notes out. */
const newPosition = signal<{ x: number; y: number }>({ x: 0, y: 0 })

const content = signal('')
const color = signal<string>(STICKY_COLORS[0].hex)
/** The colour the dialog opened with. A hex outside the palette stays
 *  on offer as "Custom" even after the user tried a palette colour. */
const originalColor = signal<string>(STICKY_COLORS[0].hex)
const submitting = signal(false)

function reset(): void {
  editingId.value = null
  editingAuthor.value = null
  content.value = ''
  color.value = STICKY_COLORS[0].hex
  originalColor.value = STICKY_COLORS[0].hex
  submitting.value = false
}

/** Open the dialog in create mode. ``spaceId`` is ``null`` for the
 *  household board, the space id for a space board. */
export function openCreateStickyDialog(
  spaceId: string | null,
  position?: { x: number; y: number },
  defaultColor?: string,
): void {
  reset()
  scopeSpaceId.value = spaceId
  newPosition.value = position ?? { x: 0, y: 0 }
  const picked = defaultColor ? paletteColor(defaultColor) : undefined
  if (picked) color.value = picked.hex
  originalColor.value = color.value
  open.value = true
}

/** Open the dialog in edit mode for an existing note. */
export function openEditStickyDialog(sticky: StickyRow, spaceId: string | null): void {
  reset()
  editingId.value = sticky.id
  editingAuthor.value = sticky.author
  scopeSpaceId.value = spaceId
  content.value = sticky.content
  color.value = sticky.color
  originalColor.value = sticky.color
  open.value = true
}

function errText(err: unknown): string {
  return String((err as Error)?.message ?? err)
}

/** Focus the board's Add button once the dialog (and the deleted
 *  note it would hand focus back to) is gone. */
function focusBoardAdd(): void {
  requestAnimationFrame(() => {
    document.querySelector<HTMLElement>('[data-sticky-add]')?.focus()
  })
}

function focusSticky(id: string): void {
  requestAnimationFrame(() => {
    const note = [...document.querySelectorAll<HTMLElement>('[data-sticky-id]')]
      .find(el => el.dataset.stickyId === id)
    note?.querySelector<HTMLElement>('.sh-sticky__body')?.focus()
  })
}

export function StickyDialog() {
  // Autofocus the textarea on open; in edit mode select its text so a
  // quick overwrite just works.
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
    const store = stickyStoreFor(scopeSpaceId.value)
    const sid = editingId.value
    try {
      if (sid) {
        await store.patch(sid, { content: trimmed, color: color.value })
      } else {
        await store.create({
          content: trimmed,
          color: color.value,
          position_x: newPosition.value.x,
          position_y: newPosition.value.y,
        })
      }
      open.value = false
    } catch (err: unknown) {
      showToast(
        t(sid ? 'stickies.error.save' : 'stickies.error.add', { error: errText(err) }),
        'error',
      )
    } finally {
      submitting.value = false
    }
  }

  const handleDelete = () => {
    const sid = editingId.value
    if (!sid) return
    const store = stickyStoreFor(scopeSpaceId.value)
    const sticky = store.find(sid)
    open.value = false
    if (!sticky) return
    store.remove(sticky, { onUndone: () => focusSticky(sid) })
    focusBoardAdd()
  }

  if (!open.value) return null

  const isEdit = !!editingId.value
  const remaining = CONTENT_MAX - content.value.length
  const custom = paletteColor(originalColor.value) ? null : originalColor.value
  const options: ChipRadioOption<string>[] = STICKY_COLORS.map(c => ({
    value: c.hex,
    // Keys for i18n:check: t('stickies.color.yellow') t('stickies.color.coral')
    // t('stickies.color.mint') t('stickies.color.sky') t('stickies.color.lilac')
    // t('stickies.color.peach')
    label: t(`stickies.color.${c.key}`),
    swatch: c.hex,
  }))
  if (custom) {
    options.push({ value: custom, label: t('stickies.color.custom'), swatch: stickyBackground(custom) })
  }
  const checked = paletteColor(color.value)?.hex ?? color.value

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title={t(isEdit ? 'stickies.dialog.edit' : 'stickies.dialog.new')}
    >
      <form class="sh-form sh-sticky-dialog" onSubmit={submit}>
        <label>
          {t('stickies.dialog.note')}
          <textarea
            ref={textareaRef}
            class={['sh-sticky-dialog-textarea', inkClass(color.value)].filter(Boolean).join(' ')}
            value={content.value}
            maxLength={CONTENT_MAX}
            rows={5}
            style={{ background: stickyBackground(color.value) }}
            placeholder={t('stickies.dialog.placeholder')}
            onInput={(e) => {
              content.value = (e.target as HTMLTextAreaElement).value
            }}
            onKeyDown={(e) => {
              // Cmd/Ctrl-Enter submits; plain Enter is a newline.
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault()
                void submit(new Event('submit'))
              }
            }}
            required
          />
          <span class="sh-sticky-dialog-counter">
            {t(isOne(remaining) ? 'stickies.dialog.remaining_one' : 'stickies.dialog.remaining',
              { n: String(remaining) })}
          </span>
        </label>

        <div class="sh-sticky-dialog-colors">
          <span class="sh-sticky-dialog-colors__label" id="sh-sticky-colour-label">
            {t('stickies.dialog.colour')}
          </span>
          <ChipRadioGroup<string>
            labelledBy="sh-sticky-colour-label"
            options={options}
            value={checked}
            onChange={(v) => { color.value = v }}
            class="sh-sticky-dialog-swatches"
          />
        </div>

        <div class="sh-form-actions sh-sticky-dialog-actions">
          {isEdit && (
            <Button
              variant="danger"
              type="button"
              onClick={handleDelete}
              disabled={submitting.value}
            >
              {t('common.delete')}
            </Button>
          )}
          {isEdit && scopeSpaceId.value && editingAuthor.value
            && editingAuthor.value !== currentUser.value?.user_id && (
            <Button
              variant="ghost"
              type="button"
              onClick={() => {
                const id = editingId.value
                const sid = scopeSpaceId.value
                open.value = false
                if (id) openReport('sticky', id, sid)
              }}
            >
              {t('report.action')}
            </Button>
          )}
          <span class="sh-sticky-dialog-actions__spacer" />
          <Button
            variant="secondary"
            type="button"
            onClick={() => { open.value = false }}
            disabled={submitting.value}
          >
            {t('common.cancel')}
          </Button>
          <Button
            type="submit"
            loading={submitting.value}
            disabled={!content.value.trim()}
          >
            {t(isEdit ? 'common.save' : 'stickies.add')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
