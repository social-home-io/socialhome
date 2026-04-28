export interface User {
  user_id: string
  username: string
  display_name: string
  is_admin: boolean
  /** Server-built URL like ``/api/users/{id}/picture?v=<hash>`` or null. */
  picture_url: string | null
  /** Short hex digest of the picture bytes; stable identity the frontend
   *  can compare to detect changes without refetching the image. */
  picture_hash: string | null
  bio: string | null
  /** Provisioning source — `'ha'` gates the "Use Home Assistant
   *  picture" button in Settings. */
  source?: 'manual' | 'ha'
  /** Free-form JSON blob owned by the frontend; parse via
   *  ``getPreferences()`` in ``@/utils/preferences``. */
  preferences_json?: string
  is_new_member: boolean
}

export interface SpaceMemberProfile {
  user_id: string
  role: 'owner' | 'admin' | 'member'
  joined_at: string
  space_display_name: string | null
  picture_hash: string | null
  picture_url: string | null
}

export interface FileAttachment {
  url: string
  mime_type: string
  original_name: string
  size_bytes: number
}

export interface FeedPost {
  id: string
  author: string
  type: 'text' | 'image' | 'video' | 'transcript' | 'poll' | 'schedule' | 'file' | 'bazaar' | 'event'
  content: string | null
  media_url: string | null
  file_meta: FileAttachment | null
  reactions: Record<string, string[]>
  comment_count: number
  pinned: boolean
  created_at: string
  edited_at: string | null
  /** Present on system-authored posts (author = "system-integration") that
   *  were created via the bot-bridge. Null when the originating bot has been
   *  deleted — feed renderer falls back to a generic Home Assistant chrome. */
  bot?: SpaceBotSummary | null
  /** Phase B: when ``type === 'event'`` this is the linked
   *  ``space_calendar_events.id``. The post body is the event summary;
   *  the comment thread is the event's discussion. NULL on non-event
   *  posts and on posts whose calendar event was deleted. */
  linked_event_id?: string | null
}

export type BotScope = 'space' | 'member'

export interface SpaceBotSummary {
  bot_id: string
  scope: BotScope
  name: string
  icon: string
  created_by_display_name: string
}

export interface SpaceBot {
  bot_id: string
  space_id: string
  scope: BotScope
  slug: string
  name: string
  icon: string
  created_by: string
  created_at: string
}

/** Shape of `POST /api/spaces/{id}/bots` and `POST .../{bot_id}/token`.
 *  `token` is shown exactly once; the backend only stores its sha256 digest. */
export interface SpaceBotWithToken extends SpaceBot {
  token: string
}

export interface Comment {
  id: string
  post_id: string
  parent_id: string | null
  author: string
  type: 'text' | 'image'
  content: string | null
  media_url: string | null
  deleted?: boolean
  edited_at: string | null
  created_at: string
}

export interface Space {
  id: string
  name: string
  description: string | null
  emoji: string | null
  space_type: 'private' | 'household' | 'public' | 'global'
  join_mode: 'invite_only' | 'open' | 'link' | 'request'
  features: SpaceFeatures
  retention_days: number | null
  /** When true, HA automations may post into this space via the
   *  bot-bridge. Required before any SpaceBot is registered. */
  bot_enabled?: boolean
}

export interface SpaceFeatures {
  calendar: boolean
  todo: boolean
  /** When true, the space exposes the per-member GPS map (§23.8.6).
   *  Each member must additionally opt in via PATCH
   *  /api/spaces/{id}/members/me/location-sharing. HA-defined zone
   *  names never reach a space-bound payload — the per-space zone
   *  catalogue (§23.8.7) is what labels GPS pins. */
  location: boolean
  /** Privacy tier for the per-space map (§23.8.6). Only meaningful
   *  when ``location`` is true. ``'gps'`` (default) broadcasts 4dp
   *  GPS to the space; ``'zone_only'`` makes the originating
   *  instance match GPS to a space-defined zone (§23.8.7) and
   *  broadcast only the zone label — raw coordinates never leave
   *  the originating household. */
  location_mode?: 'gps' | 'zone_only'
  stickies: boolean
  pages: boolean
  posts_access: 'open' | 'moderated' | 'admin_only'
}

/** Per-space display zone (§23.8.7). Members' GPS pins are matched to
 *  zones client-side for display; the wire never carries
 *  preprocessed "member X is in zone Y" labels. */
export interface SpaceZone {
  id: string
  space_id: string
  name: string
  latitude: number
  longitude: number
  radius_m: number
  color: string | null
  created_by: string
  created_at: string
  updated_at: string
}

export interface Conversation {
  id: string
  type: 'dm' | 'group_dm'
  name: string | null
  last_message_at: string | null
  /** When true, HA automations may post system messages into this DM via
   *  the bot-bridge (authenticated with the user's own API token). */
  bot_enabled?: boolean
}

export interface Message {
  id: string
  sender_user_id: string
  content: string
  type: string
  media_url: string | null
  reply_to_id: string | null
  deleted: boolean
  created_at: string
  edited_at: string | null
}

