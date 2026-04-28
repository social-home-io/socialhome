/**
 * SubscribeFeed — per-(user, space) iCal feed token UI (Phase F).
 *
 * Reveals a stable URL the user can paste into Apple Calendar / Google
 * Calendar / Outlook / Thunderbird to subscribe to the space's events.
 * Tokens are URL-embedded since most desktop calendar clients refresh
 * without OAuth — the auth middleware lets the feed path through via
 * a public-path-pattern.
 *
 * UX:
 *
 * * "Reveal" gating — the token isn't shown by default; the user has
 *   to click to copy. Reduces accidental shoulder-surfing.
 * * Copy-to-clipboard with toast confirmation.
 * * Regenerate (with confirmation): "this invalidates the existing URL".
 * * Revoke: "future fetches return 401".
 * * Per-app instructions accordion: Apple Calendar, Google Calendar,
 *   Outlook, Thunderbird.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

export interface SubscribeFeedProps {
  spaceId: string
}

interface FeedToken {
  token: string
  url: string
}

const tokenByspace = signal<Record<string, FeedToken | null>>({})
const loading = signal<Record<string, boolean>>({})
const revealed = signal<Record<string, boolean>>({})
const showInstructions = signal<Record<string, string | null>>({})

async function fetchExisting(spaceId: string) {
  // Server has no GET endpoint for the existing token (intentional —
  // forces the user to mint or regenerate explicitly). We treat
  // "absent" as the default state and only populate on POST. So this
  // helper is a no-op; included for the contract the component expects.
  if (!(spaceId in tokenByspace.value)) {
    tokenByspace.value = { ...tokenByspace.value, [spaceId]: null }
  }
}

export function SubscribeFeed({ spaceId }: SubscribeFeedProps) {
  useEffect(() => {
    fetchExisting(spaceId)
  }, [spaceId])

  const tok = tokenByspace.value[spaceId] ?? null
  const isLoading = loading.value[spaceId] ?? false
  const isRevealed = revealed.value[spaceId] ?? false

  const mintOrRegen = async (regen: boolean) => {
    if (regen && tok) {
      const ok = window.confirm(t('event.subscribe.confirm_regenerate'))
      if (!ok) return
    }
    loading.value = { ...loading.value, [spaceId]: true }
    try {
      const res = await api.post<FeedToken>(
        `/api/spaces/${spaceId}/calendar/feed-token`,
        {},
      )
      tokenByspace.value = { ...tokenByspace.value, [spaceId]: res }
      revealed.value = { ...revealed.value, [spaceId]: true }
      showToast(
        regen ? t('event.subscribe.regenerated') : t('event.subscribe.minted'),
        'success',
      )
    } catch (e) {
      const msg = (e as Error)?.message ?? t('event.subscribe.failed')
      showToast(msg, 'error')
    } finally {
      const next = { ...loading.value }
      delete next[spaceId]
      loading.value = next
    }
  }

  const revoke = async () => {
    const ok = window.confirm(t('event.subscribe.confirm_revoke'))
    if (!ok) return
    loading.value = { ...loading.value, [spaceId]: true }
    try {
      await api.delete(`/api/spaces/${spaceId}/calendar/feed-token`)
      tokenByspace.value = { ...tokenByspace.value, [spaceId]: null }
      revealed.value = { ...revealed.value, [spaceId]: false }
      showToast(t('event.subscribe.revoked'), 'success')
    } catch (e) {
      const msg = (e as Error)?.message ?? t('event.subscribe.failed')
      showToast(msg, 'error')
    } finally {
      const next = { ...loading.value }
      delete next[spaceId]
      loading.value = next
    }
  }

  const copy = async () => {
    if (!tok) return
    const url = absoluteUrl(tok.url)
    try {
      await navigator.clipboard.writeText(url)
      showToast(t('event.subscribe.copied'), 'success')
    } catch {
      showToast(t('event.subscribe.copy_failed'), 'error')
    }
  }

  const showApp = showInstructions.value[spaceId] ?? null
  const setShowApp = (app: string | null) => {
    showInstructions.value = { ...showInstructions.value, [spaceId]: app }
  }

  return (
    <section class="sh-subscribe-feed" aria-label={t('event.subscribe.aria')}>
      <h3 class="sh-subscribe-feed-heading">
        <span aria-hidden="true">📅</span> {t('event.subscribe.heading')}
      </h3>
      <p class="sh-subscribe-feed-help">{t('event.subscribe.help')}</p>

      {tok ? (
        <>
          <div class="sh-subscribe-url-row">
            <input
              type="text"
              readonly
              value={isRevealed ? absoluteUrl(tok.url) : maskUrl(absoluteUrl(tok.url))}
              class="sh-subscribe-url"
              aria-label={t('event.subscribe.url_aria')}
              onFocus={(e) => (e.target as HTMLInputElement).select()}
            />
            <Button
              variant="secondary"
              onClick={() =>
                (revealed.value = { ...revealed.value, [spaceId]: !isRevealed })
              }
            >
              {isRevealed ? t('event.subscribe.hide') : t('event.subscribe.reveal')}
            </Button>
            <Button variant="primary" onClick={copy} disabled={!isRevealed}>
              {t('event.subscribe.copy')}
            </Button>
          </div>
          <div class="sh-subscribe-actions">
            <Button
              variant="secondary"
              loading={isLoading}
              onClick={() => mintOrRegen(true)}
            >
              {t('event.subscribe.regenerate')}
            </Button>
            <Button variant="secondary" onClick={revoke}>
              {t('event.subscribe.revoke')}
            </Button>
          </div>
        </>
      ) : (
        <div class="sh-subscribe-empty">
          <p>{t('event.subscribe.empty')}</p>
          <Button
            variant="primary"
            loading={isLoading}
            onClick={() => mintOrRegen(false)}
          >
            {t('event.subscribe.create')}
          </Button>
        </div>
      )}

      <details class="sh-subscribe-instructions">
        <summary>{t('event.subscribe.how_to')}</summary>
        <div class="sh-subscribe-apps" role="tablist">
          {(['apple', 'google', 'outlook', 'thunderbird'] as const).map((app) => (
            <button
              key={app}
              type="button"
              role="tab"
              aria-selected={showApp === app}
              class={`sh-subscribe-app-tab${
                showApp === app ? ' sh-subscribe-app-tab--active' : ''
              }`}
              onClick={() => setShowApp(showApp === app ? null : app)}
            >
              {t(`event.subscribe.app.${app}`)}
            </button>
          ))}
        </div>
        {showApp && (
          <ol class="sh-subscribe-steps">
            {appSteps(showApp).map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        )}
      </details>
    </section>
  )
}

function appSteps(app: string): string[] {
  // i18n keys for each step; falls back gracefully if some locales
  // skip the deeper instructions.
  const out: string[] = []
  for (let i = 1; i <= 4; i++) {
    const k = `event.subscribe.steps.${app}.${i}`
    const text = t(k)
    if (text === k) break // no more steps in this locale
    out.push(text)
  }
  return out
}

function absoluteUrl(path: string): string {
  if (typeof window === 'undefined') return path
  if (path.startsWith('http')) return path
  return `${window.location.origin}${path}`
}

function maskUrl(url: string): string {
  // Show host + ".../calendar/export.ics?token=••••" — gives the user
  // enough context to know which space without revealing the secret.
  return url.replace(/(token=)[^&]+/, '$1••••••••')
}
