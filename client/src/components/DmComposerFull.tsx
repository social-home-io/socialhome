/**
 * DmComposerFull — full-featured DM composer (§23.47e).
 * Extends the basic input with media attachment, type picker, and mentions.
 */
import { signal } from '@preact/signals'
import { Button } from './Button'
import { SttButton } from './SttButton'
import { UploadProgressBar, uploadWithProgress } from './UploadProgress'
import { sendTyping } from './TypingIndicator'

const content = signal('')
const msgType = signal('text')
// Canonical (unsigned) media URL, sent on the ``send_message`` API call.
const mediaUrl = signal<string | null>(null)
// Short-lived signed URL for the local preview ``<img>`` only.
const mediaPreviewUrl = signal<string | null>(null)
const sending = signal(false)

const TYPE_ICONS: Record<string, string> = {
  text: '🔤', image: '📷', video: '🎬', location: '📍',
}

export function DmComposerFull({ conversationId, onSend }: {
  conversationId: string
  onSend: (content: string, type: string, mediaUrl?: string) => Promise<void>
}) {
  const handleSend = async () => {
    if (sending.value || (!content.value.trim() && !mediaUrl.value)) return
    sending.value = true
    try {
      await onSend(content.value, msgType.value, mediaUrl.value || undefined)
      content.value = ''
      mediaUrl.value = null
      mediaPreviewUrl.value = null
      msgType.value = 'text'
    } finally { sending.value = false }
  }

  const handleAttach = async () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = 'image/*,video/*'
    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) return
      try {
        const result = await uploadWithProgress(file)
        mediaUrl.value = result.url
        mediaPreviewUrl.value = result.signed_url
        msgType.value = file.type.startsWith('video/') ? 'video' : 'image'
      } catch {}
    }
    input.click()
  }

  const handleInput = (e: Event) => {
    content.value = (e.target as HTMLInputElement).value
    sendTyping(conversationId)
  }

  return (
    <div class="sh-dm-composer-full">
      <UploadProgressBar />
      <div class="sh-dm-type-picker">
        {Object.entries(TYPE_ICONS).map(([type, icon]) => (
          <button key={type} type="button"
            class={`sh-type-btn ${msgType.value === type ? 'sh-type-btn--active' : ''}`}
            onClick={() => msgType.value = type}>{icon}</button>
        ))}
        <button class="sh-attach-btn" onClick={handleAttach} title="Attach file">📎</button>
      </div>
      {mediaUrl.value && (
        <div class="sh-dm-media-preview">
          <img src={mediaPreviewUrl.value ?? mediaUrl.value} class="sh-dm-thumb" />
          <button onClick={() => {
            mediaUrl.value = null
            mediaPreviewUrl.value = null
            msgType.value = 'text'
          }}>✕</button>
        </div>
      )}
      <div class="sh-dm-input-row">
        <input value={content.value} onInput={handleInput}
          placeholder="Type a message..." autocomplete="off"
          onKeyDown={(e) => e.key === 'Enter' && handleSend()} />
        <SttButton onText={(t) => {
          const sep = content.value && !/\s$/.test(content.value) ? ' ' : ''
          content.value = content.value + sep + t
        }} />
        <Button onClick={handleSend} loading={sending.value}
          disabled={!content.value.trim() && !mediaUrl.value}>Send</Button>
      </div>
    </div>
  )
}
