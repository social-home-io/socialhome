/**
 * privateReply — reply to a highlight frame in a 1:1 chat with its author
 * (owner decision 11).
 *
 * Gets or creates the DM (``POST /api/conversations/dm`` is idempotent:
 * an existing chat with that person comes back), then sends the reply
 * through ``POST /api/highlights/frames/{id}/dm-reply``, which freezes a
 * snapshot of the frame on the message so it still reads after the frame
 * expires. A household member is addressed by username, someone from a
 * paired household by user id. Resolves to the conversation id, so the
 * caller can link to the chat.
 */
import { api } from '@/api'
import { householdUsers } from '@/store/householdUsers'

export interface PrivateReply {
  frameId: string
  authorId: string
  content: string
  /** ``?via_post=…`` when the highlight was opened from a share card. */
  via: string
}

export async function replyPrivately({ frameId, authorId, content, via }: PrivateReply): Promise<string> {
  const username = householdUsers.value.get(authorId)?.username
  const conv = await api.post(
    '/api/conversations/dm',
    username ? { username } : { user_id: authorId },
  ) as { id: string }
  await api.post(`/api/highlights/frames/${encodeURIComponent(frameId)}/dm-reply${via}`, {
    conversation_id: conv.id,
    content: content.trim(),
  })
  return conv.id
}
