/**
 * DraftPersistence — auto-save drafts to localStorage (§23.11).
 */
import { formatLocale, t } from '@/i18n/i18n'

const DRAFT_PREFIX = 'sh_draft_'

export function saveDraft(context: string, content: string) {
  if (content.trim()) {
    localStorage.setItem(DRAFT_PREFIX + context, JSON.stringify({
      content, savedAt: new Date().toISOString(),
    }))
  } else {
    localStorage.removeItem(DRAFT_PREFIX + context)
  }
}

export function loadDraft(context: string): { content: string; savedAt: string } | null {
  const raw = localStorage.getItem(DRAFT_PREFIX + context)
  if (!raw) return null
  try { return JSON.parse(raw) } catch { return null }
}

export function clearDraft(context: string) {
  localStorage.removeItem(DRAFT_PREFIX + context)
}

export function DraftBanner({ context, onRestore, onDiscard }: {
  context: string; onRestore: (content: string) => void; onDiscard: () => void
}) {
  const draft = loadDraft(context)
  if (!draft) return null
  const timeAgo = new Date(draft.savedAt).toLocaleString(formatLocale())
  return (
    <div class="sh-draft-banner" role="alert">
      <span>{t('draft.unsaved_from', { time: timeAgo })}</span>
      <button class="sh-link" onClick={() => { onRestore(draft.content); clearDraft(context) }}>{t('draft.restore')}</button>
      <button class="sh-link sh-link--danger" onClick={() => { onDiscard(); clearDraft(context) }}>{t('draft.discard')}</button>
    </div>
  )
}
