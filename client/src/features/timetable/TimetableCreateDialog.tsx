/**
 * TimetableCreateDialog — name, who it's for, a template, days, week
 * start. The name is prefilled from the first assignee ("Emma's
 * timetable") and pre-selected so typing replaces it; picking a
 * different first assignee re-derives it until the user edits it.
 */
import { signal } from '@preact/signals'
import { useRef, useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { RadioCardGroup } from '@/components/RadioCardGroup'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { householdUsers } from '@/store/householdUsers'
import { createTimetable } from '@/store/timetables'
import type { Timetable } from '@/types'
import { currentWeekStart, type WeekStart } from '@/utils/week'
import { AssigneePicker } from './AssigneePicker'
import { DaysPicker } from './DaysPicker'
import { WeekStartField } from './WeekStartField'
import { useAutofocus } from './useAutofocus'

type Template = 'school' | 'empty'

const openWith = signal<Template | null>(null)

export function openCreateDialog(template: Template = 'school'): void {
  openWith.value = template
}

export function closeCreateDialog(): void {
  openWith.value = null
}

/** "Emma's timetable" for the first assignee, "Timetable" without one. */
export function defaultName(assignees: readonly string[]): string {
  const id = assignees[0]
  if (!id) return t('timetable.create.default_name_plain')
  const u = householdUsers.value.get(id)
    ?? (currentUser.value?.user_id === id ? currentUser.value : undefined)
  const full = u?.display_name || u?.username || ''
  const first = full.trim().split(/\s+/)[0]
  return first ? t('timetable.create.default_name', { name: first }) : t('timetable.create.default_name_plain')
}

interface Props {
  onCreated: (tt: Timetable) => void
}

export function TimetableCreateDialog({ onCreated }: Props) {
  const template = openWith.value
  if (template === null) return null
  return (
    <Modal open onClose={closeCreateDialog} title={t('timetable.create.title')}>
      <CreateForm initialTemplate={template} onCreated={onCreated} />
    </Modal>
  )
}

function CreateForm({ initialTemplate, onCreated }: { initialTemplate: Template } & Props) {
  const me = currentUser.value?.user_id
  const [assignees, setAssignees] = useState<string[]>(me ? [me] : [])
  const [name, setName] = useState(() => defaultName(me ? [me] : []))
  const [nameEdited, setNameEdited] = useState(false)
  const [template, setTemplate] = useState<Template>(initialTemplate)
  const [days, setDays] = useState<number[]>([0, 1, 2, 3, 4])
  const [weekStart, setWeekStart] = useState<WeekStart>(currentWeekStart())
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const nameRef = useRef<HTMLInputElement | null>(null)
  // Touch devices skip the autofocus (keyboard stays down), so the
  // prefill is selected on the first tap instead: typing replaces it.
  const selectedOnce = useRef(false)
  useAutofocus(nameRef, true, { select: true })

  const onAssignees = (next: string[]) => {
    setAssignees(next)
    if (!nameEdited && next[0] !== assignees[0]) setName(defaultName(next))
  }

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (!name.trim()) {
      setError(t('timetable.name_required'))
      nameRef.current?.focus()
      return
    }
    setError(null)
    setSaving(true)
    try {
      const tt = await createTimetable({
        name: name.trim(), template, days, week_start: weekStart, assignees,
      })
      closeCreateDialog()
      onCreated(tt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form class="sh-form sh-timetable-form" onSubmit={submit} noValidate>
      <div>
        <label for="sh-tt-create-name">{t('timetable.create.name')}</label>
        <input
          ref={nameRef}
          id="sh-tt-create-name"
          value={name}
          maxLength={60}
          required
          aria-invalid={error === t('timetable.name_required') ? 'true' : undefined}
          aria-describedby="sh-tt-create-err"
          placeholder={t('timetable.create.name_placeholder')}
          onFocus={(e) => {
            if (selectedOnce.current || nameEdited) return
            selectedOnce.current = true
            ;(e.target as HTMLInputElement).select()
          }}
          onInput={(e) => {
            setName((e.target as HTMLInputElement).value)
            setNameEdited(true)
          }}
        />
      </div>
      <AssigneePicker value={assignees} onChange={onAssignees}
                      legend={t('timetable.create.assignees')}
                      hint={t('timetable.create.assignees_hint')} />
      <RadioCardGroup
        legend={t('timetable.create.template')}
        name="sh-tt-create-template"
        value={template}
        onChange={(v) => setTemplate(v as Template)}
        options={[
          { value: 'school', icon: '🏫', title: t('timetable.create.template_school'),
            subtitle: t('timetable.create.template_school_sub') },
          { value: 'empty', icon: '📄', title: t('timetable.create.template_empty'),
            subtitle: t('timetable.create.template_empty_sub') },
        ]}
      />
      <DaysPicker value={days} onChange={setDays} weekStart={weekStart} />
      <WeekStartField name="sh-tt-create-ws" value={weekStart} onChange={setWeekStart} />
      <FormError id="sh-tt-create-err" message={error} />
      <div class="sh-form-actions">
        <Button type="button" variant="secondary" onClick={closeCreateDialog}>
          {t('timetable.cancel')}
        </Button>
        <Button type="submit" loading={saving}>{t('timetable.create.submit')}</Button>
      </div>
    </form>
  )
}
