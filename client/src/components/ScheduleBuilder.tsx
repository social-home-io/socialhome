/**
 * ScheduleBuilder — dialog for creating a schedule poll (§23.53).
 *
 * Opened from the Composer when post type = schedule. Captures a
 * title + one or more date/time slots. On submit it resolves with
 * ``{title, slots[]}`` so the caller can POST to
 * ``/api/posts/{id}/schedule-poll`` after the parent post is created.
 */
import { useState } from 'preact/hooks'
import { Button } from './Button'
import { Modal } from './Modal'

export interface SlotDraft {
  slot_date:   string
  start_time?: string
  end_time?:   string
}

export interface ScheduleDraft {
  title: string
  slots: SlotDraft[]
}

interface Props {
  open: boolean
  onSubmit: (draft: ScheduleDraft) => void
  onClose: () => void
}

export function ScheduleBuilder({ open, onSubmit, onClose }: Props) {
  const [title, setTitle] = useState('')
  const [slots, setSlots] = useState<SlotDraft[]>([
    { slot_date: '', start_time: '', end_time: '' },
  ])

  const addSlot = () => setSlots([...slots, { slot_date: '' }])
  const removeSlot = (i: number) =>
    setSlots(slots.filter((_, idx) => idx !== i))
  const updateSlot = (i: number, patch: Partial<SlotDraft>) =>
    setSlots(slots.map((s, idx) => idx === i ? { ...s, ...patch } : s))

  const canSubmit =
    title.trim().length > 0 &&
    slots.length > 0 &&
    slots.every(s => s.slot_date.trim().length > 0)

  const handleSubmit = (e: Event) => {
    e.preventDefault()
    if (!canSubmit) return
    onSubmit({
      title: title.trim(),
      slots: slots.map(s => ({
        slot_date:  s.slot_date,
        start_time: s.start_time || undefined,
        end_time:   s.end_time || undefined,
      })),
    })
  }

  return (
    <Modal open={open} onClose={onClose} title="📅 Propose meeting times">
      <form class="sh-form sh-schedule-builder" onSubmit={handleSubmit}>
        <label>
          Title
          <input type="text" value={title} maxLength={120}
                 onInput={(e) =>
                   setTitle((e.target as HTMLInputElement).value)}
                 placeholder="e.g. Pizza night"
                 autoFocus required />
        </label>

        <div>
          <strong style={{ fontSize: 'var(--sh-font-size-sm)' }}>Times</strong>
          <p class="sh-muted" style={{
            fontSize: 'var(--sh-font-size-xs)',
            margin: '0 0 var(--sh-space-sm)',
          }}>
            Add one row per possible time. Leave the time fields blank
            for all-day options.
          </p>
          {slots.map((s, i) => (
            <div key={i} class="sh-schedule-builder-row">
              <input type="date" aria-label={`Slot ${i + 1} date`}
                     value={s.slot_date} required
                     onInput={(e) => updateSlot(i, {
                       slot_date: (e.target as HTMLInputElement).value,
                     })} />
              <input type="time" aria-label={`Slot ${i + 1} start time`}
                     value={s.start_time} placeholder="Start"
                     onInput={(e) => updateSlot(i, {
                       start_time: (e.target as HTMLInputElement).value,
                     })} />
              <input type="time" aria-label={`Slot ${i + 1} end time`}
                     value={s.end_time} placeholder="End"
                     onInput={(e) => updateSlot(i, {
                       end_time: (e.target as HTMLInputElement).value,
                     })} />
              <button type="button" class="sh-poll-remove"
                      aria-label={`Remove slot ${i + 1}`}
                      disabled={slots.length === 1}
                      onClick={() => removeSlot(i)}>✕</button>
            </div>
          ))}
          <button type="button" class="sh-link" onClick={addSlot}>
            + Add another time
          </button>
        </div>

        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            Propose
          </Button>
        </div>
      </form>
    </Modal>
  )
}
