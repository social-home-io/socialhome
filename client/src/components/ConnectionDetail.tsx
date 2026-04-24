/**
 * ConnectionDetail — per-connection settings (§23.88, §23.89, §23.90).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { showToast } from './Toast'

interface Connection {
  instance_id: string; display_name: string; status: string
  inbox_url: string; intro_relay_enabled: boolean
  unreachable_since: string | null; paired_at: string | null
}

const showRevoke = signal(false)

export function ConnectionDetail({ conn, onClose, onRevoke }: {
  conn: Connection; onClose: () => void; onRevoke: () => void
}) {
  const toggleRelay = async () => {
    try {
      await api.patch(`/api/pairing/connections/${conn.instance_id}/settings`, {
        intro_relay_enabled: !conn.intro_relay_enabled,
      })
      showToast('Setting updated', 'success')
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  const revoke = async () => {
    try {
      await api.delete(`/api/pairing/connections/${conn.instance_id}`)
      showToast('Connection revoked', 'info')
      showRevoke.value = false
      onRevoke()
    } catch (e: any) { showToast(e.message || 'Failed', 'error') }
  }

  return (
    <Modal open={true} onClose={onClose} title={conn.display_name}>
      <div class="sh-connection-detail">
        <dl>
          <dt>Instance ID</dt><dd class="sh-mono">{conn.instance_id}</dd>
          <dt>Status</dt><dd class={`sh-status sh-status--${conn.status}`}>{conn.status}</dd>
          <dt>Inbox</dt><dd class="sh-mono sh-muted">{conn.inbox_url}</dd>
          {conn.paired_at && <><dt>Paired</dt><dd>{new Date(conn.paired_at).toLocaleString()}</dd></>}
          {conn.unreachable_since && (
            <><dt>Unreachable since</dt><dd class="sh-text-warning">{new Date(conn.unreachable_since).toLocaleString()}</dd></>
          )}
        </dl>
        <label class="sh-toggle-row">
          <input type="checkbox" checked={conn.intro_relay_enabled} onChange={toggleRelay} />
          Allow introduced pairing (friend-of-a-friend)
        </label>
        <hr />
        <Button variant="danger" onClick={() => showRevoke.value = true}>Revoke connection</Button>
      </div>
      <ConfirmDialog open={showRevoke.value} title="Revoke connection?"
        message="This will permanently disconnect this household. All shared spaces will stop syncing. You'll need to re-pair via QR to reconnect."
        confirmLabel="Revoke" destructive onConfirm={revoke}
        onCancel={() => showRevoke.value = false} />
    </Modal>
  )
}
