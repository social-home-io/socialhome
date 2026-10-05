/**
 * Tiny "..." overflow menu for calendar event affordances.
 *
 * Used by :class:`EventPostCard` and the calendar event-detail row to
 * tuck low-frequency actions (currently just "Add to my calendar"
 * via the .ics export) behind a kebab so they don't crowd the
 * primary RSVP / Edit / Delete affordances.
 *
 * Reuses the ``.sh-post-overflow`` + ``.sh-post-menu`` CSS family —
 * same visual + a11y pattern PostCard uses for its menu, no extra
 * stylesheet surface required.
 */
import { useState } from 'preact/hooks'
import type { ComponentChildren } from 'preact'
import { t } from '@/i18n/i18n'

export interface EventOverflowMenuProps {
  /** Calendar event id — used to build the .ics download URL. */
  eventId: string
  /** Optional extra menu items rendered above the export item. The
   *  parent passes JSX (typically `<button role="menuitem">`); the
   *  menu's open/close lifecycle stays opaque to the parent. */
  children?: ComponentChildren
  /** ARIA label on the trigger button. Defaults to "Event actions". */
  label?: string
  /** When ``false`` the built-in "Add to my calendar" item is
   *  suppressed — the parent surfaces the .ics affordance somewhere
   *  more discoverable (a sibling button on the RSVP row, for
   *  example). Defaults to ``true`` so existing call sites
   *  (CalendarPage detail row) keep their behaviour. */
  showIcsItem?: boolean
}

export function EventOverflowMenu({
  eventId,
  children,
  label,
  showIcsItem = true,
}: EventOverflowMenuProps) {
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)
  return (
    <div class="sh-post-overflow-wrap">
      <button
        type="button"
        class="sh-post-overflow"
        aria-label={label ?? t('event.actions')}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        // 100ms blur delay matches PostCard so a click on a menuitem
        // (which steals focus) still fires before the menu closes.
        onBlur={() => setTimeout(close, 100)}
      >
        ···
      </button>
      {open && (
        <div class="sh-post-menu" role="menu">
          {children}
          {showIcsItem && (
            <a
              role="menuitem"
              // Relative URL (no leading slash) so the browser resolves
              // it against ``<base href>``. ``download`` makes the
              // browser fetch the URL directly (not the SPA router), so
              // an absolute ``/api/...`` would skip the HA Supervisor
              // ingress prefix and 404. Same class as #303.
              href={`api/calendars/events/${eventId}/export.ics`}
              download
              onMouseDown={(e) => e.preventDefault()}
              onClick={close}
            >
              {t('event.add_to_calendar')}
            </a>
          )}
        </div>
      )}
    </div>
  )
}
