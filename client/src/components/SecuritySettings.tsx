/**
 * SecuritySettings — the caller's own sign-ins and personal API tokens
 * (Settings → Security).
 *
 * Backed by ``GET/POST /api/me/tokens`` + ``DELETE /api/me/tokens/{id}``.
 * The list is every live ``api_tokens`` row the caller owns, which
 * includes browser sign-ins (labelled ``web`` by the login route) next
 * to tokens minted here — so it doubles as "where am I signed in".
 *
 * A new token's raw value exists only in the POST response (the server
 * keeps its SHA-256), so it is shown once via ``SecretReveal``. The API
 * base shown next to it is the server-derived public origin
 * (``base_url``) — never ``document.baseURI``, which under HA ingress is
 * an ingress path an external script can't use.
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { Button } from './Button'
import { ProtectedNotice, isRestricted } from './ProtectedNotice'
import { FormError } from './FormError'
import { SecretReveal } from './SecretReveal'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { confirmDialog } from './confirm'
import { normaliseTimestamp, relativeDocsTime } from '@/utils/relativeTime'
import { isSupervisorAddon } from '@/platform'
import { formatLocale, t } from '@/i18n/i18n'

export interface ApiTokenRow {
  token_id: string
  label: string
  created_at: string | null
  last_used_at: string | null
  expires_at: string | null
}

interface TokenListResponse {
  base_url: string | null
  tokens: ApiTokenRow[]
}

/** Label the login route stamps on a browser sign-in's token row. */
const SIGN_IN_LABEL = 'web'

/** Expiry choices. ``null`` days = never expires. ``labelKey`` is
 *  resolved at render time so a language switch relabels the picker. */
const EXPIRY_OPTIONS: { value: string; labelKey: string; days: number | null }[] = [
  { value: '30', labelKey: 'security.expiry_30', days: 30 },
  { value: '90', labelKey: 'security.expiry_90', days: 90 },
  { value: '365', labelKey: 'security.expiry_365', days: 365 },
  { value: 'never', labelKey: 'security.expiry_never', days: null },
]
const DEFAULT_EXPIRY = '90'

