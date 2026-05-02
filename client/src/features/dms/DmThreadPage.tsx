import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { ws } from '@/ws'
import type { Message } from '@/types'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { ReadReceipt, readReceiptsEnabled } from '@/components/ReadReceipts'
import { TypingIndicator, sendTyping } from '@/components/TypingIndicator'
import { currentUser } from '@/store/auth'

const messages = signal<Message[]>([])
const loading = signal(true)
const readMessageIds = signal<Set<string>>(new Set())
const deliveredMessageIds = signal<Set<string>>(new Set())
const memberCount = signal<number>(0)

interface ThreadMember {
  user_id: string
  username: string
  display_name: string
  is_self: boolean
  is_online: boolean
  is_idle: boolean
  last_seen_at: string | null
}

const threadMembers = signal<ThreadMember[]>([])
/** WhatsApp-style reply target. When set, the composer shows a chip
 *  with the parent message preview and the next send carries
 *  ``reply_to_id``. Cleared after send or by the chip's "×" button. */
const replyTo = signal<Message | null>(null)

/** WhatsApp-style "Last seen 12 min ago" formatter — same shape as the
 *  presence-page helper but inline so this page doesn't grow a util
 *  module just for one consumer. */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (sec < 60)      return 'just now'
  if (sec < 3600)    return `${Math.floor(sec / 60)} min ago`
  if (sec < 86400)   return `${Math.floor(sec / 3600)} h ago`
  return `${Math.floor(sec / 86400)} d ago`
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
    if (p.is_online && p.is_idle) return 'Idle'
    if (p.is_online)              return 'Online'
    const ago = humanizeAgo(p.last_seen_at)
    return ago ? `Last seen ${ago}` : 'Offline'
  }
  const onlineCount = peers.filter(p => p.is_online).length
  if (onlineCount === 0) return null
  return `${onlineCount} online`
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

const gaps = signal<MessageGap[]>([])

/**
 * Render a ``type="call_event"`` system message as a compact centred row
 * in the DM thread (spec §26.8). The backend stores a JSON blob describing
 * the event; we parse it at render-time and offer a one-tap "Call back"
 * on missed/declined events.
 */
function CallEventRow({ m, onCallBack }: { m: Message, onCallBack: (type: 'audio' | 'video') => void }) {
  let ev: { event?: string, call_type?: string, duration_seconds?: number | null } = {}
  try { ev = JSON.parse(m.content) } catch { /* noop */ }
  const ic = ev.call_type === 'video' ? '📹' : '📞'
  const label = ev.event === 'missed' ? 'Missed call'
    : ev.event === 'declined' ? 'Declined call'
    : ev.event === 'ended'    ? 'Call'
    : 'Call started'
  const dur = ev.duration_seconds && ev.duration_seconds > 0
    ? ` · ${formatDuration(ev.duration_seconds)}` : ''
  const when = new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const showBack = ev.event === 'missed' || ev.event === 'declined'
  return (
    <div class="sh-call-event">
      <span class="sh-call-event-icon">{ic}</span>
      <span class="sh-call-event-label">{label}</span>
      <span class="sh-call-event-meta">{dur} · {when}</span>
      {showBack && (
        <Button onClick={() => onCallBack((ev.call_type as 'audio' | 'video') ?? 'audio')}>
          Call back
        </Button>
      )}
    </div>
  )
}

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

/** Thread-header call buttons (§26.2). */
function CallButtons({ convId, onStart }: { convId: string, onStart: (t: 'audio' | 'video') => void }) {
  // Only meaningful when the DM has ≥ 1 peer.
  if (memberCount.value < 2) return null
  return (
    <div class="sh-thread-call-buttons">
      <button type="button" class="sh-icon-btn" title="Audio call"
              onClick={() => onStart('audio')}
              aria-label={`Start audio call in conversation ${convId}`}>📞</button>
      <button type="button" class="sh-icon-btn" title="Video call"
              onClick={() => onStart('video')}
              aria-label={`Start video call in conversation ${convId}`}>📹</button>
    </div>
  )
}

