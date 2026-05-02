import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { Conversation } from '@/types'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { openNewDm } from '@/components/NewDmDialog'
import { Avatar } from '@/components/Avatar'

const conversations = signal<Conversation[]>([])
const loading = signal(true)

const reload = () =>
  api.get('/api/conversations').then(data => {
    conversations.value = data
  }).catch(() => { /* noop — keep current list on transient failures */ })

export default function DmInboxPage() {
  useTitle('Messages')
  useEffect(() => {
    void reload().finally(() => { loading.value = false })
    // Refresh on any DM frame the server fans out — new conversations
    // and new messages both bump ``last_message_at`` ordering.
    const offMsg  = ws.on('dm.message',              () => { void reload() })
    const offConv = ws.on('dm.conversation.created', () => { void reload() })
    return () => { offMsg(); offConv() }
  }, [])

  if (loading.value) return <Spinner />

  return (
    <div class="sh-dms">
      <div class="sh-page-header">
        <Button onClick={() => openNewDm()}>+ New message</Button>
      </div>
      {conversations.value.length === 0 && (
        <div class="sh-empty-state">
          <p>No conversations yet.</p>
          <p class="sh-muted">Start a conversation with someone in your household.</p>
        </div>
      )}
      {conversations.value.map(c => (
        <a key={c.id} href={`/dms/${c.id}`} class="sh-dm-row">
          <Avatar name={c.name || 'DM'} size={40} />
          <div class="sh-dm-info">
            <strong>{c.name || 'Direct message'}</strong>
            <span class="sh-badge">{c.type === 'group_dm' ? 'Group' : 'DM'}</span>
          </div>
          {c.last_message_at && (
            <time class="sh-muted">{new Date(c.last_message_at).toLocaleString()}</time>
          )}
        </a>
      ))}
    </div>
  )
}
