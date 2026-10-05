/**
 * SpaceJoinByCodeDialog — modal for pasting an invite code (or
 * scanning a QR) to join a space.
 *
 * Opens via :func:`openSpaceJoinByCode` from the Spaces dashboard
 * header (next to "+ Create space"). Mirrors the §11 household
 * pairing dialog's UX: a two-tile method picker (📷 Scan QR / 📋
 * Paste code) at the top, the active panel below. Same class names
 * (``sh-pairing-method-grid``, ``sh-pairing-method-card``) so the
 * visual stays in lockstep with pairing's muscle memory.
 *
 * Decoder accepts the new ``socialhome://invite#…`` shape, raw JSON,
 * and bare hex tokens (see :mod:`spaceInviteCode`). Three distinct
 * failure modes get three distinct messages.
 */
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import { addBase } from '@/baseUrl'
import { instanceConfig } from '@/store/instance'
import { decodeInviteCode } from '@/lib/spaceInviteCode'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { QrScanner } from '@/components/QrScanner'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

type InviteMethod = 'paste' | 'qr'

const open = signal(false)
const method = signal<InviteMethod>('paste')
const draft = signal('')
const submitting = signal(false)
const errorMsg = signal<string | null>(null)

export function openSpaceJoinByCode() {
  // Reset state so a re-open after a prior cancel / error starts clean.
  method.value = 'paste'
  draft.value = ''
  submitting.value = false
  errorMsg.value = null
  open.value = true
}

/** Returns the issuer's instance id when the invite was minted on a
 *  DIFFERENT instance than ours — the cross-instance redeem path
 *  needs that id to route the SPACE_INVITE_TOKEN_REDEEM envelope.
 *  Returns ``null`` for a same-instance code (or one without an
 *  embedded id at all — bare-token pastes go through the local
 *  endpoint as before). */
function crossInstanceIssuer(
  payload: ReturnType<typeof decodeInviteCode>,
): string | null {
  if (!payload?.issuer_instance_id) return null
  const ours = instanceConfig.value?.instance_id
  if (ours && payload.issuer_instance_id === ours) return null
  return payload.issuer_instance_id
}

