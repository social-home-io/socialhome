/**
 * ShareHomeToggle — admin-only toggle that controls whether our household's
 * home pin is visible on this peer's Connections → Map (§23.90).
 *
 * Optimistic local-state flip + PATCH to /api/pairing/connections/{id},
 * revert + toast on error.
 */
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { showToast } from './Toast'

export interface ShareHomeToggleProps {
  instanceId: string
  peerName: string
  initialValue: boolean
  onChange?: (newValue: boolean) => void
}

export function ShareHomeToggle({
  instanceId,
  peerName,
  initialValue,
  onChange,
}: ShareHomeToggleProps) {
  const [enabled, setEnabled] = useState(initialValue)

  const toggle = async () => {
    const next = !enabled
    // Optimistic flip
    setEnabled(next)
    try {
      await api.patch(`/api/pairing/connections/${instanceId}`, { share_home: next })
      onChange?.(next)
    } catch (e: any) {
      // Revert on error
      setEnabled(!next)
      showToast(e.message || 'Failed to update home-sharing setting', 'error')
    }
  }

  const helpText = enabled
    ? `On — your "You" pin appears on ${peerName}'s Connections → Map.`
    : `Off — your home stays hidden from ${peerName}'s map.`

  return (
    <div class="sh-share-home-toggle">
      <label class="sh-toggle-row">
        <input
          type="checkbox"
          checked={enabled}
          onChange={() => void toggle()}
        />
        Share our home location with {peerName}
      </label>
      <p class="sh-muted sh-share-home-toggle__help" style={{ marginTop: 'var(--sh-space-xs)', fontSize: 'var(--sh-font-size-sm)' }}>
        {helpText}
      </p>
    </div>
  )
}
