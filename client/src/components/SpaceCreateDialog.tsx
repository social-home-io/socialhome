/**
 * SpaceCreateDialog — space creation flow (§23.50).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { loadSpaces } from '@/store/spaces'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'

const open = signal(false)
const name = signal('')
const description = signal('')
const emoji = signal('')
const spaceType = signal('private')
const joinMode = signal('invite_only')
const submitting = signal(false)

export function openSpaceCreate() {
  open.value = true
  name.value = ''
  description.value = ''
  emoji.value = ''
  spaceType.value = 'private'
  joinMode.value = 'invite_only'
}

export function SpaceCreateDialog() {
  const handleSubmit = async () => {
    if (!name.value.trim() || submitting.value) return
    submitting.value = true
    try {
      await api.post('/api/spaces', {
        name: name.value,
        description: description.value || undefined,
        emoji: emoji.value || undefined,
        space_type: spaceType.value,
        join_mode: joinMode.value,
      })
      // Refresh the cached spaces list so the new row appears on the
      // list page without a hard reload.
      await loadSpaces()
      showToast('Space created', 'success')
      open.value = false
    } catch (e: any) {
      showToast(e.message || 'Failed to create space', 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value} onClose={() => open.value = false} title="Create Space">
      <div class="sh-form">
        <label>
          Name *
          <input value={name.value} onInput={(e) => name.value = (e.target as HTMLInputElement).value}
            placeholder="e.g. Family, Makers Club" />
        </label>
        <label>
          Description
          <textarea value={description.value}
            onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value}
            placeholder="What's this space about?" rows={2} />
        </label>
        <label>
          Emoji
          <input value={emoji.value} maxLength={2}
            onInput={(e) => emoji.value = (e.target as HTMLInputElement).value}
            placeholder="🏠" />
        </label>
        <label>
          Type
          <select value={spaceType.value}
            onChange={(e) => spaceType.value = (e.target as HTMLSelectElement).value}>
            <option value="private">Private (invite only)</option>
            <option value="household">Household (all members)</option>
            <option value="public">Public (discoverable)</option>
          </select>
        </label>
        {spaceType.value !== 'private' && (
          <label>
            Join mode
            <select value={joinMode.value}
              onChange={(e) => joinMode.value = (e.target as HTMLSelectElement).value}>
              <option value="invite_only">Invite only</option>
              <option value="open">Open (anyone can join)</option>
              <option value="link">Link (join via invite link)</option>
              <option value="request">Request (admin approves)</option>
            </select>
          </label>
        )}
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => open.value = false}>Cancel</Button>
          <Button onClick={handleSubmit} loading={submitting.value}
            disabled={!name.value.trim()}>
            Create
          </Button>
        </div>
      </div>
    </Modal>
  )
}
