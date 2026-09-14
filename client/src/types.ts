export interface User {
  user_id: string
  username: string
  /** Public ``@``-name — how others find this user. Editable by all users
   *  via ``POST /api/me/handle``; may be null before one is set, in which
   *  case display surfaces fall back to ``username``. */
  handle?: string | null
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
  /** IANA timezone anchor for this user's *personal* calendar events.
   *  Defaults to ``"UTC"`` server-side; the SPA's cold-start probe
   *  detects the browser's resolved zone via ``Intl`` on first login
   *  and PATCHes ``/api/me`` so future personal events land in the
   *  user's actual wall clock. Editable from the settings page. */
  tz?: string
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

export interface LocationData {
  /** 4-decimal-place truncated by the server (~11 m precision). */
  lat: number
  lon: number
  /** Optional user-typed label (max 80 chars). */
  label?: string | null
}

export interface FeedPost {
  id: string
  author: string
  type: 'text' | 'image' | 'video' | 'transcript' | 'poll' | 'schedule' | 'file' | 'bazaar' | 'event' | 'location' | 'highlight_share'
  content: string | null
  /** Single-URL slot for ``video`` and ``file`` posts. ``image`` posts
   *  leave this ``null`` and use :attr:`image_urls` instead. */
  media_url: string | null
  /** Background-transcode state for ``video`` posts — ``'processing'``
   *  while the worker encodes, ``'failed'`` if it gave up, ``'ready'``
   *  once playable. Absent on non-video posts and on older payloads
   *  (treated as ready). */
  media_status?: 'processing' | 'failed' | 'ready'
  /** Signed ``.webp`` poster URL for ``video`` posts — shares a UUID
   *  stem with :attr:`media_url`'s ``.webm``. Used as the player poster
   *  and the "Processing…" placeholder background. Absent on non-video
   *  posts and older payloads. */
  media_thumbnail_url?: string
  /** 1..5 signed URLs when ``type === 'image'``; empty otherwise. */
  image_urls: string[]
  file_meta: FileAttachment | null
  /** Present when ``type === 'location'`` — drops a marker on a small
   *  map in the feed card. The composer's LocationPicker captures
   *  coordinates via navigator.geolocation. */
  location?: LocationData | null
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
  /** When ``type === 'highlight_share'`` this points at the shared
   *  ``highlights.id``. Becomes ``null`` when the linked highlight is purged
   *  by retention — the share-card renders an "ended" placeholder. */
  linked_highlight_id?: string | null
  /** Newest non-deleted comment on this post, served by ``GET /api/feed``
   *  so the SPA can render a "Lina: Yes please!" preview line under the
   *  card without an N+1 fetch.  ``null`` means "no comments yet" (or
   *  every comment was soft-deleted). Optional in the type because
   *  space feed entries come from a separate route that doesn't carry
   *  the field. */
  latest_comment?: Comment | null
}

// ─── Highlights (§Highlights) ─────────────────────────────────────────────

/** Audience kind for a highlight (matches backend ``HighlightAudience`` enum). */
export type HighlightAudienceKind = 'all_paired' | 'households' | 'users'

/** Single image/video frame inside a :class:`Highlight`. */
export interface HighlightFrame {
  id: string
  highlight_id: string
  sequence: number
  frame_type: 'image' | 'video'
  /** Canonical ``/api/media/{filename}`` path. The browser is expected
   *  to fetch via this URL; the server signs at read time. */
  media_url: string
  caption_text: string | null
  caption_emoji: string | null
  /** Video frames only. Milliseconds. */
  duration_ms: number | null
  created_at: string
}

/** Per-author per-day highlight aggregate. */
export interface Highlight {
  id: string
  author_user_id: string
  /** ``YYYY-MM-DD`` UTC. */
  highlight_date: string
  audience_kind: HighlightAudienceKind
  /** List of ``instance_id``s (for ``households``) or ``user_id``s
   *  (for ``users``). Empty for ``all_paired``. */
  audience: string[]
  created_at: string
  expires_at: string
  /** When the author has opted into public sharing via a GFS, the
   *  ``gfs_connections.id``. ``null`` while not published. */
  public_gfs_id?: string | null
  public_published_at?: string | null
}

/** Inbox item: highlight + frames + how many frames the viewer has not seen. */
export interface HighlightInboxItem {
  highlight: Highlight
  frames: HighlightFrame[]
  unseen_count: number
}

/** A single Momentum post (§Momentum) — household broadcast that fans
 *  out to a 3-hop peer mesh. ``parent_moment_id`` is the reply parent
 *  (always the *root* of the thread; v1 keeps replies flat).
 *
 *  ``reaction_count`` and ``reply_count`` are aggregated server-side
 *  for the Twitter-style row chips so the inbox doesn't need a per-
 *  row follow-up fetch. */
export interface Moment {
  id:                 string
  author_user_id:     string
  content:            string
  media_url:          string | null
  media_type:         'image' | 'video' | null
  /** Background-transcode state for ``video`` moments — ``'processing'``
   *  while the worker encodes, ``'ready'`` once playable. Absent on
   *  non-video moments and on older payloads (treated as ready). */
  media_status?:      'processing' | 'failed' | 'ready'
  /** Signed ``.webp`` poster URL for ``video`` moments — shares a UUID
   *  stem with :attr:`media_url`'s ``.webm``. Absent on non-video
   *  moments and older payloads. */
  media_thumbnail_url?: string
  duration_ms:        number | null
  parent_moment_id:   string | null
  origin_instance_id: string
  created_at:         string
  expires_at:         string
  reaction_count:     number
  reply_count:        number
  /** Author opted to fan this moment out via at least one GFS
   *  (§Momentum-public). The Inbox renders a "via {gfs}" chip when
   *  combined with ``received_via === 'gfs'``. */
  is_public?:         boolean
  /** ``self`` / ``household`` / ``gfs`` — provenance of the row. */
  received_via?:      'self' | 'household' | 'gfs'
  /** When ``received_via === 'gfs'``, points at the GFS connection
   *  the row arrived through (so the inbox chip can show a label). */
  received_via_gfs_id?: string | null
}

/** Author-side public-Momentum registration row. */
export interface MomentPublicRegistration {
  user_id:        string
  gfs_id:         string
  registered_at:  string
  default_share:  boolean
}

/** Follower-side public-Momentum follow row, surfaces the cached
 *  display fields so the Discover/inbox surfaces don't need a GFS
 *  round-trip on every render. */
export interface MomentPublicFollow {
  follower_user_id:      string
  followed_user_id:      string
  gfs_id:                string
  followed_username:     string
  followed_display_name: string
  created_at:            string
}

/** Entry in a GFS public-user directory (``GET /api/gfs/{id}/moments/users``). */
export interface MomentPublicDirectoryUser {
  user_id:           string
  instance_id:       string
  username:          string
  display_name:      string
  picture_url:       string | null
  picture_digest:    string | null
  bio:               string | null
  home_instance_pk:  string
  registered_at:     number
  follower_count?:   number
}

export interface MomentReaction {
  moment_id:       string
  reactor_user_id: string
  emoji:           string
  reacted_at:      string
}

export interface MomentDetail {
  moment:    Moment
  replies:   Moment[]
  reactions: MomentReaction[]
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
  join_mode: 'invite_only' | 'open' | 'request'
  /** Discovery category (e.g. ``'gaming'``); ``'general'``/absent = none. */
  category?: string
  features: SpaceFeatures
  retention_days: number | null
  /** When true, HA automations may post into this space via the
   *  bot-bridge. Required before any SpaceBot is registered. */
  bot_enabled?: boolean
  /** Soft, reversible archive. When true the space is read-only (the
   *  server rejects writes) and the SPA groups it out of the active
   *  list + shows a read-only banner. Distinct from a dissolve (hard
   *  delete). */
  archived?: boolean
  /** Why the space is archived. ``null``/absent = a normal reversible
   *  admin archive; ``'dissolved'`` = the owner host dissolved it;
   *  ``'removed'`` = this household was removed. The latter two are
   *  remote-terminated: read-only, content kept, NOT unarchivable. */
  archived_reason?: 'dissolved' | 'removed' | null
  /** §D1b — the instance that originally created this space. When
   *  this differs from the caller's own instance, the local row is a
   *  *stub* mirroring a space hosted on another household. The SPA
   *  gates settings + admin gestures on this field so a remote
   *  member never tries to mutate state the host owns. */
  owner_instance_id?: string
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
  /** Per-space gallery tab (§23.119). When false the gallery tab is
   *  hidden in the SPA shell; existing albums remain in storage. */
  gallery: boolean
  /** Per-space Bazaar tab (§23.15). When false the marketplace tab is
   *  hidden and new listings are rejected; existing listings remain. */
  bazaar?: boolean
  posts_access: 'open' | 'moderated' | 'admin_only'
  /** Subscriber-engagement opt-ins (§23.49). Subscribers are
   *  read-only by default; admins flip these to let followers leave
   *  reactions / comments without promoting them to full members. */
  allow_subscriber_comment?: boolean
  allow_subscriber_react?: boolean
  /** Owner opt-in (delegated-admin epic, Phase 1a). When true the owner
   *  has authorised the space's admins to act on the owner's behalf
   *  (moderate / invite / publish) while the owner is offline. Defaults
   *  false (least-privilege). Absent → treat as false. */
  delegated_admin_authority?: boolean
  /** Post types members may compose in this space (§23.49). An admin
   *  toggles these in space settings to hide post kinds the space
   *  doesn't want (e.g. no polls). Absent → treat as all-allowed
   *  (a freshly-stubbed remote space before the host's config lands). */
  allowed_post_types?: string[]
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

export interface ConversationMemberPreview {
  user_id: string
  username: string
  display_name: string
  picture_url: string | null
}

export interface Conversation {
  id: string
  type: 'dm' | 'group_dm'
  name: string | null
  last_message_at: string | null
  /** When true, HA automations may post system messages into this DM via
   *  the bot-bridge (authenticated with the user's own API token). */
  bot_enabled?: boolean
  /** Other members (not including the caller) — populated by
   *  ``GET /api/conversations`` so the inbox can render avatar stacks
   *  + a peer-name fallback when ``name`` is null. */
  members?: ConversationMemberPreview[]
  member_count?: number
  /** Unread message count for the caller. Populated by
   *  ``GET /api/conversations`` so the inbox can render per-row
   *  chips and the sidebar can sum across rows for the Chats badge. */
  unread?: number
}

export interface Message {
  id: string
  sender_user_id: string
  content: string
  /** ``"text" | "image" | "video" | "file" | "audio" | "transcript" |
   *  "location" | "call_event"`` — keep ``string`` so future
   *  additions don't require a SPA-side type bump. */
  type: string
  media_url: string | null
  /** ``"image"``/``"video"``/``"file"`` only: original filename + IANA
   *  MIME type + raw byte count of the full media. The SPA uses
   *  ``mime_type`` to pick the render branch (``image/*`` → inline
   *  ``<img>``, ``video/*`` → ``<video>``, everything else → file
   *  pill with a glyph). ``null`` on legacy / non-media messages. */
  file_name?: string | null
  mime_type?: string | null
  file_size_bytes?: number | null
  /** Cross-household sync state. ``"pending"`` = the bubble currently
   *  renders the small preview embedded in the federation envelope
   *  and is waiting for the matching full-bytes blob. ``"failed"`` =
   *  the sender's outbox exhausted its retry budget. ``null`` on
   *  same-household messages or once the full bytes have arrived. */
  media_sync_status?: 'pending' | 'failed' | null
  /** Background-transcode state for ``video`` messages — ``'processing'``
   *  while the worker encodes, ``'ready'`` once playable. Distinct from
   *  :attr:`media_sync_status` (cross-household blob transfer). Absent on
   *  non-video messages and on older payloads (treated as ready). */
  media_status?: 'processing' | 'failed' | 'ready'
  /** Signed ``.webp`` poster URL for ``video`` messages — shares a UUID
   *  stem with :attr:`media_url`'s ``.webm``. Absent on non-video
   *  messages and older payloads. */
  media_thumbnail_url?: string
  /** SPA-only fields set on the optimistic ``tmp-...`` bubble when
   *  its background ``POST`` fails. Never reach the wire / server. */
  send_failed?: boolean
  send_failed_reason?: string
  reply_to_id: string | null
  /** Per-user emoji reactions on this DM message. Each row is one
   *  emoji from one reactor. The SPA aggregates by glyph for the
   *  reaction strip; ``user_id === currentUser.user_id`` flags the
   *  caller's own reaction (so a second tap on the same chip toggles
   *  it off). */
  reactions?: { user_id: string; emoji: string }[]
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
  /** Free-form store / shop name (``"Aldi"``, ``"Bakery"``, …). The
   *  SPA groups items by this when "Group by store" is on; ``null``
   *  / undefined lands in the trailing "No store" section. */
  store?: string | null
}

/** A row in the household's shopping-store catalogue. Carries only
 *  what the SPA needs for the grouped view: the name and the
 *  household-defined position in the list. */
export interface ShoppingStore {
  name: string
  sort_order: number
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
  /** Whether this event invites a yes/no/maybe response from
   *  ``attendees``. Household events default to ``false`` — they're
   *  just placed on the assigned member's calendar. The host opts in
   *  via the dialog's "Ask invitees to respond" checkbox. Space
   *  events with ``capacity`` set behave as if RSVP is enabled. */
  rsvp_enabled?: boolean
  /** Optional cover image URL (relative ``/api/media/{filename}``).
   *  Renders at the top of EventPostCard on the feed and as a
   *  horizontal thumbnail in the calendar list view. */
  cover_url?: string | null
  /** Free-form location (venue, address, room name, conference URL).
   *  Surfaces under the event title on the card and is emitted on ICS
   *  export. ``null`` means "no location". */
  location?: string | null
  /** IANA timezone the host had in mind when they created the event
   *  (``"Europe/Berlin"``, ``"America/New_York"``). The SPA renders
   *  the event in this wall clock via ``formatEventTime`` and
   *  annotates "≈ HH:MM your time" when the viewer's tz differs.
   *  Defaults to ``"UTC"`` server-side so a missing field still
   *  resolves consistently. */
  tz?: string
  /** Client-stamped grouping uuid. The composer mints one before a
   *  multi-target fan-out and ships it on every ``POST`` in the
   *  batch; every resulting row carries the same value. The
   *  agenda's :func:`groupSharedEvents` prefers this when both rows
   *  have it (intent-driven), falling back to the content-key
   *  heuristic for legacy / sub-version rows. ``null`` for
   *  single-target events and externally-imported rows. Issue #327. */
  client_event_uuid?: string | null
  /** **Authoritative** copy set of a shared event — every row that
   *  shares this event's ``client_event_uuid``, regardless of which
   *  calendars the SPA happens to have loaded. Server-built and
   *  ordered oldest-first (``created_at, id``), with at most one
   *  entry per ``calendar_id`` — the ``ux_calendar_events_fanout``
   *  partial unique index (migration 0047) makes a second local copy
   *  on one calendar impossible. ``[]`` for an event with no
   *  ``client_event_uuid`` (legacy / ICS-imported rows that still rely
   *  on the content-key grouping) and for space events. Never carries
   *  cross-household ``remote_invite`` mirrors. Anything that WRITES
   *  (the edit dialog's full sync) must read this, never the
   *  ``_grouped_*`` render artifacts below. */
  copies?: { event_id: string; calendar_id: string; owner_username: string }[]
  /** SPA-only group key, set by :func:`groupSharedEvents`. The composer
   *  fans out a multi-attendee event as one ``POST`` per picked
   *  calendar, which lands as N rows in the DB with the same
   *  ``summary`` / ``start`` / ``end`` / ``created_by`` / ``description``
   *  / ``location`` / ``cover_url``. The grouper merges them into a
   *  single rendered row whose ``_grouped_calendar_ids`` lists every
   *  underlying row's ``calendar_id`` and ``_grouped_event_ids`` lists
   *  the matching DB ids — used to render multi-owner chips on the
   *  agenda. Never reaches the wire.
   *
   *  **Render-time only.** These list the rows that were *loaded and
   *  merged for this render* — i.e. the copies on the currently
   *  VISIBLE calendars. They are NOT the authoritative copy set: a
   *  shared event whose other holders' calendars are hidden merges
   *  into a single-entry group. Use :attr:`copies` for anything that
   *  drives writes; treating these as the copy set is what duplicated
   *  shared events across every member's calendar on edit. */
  _grouped_calendar_ids?: string[]
  _grouped_event_ids?: string[]
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
  join_mode: 'invite_only' | 'open' | 'request'
  min_age: number
  /** Discovery category (e.g. ``'gaming'``). ``'general'``/absent =
   *  unspecified — the card surfaces no category chip in that case. */
  category?: string
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

export interface LocalInvite {
  invitation_id: string
  space_id: string
  /** Inviter's local ``user_id`` (a same-household admin). */
  invited_by: string | null
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
  /** Live WebSocket liveness from the supervisor — distinct from the
   *  stored ``status`` (the pairing state). True only when a socket is
   *  actually open right now. */
  connected?: boolean
  /** Last auth/close reason when not connected (e.g. ``unknown-instance``,
   *  ``bad-signature``, ``ts-skew``); null when connected or never errored.
   *  GFS-controlled — render only via mapped strings or escaped text. */
  last_error?: string | null
}

export interface GfsSpacePublication {
  space_id: string
  gfs_connection_id: string
  /** Only the admin ``/api/gfs/publications`` view carries this; the
   *  per-space ``/api/spaces/{id}/publications`` endpoint omits it. */
  gfs_display_name?: string
  published_at: string
  /** ``active`` = live & discoverable; ``pending`` = held for GFS
   *  moderator approval (not yet discoverable); ``banned`` = removed /
   *  rejected by the GFS. */
  status: 'active' | 'pending' | 'banned'
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
  space_id: string
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
