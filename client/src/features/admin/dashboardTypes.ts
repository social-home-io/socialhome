/**
 * Shapes shared by the Admin › Dashboard tab and its step components
 * (``DashboardTab.tsx``, ``DashboardTabletStep.tsx``) — the
 * ``GET /api/admin/dashboard`` answer and the settings-save callback.
 */


/** One household member as ``GET /api/admin/dashboard`` lists them. */
export interface DashboardPerson {
  user_id: string
  username: string
  display_name: string
  picture_url: string | null
  show: boolean
  /** Stored choice; ``null`` = the default (on for child-protected minors). */
  picture: boolean | null
  picture_effective: boolean
}

/** ``GET /api/admin/dashboard``. */
export interface DashboardState {
  enabled: boolean
  running: boolean
  running_port: number | null
  port: number
  port_fixed: boolean
  password_set: boolean
  /** ISO timestamp of the last password change (``null``: never set). */
  password_set_at: string | null
  pin_set: boolean
  address: string | null
  url: string
  url_host_allowed: boolean
  /** Home Assistant App only: ``false`` = the App offers the dashboard
   *  port but no host port is mapped yet; ``null`` = not applicable. */
  host_port_mapped?: boolean | null
  locale: string
  idle_return_minutes: number
  night_start: string
  night_end: string
  keep_screen_on: boolean
  show_status_text: boolean
  /** Home shows the last few household-chat messages (admin opt-in). */
  show_household_messages: boolean
  /** ``feat_household_chat`` is on — the messages switch needs it. */
  household_chat_enabled: boolean
  last_seen_at: string | null
  people: DashboardPerson[]
}

export type Patch = Partial<Omit<DashboardState, 'people'>> & {
  people?: Record<string, { show: boolean, picture: boolean | null }>
}

export interface SaveOpts {
  /** ``false``: report the error to the caller (shown inline) instead. */
  toast?: boolean
  /** ``false``: wait for the server — for changes that move the listener
   *  (on/off, port) or the URL, where a guess would mislead. */
  optimistic?: boolean
}
export type SaveFn = (patch: Patch, opts?: SaveOpts) => Promise<Error | null>
