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

/** A paired wall panel (``GET /api/admin/dashboard/displays``). Its
 *  token never reaches the admin UI. */
export interface DashboardDisplay {
  id: string
  name: string
  /** What the panel reports about itself (``null``: never said). */
  model: string | null
  firmware: string | null
  /** ISO timestamps (UTC). */
  created_at: string
  last_seen_at: string | null
  locked: boolean
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
  /** A short on-device tone for a new @here / @name mention (default on;
   *  only matters while ``show_household_messages`` is). */
  mention_sound: boolean
  /** A Home Assistant automation may lift a wall panel's privacy lock
   *  without the PIN (admin opt-in, default off). */
  panel_unlock_from_ha: boolean
  /** ``feat_household_chat`` is on — the messages switch needs it. */
  household_chat_enabled: boolean
  last_seen_at: string | null
  people: DashboardPerson[]
  /** Paired wall panels, oldest first. */
  displays: DashboardDisplay[]
  /** Wall panels adopted in Home Assistant can be listed and connected
   *  (``Capability.HA_DISPLAY_DISCOVERY`` — ha / Home Assistant App). */
  ha_display_discovery?: boolean
}

/** A wall panel found in Home Assistant
 *  (``GET /api/admin/dashboard/displays/ha``). */
export interface HaPanel {
  /** Home Assistant's device id. */
  id: string
  /** Its name in Home Assistant. */
  name: string
  /** ``"<firmware> (ESPHome <version>)"``. */
  sw_version: string | null
  /** ``connected`` — paired here; ``ready`` — Connect works;
   *  ``offline`` — Home Assistant lost it; ``needs_update`` — its
   *  firmware can't be connected from here. */
  status: 'connected' | 'ready' | 'offline' | 'needs_update'
  can_connect: boolean
  display_id: string | null
  display_name: string | null
}

export type Patch = Partial<Omit<DashboardState, 'people' | 'displays' | 'ha_display_discovery'>> & {
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
