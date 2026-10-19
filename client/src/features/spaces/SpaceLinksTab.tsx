/**
 * SpaceLinksTab — the quick-links editor inside Settings › Basics.
 *
 * Part of the Basics draft (owner decision 8): adding, renaming,
 * reordering and removing links only change the draft, which the page's
 * save bar saves or discards together with the text and the theme
 * (``settings/linksDraft.ts`` holds the diff and the save sequence).
 * Members see the saved links as chips in the space hero.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import {
  diffLinks, draftOf, invalidLinks, newDraftKey, saveLinks, validLink,
  type DraftLink, type StoredLink,
} from './settings/linksDraft'

/** What the settings page's save bar needs from the links editor. */
export interface LinksDraftHandle {
  /** Unsaved changes, as the save bar counts them. */
  count: number
  /** Rows Save can't send yet (no name / not a web address). */
  invalid: number
  /** Apply the draft; ``true`` when every call landed. */
  save: () => Promise<boolean>
  discard: () => void
}

interface Props {
  spaceId: string
  /** Reports the draft on every change (``null`` when it unmounts). */
  onDraft?: (draft: LinksDraftHandle | null) => void
}

const EMPTY = { label: '', url: '' }

export function SpaceLinksTab({ spaceId, onDraft }: Props) {
  const [stored, setStored] = useState<StoredLink[]>([])
  const [draft, setDraft] = useState<DraftLink[]>([])
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [loadError, setLoadError] = useState('')
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set())
  const [adding, setAdding] = useState(EMPTY)
  const [addError, setAddError] = useState<'required' | 'url' | null>(null)
  const addLabelRef = useRef<HTMLInputElement | null>(null)

  const load = async () => {
    setStatus('loading')
    try {
      const body = await api.get(`/api/spaces/${spaceId}/links`) as { links?: StoredLink[] }
      const rows = body.links ?? []
      setStored(rows)
      setDraft(draftOf(rows))
      setFailed(new Set())
      setStatus('ready')
    } catch (err: unknown) {
      setLoadError((err as Error)?.message ?? String(err))
      setStatus('error')
    }
  }

  useEffect(() => { void load() }, [spaceId])

  const diff = diffLinks(stored, draft)
  const invalid = invalidLinks(draft)

  const save = async (): Promise<boolean> => {
    const res = await saveLinks(api, spaceId, stored, draft)
    setStored(res.stored)
    // All landed: the draft becomes exactly what the server holds.
    setDraft(res.failed.length ? res.draft : draftOf(res.stored))
    setFailed(new Set(res.failed.map(f => f.key)))
    if (res.failed.length) {
      showToast(t('space.links.save_partial', { links: res.failed.map(f => f.label).join(', ') }), 'error')
    }
    return res.failed.length === 0
  }

  const discard = () => {
    setDraft(draftOf(stored))
    setFailed(new Set())
    setAdding(EMPTY)
    setAddError(null)
  }

  // Report the draft to the page (fresh closures each change).
  const key = JSON.stringify([stored, draft])
  useEffect(() => {
    onDraft?.({ count: diff.count, invalid: invalid.length, save, discard })
  }, [key])
  useEffect(() => () => onDraft?.(null), [])

  const edit = (k: string, patch: Partial<DraftLink>) => {
    setDraft(d => d.map(l => (l.key === k ? { ...l, ...patch } : l)))
    if (failed.has(k)) setFailed(f => new Set([...f].filter(x => x !== k)))
  }

  const move = (index: number, delta: -1 | 1) => {
    const to = index + delta
    if (to < 0 || to >= draft.length) return
    setDraft(d => {
      const next = [...d]
      const [row] = next.splice(index, 1)
      next.splice(to, 0, row)
      return next
    })
  }

  const remove = (k: string) => setDraft(d => d.filter(l => l.key !== k))

  const add = (e: Event) => {
    e.preventDefault()
    const verdict = validLink(adding.label, adding.url)
    if (verdict !== 'ok') {
      setAddError(verdict)
      return
    }
    setDraft(d => [...d, { key: newDraftKey(), id: null, label: adding.label.trim(), url: adding.url.trim() }])
    setAdding(EMPTY)
    setAddError(null)
    addLabelRef.current?.focus()
  }

  const isNew = (l: DraftLink) => !(l.id && stored.some(s => s.id === l.id))

  return (
    <section class="sh-space-links-tab">
      <h3 class="sh-ss-subheading">{t('space.links.title')}</h3>
      <p class="sh-ss-hint">{t('space.links.intro')}</p>

      {status === 'loading' && <Spinner />}

      {status === 'error' && (
        <div class="sh-space-links-editor__error" role="alert">
          <p>{t('space.links.load_failed', { error: loadError })}</p>
          <Button variant="secondary" onClick={() => void load()}>{t('common.try_again')}</Button>
        </div>
      )}

      {status === 'ready' && draft.length === 0 && (
        <p class="sh-muted">{t('space.links.empty')}</p>
      )}

      {status === 'ready' && draft.length > 0 && (
        <ul class="sh-space-links-editor" role="list">
          {draft.map((link, i) => {
            const verdict = validLink(link.label, link.url)
            const msgId = `sh-link-msg-${link.key}`
            const message = verdict === 'required'
              ? t('space.links.required')
              : verdict === 'url'
                ? t('space.links.invalid_url')
                : failed.has(link.key) ? t('space.links.not_saved') : null
            return (
              <li key={link.key} class="sh-space-links-editor__row" data-new={isNew(link) ? '' : undefined}>
                <input
                  class="sh-space-links-editor__label"
                  value={link.label}
                  maxLength={64}
                  aria-label={t('space.links.label_aria')}
                  aria-invalid={!link.label.trim() || undefined}
                  aria-describedby={message ? msgId : undefined}
                  onInput={(e) => edit(link.key, { label: (e.target as HTMLInputElement).value })}
                />
                <input
                  class="sh-space-links-editor__url"
                  type="url"
                  dir="ltr"
                  value={link.url}
                  maxLength={2048}
                  aria-label={t('space.links.url_aria')}
                  aria-invalid={verdict === 'url' || !link.url.trim() || undefined}
                  aria-describedby={message ? msgId : undefined}
                  onInput={(e) => edit(link.key, { url: (e.target as HTMLInputElement).value })}
                />
                <div class="sh-space-links-editor__actions">
                  <button type="button"
                          class="sh-icon-btn"
                          aria-label={t('space.links.move_up_named', { label: link.label || link.url })}
                          disabled={i === 0}
                          onClick={() => move(i, -1)}>↑</button>
                  <button type="button"
                          class="sh-icon-btn"
                          aria-label={t('space.links.move_down_named', { label: link.label || link.url })}
                          disabled={i === draft.length - 1}
                          onClick={() => move(i, 1)}>↓</button>
                  <button type="button"
                          class="sh-icon-btn sh-icon-btn--danger"
                          aria-label={t('space.links.remove_aria', { label: link.label || link.url })}
                          onClick={() => remove(link.key)}>✕</button>
                </div>
                {message && (
                  <p id={msgId} class="sh-space-links-editor__msg">{message}</p>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {status === 'ready' && (
        <form class="sh-space-links-editor__create" onSubmit={add} noValidate>
          <h4>{t('space.links.add')}</h4>
          <input type="text"
                 ref={addLabelRef}
                 value={adding.label}
                 aria-label={t('space.links.label_aria')}
                 placeholder={t('space.links.label_placeholder')}
                 maxLength={64}
                 aria-invalid={addError === 'required' && !adding.label.trim() ? true : undefined}
                 aria-describedby={addError ? 'sh-link-add-msg' : undefined}
                 onInput={(e) => { const label = (e.target as HTMLInputElement).value; setAdding(a => ({ ...a, label })) }} />
          <input type="url"
                 dir="ltr"
                 value={adding.url}
                 aria-label={t('space.links.url_aria')}
                 placeholder="https://…"
                 maxLength={2048}
                 aria-invalid={addError === 'url' || (addError === 'required' && !adding.url.trim()) ? true : undefined}
                 aria-describedby={addError ? 'sh-link-add-msg' : undefined}
                 onInput={(e) => { const url = (e.target as HTMLInputElement).value; setAdding(a => ({ ...a, url })) }} />
          <Button type="submit" variant="secondary">{t('space.links.add')}</Button>
          {addError && (
            <p id="sh-link-add-msg" class="sh-space-links-editor__msg" role="alert">
              {t(addError === 'required' ? 'space.links.required' : 'space.links.invalid_url')}
            </p>
          )}
        </form>
      )}
    </section>
  )
}
