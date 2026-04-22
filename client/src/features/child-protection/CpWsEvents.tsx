/**
 * CpWsEvents — WebSocket event bridge for Child Protection (spec §23.107).
 *
 * Subscribes to every ``cp.*`` event the backend publishes and renders
 * toast notifications so admins + guardians see the effect of an action
 * immediately, even when it was triggered from another device.
 *
 * Event names match :mod:`socialhome.services.realtime_service`:
 *
 * * ``cp.protection_enabled``    — admin turned protection on for X
 * * ``cp.protection_disabled``   — admin turned protection off for X
 * * ``cp.guardian_added``        — admin assigned a guardian
 * * ``cp.guardian_removed``      — admin removed a guardian
 * * ``cp.block_added``           — guardian blocked a user for a minor
 * * ``cp.block_removed``         — guardian unblocked a user for a minor
 * * ``cp.age_gate_changed``      — admin updated a space's min_age
 */
import { ws } from '@/ws'
import { showToast } from '@/components/Toast'

type CpEventOff = () => void

/** Install the CP toast handlers. Returns a disposer. */
export function initCpWsListeners(): CpEventOff {
  const offs: CpEventOff[] = []

  offs.push(ws.on('cp.protection_enabled', (evt) => {
    const d = evt.data as { minor_username: string; declared_age: number }
    showToast(
      `Protection enabled for @${d.minor_username} (age ${d.declared_age})`,
      'info',
    )
  }))

  offs.push(ws.on('cp.protection_disabled', (evt) => {
    const d = evt.data as { minor_username: string }
    showToast(`Protection removed for @${d.minor_username}`, 'info')
  }))

  offs.push(ws.on('cp.guardian_added', (evt) => {
    const d = evt.data as { minor_user_id: string; guardian_user_id: string }
    showToast(
      `Guardian assigned: ${d.guardian_user_id} → ${d.minor_user_id}`,
      'info',
    )
  }))

  offs.push(ws.on('cp.guardian_removed', (evt) => {
    const d = evt.data as { minor_user_id: string; guardian_user_id: string }
    showToast(
      `Guardian removed: ${d.guardian_user_id} ≠ ${d.minor_user_id}`,
      'info',
    )
  }))

  offs.push(ws.on('cp.block_added', (evt) => {
    const d = evt.data as { minor_user_id: string; blocked_user_id: string }
    showToast(
      `Blocked ${d.blocked_user_id} for minor ${d.minor_user_id}`,
      'info',
    )
  }))

  offs.push(ws.on('cp.block_removed', (evt) => {
    const d = evt.data as { minor_user_id: string; blocked_user_id: string }
    showToast(
      `Unblocked ${d.blocked_user_id} for minor ${d.minor_user_id}`,
      'info',
    )
  }))

  offs.push(ws.on('cp.age_gate_changed', (evt) => {
    const d = evt.data as {
      space_id: string; min_age: number; target_audience: string
    }
    showToast(
      `Age gate for ${d.space_id}: ${d.min_age}+ / ${d.target_audience}`,
      'info',
    )
  }))

  return () => { offs.forEach(o => o()) }
}
