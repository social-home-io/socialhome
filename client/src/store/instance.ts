import { signal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'

/**
 * Instance metadata fetched from `GET /api/instance/config`.
 *
 * Public endpoint (no token required). The SPA queries this on cold
 * start to decide whether to redirect to `/setup` and which login flow
 * to render. Re-fetched only on cold start; once the SPA has any
 * evidence the instance is set up (a 200 from /api/me) we don't
 * re-poll within the session.
 */
export interface InstanceConfig {
  mode: 'standalone' | 'ha' | 'haos'
  instance_name: string
  /** Stable base32 id of this Social Home. Surfaced so cross-instance
   *  payloads (space-invite codes, the future GFS-redirect path) can
   *  embed it without an extra /api/friends round-trip. ``null`` when
   *  the federation tables aren't seeded yet (pre-setup cold start). */
  instance_id: string | null
  capabilities: string[]
  setup_required: boolean
  /** Vite content hash of the entry bundle the backend is currently
   *  serving (parsed from ``static/index.html``'s
   *  ``<script src="./assets/index-{hash}.js">`` tag). ``null`` when
   *  the backend isn't serving the SPA (Vite dev mode) or the bundle
   *  template is missing the canonical script tag. The
   *  ``SpaUpdateBanner`` compares this against the hash the running
   *  tab booted with to prompt the user when a deploy lands. */
  spa_bundle_hash?: string | null
}

export const instanceConfig = signal<InstanceConfig | null>(null)
export const instanceConfigError = signal<string | null>(null)

let inflight: Promise<InstanceConfig> | null = null

export async function loadInstanceConfig(): Promise<InstanceConfig> {
  if (inflight) return inflight
  inflight = api.get('/api/instance/config')
    .then((cfg) => {
      const typed = cfg as InstanceConfig
      instanceConfig.value = typed
      instanceConfigError.value = null
      return typed
    })
    .catch((err) => {
      instanceConfigError.value = err?.message || t('instance.config_failed')
      throw err
    })
    .finally(() => { inflight = null })
  return inflight
}

/** Same network call as :func:`loadInstanceConfig`, but does NOT use
 *  the inflight-dedup gate — a deliberate poll always issues a fresh
 *  request. Used by :class:`SpaUpdateBanner` so the bundle-hash check
 *  doesn't piggyback on a stale in-flight cold-start fetch. */
export async function recheckInstanceConfig(): Promise<InstanceConfig> {
  const cfg = await api.get('/api/instance/config') as InstanceConfig
  instanceConfig.value = cfg
  instanceConfigError.value = null
  return cfg
}

export function hasCapability(cap: string): boolean {
  return instanceConfig.value?.capabilities.includes(cap) ?? false
}
