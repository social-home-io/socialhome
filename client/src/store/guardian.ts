import { signal } from '@preact/signals'
import { api } from '@/api'

/**
 * Guardian status — `true` when the caller has at least one assigned
 * minor (`GET /api/cp/minors` returns a non-empty array). Drives the
 * Parent Control link in the sidebar so non-guardians never see it.
 *
 * Initial value `null` means "not yet loaded" — the sidebar should
 * hide the link in that state too, to avoid a flash-then-disappear.
 */
export const isGuardian = signal<boolean | null>(null)

let inflight: Promise<void> | null = null

export async function loadGuardian(): Promise<void> {
  if (inflight) return inflight
  inflight = api.get('/api/cp/minors')
    .then((resp) => {
      const minors = (resp as { minors?: string[] }).minors ?? []
      isGuardian.value = minors.length > 0
    })
    .catch(() => {
      // Endpoint 403s for some misconfigurations or non-admin contexts;
      // either way the safe default is "not a guardian".
      isGuardian.value = false
    })
    .finally(() => { inflight = null })
  return inflight
}
