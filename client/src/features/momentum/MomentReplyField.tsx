/**
 * MomentReplyField — the reply field at the end of a moment's page, the one
 * place a reply is written (``/momentum/{id}/reply`` focuses it).
 *
 * Says where the reply goes, as the composer does (#1188): a reply is a
 * moment of its own from your household, 3 hops out (your household and
 * paired households for a protected account). The public switch shows only
 * under a public moment you could share publicly anyway (decision 2), and
 * is never pre-ticked — "Make new moments public" is for new moments.
 */
import type { Ref } from 'preact'
import { useId } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { isRestricted } from '@/components/ProtectedNotice'
import { Switch } from '@/components/Switch'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { registrations } from '@/store/momentPublic'
import type { Moment } from '@/types'
import { publicOnLabel } from './gfsNames'

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
  const hintId = useId()
  const publicHintId = useId()
  const me = currentUser.value
  const isProtected = me?.protected === true
  const registered = registrations.value.length > 0
  const showPublic = registered && !isRestricted('public_moments') && parent.is_public === true

  const submit = async (ev: Event) => {
    ev.preventDefault()
    const content = text.value.trim()
    if (!content || busy.value) return
    busy.value = true
    const body: Record<string, unknown> = { content, parent_moment_id: parent.id }
    if (registered) body.is_public = showPublic && isPublic.value
    try {
      const m = await api.post('/api/moments', body) as Moment
      text.value = ''
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
        <Button type="submit" loading={busy.value} disabled={text.value.trim() === ''}>
          {t(showPublic && isPublic.value ? 'moment.composer.reply_public' : 'moment.composer.reply')}
        </Button>
      </div>
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
