/**
 * SpaceJoinLanding — handler for ``/join?token=...`` invite deep-links
 * (spec §23.62).
 *
 * Three branches:
 *
 *  1. Token consumed cleanly → success card, "Open space" CTA.
 *  2. Token rejected by the API (404 / 410 / 403) → "wrong instance"
 *     fallback panel that re-renders the token as a
 *     ``socialhome://invite#…`` code + QR + Copy CTA, with copy
 *     telling the receiver to paste it into their own Social Home's
 *     Spaces → Join with code card. This is the common case under
 *     HA ingress where a sender shares an HTTPS link that lands the
 *     receiver on the issuer's instance instead of their own.
 *  3. Other errors → bare "Couldn't join" message + back button.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import { addBase } from '@/baseUrl'
import { instanceConfig } from '@/store/instance'
import { buildInviteCode } from '@/lib/spaceInviteCode'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { QrCodeImg } from '@/components/QrCodeImg'
import { showToast } from '@/components/Toast'

type Status = 'loading' | 'joined' | 'wrong-instance' | 'error'

const status  = signal<Status>('loading')
const message = signal<string>('')
const joined  = signal<{ space_id: string } | null>(null)
const pasteCode = signal<string>('')

async function consumeToken(token: string, spaceId: string | null) {
  try {
    const r = await api.post('/api/spaces/join', { token }) as {
      space_id: string
      role: string
    }
    joined.value = r
    status.value = 'joined'
  } catch (err: unknown) {
    if (err instanceof ApiError && [403, 404, 410].includes(err.status)) {
      // The link landed on the ISSUER's instance — which is this one —
      // so our own instance id is exactly the issuer id the receiver's
      // Social Home needs to route the redeem over federation. Minting
      // the fallback code without it produced a code that could only
      // ever take the local path, i.e. fail the same way again on the
      // other side.
      // Prefer the issuer's own, bootstrap-capable code; fall back to the
      // locally-built one when the endpoint isn't there or says 404.
      pasteCode.value = await fetchIssuerCode(token) ?? buildInviteCode({
        token,
        space_id: spaceId,
        issuer_instance_id: instanceConfig.value?.instance_id ?? null,
      })
      status.value = 'wrong-instance'
      return
    }
    const msg = (err as Error)?.message ?? String(err)
    message.value = msg || 'Invite link rejected'
    status.value = 'error'
  }
}

/**
 * Ask the issuing household for the *complete* paste code for this token.
 *
 * A locally-minted `buildInviteCode(...)` carries only the token / space /
 * issuer id — it has no bootstrap block (the issuer's identity + key-wrap
 * keys and the connection-server URL), so the receiving household can never
 * actually bootstrap a redeem with it. `GET /api/invite-links/{token}/code`
 * returns the real, bootstrap-capable blob (the token is itself the
 * credential, so the endpoint is public + per-IP rate limited).
 *
 * Returns `null` when the token isn't a live invite link here (404) or the
 * request fails — the caller then falls back to the local code, so this
 * page is never worse than before.
 */
async function fetchIssuerCode(token: string): Promise<string | null> {
  try {
    const r = await api.get(`/api/invite-links/${encodeURIComponent(token)}/code`) as {
      code?: string | null
    }
    const code = r?.code
    return typeof code === 'string' && code !== '' ? code : null
  } catch {
    return null
  }
}

async function copyCode() {
  try {
    await navigator.clipboard.writeText(pasteCode.value)
    showToast('Code copied!', 'success')
  } catch {
    showToast('Could not copy — select the code to copy manually.', 'error')
  }
}

export default function SpaceJoinLanding() {
  const loc = useLocation()

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const token = params.get('token') || ''
    // Optional — a link minted with the space id attached lets the
    // fallback code carry it, so the receiver's join card can name the
    // space and the backend can address the bootstrap redeem.
    const spaceId = params.get('space_id') || params.get('space') || null
    if (!token) {
      status.value = 'error'
      message.value = 'This invite link is missing its token.'
      return
    }
    void consumeToken(token, spaceId)
  }, [])

  if (status.value === 'loading') {
    return (
      <div class="sh-join-landing">
        <Spinner />
        <p>Joining the space…</p>
      </div>
    )
  }
  if (status.value === 'joined' && joined.value) {
    return (
      <div class="sh-join-landing sh-card">
        <h2>You're in! 🎉</h2>
        <p>Welcome to the space.</p>
        <Button onClick={() => loc.route(addBase(`/spaces/${joined.value!.space_id}`))}>
          Open space
        </Button>
      </div>
    )
  }
  if (status.value === 'wrong-instance') {
    return (
      <div class="sh-join-landing sh-card" data-testid="join-landing-wrong-instance">
        <h2>This invite is for another Social Home</h2>
        <p>
          Open <strong>your own</strong> Social Home, go to{' '}
          <strong>Spaces</strong>, and paste this code into the
          "Join with invite code" card:
        </p>
        <code class="sh-invite-link" data-testid="fallback-code">
          {pasteCode.value}
        </code>
        <div class="sh-invite-artifact sh-invite-artifact--qr">
          <QrCodeImg data={pasteCode.value} size={180} alt="Invite QR code" />
        </div>
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={() => loc.route(addBase('/spaces'))}>
            Back to spaces
          </Button>
          <Button onClick={copyCode}>Copy code</Button>
        </div>
      </div>
    )
  }
  return (
    <div class="sh-join-landing sh-card sh-error">
      <h2>Couldn't join</h2>
      <p>{message.value}</p>
      <Button onClick={() => loc.route(addBase('/spaces'))}>Back to spaces</Button>
    </div>
  )
}
