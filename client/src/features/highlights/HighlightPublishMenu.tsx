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
      showToast('Public link minted', 'success')
    } catch (err: unknown) {
      showToast(`Couldn't publish: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      submitting.value = false
    }
  }

  const copy = async () => {
    if (!lastIssued.value) return
    try {
      await navigator.clipboard.writeText(lastIssued.value.url)
      showToast('Link copied', 'success')
    } catch {
      showToast('Copy failed — long-press the link to copy manually', 'info')
    }
  }

  const unpublishAll = async () => {
    if (submitting.value) return
    submitting.value = true
    try {
      await api.delete(`/api/highlights/${highlightId.value}/publish`)
      isPublished.value = false
      lastIssued.value = null
      showToast('Public link removed', 'info')
    } catch (err: unknown) {
      showToast(`Couldn't unpublish: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal
      open={open.value}
      onClose={() => { open.value = false }}
      title="Share this highlight publicly"
    >
      <p class="sh-muted">
        Mint a link anyone can open in a browser. The highlight streams
        directly from your home server — the relay only brokers the
        handshake. The link stops working when the highlight expires, or
        if you tap unpublish.
      </p>

      {isRestricted('public_links') && (
        <ProtectedNotice capability="public_links" />
      )}

      {!isRestricted('public_links') && connections.value.length === 0 && (
        <p class="sh-muted">
          You're not connected to any Global Federation Server yet.
          Connect one in <a href={addBase('/settings/connections')}>Settings → Connections</a>.
        </p>
      )}

      {!isRestricted('public_links') && connections.value.length > 0 && !lastIssued.value && (
        <form
          class="sh-highlight-publish-form"
          onSubmit={(e) => { e.preventDefault(); void submit() }}
        >
          <label class="sh-form-row">
            <span>Relay</span>
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
                <option value="">Pick a relay…</option>
                {connections.value.map(c => (
                  <option key={c.id} value={c.id}>{c.display_name}</option>
                ))}
              </select>
            )}
          </label>
          <label class="sh-form-row">
            <span>Label (optional)</span>
            <input
              type="text"
              maxLength={64}
              placeholder="e.g. twitter"
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
              {submitting.value ? 'Publishing…' : 'Mint link'}
            </Button>
          </div>
        </form>
      )}

      {lastIssued.value && (
        <div class="sh-highlight-publish-issued">
          <p><strong>Your link:</strong></p>
          <code class="sh-highlight-publish-url">{lastIssued.value.url}</code>
          <div class="sh-modal-actions">
            <Button onClick={copy}>Copy</Button>
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
            Unpublish all links for this highlight
          </Button>
        </div>
      )}
    </Modal>
  )
}
