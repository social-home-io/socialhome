/**
 * PublicSharingSettings — manage GFS-public Momentum registrations.
 *
 * Renders one row per paired GFS with three controls:
 *   * Register / Unregister
 *   * "Default ON / OFF" — flips ``default_share`` so every new
 *     moment fans through this GFS by default (composer can still
 *     opt out per-moment).
 *
 * Backed by the SH ``/api/moments/public/registrations`` surface +
 * the cached ``registrations`` signal in ``store/momentPublic``.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import {
  deregisterFromGfs,
  loadRegistrations,
  registerOnGfs,
  registrations,
  setDefaultShare,
} from '@/store/momentPublic'
import type { GfsConnection } from '@/types'

/** Public, guest-readable index of the caller's current public moments. */
function shareUrl(conn: GfsConnection): string | null {
  const uid = currentUser.value?.user_id
  if (!uid) return null
  return `${conn.inbox_url.replace(/\/+$/, '')}/moments/${encodeURIComponent(uid)}`
}

async function copyShareUrl(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url)
    showToast('Share link copied', 'success')
  } catch {
    showToast(url, 'info')
  }
}

const gfses = signal<GfsConnection[]>([])
const loading = signal(true)

async function reload(): Promise<void> {
  loading.value = true
  try {
    // Only active GFS can accept a moment registration — a pending/suspended
    // connection would render a Register button that fails server-side
    // (moment_public_service._require_active_gfs). Pending state is surfaced
    // on the Connections page; here we only offer ready servers.
    const conns = await api.get<GfsConnection[]>('/api/gfs/connections')
    gfses.value = (conns ?? []).filter(c => c.status === 'active')
    await loadRegistrations()
  } catch (err) {
    showToast(
      `Couldn't load GFS connections: ${(err as Error)?.message ?? err}`,
      'error',
    )
  } finally {
    loading.value = false
  }
}

export default function PublicSharingSettings() {
  useTitle(t('page_title.public_sharing'))
  useEffect(() => {
    void reload()
  }, [])

  if (isRestricted('public_moments')) {
    return (
      <section class="sh-settings-section">
        <h2>Share Moments via a GFS</h2>
        <ProtectedNotice capability="public_moments" />
      </section>
    )
  }

  if (loading.value) return <Spinner />

  if (gfses.value.length === 0) {
    return (
      <div class="sh-empty-state">
        <div aria-hidden="true">🌐</div>
        <h3>No GFS pairings yet</h3>
        <p>
          Pair a Global Federation Server first — then come back here to
          choose which ones can fan your Moments to a wider audience.
        </p>
      </div>
    )
  }

  const isRegistered = (gfsId: string) =>
    registrations.value.some((r) => r.gfs_id === gfsId)
  const defaultShare = (gfsId: string) =>
    registrations.value.find((r) => r.gfs_id === gfsId)?.default_share ?? true

  const onRegister = async (gfsId: string) => {
    try {
      await registerOnGfs(gfsId, true)
      showToast('Registered. Future moments will fan via this GFS.', 'success')
    } catch (err) {
      showToast(
        `Register failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }
  const onDeregister = async (gfsId: string) => {
    try {
      await deregisterFromGfs(gfsId)
      showToast('Unregistered. Future moments stay household-only.', 'success')
    } catch (err) {
      showToast(
        `Unregister failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }
  const onToggleDefault = async (gfsId: string, next: boolean) => {
    try {
      await setDefaultShare(gfsId, next)
    } catch (err) {
      showToast(
        `Toggle failed: ${(err as Error)?.message ?? err}`,
        'error',
      )
    }
  }

  return (
    <div class="sh-public-sharing">
      <h2>Share Moments via a GFS</h2>
      <p class="sh-muted">
        Registering on a GFS lets people outside your paired households
        follow you and receive your moments. You can still opt out per
        moment in the composer.
      </p>
      <ul class="sh-public-sharing-list">
        {gfses.value.map((g) => {
          const reg = isRegistered(g.id)
          return (
            <li key={g.id} class="sh-public-sharing-row">
              <div class="sh-public-sharing-name">
                <strong>{g.display_name}</strong>
                <span class="sh-muted"> · {g.inbox_url}</span>
              </div>
              {reg ? (
                <>
                  <label class="sh-public-sharing-default">
                    <input
                      type="checkbox"
                      checked={defaultShare(g.id)}
                      onChange={(ev) =>
                        void onToggleDefault(
                          g.id,
                          (ev.currentTarget as HTMLInputElement).checked,
                        )
                      }
                    />
                    Default ON
                  </label>
                  <Button
                    variant="secondary"
                    onClick={() => void onDeregister(g.id)}
                  >
                    Unregister
                  </Button>
                  {shareUrl(g) && (
                    <Button
                      variant="secondary"
                      onClick={() => void copyShareUrl(shareUrl(g)!)}
                      title="Copy a public link anyone can open to read your current public moments"
                    >
                      Copy share link
                    </Button>
                  )}
                </>
              ) : (
                <Button onClick={() => void onRegister(g.id)}>Register</Button>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
