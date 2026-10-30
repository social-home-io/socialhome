/**
 * A listing's public questions and answers — the seller's FAQ (v_75).
 *
 * * :func:`ListingFaq` — the "Questions and answers" block on a listing.
 *   Everyone who sees the listing reads it; on a feed card it is
 *   collapsed (``collapsible``), on the listing detail it is open. The
 *   seller adds, rewords and removes entries here while the listing is
 *   active.
 * * :func:`BazaarFaqEditor` — the add / edit dialog. "Add to FAQ" in an
 *   Ask-the-seller thread opens it pre-filled (the buyer's question, the
 *   seller's reply); the seller rewords before anything is published. An
 *   entry never names who asked.
 *
 * Plain text only: questions and answers render as text nodes (no
 * markdown, no HTML). The server cleans and bounds them the same way the
 * editor counts them: a one-line question ≤ 200, an answer ≤ 500.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import type { BazaarFaqEntry } from '@/types'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { confirmDialog } from './confirm'

export const FAQ_MAX_ENTRIES = 10
export const FAQ_QUESTION_MAX = 200
export const FAQ_ANSWER_MAX = 500

/** The question as the server stores it: one line, trimmed. */
export function faqQuestionText(raw: string): string {
  return raw.split(/\s+/).filter(Boolean).join(' ')
}

/** A buyer's message, cut to a question the editor accepts: one line,
 *  at most :data:`FAQ_QUESTION_MAX` characters (the seller rewords). */
export function faqQuestionFromMessage(text: string): string {
  return faqQuestionText(text).slice(0, FAQ_QUESTION_MAX)
}

export interface FaqEditorProps {
  open: boolean
  postId: string
  /** The entry being reworded; absent for a new one. */
  entry?: BazaarFaqEntry | null
  /** Pre-filled text for a new entry ("Add to FAQ" from a thread). */
  initialQuestion?: string
  initialAnswer?: string
  onClose: () => void
  onSaved?: (entry: BazaarFaqEntry) => void
}

