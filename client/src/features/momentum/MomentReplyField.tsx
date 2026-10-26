/**
 * MomentReplyField — the reply field at the end of a moment's page, the one
 * place a reply is written (``/momentum/{id}/reply`` focuses it).
 *
 * Says where the reply goes, as the composer does (#1188): a reply is a
 * moment of its own from your household, 3 hops out (your household and
 * paired households for a protected account). The public switch shows only
 * under a public moment you could share publicly anyway (decision 2), and
 * is never pre-ticked — "Make new moments public" is for new moments.
 *
 * A reply can carry one photo or video, as replies from the composer did
 * (#1190 follow-up): the same checks and upload as the composer
 * (:func:`prepareMomentMedia`), a preview with ✕ under the field, and Reply
 * waits while it uploads. A picture alone is a reply.
 */
import type { Ref } from 'preact'
import { useId, useRef } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { isRestricted } from '@/components/ProtectedNotice'
import { Switch } from '@/components/Switch'
import { showToast } from '@/components/Toast'
import { UploadProgressBar } from '@/components/UploadProgress'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { registrations } from '@/store/momentPublic'
import type { Moment } from '@/types'
import { publicOnLabel } from './gfsNames'
import { prepareMomentMedia, type MomentMedia } from './momentMedia'

/** Server: ``MOMENT_MAX_CONTENT`` (the composer's limit too). */
const MAX_CONTENT = 1_000

interface Props {
  parent: Moment
  inputRef: Ref<HTMLTextAreaElement>
  /** The reply was posted (the server's moment). */
  onPosted: (m: Moment) => void
}

export function MomentReplyField({ parent, inputRef, onPosted }: Props) {
  const text = useSignal('')
  const isPublic = useSignal(false)
  const busy = useSignal(false)
  const media = useSignal<MomentMedia | null>(null)
  /** A picked file is being checked or uploaded — Reply waits. */
  const mediaBusy = useSignal(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const hintId = useId()
  const publicHintId = useId()
  const me = currentUser.value
  const isProtected = me?.protected === true
  const registered = registrations.value.length > 0
  const showPublic = registered && !isRestricted('public_moments') && parent.is_public === true

  const onPick = async (ev: Event) => {
    const input = ev.currentTarget as HTMLInputElement
    const file = input.files?.[0]
    input.value = ''
    if (!file || mediaBusy.value) return
    mediaBusy.value = true
    try {
      const got = await prepareMomentMedia(file)
      if (got) media.value = got
    } finally {
      mediaBusy.value = false
    }
  }

  const submit = async (ev: Event) => {
    ev.preventDefault()
    const content = text.value.trim()
    const m0 = media.value
    if ((!content && !m0) || busy.value || mediaBusy.value) return
    busy.value = true
    const body: Record<string, unknown> = { content, parent_moment_id: parent.id }
    if (m0) {
      body.media_url = m0.url
      body.media_type = m0.type
      if (m0.type === 'video') body.duration_ms = m0.durationMs
    }
    if (registered) body.is_public = showPublic && isPublic.value
    try {
      const m = await api.post('/api/moments', body) as Moment
      text.value = ''
      media.value = null
      isPublic.value = false
      onPosted(m)
    } catch (err: unknown) {
      showToast(t('moment.composer.failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      busy.value = false
    }
  }

  return (
    <form class="sh-moment-replyfield" onSubmit={submit}>
      <div class="sh-moment-replyfield__row">
        <Avatar name={me?.display_name || me?.username || ''} src={me?.picture_url ?? null} size={32} />
        <textarea
          ref={inputRef}
          class="sh-moment-replyfield__input"
          rows={1}
          dir="auto"
          maxLength={MAX_CONTENT}
          placeholder={t('moment.detail.reply_placeholder')}
          aria-label={t('moment.detail.reply_aria')}
          aria-describedby={hintId}
          value={text.value}
          onInput={(e) => { text.value = (e.target as HTMLTextAreaElement).value }}
          onKeyDown={(e) => {
            // Ctrl/⌘+Enter sends; Enter alone is a new line.
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void submit(e)
          }}
        />
        <input
          ref={fileInput}
          type="file"
          accept="image/*,video/*"
          class="sh-moment-replyfield__file"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => void onPick(e)}
        />
        <button
          type="button"
          class="sh-moment-replyfield__media"
          aria-label={t('moment.detail.reply_media')}
          title={t('moment.detail.reply_media')}
          aria-busy={mediaBusy.value ? 'true' : undefined}
          disabled={mediaBusy.value || media.value !== null}
          onClick={() => fileInput.current?.click()}
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            aria-hidden="true" focusable="false">
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <circle cx="9" cy="9" r="2" />
            <path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
          </svg>
        </button>
        <Button type="submit" loading={busy.value}
          disabled={mediaBusy.value || (text.value.trim() === '' && media.value === null)}>
          {t(showPublic && isPublic.value ? 'moment.composer.reply_public' : 'moment.composer.reply')}
        </Button>
      </div>
      {mediaBusy.value && <UploadProgressBar />}
      {media.value && (
        <div class="sh-moment-replyfield__attachment">
          {media.value.type === 'image' ? (
            <img src={media.value.previewUrl} alt="" class="sh-moment-replyfield__preview" />
          ) : (
            <video src={media.value.previewUrl} class="sh-moment-replyfield__preview"
              muted preload="metadata" />
          )}
          <button
            type="button"
            class="sh-moment-replyfield__remove"
            aria-label={t('composer.remove_attachment')}
            onClick={() => { media.value = null }}
          >✕</button>
        </div>
      )}
      <p id={hintId} class="sh-moment-replyfield__hint">
        {t(isProtected ? 'moment.composer.reach_reply_protected' : 'moment.composer.reach_reply')}
      </p>
      {showPublic && (
        <label class="sh-moment-replyfield__public">
          <span class="sh-moment-replyfield__public-text">
            <strong>{publicOnLabel(registrations.value, true)}</strong>
            <span id={publicHintId}>{t('moment.composer.public_hint')}</span>
          </span>
          <Switch
            checked={isPublic.value}
            aria-describedby={publicHintId}
            onChange={(next) => { isPublic.value = next }}
          />
        </label>
      )}
    </form>
  )
}
