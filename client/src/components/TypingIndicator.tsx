/**
 * TypingIndicator — show who is typing (§23.9).
 *
 * Subscribes to WS ``conversation.user_typing`` frames (the canonical event
 * emitted by ``TypingService``). Each entry is keyed by
 * ``conversation_id|sender_user_id`` so multiple threads on screen don't
 * cross-talk; the component filters by its ``scope`` prop.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

interface TypingState {
  conversation_id: string
  display: string
  ts: number
}

const typingUsers = signal<Map<string, TypingState>>(new Map())

if (typeof window !== 'undefined') {
  ws.on('conversation.user_typing', (evt) => {
    const data = evt.data as {
      conversation_id?: string
      sender_user_id?: string
      sender_username?: string
    }
    const cid = data.conversation_id
    const uid = data.sender_user_id
    if (!cid || !uid) return
    const map = new Map(typingUsers.value)
    map.set(`${cid}|${uid}`, {
      conversation_id: cid,
      display: data.sender_username || uid,
      ts: Date.now(),
    })
    typingUsers.value = map
  })
  // Sweep entries older than 3 s — server publishes on each keystroke.
  setInterval(() => {
    const now = Date.now()
    const map = new Map(typingUsers.value)
    let changed = false
    for (const [key, st] of map) {
      if (now - st.ts > 3000) { map.delete(key); changed = true }
    }
    if (changed) typingUsers.value = map
  }, 1000)
}

export function sendTyping(conversationId: string) {
  ws.send('typing', { conversation_id: conversationId })
}

export function TypingIndicator({ scope }: { scope?: string }) {
  const all = Array.from(typingUsers.value.values())
  const users = scope ? all.filter(s => s.conversation_id === scope) : all
  if (users.length === 0) return null
  const names = users.map(s => s.display)
  const label = names.length === 1 ? `${names[0]} is typing` :
    names.length === 2 ? `${names[0]} and ${names[1]} are typing` :
    `${names.length} people are typing`
  return <div class="sh-typing" aria-live="polite"><span class="sh-typing-dots">•••</span> {label}</div>
}