export function BazaarFaqEditor({
  open, postId, entry, initialQuestion = '', initialAnswer = '', onClose, onSaved,
}: FaqEditorProps) {
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setQuestion(entry ? entry.question : faqQuestionFromMessage(initialQuestion))
    setAnswer((entry ? entry.answer : initialAnswer).slice(0, FAQ_ANSWER_MAX))
    setError(null)
  }, [open, entry, initialQuestion, initialAnswer])

  const q = faqQuestionText(question)
  const a = answer.trim()
  const valid = q.length > 0 && q.length <= FAQ_QUESTION_MAX
    && a.length > 0 && a.length <= FAQ_ANSWER_MAX

  const save = async (e?: Event) => {
    e?.preventDefault()
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    try {
      const body = { question: q, answer: a }
      const saved = (entry
        ? await api.patch(`/api/bazaar/${postId}/faq/${entry.entry_id}`, body)
        : await api.post(`/api/bazaar/${postId}/faq`, body)) as BazaarFaqEntry
      showToast(t('bazaar.faq.saved'), 'success')
      onSaved?.(saved)
      onClose()
    } catch (err: unknown) {
      setError((err as Error).message || t('bazaar.faq.save_failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose}
           title={entry ? t('bazaar.faq.editor.title_edit') : t('bazaar.faq.editor.title_add')}>
      <form class="sh-form sh-bazaar-faq-editor" onSubmit={(e) => void save(e)}>
        <p class="sh-muted sh-bazaar-faq-editor__hint">{t('bazaar.faq.editor.hint')}</p>
        <div>
          <label for="sh-faq-question">{t('bazaar.faq.editor.question')}</label>
          {/* One line on the listing: Enter saves instead of breaking it. */}
          <textarea id="sh-faq-question" rows={2}
                    maxLength={FAQ_QUESTION_MAX} value={question}
                    onInput={(e) => setQuestion((e.target as HTMLTextAreaElement).value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.isComposing) {
                        e.preventDefault()
                        void save()
                      }
                    }} />
          <span class="sh-char-count" aria-live="polite">
            {t('bazaar.faq.editor.counter', {
              n: String(q.length), max: String(FAQ_QUESTION_MAX),
            })}
          </span>
        </div>
        <div>
          <label for="sh-faq-answer">{t('bazaar.faq.editor.answer')}</label>
          <textarea id="sh-faq-answer" rows={4} maxLength={FAQ_ANSWER_MAX}
                    value={answer}
                    onInput={(e) => setAnswer((e.target as HTMLTextAreaElement).value)} />
          <span class="sh-char-count" aria-live="polite">
            {t('bazaar.faq.editor.counter', {
              n: String(a.length), max: String(FAQ_ANSWER_MAX),
            })}
          </span>
        </div>
        {error && <p class="sh-form-hint sh-form-hint--error" role="alert">{error}</p>}
        <div class="sh-form-actions">
          <Button type="button" variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" loading={busy} disabled={!valid}>
            {entry ? t('common.save') : t('bazaar.faq.editor.publish')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export interface ListingFaqProps {
  postId: string
  entries: BazaarFaqEntry[]
  /** The viewer sells this listing. */
  isSeller: boolean
  /** The listing is still active: the seller may change the FAQ. */
  editable: boolean
  /** A feed card: start folded. The listing detail shows it open. */
  collapsible?: boolean
  /** The FAQ changed here (the WS frame refreshes other viewers). */
  onChanged?: () => void
}

export function ListingFaq({
  postId, entries, isSeller, editable, collapsible = false, onChanged,
}: ListingFaqProps) {
  const [editing, setEditing] = useState<BazaarFaqEntry | null>(null)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const canEdit = isSeller && editable
  if (entries.length === 0 && !canEdit) return null

  const full = entries.length >= FAQ_MAX_ENTRIES
  const remove = async (entry: BazaarFaqEntry) => {
    if (!await confirmDialog(t('bazaar.faq.remove_confirm'), { destructive: true })) return
    setBusy(entry.entry_id)
    try {
      await api.delete(`/api/bazaar/${postId}/faq/${entry.entry_id}`)
      showToast(t('bazaar.faq.removed'), 'info')
      onChanged?.()
    } catch (err: unknown) {
      showToast((err as Error).message, 'error')
    } finally {
      setBusy(null)
    }
  }

  const list = entries.length > 0 && (
    <dl class="sh-bazaar-faq__list">
      {entries.map(e => (
        <div key={e.entry_id} class="sh-bazaar-faq__item">
          <dt class="sh-bazaar-faq__q">{e.question}</dt>
          <dd class="sh-bazaar-faq__a">{e.answer}</dd>
          {canEdit && (
            <dd class="sh-bazaar-faq__actions">
              <button type="button" class="sh-link sh-bazaar-faq__action"
                      aria-label={t('bazaar.faq.edit_aria', { question: e.question })}
                      onClick={() => setEditing(e)}>
                {t('common.edit')}
              </button>
              <button type="button" class="sh-link sh-link--danger sh-bazaar-faq__action"
                      disabled={busy === e.entry_id}
                      aria-label={t('bazaar.faq.remove_aria', { question: e.question })}
                      onClick={() => void remove(e)}>
                {t('bazaar.faq.remove')}
              </button>
            </dd>
          )}
        </div>
      ))}
    </dl>
  )
  const sellerTools = canEdit && (
    <div class="sh-bazaar-faq__tools">
      {entries.length === 0 && (
        <p class="sh-muted sh-bazaar-faq__empty">{t('bazaar.faq.empty_seller')}</p>
      )}
      {full ? (
        <p class="sh-muted sh-bazaar-faq__full">
          {t('bazaar.faq.full', { max: String(FAQ_MAX_ENTRIES) })}
        </p>
      ) : (
        <Button variant="secondary" onClick={() => setAdding(true)}>
          + {t('bazaar.faq.add')}
        </Button>
      )}
    </div>
  )
  const dialogs = canEdit && (
    <BazaarFaqEditor open={adding || editing !== null} postId={postId}
                     entry={editing}
                     onClose={() => { setAdding(false); setEditing(null) }}
                     onSaved={() => onChanged?.()} />
  )
  const count = entries.length > 0 && (
    <span class="sh-badge sh-bazaar-faq__count">{entries.length}</span>
  )

  if (collapsible) {
    return (
      <details class="sh-bazaar-faq sh-bazaar-faq--collapsible">
        <summary class="sh-bazaar-faq__title">
          <span>{t('bazaar.faq.title')}</span>{count}
        </summary>
        {list}
        {sellerTools}
        {dialogs}
      </details>
    )
  }
  return (
    <section class="sh-bazaar-faq" aria-label={t('bazaar.faq.title')}>
      <h4 class="sh-bazaar-faq__title">
        <span>{t('bazaar.faq.title')}</span>{count}
      </h4>
      {list}
      {sellerTools}
      {dialogs}
    </section>
  )
}
