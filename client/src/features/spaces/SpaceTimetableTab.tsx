/**
 * SpaceTimetableTab — a space's shared timetables (a class schedule, a
 * training plan), inside the space Calendar tab.
 *
 * The household timetable UI, not a copy: ``TimetableBoard`` under a
 * space scope — the store of ``/api/spaces/{id}/timetables`` and
 * ``editable`` = the viewer is the space's owner / admin. Everyone else
 * reads: the grid, the day view, Picture / List, This week and Print,
 * with every editing affordance gone and lessons opening a read-only
 * details dialog. The selection and week mode are local to the tab
 * (the space page has no URL state per tab).
 *
 * Everyone who can see a timetable may put it on their own Home: the
 * "Show on my Home" header toggle writes the ``timetable_home_pins``
 * preference, which feeds today's lessons into the Today card.
 */
import { useCallback, useMemo, useState } from 'preact/hooks'
import { toggles } from '@/components/HouseholdToggles'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { spaceTimetableStore } from '@/store/timetables'
import { TimetableBoard } from '@/features/timetable/TimetableBoard'
import { TimetableScopeContext, type TimetableScope } from '@/features/timetable/scope'
import type { Timetable } from '@/types'
import { getPreferences, setPreference } from '@/utils/preferences'

/** What the server reads of ``timetable_home_pins`` (``MAX_TIMETABLE_HOME_PINS``). */
export const MAX_HOME_PINS = 20

interface Props {
  spaceId: string
  /** The viewer is the space's owner / admin. */
  canEdit: boolean
  /** Injected in tests; defaults to now. */
  now?: Date
}

export function SpaceTimetableTab({ spaceId, canEdit, now }: Props) {
  const store = spaceTimetableStore(spaceId)
  const scope = useMemo<TimetableScope>(
    () => ({ store, editable: canEdit, assignees: false }),
    [store, canEdit],
  )
  const [week, setWeek] = useState<string | null>(null)
  const select = useCallback((id: string | null) => { store.selectedId.value = id }, [store])

  return (
    <TimetableScopeContext.Provider value={scope}>
      <div class="sh-space-timetable">
        <TimetableBoard
          select={select}
          week={week}
          onWeek={(_id, date) => setWeek(date)}
          emptyTitle={t(canEdit ? 'timetable.space.empty_admin_title' : 'timetable.space.empty_member_title')}
          emptyBody={t(canEdit ? 'timetable.space.empty_admin_body' : 'timetable.space.empty_member_body')}
          headerExtra={(tt) => <HomePinToggle tt={tt} />}
          headerCaption={canEdit ? undefined : t('timetable.space.read_only')}
          now={now}
        />
      </div>
    </TimetableScopeContext.Provider>
  )
}

function pinsNow(): string[] {
  const raw = getPreferences().timetable_home_pins
  return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : []
}

/** "Show on my Home" — a header toggle beside Pictures / List: 🏠 plus
 *  "Home" on a phone, the full label otherwise. */
function HomePinToggle({ tt }: { tt: Timetable }) {
  // ``getPreferences`` reads ``currentUser`` (a signal) — a save
  // re-renders this toggle with the new state.
  const pins = pinsNow()
  const pinned = pins.includes(tt.id)
  const [busy, setBusy] = useState(false)

  const toggle = async () => {
    const next = pinned ? pins.filter(id => id !== tt.id) : [...pins, tt.id]
    if (!pinned && pins.length >= MAX_HOME_PINS) {
      showToast(t('timetable.space.pin_limit', { n: String(MAX_HOME_PINS) }), 'error')
      return
    }
    setBusy(true)
    try {
      await setPreference('timetable_home_pins', next)
      if (pinned) {
        showToast(t('timetable.space.unpinned'), 'success')
      } else if (toggles.value && !toggles.value.feat_timetable) {
        // The pin is kept — it starts showing once the household turns
        // timetables back on — but say why Home stays empty for now.
        showToast(t('timetable.space.pinned_household_off'), 'info')
      } else {
        showToast(t('timetable.space.pinned'), 'success')
      }
    } catch (e) {
      showToast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const label = t('timetable.space.pin')
  return (
    <button
      type="button"
      class={`sh-timetable-toggle sh-space-timetable__pin${pinned ? ' is-on' : ''}`}
      aria-pressed={pinned}
      aria-busy={busy ? 'true' : undefined}
      aria-label={label}
      disabled={busy}
      title={t('timetable.space.pin_hint')}
      onClick={() => void toggle()}
    >
      <span class="sh-space-timetable__pinicon" aria-hidden="true">🏠</span>
      <span class="sh-timetable-toggle__text" aria-hidden="true">{label}</span>
      <span class="sh-timetable-toggle__short" aria-hidden="true">{t('timetable.space.pin_short')}</span>
    </button>
  )
}
