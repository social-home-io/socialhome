/**
 * CopyDayDialog — "Copy Monday to other days" from a day heading's ⋯
 * menu (the day builder has the same targets inline). One
 * ``POST …/days/{wd}/copy``; non-empty target days ask first (the
 * store's orphan confirm) and one Undo puts every target back.
 */
import { signal } from '@preact/signals'
import { useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { t } from '@/i18n/i18n'
import type { Timetable } from '@/types'
import { CopyTargets } from './CopyTargets'
import { focusGrid } from './focus'
import { weekdayName } from './time'
import { useTimetableScope } from './scope'

interface CopyTarget {
  timetableId: string
  weekday: number
}

export const copyDayDialog = signal<CopyTarget | null>(null)

export function openCopyDay(target: CopyTarget): void {
  copyDayDialog.value = target
}

export function closeCopyDay(): void {
  copyDayDialog.value = null
}

export function CopyDayDialog() {
  const { store } = useTimetableScope()
  const target = copyDayDialog.value
  const tt = target ? store.timetables.value.find(x => x.id === target.timetableId) : undefined
  if (!target || !tt) return null
  return (
    <Modal open onClose={closeCopyDay}
           title={t('timetable.copy.title', { day: weekdayName(target.weekday, 'long') })}>
      <CopyForm key={`${tt.id}:${target.weekday}`} tt={tt} weekday={target.weekday} />
    </Modal>
  )
}

function CopyForm({ tt: live, weekday }: { tt: Timetable; weekday: number }) {
  const { store } = useTimetableScope()
  const [tt, setTt] = useState(live)
  const [targets, setTargets] = useState<number[]>([])
  const [withSubjects, setWithSubjects] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const idp = `sh-tt-copy-${tt.id}-${weekday}`

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (targets.length === 0) { setError(t('timetable.copy.pick')); return }
    setError(null)
    setSaving(true)
    try {
      const out = await store.copyDay(tt.id, weekday, targets, withSubjects, {
        baseVersion: tt.version,
        undo: {
          message: t('timetable.copy.done', {
            day: weekdayName(weekday, 'short'),
            days: targets.map(d => weekdayName(d, 'short')).join(', '),
          }),
          onUndone: () => focusGrid(tt.id),
        },
      })
      if (out) closeCopyDay()
      else setTt(store.timetables.value.find(x => x.id === tt.id) ?? tt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form class="sh-form sh-timetable-copyform" onSubmit={submit} noValidate>
      <CopyTargets tt={tt} source={weekday} value={targets} onChange={setTargets}
                   withSubjects={withSubjects} onWithSubjects={setWithSubjects}
                   legend={t('timetable.copy.legend')} id={idp} />
      <FormError id={`${idp}-err`} message={error} />
      <div class="sh-form-actions sh-timetable-entry__actions">
        <span class="sh-timetable-entry__spacer" />
        <Button type="button" variant="secondary" onClick={closeCopyDay}>{t('timetable.cancel')}</Button>
        <Button type="submit" loading={saving}>{t('timetable.copy.submit')}</Button>
      </div>
    </form>
  )
}
