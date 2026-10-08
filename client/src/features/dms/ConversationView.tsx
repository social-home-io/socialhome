import { Fragment } from 'preact'
import { useEffect, useMemo, useRef, useLayoutEffect } from 'preact/hooks'
import { signal, type Signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { ws } from '@/ws'
import type { Message } from '@/types'
import { DmThreadSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { VoiceRecordButton } from '@/components/VoiceRecordButton'
import { AudioBubble } from '@/components/AudioBubble'
import { VideoMedia } from '@/components/VideoMedia'
import { openLightbox } from '@/components/ImageLightbox'
import { showToast } from '@/components/Toast'
import { startCall } from '@/features/calls/callSession'
import { showCallError } from '@/features/calls/CallEmbedBlockedDialog'
import { ReadReceipt, readReceiptsEnabled } from '@/components/ReadReceipts'
import { TypingIndicator, sendTyping } from '@/components/TypingIndicator'
import { UnreadDivider } from '@/components/UnreadDivider'
import { openCallTypePicker } from '@/components/CallTypePickerDialog'
import { EmojiPickButton } from '@/components/EmojiPickButton'
import { uploadWithProgress } from '@/components/UploadProgress'
import { MediaAttachmentChip } from '@/components/MediaAttachmentChip'
import { MessageContextSheet } from '@/components/MessageContextSheet'
import { ReactionPicker } from '@/components/ReactionPicker'
import { LocationPicker, type LocationDraft } from '@/components/LocationPicker'
import { t, formatLocale, isOne } from '@/i18n/i18n'
import { formatCoords, parseDmLocation, toDmLocationContent } from '@/utils/dmLocation'
import { ComposerAttachMenu } from './ComposerAttachMenu'
import { DmLocationMessage } from './DmLocationMessage'
import { GroupInfoDialog } from './GroupInfoDialog'
import { MuteButton } from './ConversationMute'
import {
  EmojiAutocomplete,
  checkForEmojiTrigger,
  closeEmojiAutocomplete,
  handleEmojiAutocompleteKey,
} from '@/components/EmojiAutocomplete'
import {
  MentionAutocomplete,
  checkForMentionTrigger,
  closeMentionAutocomplete,
  handleMentionAutocompleteKey,
  mentionInputAria,
} from '@/components/MentionAutocomplete'
import { MentionText } from '@/components/MentionText'
import {
  conversationMentionRender,
  setConversationMembers,
} from '@/store/conversationMembers'
import { emojiByShortcode } from '@/data/emojis'
import { currentUser } from '@/store/auth'
import { useTitle, useTitleAvatar, type PageAvatar } from '@/store/pageTitle'
import { normaliseTimestamp } from '@/utils/relativeTime'
import { addBase } from '@/baseUrl'
import { safeHref } from '@/utils/safeHref'

/** Page size for the lazy-load older-history fetch. The initial
 *  load uses a wider window (see ``ConversationView`` body); each
 *  follow-up "load older" page is this many messages. Backend caps
 *  any ``limit`` query at 100; 50 is the sweet spot — wide enough
 *  that you rarely need three fetches in one scroll session,
 *  narrow enough to feel instant on slow links. */
const PAGE_SIZE = 50
/** Cap how tall the composer textarea is allowed to grow before it
 *  starts to scroll internally. ~6 lines at the default font; matches
 *  WhatsApp's ceiling so a very long draft doesn't eat half the chat
 *  while the user is still typing. */
const MAX_COMPOSER_HEIGHT_PX = 160

/** Pixel slack at the visual bottom that still counts as "looking at
 *  the live edge". Picked at 80 px so a single line of swipe inertia
 *  on mobile doesn't flip the chat out of sticky-bottom mode. Shared
 *  between :func:`handleScroll` (user scroll) and the anchor-scroll
 *  layout effect (entry from notification) so both compute the same
 *  thing. */
const LIVE_EDGE_PX = 80

/** Magnitude of the user's distance from the latest message in the
 *  column-reverse scroll container.
 *
 *  Chrome / Safari / Edge / modern Firefox use **negative**
 *  ``scrollTop`` values in column-reverse — 0 at the visual bottom,
 *  ``-(scrollHeight - clientHeight)`` at the visual top. Older
 *  Firefox versions used **positive** values mirroring the
 *  non-reversed layout. We normalise either way so a future user on
 *  a legacy engine still gets correct sticky / lazy-load behaviour.
 *
 *  Exported so unit tests can pin the threshold logic without
 *  having to materialise a real scroll layout (jsdom doesn't run
 *  layout, so manipulating ``scrollHeight`` / ``scrollTop`` directly
 *  is the only way to drive this through the page). */
export function columnReverseDistFromBottom(el: Pick<
  HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight'
>): number {
  const maxScroll = Math.max(el.scrollHeight - el.clientHeight, 0)
  return el.scrollTop <= 0
    ? -el.scrollTop                 // Chrome / modern Firefox
    : maxScroll - el.scrollTop      // legacy positive-scrollTop
}

/** True when the user is within ``LIVE_EDGE_PX`` of the visual bottom
 *  of a column-reverse scroll container — the "looking at the latest
 *  message" state. Drives the jump-down chip visibility and the
 *  read-watermark advance. */
export function isAtLiveEdge(el: Pick<
  HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight'
>): boolean {
  return columnReverseDistFromBottom(el) < LIVE_EDGE_PX
}

/** Human-readable byte-size pill. Mirrors what the feed composer's
 *  upload UI shows so the DM composer reads in the same language. */
function formatFileSize(bytes: number | null | undefined): string {
  if (!bytes || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  const mb = bytes / (1024 * 1024)
  if (mb < 10) return `${mb.toFixed(1)} MB`
  return `${Math.round(mb)} MB`
}
/** Pending attachment chosen via the paperclip button. ``null`` when
 *  no file is staged. The chip in the composer (above the textarea)
 *  shows the upload spinner / preview thumb / filename so the user
 *  knows what they're about to send. Cleared on send + on explicit
 *  cancel via the chip's "×". An attachment also drives the send
 *  button to render even when the text body is empty (a picture
 *  message has no caption requirement). */
interface PendingAttachment {
  /** ``image`` / ``video`` / ``file`` — picks the bubble render. */
  type: 'image' | 'video' | 'file'
  /** Signed URL the SPA can drop into ``<img src>`` for the
   *  in-composer preview. */
  preview_url: string
  /** Canonical (unsigned) URL — what gets POSTed back in the
   *  ``media_url`` field. The backend signs fresh per read. */
  media_url: string
  file_name: string
  mime_type: string
  file_size_bytes: number
}
/** Live state of an in-flight upload — distinct from
 *  ``pendingAttachment`` (which only carries the *completed* result).
 *  Surfaced via :class:`MediaAttachmentChip` so the composer shows
 *  "Uploading… 45%" while bytes flow + "Processing image…" while the
 *  server transcodes. Cleared on success (``pendingAttachment`` takes
 *  over) or on cancel; left in ``failed`` state on error so the user
 *  can retry without re-picking the file. */
interface UploadingAttachment {
  kind: 'image' | 'video' | 'file'
  filename: string
  fileSize: number
  /** ``URL.createObjectURL(file)`` for image / video previews;
   *  ``null`` for generic files. The chip revokes the URL on
   *  unmount so we don't leak the blob. */
  previewUrl: string | null
  phase: 'uploading' | 'processing' | 'failed'
  percent: number
  errorMessage?: string
  /** Kept so the retry button can re-issue ``uploadWithProgress``
   *  with the same File object. */
  file: File
}
/** Same-author message-grouping window. Consecutive bubbles from one
 *  sender within this many ms share one timestamp + ReadReceipt
 *  footer and get flush vertical borders so the burst reads as one
 *  speech turn instead of N independent rows. Five minutes is the
 *  same window Apple Messages and Signal use. */
const GROUP_GAP_MS = 5 * 60_000
/** Flat list item rendered by the messages map. The chronological
 *  list of messages is interleaved with ``day`` separators at
 *  midnight boundaries, and each ``msg`` carries pre-computed
 *  ``showHeader`` / ``showFooter`` flags so the JSX doesn't have to
 *  look at its neighbours at render time. */
type FlatItem =
  | { kind: 'day'; label: string; key: string }
  | { kind: 'msg'; m: Message; showHeader: boolean; showFooter: boolean }

/** Human-friendly day label for a separator pill.
 *
 *  Today / Yesterday for the two most recent days; weekday name
 *  (``Monday``) for the rest of the past week; ``Mon 14 May`` for
 *  this year; ``14 May 2025`` for older. Resolves locale separators
 *  via ``toLocaleDateString`` so the formatter matches the rest of
 *  the SPA's relative-time vocabulary. */
function dmDayLabel(ts: number, now: number = Date.now()): string {
  const todayKey = new Date(now).toDateString()
  const yesterdayKey = new Date(now - 86_400_000).toDateString()
  const tKey = new Date(ts).toDateString()
  if (tKey === todayKey) return t('dms.day.today')
  if (tKey === yesterdayKey) return t('dms.day.yesterday')
  const d = new Date(ts)
  if (now - ts < 6 * 86_400_000) {
    return d.toLocaleDateString(formatLocale(), { weekday: 'long' })
  }
  const sameYear = d.getFullYear() === new Date(now).getFullYear()
  return d.toLocaleDateString(formatLocale(), {
    month: 'short',
    day: 'numeric',
    year: sameYear ? undefined : 'numeric',
  })
}

/** Build the chronological flat render list from a message array.
 *
 *  - Inserts a ``day`` separator before the first message of each
 *    calendar day (viewer's local timezone).
 *  - Sets ``showHeader`` on each ``msg`` item to ``true`` only when
 *    it starts a new same-author cluster (different sender from the
 *    previous, or > ``GROUP_GAP_MS`` apart, or first after a
 *    ``call_event``).
 *  - Back-patches the previous item's ``showFooter`` to ``false``
 *    when the current message continues the cluster, so only the
 *    *last* bubble in a burst carries the timestamp + receipt.
 *
 *  Call events always render in their own group — they stand alone
 *  as system-style rows and never group with adjacent text. */
function buildFlatItems(msgs: Message[]): FlatItem[] {
  const items: FlatItem[] = []
  let lastDayKey: string | null = null
  let lastSender: string | null = null
  let lastTs: number | null = null
  for (const m of msgs) {
    const ts = Date.parse(normaliseTimestamp(m.created_at))
    const dayKey = Number.isNaN(ts) ? '?' : new Date(ts).toDateString()
    if (dayKey !== lastDayKey) {
      const label = Number.isNaN(ts) ? '' : dmDayLabel(ts)
      items.push({ kind: 'day', label, key: `day-${dayKey}` })
      lastDayKey = dayKey
      // Day change always starts a new cluster.
      lastSender = null
      lastTs = null
    }
    if (m.type === 'call_event') {
      items.push({ kind: 'msg', m, showHeader: true, showFooter: true })
      lastSender = null
      lastTs = null
      continue
    }
    const isContinuation =
      m.sender_user_id === lastSender &&
      lastTs !== null && !Number.isNaN(ts) &&
      (ts - lastTs) < GROUP_GAP_MS
    if (isContinuation && items.length > 0) {
      // The previous message item (now the second-to-last after this
      // push) loses its footer — only the LAST bubble in a cluster
      // carries the timestamp + ReadReceipt.
      const prev = items[items.length - 1]
      if (prev.kind === 'msg') prev.showFooter = false
    }
    items.push({
      kind: 'msg', m,
      showHeader: !isContinuation,
      showFooter: true,
    })
    lastSender = m.sender_user_id
    lastTs = Number.isNaN(ts) ? lastTs : ts
  }
  return items
}

interface ThreadMember {
  user_id: string
  username: string
  display_name: string
  /** Backend-signed avatar URL — comes pre-tagged with ``?exp=&sig=``
   *  so the browser can load it via raw ``<img>`` without an
   *  ``Authorization`` header. Null when the user has no profile
   *  picture set; the Avatar component falls back to initials. */
  picture_url: string | null
  is_self: boolean
  is_online: boolean
  is_idle: boolean
  last_seen_at: string | null
  /** ``null`` for this household's people; the household of a remote
   *  member otherwise (``household_name`` is null when not paired). */
  instance_id?: string | null
  household_name?: string | null
  /** §23.42 — the exact @-token (no ``@``) that mentions this member in
   *  the chat; ``null`` when they can't be mentioned by token. */
  mention?: string | null
}

/** This thread's metadata from ``GET /api/conversations/{id}`` (the same
 *  row shape the inbox list ships) — type, group name, and whether this
 *  household keeps the group's member list. */
interface ThreadInfo {
  type: string
  name: string | null
  managed_here: boolean
  /** The viewer's own mute of this thread (``null`` = not muted). */
  muted_until: string | null
  /** Groups: the viewer's own level — every message, or only @-mentions. */
  notif_level: 'all' | 'mentions'
}

/** One conversation's row as ``GET /api/conversations/{id}`` ships it —
 *  only the fields the thread reads. */
interface ConversationRow {
  id?: string
  type?: string
  name?: string | null
  managed_here?: boolean
  unread?: number
  last_read_at?: string | null
  muted_until?: string | null
  notif_level?: string
}

/** The thread's metadata from its row; ``null`` when the response isn't
 *  a conversation row at all. */
function toThreadInfo(row: ConversationRow | null | undefined): ThreadInfo | null {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null
  return {
    type: row.type ?? 'dm',
    name: row.name ?? null,
    managed_here: row.managed_here === true,
    muted_until: row.muted_until ?? null,
    notif_level: row.notif_level === 'mentions' ? 'mentions' : 'all',
  }
}

/** Own, sent, not deleted, and text the sender wrote: a text message or a
 *  media caption. Voice notes (machine transcript) and location pins are
 *  not edited from the thread. */
export function canEditMessage(m: Message, myUserId: string | null | undefined): boolean {
  if (!myUserId || m.sender_user_id !== myUserId) return false
  if (m.deleted || m.send_failed || m.id.startsWith('tmp-')) return false
  if (m.type === 'text') return true
  return ['image', 'video', 'file'].includes(m.type) && Boolean(m.content)
}

/** WhatsApp-style "Last seen 12 min ago" formatter — same shape as the
 *  presence-page helper but inline so this page doesn't grow a util
 *  module just for one consumer. */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return null
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (sec < 60)      return t('time.just_now')
  if (sec < 3600)    return t('time.minutes_ago', { n: String(Math.floor(sec / 60)) })
  if (sec < 86400)   return t('time.hours_ago', { n: String(Math.floor(sec / 3600)) })
  return t('time.days_ago', { n: String(Math.floor(sec / 86400)) })
}

/** Build the WhatsApp-style status line for the thread header.
 *  • 1:1 DM → peer's online state, or "last seen X" when offline.
 *  • Group DM → "<n> online" when ≥ 1 peer is online; otherwise null
 *    (group threads don't surface a per-peer last-seen line — too noisy). */
function statusLine(members: ThreadMember[]): string | null {
  const peers = members.filter(m => !m.is_self)
  if (peers.length === 0) return null
  if (peers.length === 1) {
    const p = peers[0]
    if (p.is_online && p.is_idle) return t('dms.status.idle')
    if (p.is_online)              return t('dms.status.online')
    const ago = humanizeAgo(p.last_seen_at)
    return ago ? t('dms.status.last_seen', { ago }) : t('dms.status.offline')
  }
  const onlineCount = peers.filter(p => p.is_online).length
  if (onlineCount === 0) return null
  return t('dms.status.n_online', { n: String(onlineCount) })
}

interface DeliveryState {
  message_id: string
  user_id: string
  state: 'delivered' | 'read'
  state_at: string
}

interface MessageGap {
  sender_user_id: string
  expected_seq: number
  detected_at: string
}

/** Everything one open thread keeps while it is on screen.
 *
 *  Created per :func:`ConversationView` instance AND per conversation
 *  (``useMemo`` keyed on the id), never at module level: two views
 *  mounted side by side never share a reply target, a draft edit or a
 *  message list, and switching ``conversationId`` starts from a clean
 *  slate. A request still in flight for the thread we left resolves
 *  into that thread's own (now unrendered) store, so it can never
 *  splice its rows into the thread on screen. */
interface ThreadState {
  messages: Signal<Message[]>
  loading: Signal<boolean>
  /** Whether older messages are available to fetch via
   *  ``?before=<oldest_id>``. Set to ``false`` when a fetch returns
   *  fewer messages than the requested limit (= no more history). */
  hasMoreHistory: Signal<boolean>
  /** True while a back-fill ``loadOlder()`` request is in flight.
   *  Drives the spinner at the top of the messages list and stops
   *  the scroll handler from queueing parallel requests. */
  isLoadingOlder: Signal<boolean>
  /** First-unread anchor used to render a "New messages" divider on
   *  entry. ``message_id`` is the id of the first message the caller
   *  hasn't read yet; the SPA scrolls that row into view. ``null`` if
   *  there are no unread messages in the loaded window, in which case
   *  the entry effect falls back to scroll-to-bottom. */
  unreadAnchor: Signal<{ message_id: string } | null>
  /** Counter of new messages received since the user scrolled up off
   *  the bottom. Drives the "↓ N new messages" jump-down chip. Resets
   *  to zero when the user reaches the bottom (either by scrolling or
   *  by clicking the chip). */
  newSinceScrollUp: Signal<number>
  /** ``true`` when the composer textarea has at least one non-whitespace
   *  character. Drives the mic⇄send slot swap: an empty composer shows
   *  the round push-to-talk mic (when STT is available); typing morphs
   *  it into the round Send button — same slot, mutually exclusive
   *  actions, the chat-bar idiom every mainstream messenger uses. */
  composerHasContent: Signal<boolean>
  /** The "Share a location" picker (attach menu → Location). Its map is
   *  the preview: nothing is sent until the user confirms there. */
  locationPickerOpen: Signal<boolean>
  pendingAttachment: Signal<PendingAttachment | null>
  uploadingAttachment: Signal<UploadingAttachment | null>
  /** Most-recent attachment-related error to surface in a toast (or
   *  inline below the composer). Cleared when the user picks a new
   *  file or sends. Drives the "Media can only be shared with
   *  directly-paired households" copy when the backend rejects a
   *  send on a relayed DM. */
  attachmentError: Signal<string | null>
  readMessageIds: Signal<Set<string>>
  deliveredMessageIds: Signal<Set<string>>
  memberCount: Signal<number>
  threadMembers: Signal<ThreadMember[]>
  threadInfo: Signal<ThreadInfo | null>
  groupInfoOpen: Signal<boolean>
  /** WhatsApp-style reply target. When set, the composer shows a chip
   *  with the parent message preview and the next send carries
   *  ``reply_to_id``. Cleared after send or by the chip's "×" button. */
  replyTo: Signal<Message | null>
  /** The own message being edited in place (its bubble shows a text box). */
  editing: Signal<{ id: string, draft: string } | null>
  /** Touch-only context sheet target. Set on long-press, cleared by
   *  the sheet itself or when an action runs. Hover/keyboard users
   *  trigger Reply through the inline ``.sh-message-reply-btn``
   *  chip, so the sheet only ever surfaces on touch. */
  contextSheetFor: Signal<Message | null>
  /** Active emoji-picker target. When set, the full
   *  :class:`ReactionPicker` opens centred over the thread; tapping
   *  a glyph calls ``toggleReaction`` on this message and clears
   *  the signal. */
  reactionPickerFor: Signal<Message | null>
  gaps: Signal<MessageGap[]>
  /** The viewer left / was removed while embedded with no ``onLeave``
   *  host to hand off to — the view shows a short note instead of a
   *  thread they can no longer use. */
  left: Signal<boolean>
  /** ``true`` while this store's thread is the one on screen. Read by
   *  ``loadOlder`` — a callback outside the load effect, so it can't
   *  see that effect's per-run ``cancelled`` closure — before it writes
   *  an awaited page. A plain field rather than a signal because
   *  nothing renders from it and a write must not schedule a
   *  re-render. */
  active: boolean
}

function createThreadState(): ThreadState {
  return {
    messages: signal<Message[]>([]),
    loading: signal(true),
    hasMoreHistory: signal(true),
    isLoadingOlder: signal(false),
    unreadAnchor: signal<{ message_id: string } | null>(null),
    newSinceScrollUp: signal(0),
    composerHasContent: signal(false),
    locationPickerOpen: signal(false),
    pendingAttachment: signal<PendingAttachment | null>(null),
    uploadingAttachment: signal<UploadingAttachment | null>(null),
    attachmentError: signal<string | null>(null),
    readMessageIds: signal<Set<string>>(new Set()),
    deliveredMessageIds: signal<Set<string>>(new Set()),
    memberCount: signal<number>(0),
    threadMembers: signal<ThreadMember[]>([]),
    threadInfo: signal<ThreadInfo | null>(null),
    groupInfoOpen: signal(false),
    replyTo: signal<Message | null>(null),
    editing: signal<{ id: string, draft: string } | null>(null),
    contextSheetFor: signal<Message | null>(null),
    reactionPickerFor: signal<Message | null>(null),
    gaps: signal<MessageGap[]>([]),
    left: signal(false),
    active: false,
  }
}

/** The mounted view that last told the backend "this thread is open"
 *  (``dm.active``), or ``null``. Module-level on purpose: the claim is
 *  one per WS connection, shared by every view on the page. */
let activeOwner: symbol | null = null

/** Refetch the roster. Resolves ``false`` when the viewer is no longer a
 *  member (403) — they were removed from a group, or left it elsewhere. */
async function fetchRoster(s: ThreadState, convId: string): Promise<boolean> {
  try {
    const rows = await api.get(`/api/conversations/${convId}/members`) as ThreadMember[]
    s.threadMembers.value = rows
    s.memberCount.value = rows.length || 2
    setConversationMembers(convId, rows)
    return true
  } catch (e: any) {
    if (e?.status === 403) return false
    return true
  }
}

/** Refetch this thread's metadata (name, mute, level, …). */
async function fetchThreadInfo(s: ThreadState, convId: string): Promise<void> {
  try {
    const row = await api.get(`/api/conversations/${convId}`) as ConversationRow
    s.threadInfo.value = toThreadInfo(row)
  } catch {
    /* keep what we have */
  }
}

/**
 * Render a ``type="call_event"`` system message as a compact centred row
 * in the DM thread (spec §26.8). The backend stores a JSON blob describing
 * the event; we parse it at render-time and offer a one-tap "Call back"
 * on missed/declined events.
 */
function CallEventRow({ m, onCallBack }: {
  m: Message
  /** ``undefined`` hides "Call back" (calls are off for this view). */
  onCallBack?: (type: 'audio' | 'video') => void
}) {
  let ev: { event?: string, call_type?: string, duration_seconds?: number | null } = {}
  try { ev = JSON.parse(m.content) } catch { /* noop */ }
  const ic = ev.call_type === 'video' ? '📹' : '📞'
  const label = ev.event === 'missed' ? t('dms.call_event.missed')
    : ev.event === 'declined' ? t('dms.call_event.declined')
    : ev.event === 'ended'    ? t('dms.call_event.ended')
    : t('dms.call_event.started')
  const dur = ev.duration_seconds && ev.duration_seconds > 0
    ? ` · ${formatDuration(ev.duration_seconds)}` : ''
  const when = new Date(m.created_at).toLocaleTimeString(formatLocale(), { hour: '2-digit', minute: '2-digit' })
  const showBack = ev.event === 'missed' || ev.event === 'declined'
  return (
    <div class="sh-call-event">
      <span class="sh-call-event-icon">{ic}</span>
      <span class="sh-call-event-label">{label}</span>
      <span class="sh-call-event-meta">{dur} · {when}</span>
      {showBack && onCallBack && (
        <Button onClick={() => onCallBack((ev.call_type as 'audio' | 'video') ?? 'audio')}>
          {t('dms.call_event.call_back')}
        </Button>
      )}
    </div>
  )
}

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return m > 0
    ? t('calls.history.duration_min_sec', { m: String(m), s: String(s) })
    : t('calls.history.duration_sec', { s: String(s) })
}

/** Thread-header call button (§26.2).
 *
 * One phone icon — tapping it opens :class:`CallTypePickerDialog` so the
 * user picks audio vs. video on a focused dialog rather than having to
 * choose between two cramped header icons. The chosen type is fixed at
 * offer time on the backend; mid-call enable/disable of video is handled
 * by :func:`InCallPage.toggleCamera`.
 */
function CallButton({ convId, memberCount }: { convId: string, memberCount: number }) {
  // Only meaningful when the DM has ≥ 1 peer.
  if (memberCount < 2) return null
  return (
    <div class="sh-thread-call-buttons">
      <button type="button" class="sh-icon-btn" title={t('calls.picker.title')}
              onClick={() => openCallTypePicker(convId)}
              aria-label={t('calls.picker.title')}>📞</button>
    </div>
  )
}

/** Sets the TopBar title (+ the 1:1 peer's avatar) while mounted. A
 *  component rather than a direct hook call so an embedded view can
 *  leave the page title alone without breaking the rules of hooks. */
function ThreadPageTitle({ title, avatar }: { title: string, avatar: PageAvatar | null }) {
  useTitle(title)
  useTitleAvatar(avatar)
  return null
}

export interface ConversationViewProps {
  /** The conversation to show. Changing it resets every piece of thread
   *  state (messages, reply / edit, composer, roster) and loads the new
   *  one. */
  conversationId: string
  /** Rendered inside another surface rather than as the routed DM page:
   *  no back chevron, no TopBar title / avatar, no full-bleed body
   *  class, no global "n" composer shortcut, and leaving / being
   *  removed calls ``onLeave`` instead of routing to ``/dms``. */
  embedded?: boolean
  /** The thread header (status line, mute, group info, calls). */
  showHeader?: boolean
  /** The composer's attach menu (photo / video / file, location). */
  allowAttachments?: boolean
  /** The group-info button and dialog on a group conversation. */
  showGroupInfo?: boolean
  /** The call button, the call-history link and "Call back" on missed
   *  calls. */
  allowCalls?: boolean
  /** The viewer left the conversation or was removed from it. The
   *  routed page (default) goes back to the chat list. */
  onLeave?: () => void
}

/** One conversation's thread — header, message list, composer — usable
 *  as the routed ``/dms/:id`` page (:func:`DmThreadPage`) or embedded in
 *  another surface. All state lives per instance and per conversation
 *  (see :class:`ThreadState`), so two views never share it. */
export function ConversationView({
  conversationId,
  embedded = false,
  showHeader = true,
  allowAttachments = true,
  showGroupInfo = true,
  allowCalls = true,
  onLeave,
}: ConversationViewProps) {
  const convId = conversationId
  const location = useLocation()
  /** Leave the thread after the viewer left / was removed. Read from the
   *  long-lived WS handlers through a ref so they don't re-subscribe. */
  // A fresh store per conversation: switching ``conversationId`` starts
  // clean, and a late write from the thread we left lands in its own,
  // no-longer-rendered store.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- ``convId`` is the reset key, not an input
  const s = useMemo(() => createThreadState(), [convId])
  const afterLeave = () => {
    if (onLeave) onLeave()
    else if (embedded) s.left.value = true
    else location.route('/dms')
  }
  const afterLeaveRef = useRef(afterLeave)
  afterLeaveRef.current = afterLeave
  const {
    messages, loading, hasMoreHistory, isLoadingOlder, unreadAnchor,
    newSinceScrollUp, composerHasContent, locationPickerOpen,
    pendingAttachment, uploadingAttachment, attachmentError,
    readMessageIds, deliveredMessageIds, memberCount, threadMembers,
    threadInfo, groupInfoOpen, replyTo, editing, contextSheetFor,
    reactionPickerFor, gaps,
  } = s
  /** The viewer muted / unmuted this thread (header bell or Group info). */
  const setThreadMute = (mutedUntil: string | null): void => {
    if (threadInfo.value) threadInfo.value = { ...threadInfo.value, muted_until: mutedUntil }
  }
  /** The viewer changed their group level (header bell or Group info). */
  const setThreadLevel = (level: 'all' | 'mentions'): void => {
    if (threadInfo.value) threadInfo.value = { ...threadInfo.value, notif_level: level }
  }
  // Composer ``<input>`` ref — STT (push-to-talk transcription) appends
  // its final transcript here so the user can review + edit before
  // sending. Uncontrolled input + ref keeps the existing FormData send
  // path untouched.
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null)
  /** Hidden ``<input type="file">`` driven by the paperclip button.
   *  Ref-managed so the on-click handler can call ``click()`` and
   *  open the system file picker without rendering the ugly default
   *  input chrome. */
  const attachInputRef = useRef<HTMLInputElement | null>(null)
  /** Scrolling container for the messages list.
   *
   *  The container is laid out with ``flex-direction: column-reverse``
   *  (see ``.sh-messages`` in ``app.css``), which inverts the
   *  scroll coordinate system: ``scrollTop=0`` is the visual
   *  **bottom** (latest message), and scrolling up *increases*
   *  ``scrollTop``. This is the classic chat-app trick — entry
   *  needs no positioning effect because the browser naturally
   *  lands the user at the bottom, new messages appear at the
   *  bottom without any JS scroll, and prepending older history
   *  doesn't move the user's viewport because ``scrollTop`` is
   *  anchored relative to the visual bottom (not the visual top).
   *  All the scroll-position-restoration math the old code carried
   *  goes away. */
  const messagesScrollRef = useRef<HTMLDivElement | null>(null)
  /** ``true`` when the user is within 80 px of the visual bottom —
   *  i.e. they're "looking at the live edge" of the conversation.
   *  Drives the jump-down CTA visibility and the read-watermark
   *  advance: marks-as-read fire only when the user is actually
   *  caught up. In column-reverse, "near the bottom" means
   *  ``scrollTop < 80`` (recall: scrollTop=0 is the visual bottom).
   *  Maintained by ``handleScroll``. */
  const stickToBottom = useRef(true)

  // Tag the body so the layout can hide the bottom tab bar and
  // full-bleed the thread — the chat surface should claim the whole
  // viewport. The class is scoped to the DM thread route via the
  // useEffect lifecycle. Page chrome — an embedded view leaves the
  // host surface's layout alone.
  useEffect(() => {
    if (embedded) return
    document.body.classList.add('sh-dm-thread-open')
    return () => document.body.classList.remove('sh-dm-thread-open')
  }, [embedded])

  // Tell the backend "I have this thread open right now" so inbound
  // DMs on this conversation skip the notification bell + push for
  // this user. Mirrors the backend's ``WebSocketManager
  // .set_active_conversation``; the SPA side only sends the frame
  // (no reply expected). Re-emitted on tab visibility flip so the
  // backend stops suppressing when the user tabs away — without
  // that, a backgrounded tab would silently swallow notifications
  // forever, which is worse than the original bug.
  //
  // An embedded view claims it too — an open embedded chat is a thread
  // the user is looking at. The backend tracks ONE active conversation
  // per connection, so the last view to open owns the claim
  // (``activeOwner``) and a view only clears it while it still owns it:
  // an older view unmounting must not un-suppress the one on screen.
  useEffect(() => {
    if (!convId) return
    const me = Symbol(convId)
    const claim = () => {
      activeOwner = me
      ws.send('dm.active', { conversation_id: convId })
    }
    const release = () => {
      if (activeOwner !== me) return
      activeOwner = null
      ws.send('dm.active', { conversation_id: null })
    }
    claim()
    const onVis = () => {
      if (document.visibilityState === 'visible') claim()
      else release()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      release()
    }
  }, [convId])

  const messageCount = messages.value.length
  const lastTailMessageId = useRef<string | null>(null)
  const isLoading = loading.value

  // Count new appended messages while the user is scrolled up so the
  // jump-down CTA can show "↓ N new". No scroll positioning here —
  // ``column-reverse`` handles "new messages naturally appear at the
  // visual bottom" for free; the only thing we need to track is
  // whether to surface the CTA. Self-sends set ``stickToBottom``
  // back to true in ``handleSend`` so they take the "user is at
  // bottom" branch and don't bump the counter. Prepended older
  // history (from ``loadOlder``) doesn't change the tail message id
  // so the ``tailChanged`` discriminator skips it.
  //
  // The ``previousTail !== null`` guard is the belt-and-suspenders
  // for "initial population isn't an arrival" — if any other effect
  // ever sets ``stickToBottom = false`` before this effect runs on
  // the first messages-loaded render (the anchor-scroll layout
  // effect used to do exactly that), without the guard the counter
  // would tick to 1 against an "arrival" that's just the freshly-
  // mounted thread loading its own history. Same bug class as the
  // chip-at-bottom regression — pinned defensively.
  useEffect(() => {
    const tailId = messages.value.length > 0
      ? messages.value[messages.value.length - 1].id
      : null
    const previousTail = lastTailMessageId.current
    const tailChanged =
      tailId !== null
      && previousTail !== null
      && tailId !== previousTail
    lastTailMessageId.current = tailId
    if (!tailChanged || stickToBottom.current) return
    newSinceScrollUp.value += 1
  // eslint-disable-next-line react-hooks/exhaustive-deps -- store signals are fixed per ``convId``
  }, [messageCount])

  // Entry-scroll layout effect — only meaningful when there's an
  // unread anchor. With column-reverse, the no-anchor case is
  // automatic: the browser lands the user at ``scrollTop=0`` (visual
  // bottom = latest message). For the unread-anchor case we still
  // need to bring the "New messages" divider into view, so we scroll
  // up until the divider is at the top of the viewport.
  // ``useLayoutEffect`` runs after Preact's DOM mutations but before
  // the browser paints, so the user never sees the intermediate
  // scrollTop=0 frame (which would show the latest message instead
  // of the divider).
  //
  // The effect is deliberately limited to DOM reads + writes:
  // ``scrollIntoView`` + ``stickToBottom.current``. Side effects
  // (clearing the anchor, resetting the counter, posting the read
  // watermark) live in the follow-up ``useEffect`` below — those
  // don't belong in a paint-blocking layout phase.
  const anchor = unreadAnchor.value
  /** Set by the layout effect when the entry-scroll landed at the
   *  live edge — read by the follow-up effect to fire side effects.
   *  A ref (not a signal) avoids an extra re-render between the
   *  layout commit and the post-paint side-effect commit. */
  const entryLandedAtLiveEdge = useRef(false)
  useLayoutEffect(() => {
    entryLandedAtLiveEdge.current = false
    if (isLoading || !anchor) return
    const el = messagesScrollRef.current
    if (!el) return
    const divider = el.querySelector('.sh-dm-unread-divider')
    if (divider) {
      divider.scrollIntoView({ block: 'start', behavior: 'instant' })
    } else {
      // Race fallback — anchor row rendered but divider didn't.
      const row = el.querySelector(`[data-msg-id="${anchor.message_id}"]`)
      if (row) row.scrollIntoView({ block: 'start', behavior: 'instant' })
    }
    // Re-derive ``stickToBottom`` from the position the anchor scroll
    // actually landed on, using the same threshold ``handleScroll``
    // applies on user scroll. An earlier version of this effect
    // hard-coded ``stickToBottom = false``, which lit up the
    // "↓ N new messages" chip even when the entry-scroll didn't move
    // the viewport — typical for a notification-driven entry where
    // the single unread message is the latest one and the column-
    // reverse default already shows it at the visual bottom.
    stickToBottom.current = isAtLiveEdge(el)
    entryLandedAtLiveEdge.current = stickToBottom.current
  }, [convId, isLoading, anchor?.message_id])

  // Follow-up to the layout effect above: when the entry-scroll
  // landed us at the live edge, the unread message is already in
  // view — clear the divider + counter and stamp the read watermark
  // so the next entry starts clean. Without this the chip would
  // still surface on the very next inbound message (the tail-
  // tracking effect would bump it, even though the user has seen
  // everything). Lives in a regular ``useEffect`` so the
  // fire-and-forget ``api.post`` and signal writes happen *after*
  // paint, not during the layout commit.
  useEffect(() => {
    if (!entryLandedAtLiveEdge.current) return
    if (newSinceScrollUp.value !== 0) newSinceScrollUp.value = 0
    if (unreadAnchor.value) unreadAnchor.value = null
    if (readReceiptsEnabled.value) {
      api.post(`/api/conversations/${convId}/read`).catch(() => {})
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- store signals are fixed per ``convId``; ``anchor`` is tracked by its id
  }, [convId, isLoading, anchor?.message_id])

  useEffect(() => {
    // Staleness guard for this effect run. ``convId`` changes re-run the
    // effect, but the requests fired for the thread we just left are
    // still in flight. Each thread has its own store, so a late write
    // can't reach the new thread's signals — but the flag still stops a
    // dead run from doing work (read POSTs, roster writes) for a thread
    // that is no longer on screen. The cleanup flips it, so each
    // continuation re-checks "am I still the active thread?" first.
    let cancelled = false
    // Mark this store's thread as the one on screen for the
    // out-of-effect callbacks (``loadOlder``). Must land before the
    // first ``await`` anywhere below.
    s.active = true
    loading.value = true
    // Reset the lazy-load + anchor state for the new thread. Without
    // this a re-entry would inherit the previous thread's divider or
    // "no more history" flag, both wrong for the new context.
    hasMoreHistory.value = true
    isLoadingOlder.value = false
    unreadAnchor.value = null
    newSinceScrollUp.value = 0
    composerHasContent.value = false
    pendingAttachment.value = null
    attachmentError.value = null
    // Reset messages eagerly so the brief moment between the convId
    // change and the new fetch's ``then`` doesn't flash the previous
    // thread's content. Column-reverse means the empty list shows
    // an empty container at scrollTop=0 (visual bottom) and the
    // loading pill below.
    messages.value = []
    stickToBottom.current = true
    // Read this thread's own row (``GET /api/conversations/{id}``, the
    // inbox-list row shape) for ``unread`` + ``last_read_at`` — the SPA
    // uses both to size the initial message window (so the first-unread
    // message is in the window) and to anchor the entry scroll on a
    // "New messages" divider — plus the header metadata. A missed
    // lookup (403 / 5xx) falls back to "no anchor, no unreads"
    // gracefully.
    let unreadHint = 0
    let lastReadAt: string | null = null
    threadInfo.value = null
    groupInfoOpen.value = false
    const summaryPromise = api.get(`/api/conversations/${convId}`).then(
      (row: ConversationRow | null) => {
        const info = toThreadInfo(row)
        if (row && info) {
          unreadHint = Math.max(0, row.unread ?? 0)
          lastReadAt = row.last_read_at ?? null
          if (!cancelled) threadInfo.value = info
        }
      },
    ).catch(() => {
      /* fall through with the defaults */
    })

    summaryPromise.then(() => {
      if (cancelled) return
      // Window size: enough to overflow the viewport comfortably (so
      // the user can actually scroll up and trigger ``loadOlder``),
      // but small enough that the entry skeleton-to-content swap
      // doesn't read as a long jump. 25 messages is the floor —
      // ~1600 px of content vs. a typical 600-800 px container,
      // giving a healthy ~1000 px of scroll-up headroom. Unread
      // spike widens further so the first-unread divider always
      // lands in the loaded set; capped at the backend's 100/request
      // ceiling so a very busy thread doesn't pay for a giant
      // payload on entry. The skeleton's ``justify-content: flex-end``
      // (see app.css) places its placeholder bubbles at the same
      // screen position as the real bottom-of-thread, so the swap
      // looks like a fade-in, not a scroll.
      const limit = Math.min(Math.max(unreadHint + 5, 25), 100)
      api.get(`/api/conversations/${convId}/messages?limit=${limit}`).then(
        data => {
          if (cancelled) return
          const msgs: Message[] = (data ?? []).slice().reverse()
          messages.value = msgs
          loading.value = false
          // If we got fewer than ``limit`` back, the thread is shorter
          // than the window — no older history to fetch.
          hasMoreHistory.value = (data ?? []).length === limit
          // Pick the first-unread message in the loaded window. A
          // message counts as unread when it was created strictly after
          // the caller's ``last_read_at`` AND was not authored by the
          // caller (a user's own message can't be "unread" to them).
          if (lastReadAt && unreadHint > 0) {
            const myId = currentUser.value?.user_id
            // ``normaliseTimestamp`` tags the naive SQLite shape
            // ("YYYY-MM-DD HH:MM:SS", no Z) as UTC before parsing —
            // without this, viewers in a non-UTC zone see V8 interpret
            // the naive string as *their* local wall clock and shift
            // ``lastReadMs`` by the UTC offset, landing the divider on
            // the wrong message (or no divider at all). The message's
            // own ``created_at`` is already tz-aware ISO but routing
            // both sides through the same helper keeps the math
            // consistent if that ever drifts.
            const lastReadMs = Date.parse(normaliseTimestamp(lastReadAt))
            if (!Number.isNaN(lastReadMs)) {
              const firstUnread = msgs.find(m =>
                m.sender_user_id !== myId
                && Date.parse(normaliseTimestamp(m.created_at)) > lastReadMs,
              )
              if (firstUnread) {
                unreadAnchor.value = { message_id: firstUnread.id }
              }
            }
          }
          // If there were no unreads (or no last_read_at), entry will
          // scroll to bottom — mark-as-read on entry stays unchanged for
          // that case. When there ARE unreads we defer the read POST
          // until the user actually scrolls past the divider (see the
          // ``handleScroll`` branch); marking on entry would advance the
          // watermark before the user has seen anything and the next
          // entry wouldn't render the divider.
          if (!unreadAnchor.value && readReceiptsEnabled.value) {
            api.post(`/api/conversations/${convId}/read`).catch(() => {})
          }
        },
        // Second argument, NOT a trailing ``.catch`` — this branch must
        // see *only* a rejected fetch. A trailing catch would also
        // swallow anything thrown by the success handler above and
        // respond by blanking the thread, so a bug in the anchor math
        // presented to the user as "this conversation is empty".
        () => {
          if (cancelled) return
          // Network blip or 5xx — don't strand the user on the skeleton
          // forever. The thread page renders an empty list (which the
          // existing empty-state copy handles) and the next entry
          // retries; surfacing a toast would be louder than necessary
          // for a transient backend glitch.
          loading.value = false
          messages.value = []
          hasMoreHistory.value = false
        },
      ).catch(err => {
        // A throw out of the success handler — our own bug, not a
        // transient backend glitch (that one is handled above). Clear
        // the skeleton so it can't strand, but leave ``messages`` /
        // ``hasMoreHistory`` alone: the fetched thread is already on
        // screen and blanking what the user is reading is strictly
        // worse than a partially-applied load. Log it so the failure
        // is diagnosable instead of silent.
        if (cancelled) return
        loading.value = false
        console.error(
          `[ConversationView] messages load handler threw for conversation ${convId}`,
          err,
        )
      })
      // Hydrate delivery/read state for every message so ticks render
      // immediately — not just on messages we've seen WS frames for.
      api.get(`/api/conversations/${convId}/delivery-states`).then(
        (body: { states: DeliveryState[] }) => {
          if (cancelled) return
          const delivered = new Set<string>()
          const read = new Set<string>()
          for (const s of body.states || []) {
            if (s.state === 'read') read.add(s.message_id)
            else if (s.state === 'delivered') delivered.add(s.message_id)
          }
          deliveredMessageIds.value = delivered
          readMessageIds.value = new Set([...readMessageIds.value, ...read])
        },
      ).catch(() => {})
      // Poll for open sequence gaps — tiny endpoint, once per thread load.
      api.get(`/api/conversations/${convId}/gaps`).then(
        (body: { gaps: MessageGap[] }) => {
          if (cancelled) return
          gaps.value = body.gaps || []
        },
      ).catch(() => {
        if (cancelled) return
        gaps.value = []
      })
    })
    // Roster fetch — drives the call-button visibility (member_count) AND
    // the WhatsApp-style "Online" / "Last seen 2 h ago" status line in the
    // thread header. Live-patched below by the user.online/idle/offline
    // WS frames so the header stays current without polling.
    api.get(`/api/conversations/${convId}/members`).then((rows: ThreadMember[]) => {
      if (cancelled) return
      threadMembers.value = rows
      memberCount.value = rows.length || 2
      // Seeds the @-mention picker + highlights: no second roster fetch.
      setConversationMembers(convId, rows)
    }).catch(() => {
      if (cancelled) return
      threadMembers.value = []
      memberCount.value = 2
    })

    const offNewMsg = ws.on('dm.message', (evt) => {
      const data = evt.data as { conversation_id?: string; message?: Message }
      if (data.conversation_id !== convId || !data.message) return
      const msg = data.message
      const mine = msg.sender_user_id === currentUser.value?.user_id
      // Strip any optimistic ``tmp-…`` row from the sender that's still
      // hanging around: the WS broadcast for our own send can race the
      // POST response back, and ``handleSend``'s id-swap only fires
      // once the response lands. Match on content (same sender + same
      // text) to avoid leaving the temp bubble next to the canonical
      // one. Other users' messages skip this branch entirely.
      let next = messages.value
      if (mine) {
        next = next.filter(m =>
          !(typeof m.id === 'string'
            && m.id.startsWith('tmp-')
            && m.content === msg.content),
        )
      }
      if (!next.some(m => m.id === msg.id)) {
        next = [...next, msg]
        if (!mine && readReceiptsEnabled.value) {
          // Ack delivery as soon as the frame lands. The server upsert
          // is idempotent; a later ``read`` supersedes.
          api.post(
            `/api/conversations/${convId}/messages/${msg.id}/delivered`,
          ).catch(() => {})
        }
      }
      if (next !== messages.value) messages.value = next
      // Only advance the watermark when the user is actually at the
      // bottom looking at the live edge — if they're scrolled up
      // reading historic context above the "New messages" divider,
      // the inbound message has NOT been seen yet, and marking it
      // as read here would defeat the deferred-read design (next
      // entry would see ``unread = 0`` and skip the divider).
      // ``stickToBottom`` is the same flag ``handleScroll`` maintains;
      // a self-send always satisfies it because ``handleSend`` flips
      // it to true before the optimistic append. The
      // sticky-bottom transition branch in ``handleScroll`` covers
      // the "user scrolls down to catch up" path.
      if (readReceiptsEnabled.value && stickToBottom.current) {
        api.post(`/api/conversations/${convId}/read`).catch(() => {})
      }
    })
    // Live-patch the thread-member roster on session-presence frames so
    // the header status line stays current.
    const patchMember = (
      user_id: string,
      next: { is_online: boolean; is_idle: boolean; last_seen_at?: string | null },
    ) => {
      threadMembers.value = threadMembers.value.map(m =>
        m.user_id === user_id
          ? {
              ...m,
              is_online: next.is_online,
              is_idle: next.is_idle,
              ...(next.last_seen_at !== undefined ? { last_seen_at: next.last_seen_at } : {}),
            }
          : m,
      )
    }
    // Cross-household media-ready swap. When the receiver's instance
    // finishes ingesting the full ``DM_MEDIA_BLOB`` (which arrives
    // asynchronously some time after the ``DM_MESSAGE`` envelope
    // that carried the small preview), the backend publishes this
    // frame to every local participant. We swap ``media_url`` to
    // the full URL and clear ``media_sync_status`` so the bubble's
    // brightness pulse goes away.
    const offMediaReady = ws.on('dm.media_ready', (e) => {
      const d = e.data as {
        conversation_id?: string
        message_id?: string
        media_url?: string
      }
      if (d.conversation_id !== convId) return
      if (!d.message_id || !d.media_url) return
      const list = messages.value
      const idx = list.findIndex(m => m.id === d.message_id)
      if (idx < 0) return
      const next = list.slice()
      next[idx] = {
        ...next[idx],
        media_url: d.media_url,
        media_sync_status: null,
      }
      messages.value = next
    })
    // In-place content updates. Today this drives voice-note
    // transcripts: the sender's STT (or the recipient's local
    // fallback) lands after the audio bubble has been rendered, and
    // the receiver's bubble swaps "Transcribing…" for the actual
    // transcript without a re-render. Future edits (composer edit
    // flow) will reuse the same frame shape.
    const offMessageUpdated = ws.on('dm.message_updated', (e) => {
      const d = e.data as {
        conversation_id?: string
        message_id?: string
        content?: string
        edited_at?: string | null
      }
      if (d.conversation_id !== convId) return
      if (!d.message_id) return
      const list = messages.value
      const idx = list.findIndex(m => m.id === d.message_id)
      if (idx < 0) return
      const next = list.slice()
      next[idx] = {
        ...next[idx],
        content: d.content ?? next[idx].content,
        edited_at: d.edited_at ?? next[idx].edited_at,
      }
      messages.value = next
    })
    // Reaction add / remove from any session — sender's own
    // sessions get the frame too so a mobile + desktop mirror stay
    // in lockstep. The optimistic patch in ``toggleReaction`` is
    // idempotent against this echo (same emoji + same user_id =
    // no-op net change).
    const offReaction = ws.on('dm.message_reaction', (e) => {
      const d = e.data as {
        conversation_id?: string
        message_id?: string
        user_id?: string
        emoji?: string
        action?: 'add' | 'remove'
      }
      if (d.conversation_id !== convId) return
      if (!d.message_id || !d.user_id || !d.emoji) return
      const list = messages.value
      const idx = list.findIndex(m => m.id === d.message_id)
      if (idx < 0) return
      const current = list[idx].reactions ?? []
      const exists = current.some(
        r => r.user_id === d.user_id && r.emoji === d.emoji,
      )
      let nextReactions = current
      if (d.action === 'remove' && exists) {
        nextReactions = current.filter(
          r => !(r.user_id === d.user_id && r.emoji === d.emoji),
        )
      } else if (d.action !== 'remove' && !exists) {
        nextReactions = [
          ...current,
          { user_id: d.user_id, emoji: d.emoji },
        ]
      } else {
        return // already in the target state
      }
      const next = list.slice()
      next[idx] = { ...next[idx], reactions: nextReactions }
      messages.value = next
    })
    // A group's member list or name changed (here or on its home
    // household): refetch. A 403 means we're no longer in it.
    const offGroupUpdated = ws.on('dm.group.updated', (e) => {
      const d = e.data as { conversation_id?: string }
      if (d.conversation_id !== convId) return
      void fetchThreadInfo(s, convId)
      void fetchRoster(s, convId).then((stillIn) => {
        if (stillIn || cancelled) return
        groupInfoOpen.value = false
        showToast(t('dms.removed_from_group'), 'info')
        afterLeaveRef.current()
      })
    })
    const offUserOnline = ws.on('user.online', (e) => {
      const d = e.data as { user_id?: string }
      if (d.user_id) patchMember(d.user_id, { is_online: true, is_idle: false })
    })
    const offUserIdle = ws.on('user.idle', (e) => {
      const d = e.data as { user_id?: string }
      if (d.user_id) patchMember(d.user_id, { is_online: true, is_idle: true })
    })
    const offUserOffline = ws.on('user.offline', (e) => {
      const d = e.data as { user_id?: string; last_seen_at?: string | null }
      if (d.user_id) patchMember(d.user_id, {
        is_online: false,
        is_idle: false,
        last_seen_at: d.last_seen_at ?? null,
      })
    })
    return () => {
      cancelled = true
      // This store's thread is off screen once this runs — a page that
      // lands now has nowhere it legitimately belongs.
      s.active = false
      offNewMsg(); offMediaReady(); offMessageUpdated()
      offReaction(); offGroupUpdated()
      offUserOnline(); offUserIdle(); offUserOffline()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the store's signals are fixed per ``convId`` (``s`` is memoised on it)
  }, [convId])

  let typingTimer: ReturnType<typeof setTimeout> | null = null

  /** Grow the composer textarea to fit its content up to a hard cap,
   *  then scroll internally. Called on every input event + after send
   *  / reset to land the height back at one line. ``scrollHeight`` is
   *  the layout height required to show all content; assigning ``auto``
   *  first lets it shrink when the user deletes lines. */
  const autoResize = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto'
    // Under ``box-sizing: border-box`` the textarea's ``height``
    // includes its 2 px top + bottom border, but ``scrollHeight``
    // does not. Setting ``height = scrollHeight`` therefore leaves
    // the content area 2 px short of the natural row size and
    // triggers ``overflow-y: auto``'s scrollbar at every rest
    // point. Add the border delta so a 1-row composer stays
    // scrollbar-free; the cap still applies past the limit.
    const borderDelta = el.offsetHeight - el.clientHeight
    const target = Math.min(
      el.scrollHeight + borderDelta,
      MAX_COMPOSER_HEIGHT_PX,
    )
    el.style.height = `${target}px`
    // Toggle the vertical scrollbar: visible only when the user has
    // typed past the height cap. On Linux Chrome (classic
    // scrollbars) ``overflow-y: auto`` always reserves a 17 px
    // gutter on the right, eating into the input width and making
    // tiny step arrows show at rest. Flipping to ``hidden`` below
    // the cap reclaims that gutter for the text; only the rare
    // "drafting a screenplay in the chat bar" case still gets a
    // scrollbar.
    el.style.overflowY = target >= MAX_COMPOSER_HEIGHT_PX ? 'auto' : 'hidden'
  }

  /** Drive a file picker → ``/api/media/upload`` → composer chip.
   *
   *  The attach button on the composer triggers a hidden ``<input
   *  type="file">``; on change we upload via the shared
   *  :func:`uploadWithProgress` helper (which already shows the
   *  global upload progress bar) and stash the result on
   *  ``pendingAttachment``. The chip above the textarea then renders
   *  the preview, and the next send carries ``type`` /
   *  ``media_url`` / ``file_name`` / ``mime_type`` /
   *  ``file_size_bytes`` instead of an empty body. */
  const handleAttachPicked = (e: Event) => {
    const input = e.currentTarget as HTMLInputElement
    const file = input.files?.[0]
    // Reset the input so picking the same file again re-fires
    // ``onchange`` (browsers de-dup identical paths otherwise).
    input.value = ''
    if (!file) return
    void _startAttachmentUpload(file)
  }

  /** Drives the file → ``uploadingAttachment`` (in-flight) →
   *  ``pendingAttachment`` (ready) progression. Extracted so the
   *  chip's Retry button can replay it on the same File without
   *  re-opening the picker. */
  const _startAttachmentUpload = async (file: File) => {
    attachmentError.value = null
    const kind: 'image' | 'video' | 'file' =
      file.type.startsWith('image/') ? 'image'
      : file.type.startsWith('video/') ? 'video'
      : 'file'
    // Local blob URL for the chip preview — the user sees what
    // they're sending the instant they pick the file, not after the
    // upload + server processing finishes.
    const localPreview = (kind === 'image' || kind === 'video')
      ? URL.createObjectURL(file)
      : null
    uploadingAttachment.value = {
      kind,
      filename: file.name,
      fileSize: file.size,
      previewUrl: localPreview,
      phase: 'uploading',
      percent: 0,
      file,
    }
    try {
      const res = await uploadWithProgress(file, (ev) => {
        // Phase + percent updates feed the chip's progress ring + the
        // "Processing image…" / "Processing video…" copy.
        const cur = uploadingAttachment.value
        if (!cur || cur.file !== file) return  // user cancelled / replaced
        if (ev.phase === 'uploading' || ev.phase === 'processing') {
          uploadingAttachment.value = {
            ...cur,
            phase: ev.phase,
            percent: ev.percent,
          }
        }
      })
      // Upload succeeded → promote to ``pendingAttachment``, free the
      // local blob URL (the chip will use the server-signed preview).
      if (localPreview) URL.revokeObjectURL(localPreview)
      uploadingAttachment.value = null
      pendingAttachment.value = {
        type: kind,
        preview_url: res.signed_url,
        media_url: res.url,
        file_name: file.name,
        mime_type: file.type || 'application/octet-stream',
        file_size_bytes: file.size,
      }
      composerHasContent.value = true
    } catch (err: unknown) {
      const message = (err as Error)?.message ?? String(err)
      // Leave the chip rendered with a ``failed`` phase + retry
      // button — the user can re-try without picking the file again.
      // Keeping the local preview alive too.
      uploadingAttachment.value = uploadingAttachment.value
        ? {
            ...uploadingAttachment.value,
            phase: 'failed',
            errorMessage: message,
          }
        : null
      attachmentError.value = t('dms.upload_failed', { name: file.name, error: message })
    }
  }

  /** Cancel the in-flight upload's chip (and free the local blob).
   *  The XHR keeps running in the background — there's no clean abort
   *  hook today, but the per-call event handler bails on a mismatched
   *  ``file`` reference so a successful response after cancel is
   *  silently ignored. */
  const _cancelUpload = () => {
    const cur = uploadingAttachment.value
    if (!cur) return
    if (cur.previewUrl) URL.revokeObjectURL(cur.previewUrl)
    uploadingAttachment.value = null
    attachmentError.value = null
  }

  /** Send a freshly-recorded voice note (OGG/Opus blob).
   *
   *  Unlike the paperclip flow, voice notes skip the "stage on the
   *  composer chip then hit Send" step — the user already committed
   *  to sending by releasing the mic. We upload the blob and POST
   *  the message in one go, with an optimistic ``tmp-`` bubble so
   *  the audio appears instantly. The transcript fills in later
   *  via :type:`dm.message_updated` (sender-side STT) or the
   *  receiver's local fallback. */
  const handleVoiceNote = async (blob: Blob) => {
    if (!convId) return
    attachmentError.value = null
    // Pick the extension + bare MIME from the recorder's chosen
    // container. Firefox → OGG/Opus, Chromium → WebM/Opus, Safari
    // → MP4/AAC. The backend ``AudioProcessor`` accepts all three.
    const bareMime = (blob.type.split(';')[0] || 'audio/webm').trim()
    const ext =
      bareMime === 'audio/ogg' ? 'ogg'
      : bareMime === 'audio/mp4' ? 'm4a'
      : 'webm'
    const fileName = `voice-note-${Date.now()}.${ext}`
    const file = new File([blob], fileName, { type: bareMime })

    const tempId = newTempId()
    const myUid = currentUser.value?.user_id ?? ''
    const previewUrl = URL.createObjectURL(blob)
    const optimistic: Message = {
      id: tempId,
      sender_user_id: myUid,
      // Empty transcript — the AudioBubble shows "Transcribing…"
      // until the WS frame swaps it for the real text.
      content: '',
      type: 'audio',
      media_url: previewUrl,
      file_name: fileName,
      mime_type: bareMime,
      file_size_bytes: blob.size,
      media_sync_status: null,
      reply_to_id: null,
      deleted: false,
      created_at: new Date().toISOString(),
      edited_at: null,
    }
    messages.value = [...messages.value, optimistic]
    const scrollEl = messagesScrollRef.current
    if (scrollEl) scrollEl.scrollTop = 0
    stickToBottom.current = true
    newSinceScrollUp.value = 0
    if (unreadAnchor.value) unreadAnchor.value = null

    try {
      const res = await uploadWithProgress(file)
      const sent = (await api.post(
        `/api/conversations/${convId}/messages`,
        {
          type: 'audio',
          content: '',
          media_url: res.url,
          file_name: fileName,
          mime_type: bareMime,
          file_size_bytes: blob.size,
        },
      )) as { id: string }
      // Reconcile temp → real id. Same shape as ``handleSend``.
      const list = messages.value
      const realExists = list.some(m => m.id === sent.id)
      messages.value = realExists
        ? list.filter(m => m.id !== tempId)
        : list.map(m =>
            m.id === tempId
              ? { ...m, id: sent.id, media_url: res.signed_url }
              : m,
          )
    } catch (err: unknown) {
      messages.value = messages.value.filter(m => m.id !== tempId)
      const code = (err as { code?: string })?.code
      if (code === 'MEDIA_REQUIRES_DIRECT_PAIRING') {
        attachmentError.value =
          t('dms.voice_direct_only')
      } else {
        attachmentError.value =
          t('dms.voice_send_failed', { error: String((err as Error)?.message ?? err) })
      }
    } finally {
      try { URL.revokeObjectURL(previewUrl) } catch { /* already gone */ }
    }
  }

  /** Recompute the mic⇄send slot flag from the textarea's current
   *  value. A staged attachment also counts as "ready to send". Call
   *  this after any *programmatic* mutation of ``ta.value`` — setting
   *  ``.value`` in code does NOT fire ``onInput``, so the flag would
   *  otherwise go stale (e.g. an emoji inserted via the picker as the
   *  first character would leave the mic button up instead of Send). */
  const syncComposerHasContent = (ta: HTMLTextAreaElement) => {
    composerHasContent.value =
      ta.value.trim().length > 0 || pendingAttachment.value !== null
  }

  /** Replace ``ta.value[start:end]`` with ``emoji`` and place the caret
   *  immediately after the inserted glyph. Shared by the
   *  ``:shortcode`` autocomplete (range = the typed token) and the
   *  ``EmojiPickButton`` (range = current caret position). */
  const spliceEmojiIntoTextarea = (emoji: string, range: [number, number]) => {
    const ta = composerInputRef.current
    if (!ta) return
    const [start, end] = range
    const before = ta.value.slice(0, start)
    const after = ta.value.slice(end)
    ta.value = before + emoji + after
    autoResize(ta)
    syncComposerHasContent(ta)
    requestAnimationFrame(() => {
      if (!composerInputRef.current) return
      composerInputRef.current.focus()
      const pos = (before + emoji).length
      composerInputRef.current.setSelectionRange(pos, pos)
    })
  }

  /** Picker-button entry point — inserts at the current caret position. */
  const insertEmojiAtCursor = (emoji: string) => {
    const ta = composerInputRef.current
    if (!ta) return
    const pos = ta.selectionStart ?? ta.value.length
    spliceEmojiIntoTextarea(emoji, [pos, pos])
  }

  /** Slack-style ``:foo:`` → glyph: scan the textarea value for closed
   *  shortcode tokens (``:heart:``, ``:smile:``, …) and replace each
   *  one with the matching emoji glyph in place. Runs on every input
   *  event so the user gets immediate feedback the moment they type
   *  the closing colon. Returns the column-shift the caret should
   *  receive (number of characters removed before the caret). */
  const convertShortcodes = (ta: HTMLTextAreaElement) => {
    const before = ta.value
    const caret = ta.selectionStart ?? before.length
    let shiftBeforeCaret = 0
    const next = before.replace(
      /(^|[^a-zA-Z0-9_]):([a-zA-Z0-9_+-]+):/g,
      (match, lead: string, code: string, offset: number) => {
        const glyph = emojiByShortcode(code)
        if (!glyph) return match
        const removed = match.length - (lead.length + glyph.length)
        // Only the bytes BEFORE the caret shift its position. Tokens
        // that sit *after* the caret are still substituted but don't
        // move the caret.
        if (offset + match.length <= caret) shiftBeforeCaret += removed
        return lead + glyph
      },
    )
    if (next === before) return
    ta.value = next
    autoResize(ta)
    const newCaret = Math.max(0, caret - shiftBeforeCaret)
    ta.setSelectionRange(newCaret, newCaret)
  }

  const handleInput = (e: Event) => {
    const ta = e.currentTarget as HTMLTextAreaElement
    autoResize(ta)
    convertShortcodes(ta)
    // Drive the mic⇄send slot swap. Strip whitespace so a textarea
    // holding only spaces / newlines still reads as "empty" — the
    // user shouldn't see the send button until they actually have
    // something to send. A staged attachment also counts as "ready
    // to send" so a picture-with-no-caption keeps the Send button
    // visible. ``trim`` is cheap relative to the keystroke cadence;
    // no need to memo.
    syncComposerHasContent(ta)
    // Slack-style ``:partial`` autocomplete — fires after the
    // close-colon substitution above so a fully-typed ``:heart:`` never
    // opens the dropdown (the glyph is already in place).
    checkForEmojiTrigger(
      ta.value,
      ta.selectionStart ?? 0,
      ta,
      spliceEmojiIntoTextarea,
    )
    // Group chats: ``@`` opens the member picker (a 1:1 has nobody else
    // to single out). Same text-range splice the emoji pick uses.
    checkForMentionTrigger(
      ta.value,
      ta.selectionStart ?? 0,
      ta,
      threadInfo.value?.type === 'group_dm' ? { conversationId: convId } : null,
      spliceEmojiIntoTextarea,
    )
    if (typingTimer) return
    sendTyping(convId)
    typingTimer = setTimeout(() => { typingTimer = null }, 2000)
  }

  /** Send-on-Enter behaviour, branched by pointer kind:
   *
   *  - Desktop ``(pointer: fine)``: Enter submits, Shift+Enter inserts
   *    a newline. Same as Slack / Discord and WhatsApp Web.
   *  - Mobile ``(pointer: coarse)``: Enter inserts a newline, the user
   *    must tap the Send button to send. Matches WhatsApp on iOS /
   *    Android — the on-screen keyboard's Return key is for
   *    line-breaks, not for sending half-typed thoughts by accident.
   *
   *  IME composition (``isComposing``) bypasses the override entirely
   *  so Enter still confirms a Japanese / Chinese candidate the way
   *  the user expects.
   *
   *  When the ``:foo`` emoji autocomplete is open we hand the key off
   *  to it first so Enter / Tab / arrow keys drive the dropdown rather
   *  than the form submit.
   */
  const handleComposerKeyDown = (e: KeyboardEvent) => {
    if (handleMentionAutocompleteKey(e) || handleEmojiAutocompleteKey(e)) {
      e.preventDefault()
      return
    }
    if (e.key !== 'Enter') return
    if (e.shiftKey) return  // explicit "give me a newline"
    if (e.isComposing) return  // IME — let the input swallow Enter
    const coarsePointer =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(pointer: coarse)').matches
    if (coarsePointer) return  // touch → newline, send via button
    e.preventDefault()
    const ta = e.currentTarget as HTMLTextAreaElement
    ta.form?.requestSubmit()
  }

  const handleSend = (e: Event) => {
    e.preventDefault()
    const form = e.target as HTMLFormElement
    const content = (new FormData(form).get('content') as string ?? '').trim()
    const attachment = pendingAttachment.value
    // Either a caption or a staged attachment is required — both
    // empty means nothing to send. Also doubles as a debounce against
    // accidental double-click + accidental Enter+Enter: the second
    // submit lands after ``form.reset()`` has cleared the composer,
    // so ``content`` is empty and we bail.
    if (!content && !attachment) return
    const reply_to_id = replyTo.value?.id ?? null

    // **Optimistic append** — render the user's bubble immediately
    // and let the composer stay responsive so the next message can
    // be typed without waiting for the previous POST round-trip.
    // The POST runs in the background; ``form.reset()`` clears the
    // composer in the same frame, and the canonical row (real id,
    // server timestamp) arrives via the WS broadcast a moment later
    // and de-dupes by ``id`` (see ``offNewMsg`` above — it also strips
    // any leftover ``tmp-`` row from the sender to avoid showing the
    // bubble twice if the WS frame races the POST response). On
    // failure the optimistic bubble flips to ``send_failed=true`` and
    // surfaces a ⚠ glyph; the toast points the user at the cause and
    // they can re-type without losing whatever they were drafting
    // *next* (the old flow restored the failed draft into the
    // textarea, which clobbered an in-progress follow-up message).
    const tempId = newTempId()
    const myUid = currentUser.value?.user_id ?? ''
    const optimistic: Message = {
      id: tempId,
      sender_user_id: myUid,
      content,
      type: attachment ? attachment.type : 'text',
      media_url: attachment ? attachment.preview_url : null,
      file_name: attachment?.file_name ?? null,
      mime_type: attachment?.mime_type ?? null,
      file_size_bytes: attachment?.file_size_bytes ?? null,
      media_sync_status: null,
      reply_to_id,
      deleted: false,
      created_at: new Date().toISOString(),
      edited_at: null,
    }
    messages.value = [...messages.value, optimistic]
    // Pin the viewport to the latest message on self-send. In
    // column-reverse, ``scrollTop = 0`` is the visual bottom — any
    // earlier scroll-up gesture (even a stray touch nudge that left
    // scrollTop at a small negative value) would otherwise hide the
    // user's own bubble just below the visible area. Also flip
    // ``stickToBottom`` to true and clear the unread-since-scroll-up
    // counter so the jump-down CTA disappears for sends that
    // happened to fire while the user was reading history.
    pinToLatest()

    const draftAttachment = attachment
    form.reset()
    // Composer is now empty — flip back to the mic slot. Also clear
    // the staged attachment chip; the optimistic bubble already
    // shows the user that the send is on its way.
    composerHasContent.value = false
    pendingAttachment.value = null
    attachmentError.value = null
    // ``form.reset()`` clears the value but leaves the explicit
    // ``style.height`` from a previous autoResize call, so the
    // composer would stay tall after sending a multi-line draft.
    // Wait one frame so the cleared value has settled through
    // layout, then re-run ``autoResize`` on the empty textarea —
    // its ``scrollHeight`` is now the natural one-line height and
    // the composer collapses back down. Without the rAF the
    // ``scrollHeight`` read still returns the pre-reset content's
    // dimensions and the bar stays inflated.
    const ta0 = composerInputRef.current
    if (ta0) {
      requestAnimationFrame(() => autoResize(ta0))
    }
    replyTo.value = null
    // Fire-and-forget the POST. Reconcile happens in
    // ``dispatchSend`` — failures mark the optimistic bubble as
    // ``send_failed`` rather than restoring the draft into the
    // textarea, because the user may already be typing the next
    // message by the time the failure lands.
    void dispatchSend(tempId, {
      content,
      ...(reply_to_id ? { reply_to_id } : {}),
      // Attachment metadata. Backend ignores these fields on a
      // ``text`` send (``type`` defaults to "text"), so we only
      // include them when an attachment is actually staged.
      ...(draftAttachment ? {
        type: draftAttachment.type,
        media_url: draftAttachment.media_url,
        file_name: draftAttachment.file_name,
        mime_type: draftAttachment.mime_type,
        file_size_bytes: draftAttachment.file_size_bytes,
      } : {}),
    })
  }

  /** Mint the ``tmp-…`` id of an optimistic bubble. */
  const newTempId = (): string => `tmp-${
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }`

  /** Pin the viewport to the latest message on a self-send (see the
   *  long note in ``handleSend``). */
  const pinToLatest = () => {
    const scrollEl = messagesScrollRef.current
    if (scrollEl) scrollEl.scrollTop = 0
    stickToBottom.current = true
    newSinceScrollUp.value = 0
    if (unreadAnchor.value) unreadAnchor.value = null
  }

  /** POST a message whose optimistic bubble ``tempId`` is already on
   *  screen, then reconcile it with the server row — or flip it to
   *  ``send_failed`` and toast. */
  const dispatchSend = async (
    tempId: string,
    body: Record<string, unknown>,
  ): Promise<void> => {
    try {
      const res = await api.post(
        `/api/conversations/${convId}/messages`, body,
      ) as { id: string }
      // Reconcile the optimistic row with the server-assigned id.
      //  • If the WS broadcast already landed (real ``id`` in the
      //    list) we just drop the temp.
      //  • Otherwise we swap the temp's id for the real one so a
      //    subsequent WS frame de-dupes naturally.
      const list = messages.value
      const realExists = list.some(m => m.id === res.id)
      messages.value = realExists
        ? list.filter(m => m.id !== tempId)
        : list.map(m => m.id === tempId ? { ...m, id: res.id } : m)
    } catch (err: unknown) {
      // Mark the optimistic bubble as failed in-place — the
      // composer is already free for the next message, so we
      // can't shove the draft back into the textarea without
      // clobbering whatever the user is typing now. The ⚠ on
      // the bubble + the toast cover the failure mode.
      const errMsg = (err as Error)?.message ?? String(err)
      const isPairingError = errMsg.includes('MEDIA_REQUIRES_DIRECT_PAIRING')
        || errMsg.toLowerCase().includes('directly-paired')
      messages.value = messages.value.map(m =>
        m.id === tempId
          ? {
              ...m,
              send_failed: true,
              send_failed_reason: isPairingError
                ? t('dms.media_direct_only')
                : t('dms.send_failed_retry'),
            }
          : m,
      )
      showToast(
        isPairingError
          ? t('dms.media_direct_only_short')
          : body.type === 'location'
            ? t('dms.location.send_failed', { error: errMsg })
            : t('dms.send_failed', { error: errMsg }),
        'error',
      )
    }
  }

  /** Attach menu → Location → picker confirmed. The picker's map was
   *  the preview; this appends the optimistic location bubble and
   *  sends it. Coordinates leave here rounded to 4 dp; the server
   *  re-rounds and buckets the accuracy before storing / federating. */
  const sendLocation = (draft: LocationDraft) => {
    locationPickerOpen.value = false
    const content = toDmLocationContent(draft)
    const reply_to_id = replyTo.value?.id ?? null
    const tempId = newTempId()
    const optimistic: Message = {
      id: tempId,
      sender_user_id: currentUser.value?.user_id ?? '',
      content,
      type: 'location',
      media_url: null,
      file_name: null,
      mime_type: null,
      file_size_bytes: null,
      media_sync_status: null,
      reply_to_id,
      deleted: false,
      created_at: new Date().toISOString(),
      edited_at: null,
    }
    messages.value = [...messages.value, optimistic]
    pinToLatest()
    replyTo.value = null
    void dispatchSend(tempId, {
      type: 'location',
      content,
      ...(reply_to_id ? { reply_to_id } : {}),
    })
  }

  /** Resolve sender display name from the roster — falls back to the raw
   *  user_id (which the rest of the thread also surfaces today). */
  const senderName = (user_id: string): string => {
    const m = threadMembers.value.find(x => x.user_id === user_id)
    // Someone who has since left the group (or was removed) is no longer
    // on the roster — never show their raw id.
    return m?.display_name ?? m?.username ?? t('dms.former_member')
  }

  /** One-line preview of a message's content for the quoted-reply card.
   *  Strips newlines and truncates to keep the bubble compact. */
  const quotePreview = (m: Message): string => {
    if (m.deleted) return '(message deleted)'
    if (m.type === 'location') {
      const loc = parseDmLocation(m.content)
      return loc?.label
        ? `${t('dms.location.quote')} · ${loc.label}`
        : t('dms.location.quote')
    }
    if (!m.content) return m.media_url ? '📎 Attachment' : ''
    const flat = m.content.replace(/\s+/g, ' ').trim()
    return flat.length > 80 ? `${flat.slice(0, 80)}…` : flat
  }

  /** Scroll the original message into view + flash it briefly so the
   *  reply quote is genuinely useful as a navigation handle. */
  const scrollToMessage = (id: string) => {
    const el = document.querySelector<HTMLElement>(`[data-msg-id="${id}"]`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.add('sh-message--flash')
    setTimeout(() => el.classList.remove('sh-message--flash'), 1200)
  }

  /** Toggle the caller's reaction on a DM message. Optimistically
   *  patches the local ``messages`` array so the chip swap feels
   *  instant; the WS ``dm.message_reaction`` echo lands a moment
   *  later and reconciles. On error we revert and toast. */
  const toggleReaction = async (m: Message, emoji: string) => {
    const myUid = currentUser.value?.user_id
    if (!myUid) return
    const had = (m.reactions ?? []).some(
      r => r.user_id === myUid && r.emoji === emoji,
    )
    // Optimistic patch.
    const nextReactions = had
      ? (m.reactions ?? []).filter(
          r => !(r.user_id === myUid && r.emoji === emoji),
        )
      : [...(m.reactions ?? []), { user_id: myUid, emoji }]
    messages.value = messages.value.map(x =>
      x.id === m.id ? { ...x, reactions: nextReactions } : x,
    )
    try {
      const encoded = encodeURIComponent(emoji)
      const path = `/api/conversations/${convId}/messages/${m.id}/reactions/${encoded}`
      if (had) await api.delete(path)
      else await api.put(path, {})
    } catch {
      // Revert the optimistic patch and surface the failure.
      messages.value = messages.value.map(x =>
        x.id === m.id ? { ...x, reactions: m.reactions ?? [] } : x,
      )
      showToast(t(had ? 'dms.reaction_remove_failed' : 'dms.reaction_add_failed'), 'error')
    }
  }

  /** Save an in-place edit of the viewer's own message. Unchanged text
   *  just closes the box; a failed save keeps it open with the draft. */
  const saveEdit = async (m: Message) => {
    const cur = editing.value
    if (!cur || cur.id !== m.id) return
    const content = cur.draft.trim()
    if (!content) {
      showToast(t('dms.edit_empty'), 'error')
      return
    }
    if (content === m.content) {
      editing.value = null
      return
    }
    try {
      const body = await api.patch(
        `/api/conversations/${convId}/messages/${m.id}`, { content },
      ) as { content: string, edited_at: string }
      messages.value = messages.value.map(x =>
        x.id === m.id ? { ...x, content: body.content, edited_at: body.edited_at } : x,
      )
      editing.value = null
    } catch {
      showToast(t('dms.edit_failed'), 'error')
    }
  }

  /** Copy a message body to the clipboard — surfaced from the
   *  context sheet's "Copy" action. */
  const copyMessageText = async (m: Message) => {
    if (!m.content) return
    // A location copies as readable text ("Dam · 52.3702, 4.8952"),
    // never the raw JSON the message carries.
    const loc = m.type === 'location' ? parseDmLocation(m.content) : null
    if (m.type === 'location' && !loc) return
    const text = loc
      ? (loc.label ? `${loc.label} · ${formatCoords(loc)}` : formatCoords(loc))
      : m.content
    try {
      await navigator.clipboard.writeText(text)
      showToast(t('dms.copied'), 'success')
    } catch {
      showToast(t('dms.copy_failed'), 'error')
    }
  }

  /** Touch-only pointer handler on a message bubble. Discriminates
   *  three outcomes from a single PointerEvent stream:
   *    • Long-press (≥ 450 ms with < 10 px movement) → open the
   *      context sheet.
   *    • Right-swipe (≥ 60 px horizontal, vertical-dominant motion
   *      bails) → set ``replyTo`` (same outcome as the desktop
   *      reply chip).
   *    • Anything else → no-op; lets the native scroll proceed.
   *  Mouse / pen pointers fall through to the existing hover-chip
   *  flow; only ``pointerType === 'touch'`` arms the handler. */
  const onBubblePointerDown = (m: Message) => (e: PointerEvent) => {
    if (e.pointerType !== 'touch') return
    if (m.deleted) return
    // Some children own their own gesture (image lightbox tap,
    // file pill anchor, audio bubble scrubber). If the press
    // started on one of those, leave the gesture to them and bail.
    const target = e.target as HTMLElement | null
    if (
      target?.closest(
        '.sh-message-media-tap,'
        + '.sh-message-file,'
        + '.sh-audio-bubble,'
        + '.sh-message-quote,'
        + '.sh-reaction-strip,'
        + '.sh-message-reply-btn,'
        + '.sh-message-react-btn',
      )
    ) {
      return
    }
    const el = e.currentTarget as HTMLElement
    const startX = e.clientX
    const startY = e.clientY
    let didLongPress = false
    let bailed = false
    const longPressTimer = window.setTimeout(() => {
      if (bailed) return
      didLongPress = true
      try { (navigator as Navigator & { vibrate?: (p: number) => void }).vibrate?.(10) } catch { /* noop */ }
      contextSheetFor.value = m
      el.style.removeProperty('--sh-swipe')
    }, 450)
    const setOffset = (px: number) => {
      el.style.setProperty('--sh-swipe', `${px}px`)
      el.classList.toggle('sh-message--will-reply', px >= 60)
    }
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX
      const dy = ev.clientY - startY
      if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
        window.clearTimeout(longPressTimer)
      }
      if (didLongPress) return
      // Vertical-dominant motion: yield to the scroll container.
      if (Math.abs(dy) > Math.abs(dx) + 4) {
        bailed = true
        setOffset(0)
        cleanup()
        return
      }
      setOffset(Math.max(0, Math.min(80, dx)))
    }
    const onUp = (ev: PointerEvent) => {
      window.clearTimeout(longPressTimer)
      const dx = ev.clientX - startX
      if (!didLongPress && !bailed && dx >= 60) {
        replyTo.value = m
      }
      el.style.removeProperty('--sh-swipe')
      el.classList.remove('sh-message--will-reply')
      cleanup()
    }
    const onCancel = () => {
      window.clearTimeout(longPressTimer)
      el.style.removeProperty('--sh-swipe')
      el.classList.remove('sh-message--will-reply')
      cleanup()
    }
    function cleanup() {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
  }

  const callBack = async (callType: 'audio' | 'video') => {
    // ``startCall`` owns the peer connection + SDP offer; the call page
    // renders the session once it's ringing.
    try {
      const callId = await startCall(convId, callType)
      location.route(`/calls/${callId}`)
    } catch (err) {
      showCallError(t('calls.start_failed'), err)
    }
  }

  // Page title: show the peer's display name in the TopBar (above
  // the search box) so the user always knows whose chat they're in.
  // ``ThreadPageTitle`` renders on the loading frame too (see the
  // early-return below) so the title doesn't flicker. While the
  // thread-member roster is still in flight we show "Chats" as a
  // placeholder so the topbar isn't briefly blank on first entry.
  const peers = threadMembers.value.filter(m => !m.is_self)
  const isGroupThread = threadInfo.value?.type === 'group_dm'
  // Known @-tokens of this chat's members (+ the viewer's own) — the
  // bubbles highlight only these, never a guess.
  const mentionRender = conversationMentionRender(convId)
  const peerTitle =
    isGroupThread && threadInfo.value?.name ? threadInfo.value.name
    : peers.length === 0 ? t('nav.chats')
    : peers.length === 1 && !isGroupThread ? peers[0].display_name
    // Group DM: join the peers with " · " — same shape the inbox
    // uses as the row-title fallback, so the topbar and the inbox
    // entry agree on what to call the conversation.
    : peers.map(p => p.display_name).join(' · ')
  // Avatar next to the title — only on 1:1 DMs, where there's a
  // single peer face to show. Group DMs would need a stacked tile
  // that doesn't fit the TopBar's vertical rhythm; the joined
  // display-name list already reads as a group label. Page chrome:
  // an embedded view leaves the TopBar alone.
  const pageTitleEl = embedded ? null : (
    <ThreadPageTitle
      title={peerTitle}
      avatar={peers.length === 1 && !isGroupThread
        ? { src: peers[0].picture_url, name: peers[0].display_name }
        : null}
    />
  )

  if (s.left.value) {
    return (
      <div class="sh-thread sh-thread--embedded sh-thread--left" role="status">
        <p class="sh-thread-left-note">{t('dms.no_longer_in_conversation')}</p>
      </div>
    )
  }
  if (loading.value) return <>{pageTitleEl}<DmThreadSkeleton /></>
  const myUserId = currentUser.value?.user_id

  const handleScroll = () => {
    const el = messagesScrollRef.current
    if (!el) return
    // ``columnReverseDistFromBottom`` normalises across the
    // Chrome / Safari / Edge / modern-Firefox negative-scrollTop
    // convention and the legacy-Firefox positive one — see its
    // docstring for the long version.
    const distFromBottom = columnReverseDistFromBottom(el)
    const maxScroll = Math.max(el.scrollHeight - el.clientHeight, 0)
    const distFromTop = maxScroll - distFromBottom
    const wasSticky = stickToBottom.current
    stickToBottom.current = distFromBottom < LIVE_EDGE_PX
    if (!wasSticky && stickToBottom.current) {
      // User returned to the bottom — clear the unread-since-scroll-up
      // counter so the CTA disappears and advance the read watermark
      // for any unread messages they've now caught up on. Gated on
      // actual pending state so an oscillating user (50 px up, 50 px
      // down) doesn't spam the endpoint on every false→true edge.
      const hadPending =
        unreadAnchor.value !== null || newSinceScrollUp.value > 0
      newSinceScrollUp.value = 0
      if (readReceiptsEnabled.value && hadPending) {
        api.post(`/api/conversations/${convId}/read`).catch(() => {})
      }
      // Clear the divider once the user has caught up.
      if (unreadAnchor.value) unreadAnchor.value = null
    }
    // Lazy-load older history when the user is within 120 px of the
    // visual top — far enough out that the fetch lands before the
    // user actually hits the very top. No "user has moved" gate is
    // needed: on entry ``distFromTop`` equals ``maxScroll``, which
    // is the maximum possible distance from the trigger — only a
    // real upward gesture can satisfy this condition.
    if (
      distFromTop < 120
      && hasMoreHistory.value
      && !isLoadingOlder.value
    ) {
      void loadOlder()
    }
  }

  /** Fetch the next page of older messages and prepend them.
   *
   *  ``column-reverse`` makes this dramatically simpler than the
   *  classic layout: the user's ``scrollTop`` is anchored relative
   *  to the **visual bottom**, and the prepended history lands at
   *  the **visual top** — i.e. on the opposite side of the
   *  scrollable region from the user's viewport reference. The
   *  user's reading position therefore stays exactly where it was,
   *  for free, with no snapshot / restore math.
   *
   *  ``isLoadingOlder`` gates re-entry so a touch-scroll dragging
   *  the user across the trigger threshold can't queue multiple
   *  parallel fetches. */
  const loadOlder = async () => {
    if (isLoadingOlder.value || !hasMoreHistory.value) return
    const oldest = messages.value[0]
    if (!oldest) return
    // ``convId`` and the store ``s`` come from the render closure, so an
    // in-flight page belongs to whichever thread was on screen when the
    // fetch went out. Re-check that it still is after the await — see
    // the bail-out below.
    const myConv = convId
    isLoadingOlder.value = true
    try {
      const data: Message[] = await api.get(
        `/api/conversations/${myConv}/messages?before=${oldest.id}&limit=${PAGE_SIZE}`,
      ) ?? []
      // Staleness guard. The user may have switched threads (or left
      // the thread) while this page was in flight. The writes below go
      // to this thread's own store, so they can't reach another
      // thread's list — but bail before ANY signal write anyway,
      // including the "no older history" branch just below: nothing
      // off screen should do work.
      if (!s.active) return
      const older = data.slice().reverse()
      if (older.length === 0) {
        hasMoreHistory.value = false
        return
      }
      // Deduplicate at the seam: if a slow re-entry surfaces the same
      // bottom-of-page message twice (rare; backend uses a strict
      // ``<`` filter on ``before``), keep only ids we don't have.
      const have = new Set(messages.value.map(m => m.id))
      const fresh = older.filter(m => !have.has(m.id))
      if (fresh.length === 0) {
        if (data.length < PAGE_SIZE) hasMoreHistory.value = false
        return
      }
      messages.value = [...fresh, ...messages.value]
      if (data.length < PAGE_SIZE) hasMoreHistory.value = false
    } finally {
      // Same reason the body bails. (Each thread has its own
      // ``isLoadingOlder``, so a stale page can't kill the *new* thread's
      // older-history spinner either way.)
      if (s.active) isLoadingOlder.value = false
    }
  }

  const status = statusLine(threadMembers.value)
  // Compact status modifier for the dot in the header: 'online' → green,
  // 'idle' → amber, anything else → no dot. ``peers`` already in scope
  // (computed earlier for the topbar title + avatar).
  const headerDot: 'online' | 'idle' | null = peers.length === 1
    ? (peers[0].is_online ? (peers[0].is_idle ? 'idle' : 'online') : null)
    : (peers.some(p => p.is_online) ? 'online' : null)

  return (
    <div class={embedded ? 'sh-thread sh-thread--embedded' : 'sh-thread'}>
      {pageTitleEl}
      {showHeader && (
      <div class="sh-thread-header">
        {/* Back chevron — visible on every viewport since the
         * full-bleed chat hides both the mobile bottom tab bar and
         * the desktop card outline. Users reach for an in-chat
         * back affordance regardless of screen size. Page chrome
         * only — an embedded view has no page to go back from. */}
        {!embedded && (
          <a
            class="sh-thread-back"
            href={addBase('/dms')}
            aria-label={t('calls.page.back_to_chats')}
          >‹</a>
        )}
        <div class="sh-thread-header-status" aria-live="polite">
          {headerDot && (
            <span class={`sh-thread-header-dot sh-thread-header-dot--${headerDot}`}
                  aria-hidden="true" />
          )}
          {status && <span class="sh-thread-header-status-line">{status}</span>}
        </div>
        {isGroupThread && showGroupInfo && (
          <button
            type="button"
            class="sh-icon-btn sh-thread-group-btn"
            title={t('dms.group_info')}
            aria-label={t('dms.group_info_aria')}
            onClick={() => { groupInfoOpen.value = true }}
          >
            <span aria-hidden="true">👥</span>
          </button>
        )}
        {threadInfo.value && (
          <MuteButton
            convId={convId}
            mutedUntil={threadInfo.value.muted_until}
            onChange={setThreadMute}
            {...(isGroupThread
              ? { level: threadInfo.value.notif_level, onLevelChange: setThreadLevel }
              : {})}
          />
        )}
        {allowCalls && <CallButton convId={convId} memberCount={memberCount.value} />}
        {allowCalls && (
          <a
            class="sh-thread-history"
            href={addBase(`/dms/${convId}/calls`)}
            title={t('calls.history.title')}
            aria-label={t('calls.history.title')}
          >
            <span aria-hidden="true">🕘</span>
          </a>
        )}
      </div>
      )}
      {gaps.value.length > 0 && (
        <div class="sh-dm-gap-banner" role="status" aria-live="polite">
          <span aria-hidden="true">⚠️</span>
          <span>
            {gaps.value.length === 1
              ? t('dms.gap.one')
              : t('dms.gap.many', { n: String(gaps.value.length) })}
            {' '}{t('dms.gap.hint')}
          </span>
        </div>
      )}
      <div class="sh-messages" ref={messagesScrollRef} onScroll={handleScroll}>
        {/* In-thread typing indicator — rendered FIRST in DOM so the
         *  ``column-reverse`` flex flip lands it at the visual
         *  BOTTOM of the messages area, right above the composer
         *  where the WhatsApp/iMessage-style bubble preview lives.
         *  Empty when nobody is typing — collapses with zero
         *  height impact on the scroll position. The ``bubble``
         *  prop opts into the bubble shape. */}
        <TypingIndicator scope={convId} bubble />
        {/* Floating "↓ N new messages" chip — appears in the
         *  bottom-right corner of the scrollable container when the
         *  user has scrolled up reading history and one or more new
         *  messages have arrived. Click jumps to the bottom and
         *  resets the counter via ``handleScroll``'s sticky-bottom
         *  branch. In column-reverse, "bottom" is ``scrollTop=0``.
         *  Rendered FIRST in DOM so the column-reverse flip places
         *  the sticky chip at the visual bottom. */}
        {newSinceScrollUp.value > 0 && !stickToBottom.current && (
          <button
            type="button"
            class="sh-dm-jump-down"
            onClick={() => {
              const el = messagesScrollRef.current
              if (!el) return
              stickToBottom.current = true
              el.scrollTo({ top: 0, behavior: 'smooth' })
              newSinceScrollUp.value = 0
              if (unreadAnchor.value) unreadAnchor.value = null
              if (readReceiptsEnabled.value) {
                api.post(`/api/conversations/${convId}/read`).catch(() => {})
              }
            }}
            aria-label={t(
              isOne(newSinceScrollUp.value) ? 'dms.jump_latest_one' : 'dms.jump_latest',
              { n: String(newSinceScrollUp.value) },
            )}
          >
            <span aria-hidden="true">↓</span>
            <span class="sh-dm-jump-down__count">
              {newSinceScrollUp.value > 99 ? '99+' : newSinceScrollUp.value}
            </span>
            <span class="sh-dm-jump-down__label">{t('dms.jump_new')}</span>
          </button>
        )}
        {buildFlatItems(messages.value).slice().reverse().map((item) => {
          if (item.kind === 'day') {
            // Day-separator pill. In column-reverse this pill sits
            // visually ABOVE the first message of that day because
            // it was placed *before* the day's messages in the
            // chronological list (and the reverse iteration flips
            // that ordering for DOM, which column-reverse then
            // flips again — landing the pill back on top of its
            // day's messages visually). The key is stable across
            // re-renders so the pill doesn't unmount/remount when
            // new messages append to the same day.
            return (
              <div
                key={item.key}
                class="sh-day-header"
                role="separator"
                aria-label={t('dms.day.aria', { day: item.label })}
              >
                {item.label}
              </div>
            )
          }
          const { m, showHeader, showFooter } = item
          // Render the "New messages" divider immediately above the
          // first-unread row visually. In column-reverse the
          // visually-above element is the one rendered AFTER the
          // anchor in DOM order, so the divider goes inside the
          // Fragment AFTER the message (not before).
          const isUnreadAnchor =
            unreadAnchor.value !== null
            && unreadAnchor.value.message_id === m.id
          if (m.type === 'call_event') {
            // Keyed Fragment for the outer slot so Preact's
            // reconciler moves the row correctly across prepend
            // updates (column-reverse means prepend lands at the
            // visual top, but the underlying DOM key matching is
            // still index-based without the Fragment key).
            return (
              <Fragment key={m.id}>
                <CallEventRow m={m} onCallBack={allowCalls ? callBack : undefined} />
                {isUnreadAnchor && <UnreadDivider />}
              </Fragment>
            )
          }
          const mine = m.sender_user_id === myUserId
          // Look up the parent message for inline rendering of the
          // quoted-reply card. Missing parents (loaded out of window or
          // soft-deleted) fall through to a small placeholder.
          const parent = m.reply_to_id
            ? messages.value.find(x => x.id === m.reply_to_id)
            : null
          // ``showHeader`` / ``showFooter`` come from ``buildFlatItems``
          // and drive same-author bubble grouping: only the first
          // bubble in a cluster shows the sender name, only the last
          // carries the timestamp + ReadReceipt. The middle bubbles
          // get flush vertical borders via the grouping classes so
          // the burst reads as one speech turn.
          const groupClass =
            (showHeader ? '' : ' sh-message--grouped-top')
            + (showFooter ? '' : ' sh-message--grouped-bottom')
          return (
            <Fragment key={m.id}>
            <div
              data-msg-id={m.id}
              class={`sh-message ${mine ? 'sh-message--mine' : ''} ${m.deleted ? 'sh-message--deleted' : ''}${groupClass}`}
              onPointerDown={onBubblePointerDown(m)}
            >
              {!mine && showHeader && <strong>{senderName(m.sender_user_id)}</strong>}
              {m.reply_to_id && (
                <button
                  type="button"
                  class="sh-message-quote"
                  onClick={() => parent && scrollToMessage(parent.id)}
                  aria-label={parent
                    ? t('dms.reply.quote_aria', { name: senderName(parent.sender_user_id), text: quotePreview(parent) })
                    : t('dms.reply.quote_missing_aria')}
                >
                  <span class="sh-message-quote-author">
                    {parent ? senderName(parent.sender_user_id) : t('presence.state.unknown')}
                  </span>
                  <span class="sh-message-quote-body">
                    {parent ? quotePreview(parent) : t('dms.reply.unavailable')}
                  </span>
                </button>
              )}
              {/* Media attachments — rendered above any caption text.
               *  Image / video inline; ``file`` becomes a download
               *  pill. ``media_sync_status === 'pending'`` overlays a
               *  spinner to signal "preview shown, full bytes still
               *  arriving from peer" (the cross-household preview-
               *  now-sync-later flow). On a deleted message the
               *  media is gone — only the "(message deleted)"
               *  placeholder renders. */}
              {!m.deleted && m.type === 'image' && m.media_url && (
                <button
                  type="button"
                  class="sh-message-media-tap"
                  aria-label={m.file_name ? t('dms.media.open_named', { name: m.file_name }) : t('dms.media.open_picture')}
                  onClick={(e) => {
                    // Stop the click from bubbling to the bubble's
                    // own click handler (which toggles the reply
                    // affordance). The lightbox is the action the
                    // user expected from the picture itself.
                    e.stopPropagation()
                    openLightbox(m.media_url!)
                  }}
                >
                  <img
                    class={
                      'sh-message-media sh-message-media--image'
                      + (m.media_sync_status === 'pending'
                        ? ' sh-message-media--pending'
                        : '')
                    }
                    src={m.media_url}
                    alt={m.file_name ?? t('dms.media.picture')}
                    loading="lazy"
                  />
                </button>
              )}
              {!m.deleted && m.type === 'video' && m.media_url && (
                <VideoMedia
                  src={m.media_url}
                  poster={m.media_thumbnail_url}
                  mediaStatus={m.media_status}
                  class={
                    'sh-message-media sh-message-media--video'
                    + (m.media_sync_status === 'pending'
                      ? ' sh-message-media--pending'
                      : '')
                  }
                />
              )}
              {!m.deleted && m.type === 'file' && (() => {
                const fileHref = safeHref(m.media_url)
                const name = (
                  <span class="sh-message-file__name">
                    {m.file_name ?? t('dms.media.attachment')}
                  </span>
                )
                // No usable link (the server dropped a non-local URL, or
                // it isn't one we open): show the file as plain text so
                // the message never renders as an empty bubble.
                if (fileHref === undefined) {
                  return (
                    <span class="sh-message-file sh-message-file--unavailable">
                      <span class="sh-message-file__glyph" aria-hidden="true">📎</span>
                      <span class="sh-message-file__meta">
                        {name}
                        <span class="sh-message-file__size">
                          {t('dms.media.file_unavailable')}
                        </span>
                      </span>
                    </span>
                  )
                }
                return (
                  <a
                    class="sh-message-file"
                    href={fileHref}
                    download={m.file_name ?? 'attachment'}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <span class="sh-message-file__glyph" aria-hidden="true">📎</span>
                    <span class="sh-message-file__meta">
                      {name}
                      <span class="sh-message-file__size">
                        {formatFileSize(m.file_size_bytes)}
                      </span>
                    </span>
                  </a>
                )
              })()}
              {!m.deleted && m.type === 'location' && (
                <DmLocationMessage content={m.content} />
              )}
              {!m.deleted && m.type === 'audio' && m.media_url && (
                <AudioBubble
                  src={m.media_url}
                  transcript={m.content}
                  fileName={m.file_name}
                  pending={m.media_sync_status === 'pending'}
                />
              )}
              {/* Cross-household delivery-failure footnote. Only
               *  surfaces on the sender's own bubble — the
               *  recipient has nothing to act on. Set when the
               *  ``DM_MEDIA_BLOB`` outbox exhausted its retry
               *  budget for at least one paired peer. The file is
               *  still on the sender's device; the message just
               *  didn't make it to ⟨peer⟩'s household. */}
              {!m.deleted
                && mine
                && (m.type === 'image' || m.type === 'video' || m.type === 'file')
                && m.media_sync_status === 'failed' && (
                <div class="sh-message-media-failed" role="status">
                  <span aria-hidden="true">⚠</span>
                  <span>
                    {t('dms.media.delivery_failed')}
                  </span>
                </div>
              )}
              {/* Caption body. Empty captions on media messages
               *  collapse silently (we don't want a stray empty
               *  ``<p>`` adding visual weight to a picture-only
               *  bubble). ``audio`` messages render their transcript
               *  inside :class:`AudioBubble` above, so we skip the
               *  caption render here to avoid doubling the text. */}
              {editing.value?.id === m.id ? (
                <form
                  class="sh-message-edit"
                  onSubmit={(e) => { e.preventDefault(); void saveEdit(m) }}
                >
                  <textarea
                    class="sh-message-edit__input"
                    aria-label={t('dms.edit.aria')}
                    value={editing.value.draft}
                    rows={Math.min(6, Math.max(2, editing.value.draft.split('\n').length))}
                    ref={(el) => { if (el && document.activeElement !== el) el.focus() }}
                    onInput={(e) => {
                      editing.value = {
                        id: m.id,
                        draft: (e.target as HTMLTextAreaElement).value,
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        e.preventDefault()
                        editing.value = null
                      } else if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
                        e.preventDefault()
                        void saveEdit(m)
                      }
                    }}
                  />
                  <div class="sh-message-edit__actions">
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => { editing.value = null }}
                    >
                      {t('common.cancel')}
                    </Button>
                    <Button type="submit">{t('common.save')}</Button>
                  </div>
                </form>
              ) : (m.deleted
                || (m.content && m.type !== 'audio' && m.type !== 'location')) && (
                <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                  {m.deleted
                    ? t('dms.message_deleted')
                    : <MentionText text={m.content} {...mentionRender} />}
                </p>
              )}
              {showFooter && (
                <div class="sh-message-meta">
                  <time>{new Date(m.created_at).toLocaleTimeString(formatLocale(),
                    { hour: '2-digit', minute: '2-digit' })}</time>
                  {/* Voice notes stamp ``edited_at`` when the transcript
                   *  lands — that isn't the sender editing. */}
                  {m.edited_at && !m.deleted && m.type !== 'audio' && (
                    <span class="sh-message-edited">{t('dms.edited')}</span>
                  )}
                  {/* Per-bubble send-failure glyph. The optimistic
                   *  bubble keeps the user's content visible so they
                   *  can recall what didn't go through; the ⚠ +
                   *  hover text spell out the failure mode (relayed-
                   *  DM media rejection vs generic network /
                   *  validation error). A toast also fires from
                   *  ``handleSend`` so the user gets a non-hover
                   *  surface for the same information. */}
                  {mine && m.send_failed && (
                    <span
                      class="sh-message-send-failed"
                      title={m.send_failed_reason ?? t('dms.send_failed_short')}
                      aria-label={m.send_failed_reason ?? t('dms.send_failed_short')}
                    >
                      ⚠
                    </span>
                  )}
                  {mine && !m.send_failed && (
                    <ReadReceipt
                      sent={true}
                      delivered={
                        deliveredMessageIds.value.has(m.id) ||
                        readMessageIds.value.has(m.id)
                      }
                      read={readMessageIds.value.has(m.id)}
                    />
                  )}
                </div>
              )}
              {!m.deleted && m.reactions && m.reactions.length > 0 && (
                (() => {
                  // Aggregate per-emoji counts + flag the caller's
                  // own reactions so a second tap toggles them off
                  // (mirrors the WhatsApp / iMessage chip behavior).
                  const counts = new Map<string, { count: number; mine: boolean }>()
                  for (const r of m.reactions ?? []) {
                    const cur = counts.get(r.emoji) ?? { count: 0, mine: false }
                    cur.count += 1
                    if (r.user_id === myUserId) cur.mine = true
                    counts.set(r.emoji, cur)
                  }
                  return (
                    <div class="sh-reaction-strip" role="group" aria-label={t('dms.reactions')}>
                      {Array.from(counts.entries()).map(([emoji, info]) => (
                        <button
                          key={emoji}
                          type="button"
                          class={
                            'sh-reaction-chip'
                            + (info.mine ? ' sh-reaction-chip--mine' : '')
                          }
                          aria-pressed={info.mine}
                          aria-label={
                            info.mine
                              ? t('dms.reaction_remove_aria', { emoji, n: String(info.count) })
                              : t('dms.reaction_add_aria', { emoji, n: String(info.count) })
                          }
                          onClick={(e) => {
                            e.stopPropagation()
                            void toggleReaction(m, emoji)
                          }}
                        >
                          <span aria-hidden="true">{emoji}</span>
                          <span class="sh-reaction-chip__count">{info.count}</span>
                        </button>
                      ))}
                    </div>
                  )
                })()
              )}
              {!m.deleted && (
                <Fragment>
                  <button
                    type="button"
                    class="sh-message-react-btn"
                    title={t('dms.add_reaction')}
                    aria-label={t('dms.add_reaction_aria', { name: senderName(m.sender_user_id) })}
                    onClick={() => { contextSheetFor.value = m }}
                  >
                    😊
                  </button>
                  {canEditMessage(m, myUserId) && (
                    <button
                      type="button"
                      class="sh-message-react-btn sh-message-edit-btn"
                      title={t('common.edit')}
                      aria-label={t('dms.edit.own_aria')}
                      onClick={() => { editing.value = { id: m.id, draft: m.content } }}
                    >
                      ✎
                    </button>
                  )}
                  <button
                    type="button"
                    class="sh-message-reply-btn"
                    title={t('dms.reply.action')}
                    aria-label={t('dms.reply.to', { name: senderName(m.sender_user_id) })}
                    onClick={() => { replyTo.value = m }}
                  >
                    ↩
                  </button>
                </Fragment>
              )}
            </div>
            </Fragment>
          )
        })}
        {/* Lazy-load spinner — rendered LAST in DOM order so the
         *  ``column-reverse`` flex flip lands it at the visual TOP
         *  of the messages list, where the user is scrolling toward
         *  to fetch older history. Provides the "fetching older"
         *  affordance while the network round-trip is in flight. */}
        {isLoadingOlder.value && (
          <div class="sh-dm-load-older" aria-live="polite">{t('dms.loading_older')}</div>
        )}
      </div>
      {replyTo.value && (
        <div class="sh-composer-reply" role="status" aria-live="polite">
          <div class="sh-composer-reply-body">
            <span class="sh-composer-reply-author">
              {t('dms.reply.replying_to', { name: senderName(replyTo.value.sender_user_id) })}
            </span>
            <span class="sh-composer-reply-preview">
              {quotePreview(replyTo.value)}
            </span>
          </div>
          <button
            type="button"
            class="sh-composer-reply-clear"
            aria-label={t('dms.reply.cancel')}
            onClick={() => { replyTo.value = null }}
          >×</button>
        </div>
      )}
      {/* In-flight chip — visible the instant the user picks a file.
       *  ``MediaAttachmentChip`` surfaces ``Uploading… 45%`` →
       *  ``Processing image…`` → terminal states, with a local
       *  blob-URL preview so the user sees what they're sending
       *  before the server's signed URL is even in hand. The same
       *  chip is re-rendered with ``phase="ready"`` once the upload
       *  completes (see the ``pendingAttachment`` branch below).
       *  Failure leaves the chip in place with a Retry button so the
       *  user doesn't have to re-pick the file. */}
      {uploadingAttachment.value && (
        <MediaAttachmentChip
          phase={uploadingAttachment.value.phase}
          kind={uploadingAttachment.value.kind}
          filename={uploadingAttachment.value.filename}
          sizeBytes={uploadingAttachment.value.fileSize}
          previewUrl={uploadingAttachment.value.previewUrl}
          percent={uploadingAttachment.value.percent}
          errorMessage={uploadingAttachment.value.errorMessage}
          onClear={_cancelUpload}
          onRetry={() => {
            const cur = uploadingAttachment.value
            if (!cur) return
            void _startAttachmentUpload(cur.file)
          }}
        />
      )}
      {!uploadingAttachment.value && pendingAttachment.value && (
        <MediaAttachmentChip
          phase="ready"
          kind={pendingAttachment.value.type}
          filename={pendingAttachment.value.file_name}
          sizeBytes={pendingAttachment.value.file_size_bytes}
          previewUrl={pendingAttachment.value.preview_url}
          onClear={() => {
            pendingAttachment.value = null
            const ta = composerInputRef.current
            composerHasContent.value = (ta?.value.trim().length ?? 0) > 0
          }}
        />
      )}
      {attachmentError.value && (
        <div class="sh-dm-attach-error" role="alert">
          {attachmentError.value}
          <button
            type="button"
            class="sh-dm-attach-error__clear"
            aria-label={t('dms.dismiss_error')}
            onClick={() => { attachmentError.value = null }}
          >×</button>
        </div>
      )}
      <form class="sh-composer" onSubmit={handleSend}>
        {/* Hidden file input — driven by the paperclip button below.
         *  The picker accepts every MIME type so the user can attach
         *  a photo, a video, OR a generic file (PDF, etc.); the
         *  backend's :class:`MediaUploadView` is the authority on
         *  size + format constraints (same rules as the feed). */}
        {allowAttachments && (
          <input
            ref={attachInputRef}
            type="file"
            class="sr-only"
            aria-hidden="true"
            tabIndex={-1}
            onChange={handleAttachPicked}
          />
        )}
        {/* Attach menu — paperclip. Outside the input pill so it
         *  has its own thumb target separate from the textarea and
         *  the inline emoji picker. Opens "Photo, video or file"
         *  (the hidden input above) or "Location" (the picker). */}
        {allowAttachments && (
          <ComposerAttachMenu
            fileDisabled={
              pendingAttachment.value !== null || uploadingAttachment.value !== null
            }
            onPickFile={() => attachInputRef.current?.click()}
            onPickLocation={() => { locationPickerOpen.value = true }}
          />
        )}
        {/* Textarea + inline emoji picker. The emoji button sits
         *  inside the input pill on the right edge (WhatsApp / iMessage
         *  idiom): a small ghost-icon that doesn't claim a separate
         *  flex slot from the right-hand round button. Saves
         *  horizontal space on phones and keeps the user's eye
         *  anchored on the text they're typing. */}
        <div class="sh-dm-input-wrap">
          <textarea
            ref={composerInputRef}
            name="content"
            // The global "n" shortcut focuses the page's composer — an
            // embedded thread leaves it to the host surface's own.
            {...(embedded ? {} : { 'data-shortcut': 'composer', 'aria-keyshortcuts': 'n' })}
            placeholder={t('dms.placeholder')}
            autocomplete="off"
            rows={1}
            onInput={handleInput}
            onKeyDown={handleComposerKeyDown}
            onBlur={() => {
              closeEmojiAutocomplete()
              closeMentionAutocomplete()
            }}
            {...(isGroupThread ? mentionInputAria(composerInputRef.current) : {})}
          />
          <EmojiPickButton
            openKey={`dm-composer-${convId}`}
            onInsert={insertEmojiAtCursor}
            ariaLabel={t('dms.insert_emoji')}
            className="sh-composer-emoji-inline"
          />
        </div>
        {/* Mic⇄Send slot. Mutually exclusive primary actions occupy
         *  the same round button position to the right of the input:
         *
         *    • Empty composer → hold-to-record voice note.
         *    • Composer has content → Send.
         *
         *  The slot does NOT lock during the POST round-trip — sends
         *  fire-and-forget so the user can type and send the next
         *  message immediately. Failures show a ⚠ on the optimistic
         *  bubble (see ``send_failed`` in the messages signal) rather
         *  than throwing the draft back into the textarea, which
         *  would clobber whatever the user is typing now. */}
        {composerHasContent.value ? (
          <Button
            type="submit"
            // Block sends while an upload is in flight — otherwise a
            // user mid-upload who hit Send would ship the text only
            // and silently drop the attachment.
            disabled={!composerHasContent.value || uploadingAttachment.value !== null}
            aria-label={
              uploadingAttachment.value !== null
                ? t('dms.wait_upload')
                : t('dms.send_message')
            }
          >
            {/* Compact paper-plane icon so the composer reads as a
             * chat bar (most of the row goes to the text input)
             * rather than a form with a wide CTA. */}
            <span aria-hidden="true" class="sh-composer-send-icon">➤</span>
          </Button>
        ) : (
          <VoiceRecordButton
            className="sh-composer-mic"
            onCapture={handleVoiceNote}
          />
        )}
      </form>
      {/* Module-singleton popover for the ``:foo`` autocomplete the
       *  textarea triggers via ``checkForEmojiTrigger``. Mounting it
       *  inside the thread (rather than at app root) is fine — the
       *  popover positions itself absolutely against the input's
       *  bounding rect, not the parent. */}
      <EmojiAutocomplete />
      {isGroupThread && <MentionAutocomplete />}
      <LocationPicker
        open={locationPickerOpen.value}
        submitLabel={t('location.send')}
        onSubmit={sendLocation}
        onClose={() => { locationPickerOpen.value = false }}
      />
      {isGroupThread && showGroupInfo && threadInfo.value && (
        <GroupInfoDialog
          open={groupInfoOpen.value}
          convId={convId}
          name={threadInfo.value.name}
          managedHere={threadInfo.value.managed_here}
          mutedUntil={threadInfo.value.muted_until}
          onMuteChange={setThreadMute}
          notifLevel={threadInfo.value.notif_level}
          onNotifLevelChange={setThreadLevel}
          members={threadMembers.value}
          onClose={() => { groupInfoOpen.value = false }}
          onChanged={() => {
            void fetchThreadInfo(s, convId)
            void fetchRoster(s, convId)
          }}
          onLeft={() => {
            groupInfoOpen.value = false
            afterLeave()
          }}
        />
      )}
      {/* Touch-only long-press menu. Hover users get the inline
       *  ``.sh-message-reply-btn`` chip; this sheet only ever
       *  surfaces when a finger holds a bubble for ≥ 450 ms. */}
      {contextSheetFor.value && (() => {
        const target = contextSheetFor.value!
        const isMine = target.sender_user_id === myUserId
        // Same gate as the file chip: a peer-supplied ``javascript:`` URL
        // must never reach ``window.open`` — the action is hidden instead.
        const openUrl = safeHref(target.media_url)
        const actions = [
          {
            label: t('dms.reply.action'),
            glyph: '↩',
            onClick: () => { replyTo.value = target },
          },
          ...(target.content
            ? [{
                label: t('dms.copy_text'),
                glyph: '⧉',
                onClick: () => { void copyMessageText(target) },
              }]
            : []),
          ...(canEditMessage(target, myUserId)
            ? [{
                label: t('common.edit'),
                glyph: '✎',
                onClick: () => { editing.value = { id: target.id, draft: target.content } },
              }]
            : []),
          ...(openUrl !== undefined
            ? [{
                label: t('dms.open_new_tab'),
                glyph: '↗',
                onClick: () => { window.open(openUrl, '_blank', 'noopener,noreferrer') },
              }]
            : []),
        ]
        // `isMine` reserved for a future "Delete for everyone" action
        // (the route exists; the UI isn't shipped yet).
        void isMine
        return (
          <MessageContextSheet
            actions={actions}
            onReact={(emoji) => { void toggleReaction(target, emoji) }}
            onPickMore={() => { reactionPickerFor.value = target }}
            onClose={() => { contextSheetFor.value = null }}
          />
        )
      })()}
      {/* Full emoji picker — shown when the user taps "+" in the
       *  context sheet. Stays mounted over the thread until the
       *  user picks a glyph or closes; tapping a result toggles
       *  the reaction on the target message. */}
      {reactionPickerFor.value && (
        <div
          class="sh-reaction-picker-overlay"
          onClick={() => { reactionPickerFor.value = null }}
        >
          <ReactionPicker
            onSelect={(emoji) => {
              const target = reactionPickerFor.value
              if (target) void toggleReaction(target, emoji)
              reactionPickerFor.value = null
            }}
            onClose={() => { reactionPickerFor.value = null }}
          />
        </div>
      )}
    </div>
  )
}