export default function DmThreadPage() {
  const { params } = useRoute()
  const convId = params.id
  const location = useLocation()

  useEffect(() => {
    loading.value = true
    api.get(`/api/conversations/${convId}/messages`).then(data => {
      messages.value = data.reverse()
      loading.value = false
      if (readReceiptsEnabled.value) {
        api.post(`/api/conversations/${convId}/read`).catch(() => {})
      }
      // Hydrate delivery/read state for every message so ticks render
      // immediately — not just on messages we've seen WS frames for.
      api.get(`/api/conversations/${convId}/delivery-states`).then(
        (body: { states: DeliveryState[] }) => {
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
          gaps.value = body.gaps || []
        },
      ).catch(() => { gaps.value = [] })
    })
    // Roster fetch — drives the call-button visibility (member_count) AND
    // the WhatsApp-style "Online" / "Last seen 2 h ago" status line in the
    // thread header. Live-patched below by the user.online/idle/offline
    // WS frames so the header stays current without polling.
    api.get(`/api/conversations/${convId}/members`).then((rows: ThreadMember[]) => {
      threadMembers.value = rows
      memberCount.value = rows.length || 2
    }).catch(() => {
      threadMembers.value = []
      memberCount.value = 2
    })

    const offRead = ws.on('dm.read', (evt) => {
      const data = evt.data as { conversation_id?: string; message_ids?: string[] }
      if (data.conversation_id === convId && data.message_ids) {
        readMessageIds.value = new Set([...readMessageIds.value, ...data.message_ids])
      }
    })
    const offNewMsg = ws.on('dm.message', (evt) => {
      const data = evt.data as { conversation_id?: string; message?: Message }
      if (data.conversation_id === convId && data.message) {
        const msg = data.message
        if (!messages.value.some(m => m.id === msg.id)) {
          messages.value = [...messages.value, msg]
          // Ack delivery as soon as the frame lands. The server upsert
          // is idempotent; a later ``read`` supersedes.
          const mine = msg.sender_user_id === currentUser.value?.user_id
          if (!mine && readReceiptsEnabled.value) {
            api.post(
              `/api/conversations/${convId}/messages/${msg.id}/delivered`,
            ).catch(() => {})
          }
        }
        if (readReceiptsEnabled.value) {
          api.post(`/api/conversations/${convId}/read`).catch(() => {})
        }
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
      offRead(); offNewMsg()
      offUserOnline(); offUserIdle(); offUserOffline()
    }
  }, [convId])

  let typingTimer: ReturnType<typeof setTimeout> | null = null

  const handleInput = () => {
    if (typingTimer) return
    sendTyping(convId)
    typingTimer = setTimeout(() => { typingTimer = null }, 2000)
  }

  const handleSend = async (e: Event) => {
    e.preventDefault()
    const form = e.target as HTMLFormElement
    const content = new FormData(form).get('content') as string
    if (!content.trim()) return
    const reply_to_id = replyTo.value?.id ?? null
    try {
      await api.post(`/api/conversations/${convId}/messages`, {
        content,
        ...(reply_to_id ? { reply_to_id } : {}),
      })
      form.reset()
      replyTo.value = null
      const data = await api.get(`/api/conversations/${convId}/messages`)
      messages.value = data.reverse()
    } catch (err: unknown) {
      showToast(
        `Send failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }

  /** Resolve sender display name from the roster — falls back to the raw
   *  user_id (which the rest of the thread also surfaces today). */
  const senderName = (user_id: string): string => {
    const m = threadMembers.value.find(x => x.user_id === user_id)
    return m?.display_name ?? m?.username ?? user_id
  }

  /** One-line preview of a message's content for the quoted-reply card.
   *  Strips newlines and truncates to keep the bubble compact. */
  const quotePreview = (m: Message): string => {
    if (m.deleted) return '(message deleted)'
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

  const startCall = async (callType: 'audio' | 'video') => {
    // For v1 the backend expects a placeholder SDP — the real offer is
    // generated by ``InCallPage`` on mount via ``RtcTransport``.
    const r = await api.post('/api/calls', {
      conversation_id: convId,
      call_type: callType,
      sdp_offer: 'v=0\r\n',
    }) as { call_id: string }
    location.route(`/calls/${r.call_id}`)
  }

  if (loading.value) return <Spinner />
  const myUserId = currentUser.value?.user_id

  const status = statusLine(threadMembers.value)
  // Compact status modifier for the dot in the header: 'online' → green,
  // 'idle' → amber, anything else → no dot.
  const peers = threadMembers.value.filter(m => !m.is_self)
  const headerDot: 'online' | 'idle' | null = peers.length === 1
    ? (peers[0].is_online ? (peers[0].is_idle ? 'idle' : 'online') : null)
    : (peers.some(p => p.is_online) ? 'online' : null)

  return (
    <div class="sh-thread">
      <div class="sh-thread-header">
        <div class="sh-thread-header-status" aria-live="polite">
          {headerDot && (
            <span class={`sh-thread-header-dot sh-thread-header-dot--${headerDot}`}
                  aria-hidden="true" />
          )}
          {status && <span class="sh-thread-header-status-line">{status}</span>}
        </div>
        <CallButtons convId={convId} onStart={startCall} />
        <a class="sh-link" href={`/dms/${convId}/calls`}>History</a>
      </div>
      {gaps.value.length > 0 && (
        <div class="sh-dm-gap-banner" role="status" aria-live="polite">
          <span aria-hidden="true">⚠️</span>
          <span>
            {gaps.value.length === 1
              ? 'A message may be missing from this conversation.'
              : `${gaps.value.length} messages may be missing from this conversation.`}
            {' '}Ask the sender to repost if it looks wrong.
          </span>
        </div>
      )}
      <div class="sh-messages">
        {messages.value.map(m => {
          if (m.type === 'call_event') {
            return <CallEventRow key={m.id} m={m} onCallBack={startCall} />
          }
          const mine = m.sender_user_id === myUserId
          // Look up the parent message for inline rendering of the
          // quoted-reply card. Missing parents (loaded out of window or
          // soft-deleted) fall through to a small placeholder.
          const parent = m.reply_to_id
            ? messages.value.find(x => x.id === m.reply_to_id)
            : null
          return (
            <div
              key={m.id}
              data-msg-id={m.id}
              class={`sh-message ${mine ? 'sh-message--mine' : ''} ${m.deleted ? 'sh-message--deleted' : ''}`}
            >
              {!mine && <strong>{senderName(m.sender_user_id)}</strong>}
              {m.reply_to_id && (
                <button
                  type="button"
                  class="sh-message-quote"
                  onClick={() => parent && scrollToMessage(parent.id)}
                  aria-label={parent
                    ? `Reply to ${senderName(parent.sender_user_id)}: ${quotePreview(parent)}`
                    : 'Reply to a message'}
                >
                  <span class="sh-message-quote-author">
                    {parent ? senderName(parent.sender_user_id) : 'Unknown'}
                  </span>
                  <span class="sh-message-quote-body">
                    {parent ? quotePreview(parent) : '(message unavailable)'}
                  </span>
                </button>
              )}
              <p style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                {m.deleted ? '(message deleted)' : m.content}
              </p>
              <div class="sh-message-meta">
                <time>{new Date(m.created_at).toLocaleTimeString([],
                  { hour: '2-digit', minute: '2-digit' })}</time>
                {mine && (
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
              {!m.deleted && (
                <button
                  type="button"
                  class="sh-message-reply-btn"
                  title="Reply"
                  aria-label={`Reply to ${senderName(m.sender_user_id)}`}
                  onClick={() => { replyTo.value = m }}
                >
                  ↩
                </button>
              )}
            </div>
          )
        })}
      </div>
      <TypingIndicator scope={convId} />
      {replyTo.value && (
        <div class="sh-composer-reply" role="status" aria-live="polite">
          <div class="sh-composer-reply-body">
            <span class="sh-composer-reply-author">
              Replying to {senderName(replyTo.value.sender_user_id)}
            </span>
            <span class="sh-composer-reply-preview">
              {quotePreview(replyTo.value)}
            </span>
          </div>
          <button
            type="button"
            class="sh-composer-reply-clear"
            aria-label="Cancel reply"
            onClick={() => { replyTo.value = null }}
          >×</button>
        </div>
      )}
      <form class="sh-composer" onSubmit={handleSend}>
        <input name="content" placeholder="Type a message..." autocomplete="off"
          onInput={handleInput} />
        <Button type="submit">Send</Button>
      </form>
    </div>
  )
}
