/**
 * HighlightPublishMenu — author-only modal to publish a highlight to a paired
 * GFS and copy the public link (§highlights_public).
 *
 * Opens from a "Publish public link" button in the Highlights viewer
 * footer. Author picks a connected GFS, optionally labels the link
 * (e.g. "for-twitter"), and the modal calls
 * ``POST /api/highlights/{id}/publish``. The returned URL is shown with
 * a copy button. Author can also tap "Unpublish all" to revoke every
 * token under this highlight.
 *
 * Token list lives on the GFS — listing existing tokens is intentional
 * future work; v1 surface is "publish (mints a token), unpublish (drops
 * them all)".
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { showToast } from '@/components/Toast'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'

interface GfsConnection {
  id: string
  display_name: string
  status: string
}

interface PublishedToken {
  url: string
  token: string
  label: string | null
}

const open = signal(false)
const highlightId = signal('')
const connections = signal<GfsConnection[]>([])
const selectedGfs = signal('')
const label = signal('')
const submitting = signal(false)
const lastIssued = signal<PublishedToken | null>(null)
const isPublished = signal(false)

export function openPublishMenu(id: string, alreadyPublished: boolean): void {
  highlightId.value = id
  isPublished.value = alreadyPublished
  label.value = ''
  lastIssued.value = null
  open.value = true
}

export function HighlightPublishMenu() {
  useEffect(() => {
    if (!open.value) return
    void api.get<{ connections: GfsConnection[] }>('/api/gfs/connections')
      .then((res) => {
        const active = (res.connections ?? []).filter(c => c.status === 'active')
        connections.value = active
        if (active.length === 1) selectedGfs.value = active[0].id
      })
      .catch(() => { connections.value = [] })
  }, [open.value])

  const submit = async () => {
    if (submitting.value || !selectedGfs.value) return
    submitting.value = true
    try {
      const res = await api.post<PublishedToken>(
        `/api/highlights/${highlightId.value}/publish`,
        { gfs_id: selectedGfs.value, label: label.value || undefined },
      )
      lastIssued.value = res
      isPublished.value = true
      showToast(t('highlight.publish.created'), 'success')
    } catch (err: unknown) {
      showToast(t('highlight.publish.failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      submitting.value = false
    }
  }

  const copy = async () => {
    if (!lastIssued.value) return
    try {
      await navigator.clipboard.writeText(lastIssued.value.url)
      showToast(t('highlight.publish.copied'), 'success')
    } catch {
      showToast(t('highlight.publish.copy_failed'), 'info')
    }
  }

  const unpublishAll = async () => {
    if (submitting.value) return
    submitting.value = true
    try {
      await api.delete(`/api/highlights/${highlightId.value}/publish`)
      isPublished.value = false
      lastIssued.value = null
      showToast(t('highlight.publish.removed'), 'info')
    } catch (err: unknown) {
      showToast(t('highlight.publish.remove_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title={t('highlight.publish.title')}
    >
      <p class="sh-muted">
        {t('highlight.publish.intro')}
      </p>

      {isRestricted('public_links') && (
        <ProtectedNotice capability="public_links" />
      )}

      {!isRestricted('public_links') && connections.value.length === 0 && (
        <p class="sh-muted">
          {t('highlight.publish.no_gfs_before')}{' '}
          <a href={addBase('/settings/connections')}>{t('highlight.publish.no_gfs_link')}</a>{t('highlight.publish.no_gfs_after')}
        </p>
      )}

      {!isRestricted('public_links') && connections.value.length > 0 && !lastIssued.value && (
        <form
          class="sh-highlight-publish-form"
          onSubmit={(e) => { e.preventDefault(); void submit() }}
        >
          <label class="sh-form-row">
            <span>{t('highlight.publish.gfs_label')}</span>
            {connections.value.length === 1 ? (
              <input
                type="text"
                value={connections.value[0].display_name}
                disabled
              />
            ) : (
              <select
                value={selectedGfs.value}
                onChange={(e) => {
                  selectedGfs.value = (e.currentTarget as HTMLSelectElement).value
                }}
              >
                <option value="">{t('highlight.publish.gfs_pick')}</option>
                {connections.value.map(c => (
                  <option key={c.id} value={c.id}>{c.display_name}</option>
                ))}
              </select>
            )}
          </label>
          <label class="sh-form-row">
            <span>{t('highlight.publish.label')}</span>
            <input
              type="text"
              maxLength={64}
              placeholder={t('highlight.publish.label_placeholder')}
              value={label.value}
              onInput={(e) => {
                label.value = (e.currentTarget as HTMLInputElement).value
              }}
            />
          </label>
          <div class="sh-modal-actions">
            <Button
              type="submit"
              disabled={submitting.value || !selectedGfs.value}
            >
              {submitting.value ? t('highlight.publish.creating') : t('highlight.publish.create')}
            </Button>
          </div>
        </form>
      )}

      {lastIssued.value && (
        <div class="sh-highlight-publish-issued">
          <p><strong>{t('highlight.publish.your_link')}</strong></p>
          <code class="sh-highlight-publish-url">{lastIssued.value.url}</code>
          <div class="sh-modal-actions">
            <Button onClick={copy}>{t('highlight.publish.copy')}</Button>
          </div>
        </div>
      )}

      {(isPublished.value || lastIssued.value) && (
        <div class="sh-modal-actions sh-modal-actions--secondary">
          <Button
            variant="danger"
            onClick={unpublishAll}
            disabled={submitting.value}
          >
            {t('highlight.publish.remove_all')}
          </Button>
        </div>
      )}
    </Modal>
  )
}
