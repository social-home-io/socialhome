/**
 * BrushBar — the palette of brush mode ("🖌 Fill"). A ``radiogroup`` of
 * the timetable's subjects (title + icon + colour, deduplicated), the
 * eraser ("Clear") and "+ New subject" (a small dialog: title, icon,
 * colour — it becomes the active brush). Arrow keys move the choice
 * (roving tabindex, as a radiogroup should); "Done" or Esc leaves brush
 * mode.
 */
import { useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableColor } from '@/types'
import {
  brushState, brushSubjects, extraSubjects, newSubjectOpen, pickBrush, sameBrush, stopBrush,
  type Brush, type PaintBrush,
} from './brush'
import { ColorSwatches } from './ColorSwatches'
import { colorClass, entryColor, normalizeSubject } from './colors'
import { IconPicker } from './IconPicker'
import { suggestIcon } from './icons'
import { useAutofocus } from './useAutofocus'


export function BrushBar({ tt, narrow }: { tt: Timetable; narrow: boolean }) {
  const active = brushState.value?.active ?? null
  const planned = brushSubjects(tt)
  const extra = (extraSubjects.value[tt.id] ?? [])
    .filter(x => !planned.some(p => sameBrush(p, x)))
  const choices: Brush[] = [...planned, ...extra, { kind: 'erase' }]
  const groupRef = useRef<HTMLDivElement | null>(null)
  const current = choices.findIndex(c => sameBrush(c, active))

  const onKey = (e: KeyboardEvent) => {
    const dir = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1
      : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
    if (!dir) return
    e.preventDefault()
    const i = ((current < 0 ? 0 : current) + dir + choices.length) % choices.length
    pickBrush(choices[i])
    setTimeout(() => groupRef.current
      ?.querySelectorAll<HTMLElement>('[role="radio"]')[i]?.focus(), 0)
  }

  const addSubject = (b: PaintBrush) => {
    const list = extraSubjects.value[tt.id] ?? []
    extraSubjects.value = { ...extraSubjects.value, [tt.id]: [...list, b] }
    pickBrush(b)
  }

  return (
    <div class="sh-timetable-brushbar" role="region" aria-label={t('timetable.brush.bar_aria')}>
      <p class="sh-timetable-brushbar__hint" aria-live="polite">
        {active
          ? t(narrow ? 'timetable.brush.hint_tap' : 'timetable.brush.hint_drag')
          : t('timetable.brush.hint_pick')}
      </p>
      <div class="sh-timetable-brushbar__row">
        <div class="sh-timetable-brushbar__scroll">
          <div ref={groupRef} class="sh-timetable-brushbar__chips" role="radiogroup"
               aria-label={t('timetable.brush.palette')} onKeyDown={onKey}>
            {choices.map((c, i) => {
              const on = sameBrush(c, active)
              const tabbable = current < 0 ? i === 0 : on
              if (c.kind === 'erase') {
                return (
                  <button key="erase" type="button" role="radio" aria-checked={on}
                          tabIndex={tabbable ? 0 : -1}
                          class={`sh-timetable-brush sh-timetable-brush--erase${on ? ' is-on' : ''}`}
                          onClick={() => pickBrush(c)}>
                    <span aria-hidden="true">🧽</span> {t('timetable.brush.erase')}
                  </button>
                )
              }
              const tone = entryColor({ color: c.color, title: c.title }, tt)
              return (
                <button key={normalizeSubject(c.title)} type="button" role="radio" aria-checked={on}
                        tabIndex={tabbable ? 0 : -1}
                        class={`sh-timetable-brush ${colorClass(tone)}${on ? ' is-on' : ''}`}
                        onClick={() => pickBrush(c)}>
                  {c.icon && <span aria-hidden="true">{c.icon}</span>}
                  <span>{c.title}</span>
                </button>
              )
            })}
          </div>
          <button type="button" class="sh-chip sh-timetable-chip sh-timetable-brushbar__new"
                  onClick={() => { newSubjectOpen.value = true }}>
            {t('timetable.brush.new_subject')}
          </button>
        </div>
        <Button variant="secondary" onClick={stopBrush}>{t('timetable.brush.done')}</Button>
      </div>
      {newSubjectOpen.value && (
        <Modal open onClose={() => { newSubjectOpen.value = false }} title={t('timetable.brush.new_title')}>
          <NewSubjectForm tt={tt} onDone={(b) => { newSubjectOpen.value = false; if (b) addSubject(b) }} />
        </Modal>
      )}
    </div>
  )
}

function NewSubjectForm({ tt, onDone }: { tt: Timetable; onDone: (b: PaintBrush | null) => void }) {
  const [title, setTitle] = useState('')
  const [icon, setIcon] = useState<string | null>(null)
  const [iconManual, setIconManual] = useState(false)
  const [color, setColor] = useState<TimetableColor | null>(null)
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLInputElement | null>(null)
  useAutofocus(ref, true)
  const idp = `sh-tt-subject-${tt.id}`
  const submit = (e: Event) => {
    e.preventDefault()
    const clean = title.trim()
    if (!clean) { setError(t('timetable.brush.title_required')); return }
    onDone({ kind: 'paint', title: clean, icon, color, room: null, teacher: null })
  }
  return (
    <form class="sh-form sh-timetable-entry" onSubmit={submit} noValidate>
      <div>
        <label for={`${idp}-title`}>{t('timetable.entry.title')}</label>
        <input ref={ref} id={`${idp}-title`} value={title} maxLength={60} autocomplete="off"
               placeholder={t('timetable.entry.title_placeholder')}
               onInput={(e) => {
                 const v = (e.target as HTMLInputElement).value
                 setTitle(v)
                 setError(null)
                 if (!iconManual) setIcon(suggestIcon(v))
               }} />
      </div>
      <IconPicker value={icon} onChange={(v) => { setIcon(v); setIconManual(true) }} id={idp} />
      <ColorSwatches name={`${idp}-color`} value={color} onChange={setColor}
                     noneLabel={t('timetable.color.auto')} />
      <FormError id={`${idp}-err`} message={error} />
      <div class="sh-form-actions sh-timetable-entry__actions">
        <span class="sh-timetable-entry__spacer" />
        <Button type="button" variant="secondary" onClick={() => onDone(null)}>{t('timetable.cancel')}</Button>
        <Button type="submit">{t('timetable.brush.use_subject')}</Button>
      </div>
    </form>
  )
}