export interface Notification {
  id: string
  type: string
  title: string
  body: string | null
  link_url: string | null
  read_at: string | null
  created_at: string
}

export interface ShoppingItem {
  id: string
  text: string
  completed: boolean
  created_by: string
  created_at: string
  completed_at?: string | null
}

export interface CalendarEvent {
  id: string
  calendar_id: string
  summary: string
  description: string | null
  start: string
  end: string
  all_day: boolean
  attendees?: string[]
  created_by: string
  /** RFC 5545 RRULE string for recurring events. ``null`` for one-off events. */
  rrule?: string | null
  /** Phase C: optional per-occurrence "going" capacity. ``null`` = no
   *  cap (open RSVP); integer = host approval required for "going",
   *  overflow lands on waitlist. */
  capacity?: number | null
}

/** Per-event reminder configured by a user (Phase D). */
export interface EventReminder {
  event_id: string
  user_id: string
  occurrence_at: string
  minutes_before: number
  fire_at: string
  sent_at: string | null
}

/** Per-(event, user, occurrence) RSVP row (Phase A+C). */
export interface EventRsvp {
  user_id: string
  status: 'going' | 'maybe' | 'declined' | 'requested' | 'waitlist'
  occurrence_at: string
  updated_at: string
}

export interface TaskItem {
  id: string
  list_id: string
  title: string
  description: string | null
  status: 'todo' | 'in_progress' | 'done'
  position: number
  due_date: string | null
  assignees: string[]
  created_by: string
  created_at?: string
  updated_at?: string
}

export interface TaskListEntry {
  id: string
  name: string
  created_by?: string
}

export interface DirectoryEntry {
  /** Unique id of the listed space (host's side). */
  space_id: string
  /** Display-ready host household label. */
  host_instance_id: string
  host_display_name: string
  /** True when we've already CONFIRMED-paired with this host. */
  host_is_paired: boolean
  name: string
  description: string | null
  emoji: string | null
  member_count: number
  /** Local/household spaces use the same shape — scope tells us which
   *  browser tab / chip to surface the card in. */
  scope: 'household' | 'public' | 'global'
  join_mode: 'invite_only' | 'open' | 'link' | 'request'
  min_age: number
  target_audience: string
  /** Present only on `scope === 'household'` rows built from your own
   *  space list — lets the card surface "Open" when already a member. */
  already_member?: boolean
  /** Outgoing request pending (your request-to-join hasn't been decided
   *  yet). Surfaces as a disabled "Request pending" button. */
  request_pending?: boolean
  /** Caller has a read-only subscription (``role='subscriber'`` row in
   *  ``space_members``). Distinct from ``already_member``: a subscriber
   *  gets the content stream but can't post, comment, or react. */
  already_subscribed?: boolean
}

export interface RemoteInvite {
  invite_token: string
  space_id: string
  inviter_user_id: string
  inviter_instance_id: string
  space_display_hint: string | null
  expires_at: string | null
  created_at: string
}

export interface GfsConnection {
  id: string
  gfs_instance_id: string
  display_name: string
  inbox_url: string
  status: 'pending' | 'active' | 'suspended'
  paired_at: string
  published_space_count: number
}

export interface GfsSpacePublication {
  space_id: string
  gfs_connection_id: string
  gfs_display_name: string
  published_at: string
}

export interface Page {
  id: string
  title: string
  content: string
  created_by: string
  created_at: string
  updated_at: string
  last_editor_user_id: string | null
  last_edited_at: string | null
  space_id: string | null
  cover_image_url: string | null
  locked_by: string | null
  locked_at: string | null
  lock_expires_at: string | null
}

export interface PageVersion {
  id: string
  page_id: string
  version: number
  title: string
  content: string
  edited_by: string
  edited_at: string
  space_id: string | null
  cover_image_url: string | null
}

export interface EditLock {
  locked_by: string
  locked_at: string | null
  lock_expires_at: string | null
}

export type BazaarMode = 'fixed' | 'offer' | 'bid_from' | 'negotiable' | 'auction'
export type BazaarStatus = 'active' | 'sold' | 'expired' | 'cancelled'

export interface BazaarListing {
  post_id: string
  seller_user_id: string
  mode: BazaarMode
  title: string
  description: string | null
  image_urls: string[]
  end_time: string
  currency: string
  status: BazaarStatus
  price: number | null
  start_price: number | null
  step_price: number | null
  winner_user_id: string | null
  winning_price: number | null
  sold_at: string | null
  created_at: string
}

export interface BazaarBid {
  id: string
  listing_post_id: string
  bidder_user_id: string
  amount: number
  message: string | null
  accepted: boolean
  rejected: boolean
  rejection_reason: string | null
  withdrawn: boolean
  created_at: string
}

export type BazaarOfferStatus = 'pending' | 'accepted' | 'rejected' | 'withdrawn'

export interface BazaarOffer {
  id: string
  listing_post_id: string
  offerer_user_id: string
  amount: number
  message: string | null
  status: BazaarOfferStatus
  created_at: string
  responded_at: string | null
}
