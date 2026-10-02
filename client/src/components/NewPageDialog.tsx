/**
 * NewPageDialog — "New page" modal (title, then the editor opens).
 *
 * Composes on top of ``Modal`` so it inherits the household focus-trap
 * + Escape + focus-restore behaviour all other dialogs share.
 *
 * ``reviewed`` (a space whose pages are "Reviewed", §4.3): a member's new
 * page waits for a moderator, so the editor can't open on it — the
 * dialog then also takes the body, and says the page goes to review.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { Modal } from './Modal'
import { Button } from './Button'
import { t } from '@/i18n/i18n'

interface Props {
  open: boolean
  /** A new page is held for review: collect the body here too. */
  reviewed?: boolean
  onCreate: (title: string, content: string) => void | Promise<void>
  onCancel: () => void
}

export function NewPageDialog({ open, reviewed = false, onCreate, onCancel }: Props) {
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (open) {
      setTitle('')
      setContent('')
      setBusy(false)
      // ``Modal`` focuses the first focusable on open. The title input
      // is the first interactive element below, so this nudge keeps
      // the caret in the field after the StrictMode double-mount + the
      // input's own value/placeholder hydration completes.
      setTimeout(() => ref.current?.focus(), 10)
    }
  }, [open])

  const submit = async (e: Event) => {
    e.preventDefault()
    const clean = title.trim()
    if (!clean || busy) return
    setBusy(true)
    try { await onCreate(clean, reviewed ? content : '') }
    finally { setBusy(false) }
  }

  return (
    <Modal open={open} onClose={onCancel} title={t('pages.new_page')}>
      <form class="sh-form" onSubmit={submit}>
        <label>
          {t('pages.title_label')}
          <input
            ref={ref}
            value={title}
            maxLength={200}
            placeholder={t('pages.title_placeholder')}
            onInput={(e) => setTitle((e.target as HTMLInputElement).value)}
            required
          />
        </label>
        {reviewed && (
          <>
            <label>
              {t('pages.new_content_label')}
              <textarea
                rows={6}
                value={content}
                placeholder={t('pages.source_placeholder')}
                onInput={(e) => setContent((e.target as HTMLTextAreaElement).value)}
              />
            </label>
            <p class="sh-muted" role="note">{t('pages.new_review_note')}</p>
          </>
        )}
        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onCancel}>{t('common.cancel')}</Button>
          <Button type="submit" loading={busy} disabled={!title.trim()}>
            {reviewed ? t('pages.submit_review') : t('pages.create')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