function expiryIso(choice: string, now: Date = new Date()): string | null {
  const opt = EXPIRY_OPTIONS.find(o => o.value === choice)
  if (!opt || opt.days === null) return null
  return new Date(now.getTime() + opt.days * 86_400_000).toISOString()
}

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const ms = Date.parse(normaliseTimestamp(iso))
  if (Number.isNaN(ms)) return iso
  return new Date(ms).toLocaleDateString(formatLocale(), {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

function isExpired(iso: string | null): boolean {
  if (!iso) return false
  const ms = Date.parse(normaliseTimestamp(iso))
  return !Number.isNaN(ms) && ms <= Date.now()
}

function displayLabel(row: ApiTokenRow): string {
  return row.label === SIGN_IN_LABEL ? t('security.browser_sign_in') : (row.label || t('security.unnamed'))
}

export function SecuritySettings() {
  const tokens = useSignal<ApiTokenRow[]>([])
  const baseUrl = useSignal<string | null>(null)
  const loading = useSignal(true)
  const loadError = useSignal<string | null>(null)
  const label = useSignal('')
  const expiry = useSignal(DEFAULT_EXPIRY)
  const creating = useSignal(false)
  const createError = useSignal<string | null>(null)
  const revealed = useSignal<{ label: string; token: string } | null>(null)

  const load = async () => {
    loading.value = true
    loadError.value = null
    try {
      const data = await api.get<TokenListResponse>('/api/me/tokens')
      tokens.value = data.tokens ?? []
      baseUrl.value = data.base_url ?? null
    } catch (e) {
      loadError.value = (e as Error).message || t('security.load_failed')
    } finally {
      loading.value = false
    }
  }

  useEffect(() => { void load() }, [])

  const create = async (e: Event) => {
    e.preventDefault()
    const name = label.value.trim()
    if (!name) {
      createError.value = t('security.name_required')
      return
    }
    creating.value = true
    createError.value = null
    try {
      const res = await api.post<{ token_id: string; token: string }>(
        '/api/me/tokens',
        { label: name, expires_at: expiryIso(expiry.value) },
      )
      revealed.value = { label: name, token: res.token }
      label.value = ''
      expiry.value = DEFAULT_EXPIRY
      await load()
    } catch (err) {
      createError.value = err instanceof ApiError && err.detail
        ? err.detail
        : t('security.create_failed')
    } finally {
      creating.value = false
    }
  }

  const revoke = async (row: ApiTokenRow) => {
    const signIn = row.label === SIGN_IN_LABEL
    const ok = await confirmDialog(
      signIn
        ? t('security.confirm_sign_out')
        : t('security.confirm_revoke', { name: displayLabel(row) }),
      {
        title: signIn ? t('security.sign_out_title') : t('security.revoke_title'),
        confirmLabel: signIn ? t('security.sign_out') : t('security.revoke'),
        destructive: true,
      },
    )
    if (!ok) return
    try {
      await api.delete(`/api/me/tokens/${encodeURIComponent(row.token_id)}`)
      tokens.value = tokens.value.filter(tok => tok.token_id !== row.token_id)
      showToast(signIn ? t('security.signed_out') : t('security.revoked'), 'info')
    } catch (err) {
      showToast((err as Error).message || t('security.revoke_failed'), 'error')
    }
  }

  const exampleBase = baseUrl.value ?? 'https://<your-social-home>'

  return (
    <section class="sh-security" aria-labelledby="sh-security-tokens-heading">
      <h3 id="sh-security-tokens-heading">{t('security.title')}</h3>
      <p class="sh-muted">
        {t('security.intro')}
      </p>

      {revealed.value ? (
        <SecretReveal
          title={t('security.new_token_title', { name: revealed.value.label })}
          secret={revealed.value.token}
          secretLabel={t('security.new_token_label')}
          dismissLabel={t('security.saved_it')}
          onDismiss={() => { revealed.value = null }}
        >
          <p class="sh-muted sh-secret-reveal__hint">
            {t('security.bearer_hint')}
          </p>
          <pre class="sh-secret-reveal__snippet"><code>{
            `curl -H "Authorization: Bearer <token>" ${exampleBase}/api/me`
          }</code></pre>
          {!baseUrl.value && <NoPublicAddressNote />}
        </SecretReveal>
      ) : isRestricted('api_tokens') ? (
        <ProtectedNotice capability="api_tokens" />
      ) : (
        <form class="sh-token-create" onSubmit={create} noValidate>
          <label class="sh-token-create__field">
            {t('security.name')}
            <input
              value={label.value}
              maxLength={64}
              placeholder={t('security.name_placeholder')}
              aria-invalid={createError.value ? true : undefined}
              aria-describedby={createError.value ? 'sh-token-create-error' : undefined}
              onInput={(e) => {
                label.value = (e.target as HTMLInputElement).value
                createError.value = null
              }}
            />
          </label>
          <label class="sh-token-create__field sh-token-create__field--expiry">
            {t('security.expires')}
            <select
              value={expiry.value}
              onChange={(e) => { expiry.value = (e.target as HTMLSelectElement).value }}
            >
              {EXPIRY_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{t(o.labelKey)}</option>
              ))}
            </select>
          </label>
          <Button type="submit" loading={creating.value}>{t('security.create')}</Button>
          <FormError id="sh-token-create-error" message={createError.value} />
        </form>
      )}

      <h4 class="sh-security__list-heading">{t('security.list_heading')}</h4>
      {loading.value && <Spinner label={t('security.loading')} />}
      {!loading.value && loadError.value && (
        <div class="sh-security__error">
          <FormError id="sh-token-load-error" message={loadError.value} />
          <Button variant="secondary" onClick={() => void load()}>{t('common.try_again')}</Button>
        </div>
      )}
      {!loading.value && !loadError.value && tokens.value.length === 0 && (
        <p class="sh-muted sh-security__empty">
          {t('security.empty')}
        </p>
      )}
      {!loading.value && !loadError.value && tokens.value.length > 0 && (
        <ul class="sh-token-list">
          {tokens.value.map(row => {
            const expired = isExpired(row.expires_at)
            return (
              <li key={row.token_id} class="sh-token-row">
                <div class="sh-token-row__info">
                  <span class="sh-token-label">
                    {displayLabel(row)}
                    {expired && <span class="sh-token-row__badge">{t('security.expired_badge')}</span>}
                  </span>
                  <span class="sh-muted sh-token-row__meta">
                    {t('security.created', { date: formatDate(row.created_at) })}
                    {' · '}
                    {row.last_used_at
                      ? t('security.last_used', { time: relativeDocsTime(row.last_used_at) })
                      : t('security.never_used')}
                    {' · '}
                    {row.expires_at
                      ? t(expired ? 'security.expired_on' : 'security.expires_on',
                        { date: formatDate(row.expires_at) })
                      : t('security.no_expiry')}
                  </span>
                </div>
                <Button
                  variant="danger"
                  aria-label={t(row.label === SIGN_IN_LABEL ? 'security.sign_out_aria' : 'security.revoke_aria',
                    { name: displayLabel(row) })}
                  onClick={() => void revoke(row)}
                >
                  {row.label === SIGN_IN_LABEL ? t('security.sign_out') : t('security.revoke')}
                </Button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function NoPublicAddressNote() {
  return (
    <p class="sh-muted sh-secret-reveal__hint">
      {isSupervisorAddon()
        ? t('security.no_address_addon')
        : t('security.no_address')}
    </p>
  )
}
