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

/** Expiry choices. ``null`` days = never expires. */
const EXPIRY_OPTIONS: { value: string; label: string; days: number | null }[] = [
  { value: '30', label: '30 days', days: 30 },
  { value: '90', label: '90 days', days: 90 },
  { value: '365', label: '1 year', days: 365 },
  { value: 'never', label: 'Never', days: null },
]
const DEFAULT_EXPIRY = '90'

function expiryIso(choice: string, now: Date = new Date()): string | null {
  const opt = EXPIRY_OPTIONS.find(o => o.value === choice)
  if (!opt || opt.days === null) return null
  return new Date(now.getTime() + opt.days * 86_400_000).toISOString()
}

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const t = Date.parse(normaliseTimestamp(iso))
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}

function isExpired(iso: string | null): boolean {
  if (!iso) return false
  const t = Date.parse(normaliseTimestamp(iso))
  return !Number.isNaN(t) && t <= Date.now()
}

function displayLabel(row: ApiTokenRow): string {
  return row.label === SIGN_IN_LABEL ? 'Browser sign-in' : (row.label || 'Unnamed token')
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
      loadError.value = (e as Error).message || 'Could not load your tokens.'
    } finally {
      loading.value = false
    }
  }

  useEffect(() => { void load() }, [])

  const create = async (e: Event) => {
    e.preventDefault()
    const name = label.value.trim()
    if (!name) {
      createError.value = 'Give the token a name so you can recognise it later.'
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
        : 'Could not create the token. Try again.'
    } finally {
      creating.value = false
    }
  }

  const revoke = async (row: ApiTokenRow) => {
    const signIn = row.label === SIGN_IN_LABEL
    const ok = await confirmDialog(
      signIn
        ? 'This browser sign-in stops working immediately. If it is the '
          + 'browser you are using now, you will be signed out.'
        : `Anything using “${displayLabel(row)}” stops working immediately. `
          + 'This cannot be undone — you would need to create a new token.',
      {
        title: signIn ? 'Sign out this browser?' : 'Revoke token?',
        confirmLabel: signIn ? 'Sign out' : 'Revoke',
        destructive: true,
      },
    )
    if (!ok) return
    try {
      await api.delete(`/api/me/tokens/${encodeURIComponent(row.token_id)}`)
      tokens.value = tokens.value.filter(t => t.token_id !== row.token_id)
      showToast(signIn ? 'Signed out' : 'Token revoked', 'info')
    } catch (err) {
      showToast((err as Error).message || 'Could not revoke the token', 'error')
    }
  }

  const exampleBase = baseUrl.value ?? 'https://<your-social-home>'

  return (
    <section class="sh-security" aria-labelledby="sh-security-tokens-heading">
      <h3 id="sh-security-tokens-heading">API tokens</h3>
      <p class="sh-muted">
        A token lets a script or another app use Social Home as you — it can
        do everything you can. Treat it like a password.
      </p>

      {revealed.value ? (
        <SecretReveal
          title={`New token “${revealed.value.label}”`}
          secret={revealed.value.token}
          secretLabel="New API token"
          dismissLabel="I've saved it"
          onDismiss={() => { revealed.value = null }}
        >
          <p class="sh-muted sh-secret-reveal__hint">
            Send it as a bearer header, for example:
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
            Name
            <input
              value={label.value}
              maxLength={64}
              placeholder="e.g. Backup script"
              aria-invalid={createError.value ? true : undefined}
              aria-describedby={createError.value ? 'sh-token-create-error' : undefined}
              onInput={(e) => {
                label.value = (e.target as HTMLInputElement).value
                createError.value = null
              }}
            />
          </label>
          <label class="sh-token-create__field sh-token-create__field--expiry">
            Expires
            <select
              value={expiry.value}
              onChange={(e) => { expiry.value = (e.target as HTMLSelectElement).value }}
            >
              {EXPIRY_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <Button type="submit" loading={creating.value}>Create token</Button>
          <FormError id="sh-token-create-error" message={createError.value} />
        </form>
      )}

      <h4 class="sh-security__list-heading">Signed-in browsers and tokens</h4>
      {loading.value && <Spinner label="Loading tokens…" />}
      {!loading.value && loadError.value && (
        <div class="sh-security__error">
          <FormError id="sh-token-load-error" message={loadError.value} />
          <Button variant="secondary" onClick={() => void load()}>Try again</Button>
        </div>
      )}
      {!loading.value && !loadError.value && tokens.value.length === 0 && (
        <p class="sh-muted sh-security__empty">
          No tokens yet. Tokens you create — and browsers you sign in
          with a password — show up here.
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
                    {expired && <span class="sh-token-row__badge">Expired</span>}
                  </span>
                  <span class="sh-muted sh-token-row__meta">
                    Created {formatDate(row.created_at)}
                    {' · '}
                    {row.last_used_at
                      ? `Last used ${relativeDocsTime(row.last_used_at)}`
                      : 'Never used'}
                    {' · '}
                    {row.expires_at
                      ? `${expired ? 'Expired' : 'Expires'} ${formatDate(row.expires_at)}`
                      : 'No expiry'}
                  </span>
                </div>
                <Button
                  variant="danger"
                  aria-label={`${row.label === SIGN_IN_LABEL ? 'Sign out' : 'Revoke'} ${displayLabel(row)}`}
                  onClick={() => void revoke(row)}
                >
                  {row.label === SIGN_IN_LABEL ? 'Sign out' : 'Revoke'}
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
        ? 'This Social Home runs as a Home Assistant add-on and has no public '
          + 'web address of its own. The token works for apps that can reach '
          + 'the add-on directly.'
        : 'This Social Home has no public web address set, so we can\'t show '
          + 'the exact URL. The token works for any app that can reach Social '
          + 'Home directly; an admin can set the address on the Connections '
          + 'page (External URL).'}
    </p>
  )
}