export function SpaceJoinByCodeDialog() {
  const loc = useLocation()

  const close = () => {
    open.value = false
  }

  const pickMethod = (m: InviteMethod) => {
    errorMsg.value = null
    method.value = m
  }

  const submit = async (raw?: string) => {
    const input = (raw ?? draft.value).trim()
    if (!input) {
      errorMsg.value = t('space.join_code.empty')
      return
    }
    errorMsg.value = null
    const payload = decodeInviteCode(input)
    if (!payload) {
      errorMsg.value = t('space.join_code.invalid')
      return
    }
    submitting.value = true
    try {
      // When the issuer is a different instance than ours, hand the
      // backend the issuer_instance_id so it can route the redeem
      // over the SPACE_INVITE_TOKEN_REDEEM federation flow. Same-
      // instance + bare-token pastes use the original local path
      // (issuer_instance_id omitted).
      const issuer = crossInstanceIssuer(payload)
      const body: {
        token: string
        issuer_instance_id?: string
        space_id?: string
        issuer_identity_pk?: string
        issuer_keywrap_pk?: string
        issuer_keywrap_sig?: string
        issuer_proto_version?: number
        expires_at?: string
        gfs?: string
      } = {
        token: payload.token,
      }
      if (issuer) body.issuer_instance_id = issuer
      // §D2b — a code from a household we have never met carries the
      // issuer's public keys and the connection server that serves the
      // blob. Forwarded only alongside an issuer id; the backend uses
      // them solely when neither a direct pairing nor a mesh route
      // reaches the issuer.
      if (issuer && payload.issuer_identity_pk && payload.issuer_keywrap_pk
          && payload.issuer_keywrap_sig) {
        body.issuer_identity_pk = payload.issuer_identity_pk
        body.issuer_keywrap_pk = payload.issuer_keywrap_pk
        body.issuer_keywrap_sig = payload.issuer_keywrap_sig
        if (payload.space_id) body.space_id = payload.space_id
        if (payload.issuer_proto_version) {
          body.issuer_proto_version = payload.issuer_proto_version
        }
        if (payload.expires_at) body.expires_at = payload.expires_at
        if (payload.via_gfs?.gfs_url) body.gfs = payload.via_gfs.gfs_url
      }
      const r = await api.post('/api/spaces/join', body) as {
        space_id: string
        role?: string | null
        pending_role?: string | null
      }
      open.value = false
      draft.value = ''
      // The cross-instance redeem path queues a federated join; the
      // local space row may not exist on the receiver's instance yet,
      // so navigating into ``/spaces/{id}`` would land on an empty
      // shell. Fall back to ``/spaces`` and let the WS upsert place
      // the new card. Same-instance redeems return a row that's
      // visible immediately, so the deep-link is safe there.
      const dest = issuer ? addBase('/spaces') : addBase(`/spaces/${r.space_id}`)
      // An admin/mod link seats you as a member now; the admin role waits
      // for an owner to approve it, so say so rather than implying you are
      // already an admin.
      if (r.pending_role === 'admin') {
        showToast(t('space.join_code.admin_pending'), 'success')
      } else if (r.role === 'moderator') {
        // A moderator link seats a moderator outright. (On a household too
        // old for the role the host seats a plain member, and the generic
        // toast below is the honest one.)
        showToast(t('space.join.joined_moderator'), 'success')
      } else {
        showToast(t('space.join.joined'), 'success')
      }
      loc.route(dest)
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 410)) {
        errorMsg.value = t('space.join_code.expired')
      } else if (e instanceof ApiError && e.status === 403) {
        // Prefer the backend's specific reason — e.g. the child-protection
        // age gate's "This space is restricted to users aged 18+." — so a
        // blocked minor sees WHY, not a misleading "invite revoked".
        errorMsg.value = e.detail || t('space.join_code.forbidden')
      } else {
        errorMsg.value = (e as Error)?.message ?? t('space.join_code.failed')
      }
    } finally {
      submitting.value = false
    }
  }

  if (!open.value) return null

  return (
    <Modal open={true} onClose={close} title={t('space.join_code.title')}>
      <div class="sh-join-by-code">
        <p class="sh-muted" style={{ marginTop: 0 }}>
          {t('space.join_code.intro')}
        </p>

        {/* Two-tile method picker — same class names as PairingFlow's
         *  MethodPicker so the visual + a11y semantics stay in
         *  lockstep with the household pairing dialog. */}
        <div
          class="sh-pairing-method-grid"
          role="tablist"
          aria-label={t('space.join_code.method_aria')}
        >
          <button
            type="button"
            role="tab"
            aria-selected={method.value === 'paste'}
            class={`sh-pairing-method-card ${method.value === 'paste' ? 'sh-pairing-method-card--active' : ''}`}
            onClick={() => pickMethod('paste')}
            data-testid="invite-method-paste"
          >
            <span class="sh-pairing-method-icon" aria-hidden="true">📋</span>
            <span class="sh-pairing-method-title">
              {t('pairing.method_paste')}
            </span>
            <span class="sh-pairing-method-hint">
              {t('pairing.method_paste_hint')}
            </span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={method.value === 'qr'}
            class={`sh-pairing-method-card ${method.value === 'qr' ? 'sh-pairing-method-card--active' : ''}`}
            onClick={() => pickMethod('qr')}
            data-testid="invite-method-qr"
          >
            <span class="sh-pairing-method-icon" aria-hidden="true">📷</span>
            <span class="sh-pairing-method-title">
              {t('pairing.method_qr')}
            </span>
            <span class="sh-pairing-method-hint">
              {t('pairing.method_qr_hint')}
            </span>
          </button>
        </div>

        {method.value === 'paste' && (
          <>
            <textarea
              class="sh-textarea"
              rows={3}
              placeholder="socialhome://invite#…"
              value={draft.value}
              onInput={(e) => {
                draft.value = (e.target as HTMLTextAreaElement).value
                if (errorMsg.value) errorMsg.value = null
              }}
              aria-label={t('space.join_code.input_aria')}
              data-testid="join-by-code-input"
              autoFocus
            />
            {errorMsg.value && (
              <p class="sh-scan-error-inline" role="alert">
                {errorMsg.value}
              </p>
            )}
            <div class="sh-pairing-actions">
              <Button
                onClick={() => void submit()}
                loading={submitting.value}
                disabled={!draft.value.trim()}
              >
                {t('space.join_code.submit')}
              </Button>
            </div>
          </>
        )}

        {method.value === 'qr' && (
          <>
            <QrScanner
              onPayload={(raw) => {
                void submit(raw)
              }}
              onError={(msg) => { errorMsg.value = msg }}
            />
            {errorMsg.value && (
              <p class="sh-scan-error-inline" role="alert">
                {errorMsg.value}
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
