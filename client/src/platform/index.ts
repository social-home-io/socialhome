/**
 * SPA platform adapter — the frontend mirror of the backend's
 * `socialhome/platform/` (adapter + `Capability`) pattern.
 *
 * The backend rule is "consume Provider interfaces / `capabilities`,
 * never branch on `config.mode`". This module is the SPA equivalent: the
 * **data** lives in `store/instance` (`instanceConfig`, loaded from
 * `GET /api/instance/config`), and call sites read **intent-revealing
 * accessors** from here instead of hardcoding `'standalone'`/`'ha'`/
 * `'haos'` strings or capability literals all over the codebase.
 *
 * All helpers are plain functions that read the `instanceConfig` signal,
 * so calling them inside a component render stays reactive (the read is
 * tracked the same way `hasCapability` already was).
 *
 * The ingress transport (URL prefixing for HA Supervisor ingress) is a
 * separate, already-encapsulated concern — see `baseUrl.ts`
 * (`basePath` / `addBase` / `stripBase`) and `router/IngressLocationProvider`.
 * It isn't re-homed here to avoid churning every `addBase` import.
 */
import { instanceConfig, hasCapability } from '@/store/instance'

export type PlatformMode = 'standalone' | 'ha' | 'haos'

/** The deployment mode, or `null` before `instanceConfig` has loaded. */
export function platformMode(): PlatformMode | null {
  return instanceConfig.value?.mode ?? null
}

// ── Capability accessors (mirror backend `Capability`) ───────────────────

export { hasCapability } from '@/store/instance'
/** Adapter exposes a working STT (speech-to-text) provider. */
export const supportsStt = (): boolean => hasCapability('stt')
/** Adapter exposes a working AI provider (e.g. calendar photo import). */
export const supportsAi = (): boolean => hasCapability('ai')
/** Adapter can push notifications (Web Push and/or HA mobile app). */
export const supportsPush = (): boolean => hasCapability('push')
/** Users come from HA's `person.*` registry (HA Core + HAOS), so the SPA
 *  surfaces the HA-users import panel instead of local user creation. */
export const usesHaUserDirectory = (): boolean =>
  hasCapability('ha_person_directory')
/** The platform has zones of its own (Home Assistant's `zone.*`) that
 *  Admin › Zones can sync into the household's zones. */
export const supportsZoneSync = (): boolean => hasCapability('zone_sync')
/** The household's home location comes from the platform (Home
 *  Assistant's own location) — shown read-only, changed there; without it
 *  an admin sets it in Admin › Settings. */
export const homeLocationFromPlatform = (): boolean => hasCapability('home_location_sync')

// ── Mode-shaped accessors (intent over raw string compares) ──────────────

/** Running against Home Assistant in either form (Core REST or add-on). */
export const isHomeAssistant = (): boolean => {
  const m = platformMode()
  return m === 'ha' || m === 'haos'
}

/** The HA Supervisor add-on (HAOS): embedded behind Ingress, carries no
 *  bearer token, and renders inside HA's own chrome. */
export const isSupervisorAddon = (): boolean => platformMode() === 'haos'

/** Auth handshake is HA Supervisor ingress (no SPA-held token; the
 *  Supervisor injects the auth headers). Cold start always probes
 *  `/api/me`, and an auth failure is a deployment problem, not a session
 *  timeout. Equivalent to {@link isSupervisorAddon}; named for the auth
 *  call sites that branch on it. */
export const usesIngressAuth = (): boolean => isSupervisorAddon()

/** Social Home owns its user table (standalone) — so the admin panel can
 *  create local users; HA / HAOS pull users from HA instead. */
export const managesLocalUsers = (): boolean =>
  platformMode() === 'standalone'

/** Provisioning an HA user needs a password (HA Core mode); HAOS signs
 *  the user in via ingress so no password is collected. */
export const requiresHaUserPassword = (): boolean => platformMode() === 'ha'
