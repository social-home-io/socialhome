/**
 * HighlightPickerDialog — pick one of your active highlights to share into a
 * household / space feed (§Highlights).
 *
 * Driven by a single global signal so any composer surface can flip it
 * open and pass the destination scope. Submission POSTs
 * ``/api/highlights/{id}/share`` and the resulting feed-post id is
 * returned to the caller via the ``onShared`` callback so the surface
 * can refresh its feed inline.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { contentWrite } from '@/utils/contentWrite'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { currentUser } from '@/store/auth'
import type { HighlightInboxItem } from '@/types'
import { addBase } from '@/baseUrl'
import { t, isOne } from '@/i18n/i18n'

const open = signal(false)
const scope = signal<'household' | 'space'>('household')
const spaceId = signal<string | null>(null)
const note = signal<string>('')
const items = signal<HighlightInboxItem[]>([])
const loading = signal<boolean>(false)
const submittingId = signal<string | null>(null)
let onSharedCb: ((postId: string) => void) | null = null


export interface OpenHighlightPickerOptions {
  scope: 'household' | 'space'
  spaceId?: string | null
  onShared?: (postId: string) => void
}


export function openHighlightPicker(opts: OpenHighlightPickerOptions): void {
  scope.value = opts.scope
  spaceId.value = opts.spaceId ?? null
  note.value = ''
  items.value = []
  loading.value = true
  submittingId.value = null
  onSharedCb = opts.onShared ?? null
  open.value = true
  api.get('/api/highlights')
    .then((rows: HighlightInboxItem[]) => {
      // Only the user's own highlights are sharable — re-sharing someone
      // else's content beyond the audience they picked would be a
      // privacy footgun.
      const me = currentUser.value?.user_id
      items.value = (rows ?? []).filter(r => r.highlight.author_user_id === me)
    })
    .catch(() => {
      items.value = []
    })
    .finally(() => {
      loading.value = false
    })
}


export function HighlightPickerDialog() {
  useEffect(() => {
    if (!open.value) return
  }, [open.value])

  const submit = async (highlightId: string) => {
    if (submittingId.value) return
    submittingId.value = highlightId
    try {
      const body: Record<string, unknown> = { scope: scope.value }
      if (scope.value === 'space' && spaceId.value) {
        body.space_id = spaceId.value
      }
      if (note.value.trim()) body.note = note.value.trim()
      // Into a space whose posts are "Reviewed" (§4.3) the share is held
      // for a moderator — ``contentWrite`` toasts it; no post exists yet.
      const res = await contentWrite<{ post_id?: string }>(
        api.post(`/api/highlights/${highlightId}/share`, body),
        { spaceId: scope.value === 'space' ? spaceId.value : null },
      )
      open.value = false
      if (res.queued) return
      showToast(t('highlight.shared'), 'success')
      if (res.data?.post_id && onSharedCb) onSharedCb(res.data.post_id)
    } catch (err: unknown) {
      showToast(t('highlight.share_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      submittingId.value = null
    }
  }

  if (!open.value) return null
  return (
    <Modal open={open.value} onClose={() => { open.value = false }} title={t('composer.share_highlight')}>
      <div class="sh-highlight-picker">
        {loading.value && <p class="sh-muted">{t('highlight.picker.loading')}</p>}
        {!loading.value && items.value.length === 0 && (
          <p class="sh-muted">
            {t('highlight.picker.empty_before')}{' '}
            <a href={addBase('/highlights/new')} class="sh-link">{t('highlight.picker.empty_link')}</a>{t('highlight.picker.empty_after')}
          </p>
        )}
        {!loading.value && items.value.length > 0 && (
          <>
            <label class="sh-form-row">
              {t('highlight.picker.note_label')}
              <input
                type="text"
                value={note.value}
                onInput={e => { note.value = (e.target as HTMLInputElement).value }}
                placeholder={t('highlight.picker.note_placeholder')}
                maxLength={140}
              />
            </label>
            <div class="sh-highlight-picker-grid">
              {items.value.map(item => {
                const first = item.frames[0]
                return (
                  <button
                    key={item.highlight.id}
                    type="button"
                    class="sh-highlight-picker-tile"
                    onClick={() => void submit(item.highlight.id)}
                    disabled={submittingId.value !== null}
                  >
                    {first && first.frame_type === 'image' && (
                      <img
                        src={first.media_url}
                        alt=""
                        class="sh-highlight-picker-thumb"
                      />
                    )}
                    {first && first.frame_type === 'video' && (
                      <span class="sh-highlight-picker-thumb sh-highlight-picker-thumb--video">
                        🎬
                      </span>
                    )}
                    <span class="sh-highlight-picker-meta">
                      <strong>{item.highlight.highlight_date}</strong>
                      <span class="sh-muted">
                        {t(isOne(item.frames.length) ? 'highlight.frames_one' : 'highlight.frames', { n: String(item.frames.length) })}
                      </span>
                    </span>
                  </button>
                )
              })}
            </div>
          </>
        )}
        <div class="sh-form-actions sh-highlight-picker-actions">
          <Button variant="secondary" onClick={() => { open.value = false }}>
            {t('common.cancel')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
