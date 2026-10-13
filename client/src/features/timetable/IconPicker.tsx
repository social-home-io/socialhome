/**
 * Timetable IconPicker — the emoji a slot shows (big, in Picture view)
 * so a child who can't read yet still finds "swimming".
 *
 * Thin wrapper over the shared :component:`IconPicker` with the
 * lesson + break presets and timetable copy.
 */
import { IconPicker as SharedIconPicker } from '@/components/IconPicker'
import { t } from '@/i18n/i18n'
import { BREAK_ICONS, LESSON_ICONS } from './icons'

interface Props {
  value: string | null
  onChange: (icon: string | null) => void
  /** Unique per page — keys the EmojiField popover. */
  id: string
}

export function IconPicker({ value, onChange, id }: Props) {
  return (
    <SharedIconPicker
      value={value} onChange={onChange} id={`tt-icon-${id}`}
      groups={[
        { label: t('timetable.icon.lessons'), presets: LESSON_ICONS },
        { label: t('timetable.icon.extras'), presets: BREAK_ICONS },
      ]}
      legend={t('timetable.icon.legend')}
      noneLabel={t('timetable.icon.none')}
      customLabel={t('timetable.icon.custom')}
      customHint={t('timetable.icon.custom_hint')}
    />
  )
}
