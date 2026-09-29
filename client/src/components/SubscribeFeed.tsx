/**
 * SubscribeFeed — a private iCal link to one space's calendar, for the
 * caller only (``/api/spaces/{id}/calendar/feed-token``).
 *
 * Calendar apps (Apple Calendar, Google Calendar, Outlook, Thunderbird)
 * poll the link on their own, without signing in, so the token rides
 * in the URL. The server stores only its hash: the link exists in the
 * one POST response that minted it and is shown once. There is no GET
 * — creating a new link replaces any earlier one, which is also the
 * recovery path for a lost or leaked link.
 *
 * The link must be reachable from OUTSIDE the SPA, so it is the
 * server's ``external_url`` (built on the deployment's public origin),
 * never a ``document.baseURI``-anchored URL — under HA ingress that
 * base is an ingress path a calendar app can't load. When the server
 * has no public origin (``external_url: null``), we say so instead of
 * handing out a link that can't work.
 */
import { useSignal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { Button } from '@/components/Button'
import { ProtectedNotice, isRestricted } from '@/components/ProtectedNotice'
import { FormError } from '@/components/FormError'
import { SecretReveal } from '@/components/SecretReveal'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'
import { isSupervisorAddon } from '@/platform'

export interface SubscribeFeedProps {
  spaceId: string
}

export interface FeedTokenResponse {
  token: string
  url: string
  external_url: string | null
}

const APPS = ['apple', 'google', 'outlook', 'thunderbird'] as const

/** ``webcal://`` hands the link straight to the OS calendar app on
 *  macOS / iOS / Windows. Only offered for an ``https`` link — a plain
 *  ``http`` origin maps to ``webcal`` too, but calendar apps then reject
 *  it on most platforms, so the copy button is the honest path there. */
export function webcalUrl(url: string): string | null {
  return url.startsWith('https://') ? `webcal://${url.slice('https://'.length)}` : null
}

export function SubscribeFeed({ spaceId }: SubscribeFeedProps) {
  const feed = useSignal<FeedTokenResponse | null>(null)
  const busy = useSignal(false)
  const error = useSignal<string | null>(null)

  const mint = async (replace: boolean) => {
    if (replace) {
      const ok = await confirmDialog(t('event.subscribe.confirm_regenerate'), {
        title: t('event.subscribe.regenerate'),
        confirmLabel: t('event.subscribe.regenerate'),
        destructive: true,
      })
      if (!ok) return
    }
    busy.value = true
    error.value = null
    try {
      feed.value = await api.post<FeedTokenResponse>(
        `/api/spaces/${spaceId}/calendar/feed-token`,
        {},
      )
    } catch (e) {
      error.value = e instanceof ApiError && e.detail
        ? e.detail
        : t('event.subscribe.failed')
    } finally {
      busy.value = false
    }
  }

  const revoke = async () => {
    const ok = await confirmDialog(t('event.subscribe.confirm_revoke'), {
      title: t('event.subscribe.revoke'),
      confirmLabel: t('event.subscribe.revoke'),
      destructive: true,
    })
    if (!ok) return
    busy.value = true
    error.value = null
    try {
      await api.delete(`/api/spaces/${spaceId}/calendar/feed-token`)
      feed.value = null
      showToast(t('event.subscribe.revoked'), 'success')
    } catch (e) {
      error.value = e instanceof ApiError && e.detail
        ? e.detail
        : t('event.subscribe.failed')
    } finally {
      busy.value = false
    }
  }

  const current = feed.value
  const external = current?.external_url ?? null
  const webcal = external ? webcalUrl(external) : null

  if (isRestricted('calendar_feeds')) {
    // §CP.R: no new link — an earlier one already stopped serving, but can
    // still be turned off for good.
    return (
      <section class="sh-subscribe-feed" aria-label={t('event.subscribe.aria')}>
        <ProtectedNotice capability="calendar_feeds" />
        <p class="sh-muted sh-subscribe-feed-note">
          <button type="button" class="sh-link-button" onClick={() => void revoke()}
                  disabled={busy.value}>
            {t('event.subscribe.revoke_existing')}
          </button>
        </p>
        <FormError id={`sh-subscribe-error-${spaceId}`} message={error.value} />
      </section>
    )
  }

  return (
    <section class="sh-subscribe-feed" aria-label={t('event.subscribe.aria')}>
      <p class="sh-subscribe-feed-help">{t('event.subscribe.help')}</p>

      {!current && (
        <div class="sh-subscribe-empty">
          <Button loading={busy.value} onClick={() => void mint(false)}>
            {t('event.subscribe.create')}
          </Button>
          <p class="sh-muted sh-subscribe-feed-note">
            {t('event.subscribe.replaces_note')}{' '}
            <button type="button" class="sh-link-button" onClick={() => void revoke()}
                    disabled={busy.value}>
              {t('event.subscribe.revoke_existing')}
            </button>
          </p>
        </div>
      )}

      {current && external && (
        <SecretReveal
          title={t('event.subscribe.ready')}
          secret={external}
          secretLabel={t('event.subscribe.url_aria')}
        >
          {webcal && (
            <p class="sh-subscribe-open">
              <a class="sh-btn sh-btn--secondary" href={webcal}>
                {t('event.subscribe.open_app')}
              </a>
            </p>
          )}
        </SecretReveal>
      )}

      {current && !external && (
        <div class="sh-subscribe-unreachable" role="status">
          <p><strong>{t('event.subscribe.no_public_title')}</strong></p>
          <p class="sh-muted">
            {isSupervisorAddon()
              ? t('event.subscribe.no_public_addon')
              : t('event.subscribe.no_public_body')}
          </p>
          <details>
            <summary>{t('event.subscribe.show_path')}</summary>
            <code class="sh-subscribe-path">{current.url}</code>
          </details>
        </div>
      )}

      {current && (
        <div class="sh-subscribe-actions">
          <Button variant="secondary" loading={busy.value} onClick={() => void mint(true)}>
            {t('event.subscribe.regenerate')}
          </Button>
          <Button variant="danger" disabled={busy.value} onClick={() => void revoke()}>
            {t('event.subscribe.revoke')}
          </Button>
        </div>
      )}

      <FormError id={`sh-subscribe-error-${spaceId}`} message={error.value} />

      <details class="sh-subscribe-instructions">
        <summary>{t('event.subscribe.how_to')}</summary>
        {APPS.map(app => (
          <details key={app} class="sh-subscribe-app">
            <summary>{t(`event.subscribe.app.${app}`)}</summary>
            <ol class="sh-subscribe-steps">
              {appSteps(app).map((step, i) => <li key={i}>{step}</li>)}
            </ol>
          </details>
        ))}
      </details>
    </section>
  )
}

function appSteps(app: string): string[] {
  // Numbered keys; stop at the first one the catalogue doesn't have
  // (``t`` returns the raw key for a miss).
  const out: string[] = []
  for (let i = 1; i <= 6; i++) {
    const k = `event.subscribe.steps.${app}.${i}`
    const text = t(k)
    if (text === k) break
    out.push(text)
  }
  return out
}
