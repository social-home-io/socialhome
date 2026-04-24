/**
 * PairingFlow — QR-code household pairing (§11 / §23.4).
 *
 * Multi-step wizard:
 *   1. Start — explanation + "Generate QR"
 *   2. Scan  — real QR code + 6-digit SAS input + live "waiting" hint.
 *              When the peer accepts, a WS ``pairing.accept_received``
 *              frame auto-fills the SAS for us; when the peer confirms
 *              via ``pairing.confirmed``, we jump straight to success.
 *   3. Success — confetti-style check + "Done".
 *
 * Works in two modes: ``household`` (peer-to-peer invite) and ``gfs``
 * (Global Federation Server connection).
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import QRCode from 'qrcode'
import { api } from '@/api'
import { ws } from '@/ws'
import { Modal } from './Modal'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

type PairingMode = 'household' | 'gfs'
type PairingStep = 'idle' | 'generating' | 'waiting' | 'verifying' | 'success' | 'failed'

const step = signal<PairingStep>('idle')
const mode = signal<PairingMode>('household')
const qrPayload = signal('')
const verificationCode = signal('')
const sasDigits = signal(['', '', '', '', '', ''])
const pairingToken = signal('')
const gfsUrl = signal('')
const open = signal(false)
const onGfsConnectedCb = signal<(() => void) | null>(null)
const peerHint = signal<string | null>(null)

export function openPairing(pairingMode: PairingMode = 'household') {
  mode.value = pairingMode
  open.value = true
  step.value = 'idle'
  gfsUrl.value = ''
  peerHint.value = null
  verificationCode.value = ''
  sasDigits.value = ['', '', '', '', '', '']
}

/**
 * Real QR renderer — encodes ``data`` to a PNG data-URL via the
 * ``qrcode`` library and displays it as an <img>. Uses error-
 * correction level M (15% redundancy) which is plenty for a
 * short URL and keeps the code visually clean.
 */
function QrCodeImg({ data, size = 240 }: { data: string; size?: number }) {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    let stopped = false
    QRCode.toDataURL(data, {
      errorCorrectionLevel: 'M',
      margin: 1,
      width: size * 2,   // 2× for retina
      color: { dark: '#0f172a', light: '#ffffff' },
    }).then(url => { if (!stopped) setSrc(url) })
      .catch(() => { /* leave src null */ })
    return () => { stopped = true }
  }, [data, size])
  if (!src) {
    return (
      <div class="sh-qr-skeleton"
           style={{ width: size, height: size }}
           aria-label="Generating QR code" />
    )
  }
  return (
    <img src={src} width={size} height={size}
         class="sh-qr-code" alt="Pairing QR code" />
  )
}

function SasInput({ autofilled }: { autofilled?: boolean }) {
  const handleDigitInput = (index: number, value: string) => {
    if (!/^\d?$/.test(value)) return
    const next = [...sasDigits.value]
    next[index] = value
    sasDigits.value = next
    verificationCode.value = next.join('')
    if (value && index < 5) {
      const nextInput = document.querySelector(
        `.sh-sas-digit[data-index="${index + 1}"]`,
      ) as HTMLInputElement | null
      nextInput?.focus()
    }
  }

  const handleKeyDown = (index: number, e: KeyboardEvent) => {
    if (e.key === 'Backspace' && !sasDigits.value[index] && index > 0) {
      const prevInput = document.querySelector(
        `.sh-sas-digit[data-index="${index - 1}"]`,
      ) as HTMLInputElement | null
      prevInput?.focus()
    }
  }

  return (
    <div class="sh-sas-input">
      <label>{t('pairing.enter_code')}</label>
      <div class={`sh-sas-digits ${autofilled ? 'sh-sas-digits--autofilled' : ''}`}>
        {sasDigits.value.map((digit, i) => (
          <input
            key={i}
            type="text"
            inputMode="numeric"
            maxLength={1}
            class="sh-sas-digit"
            data-index={i}
            value={digit}
            autoFocus={i === 0 && !autofilled}
            readOnly={autofilled}
            onInput={(e) => handleDigitInput(i, (e.target as HTMLInputElement).value)}
            onKeyDown={(e) => handleKeyDown(i, e as unknown as KeyboardEvent)}
          />
        ))}
      </div>
      {autofilled && (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          ✓ Auto-filled from the other device. Confirm to finish.
        </p>
      )}
    </div>
  )
}

function GfsUrlInput({ onSubmit }: { onSubmit: () => void }) {
  return (
    <div class="sh-gfs-url-input">
      <label>{t('gfs.enter_url')}</label>
      <input
        type="url"
        class="sh-input"
        placeholder="https://gfs.example.com"
        value={gfsUrl.value}
        onInput={(e) => gfsUrl.value = (e.target as HTMLInputElement).value}
      />
      <div class="sh-pairing-actions">
        <Button onClick={onSubmit} disabled={!gfsUrl.value.trim()}>
          {t('gfs.add')}
        </Button>
      </div>
    </div>
  )
}

function StepIndicator({ current }: { current: PairingStep }) {
  const stepIndex =
    current === 'idle' ? 0 :
    current === 'generating' || current === 'waiting' ? 1 :
    current === 'verifying' ? 2 : 3
  const labels = ['Start', 'Scan', 'Verify', 'Done']
  return (
    <ol class="sh-pairing-steps" aria-label="Pairing progress">
      {labels.map((label, i) => (
        <li key={label}
            class={`sh-pairing-step ${i <= stepIndex ? 'sh-pairing-step--done' : ''} ${i === stepIndex ? 'sh-pairing-step--active' : ''}`}>
          <span class="sh-pairing-step-dot" aria-hidden="true">
            {i <= stepIndex ? '✓' : i + 1}
          </span>
          <span class="sh-pairing-step-label">{label}</span>
        </li>
      ))}
    </ol>
  )
}

export function PairingFlow({ onGfsConnected }: { onGfsConnected?: () => void }) {
  onGfsConnectedCb.value = onGfsConnected ?? null
  const sasAutofilledRef = useRef(false)

  // ── Live updates from the federation layer ─────────────────────────
  useEffect(() => {
    const offAccept = ws.on('pairing.accept_received', (e) => {
      const d = e.data as { token?: string; verification_code?: string }
      if (!open.value || mode.value !== 'household') return
      if (!pairingToken.value || d.token !== pairingToken.value) return
      if (!d.verification_code) return
      // Auto-fill the 6 digits — saves the user typing when the
      // other device just accepted.
      const digits = d.verification_code.split('')
      if (digits.length === 6) {
        sasDigits.value = digits
        verificationCode.value = d.verification_code
        sasAutofilledRef.current = true
      }
    })
    const offConfirm = ws.on('pairing.confirmed', (e) => {
      const d = e.data as { instance_id?: string; display_name?: string }
      if (!open.value) return
      peerHint.value = d.display_name ?? null
      step.value = 'success'
      showToast(t('pairing.successful'), 'success')
    })
    const offAborted = ws.on('pairing.aborted', (e) => {
      const d = e.data as { reason?: string }
      if (!open.value) return
      step.value = 'failed'
      if (d.reason) peerHint.value = d.reason
    })
    return () => { offAccept(); offConfirm(); offAborted() }
  }, [])

  const initiate = async () => {
    step.value = 'generating'
    peerHint.value = null
    sasAutofilledRef.current = false
    try {
      const result = await api.post('/api/pairing/initiate', {
        inbox_url: `${location.origin}/federation/inbox`,
      }) as { token: string; [key: string]: unknown }
      qrPayload.value = JSON.stringify(result)
      pairingToken.value = result.token
      step.value = 'waiting'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  const verify = async () => {
    step.value = 'verifying'
    try {
      await api.post('/api/pairing/confirm', {
        token: pairingToken.value,
        verification_code: verificationCode.value,
      })
      // Success is dispatched by the WS subscriber above.
      // As a fallback, mark success after the API call resolves:
      if (step.value === 'verifying') step.value = 'success'
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  const copyPayload = async () => {
    try {
      await navigator.clipboard.writeText(qrPayload.value)
      showToast('Pairing link copied', 'success')
    } catch {
      showToast('Clipboard not available', 'error')
    }
  }

  const connectGfs = async () => {
    step.value = 'generating'
    try {
      await api.post('/api/gfs/connections', { inbox_url: gfsUrl.value.trim() })
      step.value = 'success'
      showToast(t('gfs.pair_success'), 'success')
      if (onGfsConnectedCb.value) onGfsConnectedCb.value()
    } catch (err: unknown) {
      step.value = 'failed'
      peerHint.value = (err as Error).message ?? null
    }
  }

  const resetSas = () => {
    sasDigits.value = ['', '', '', '', '', '']
    verificationCode.value = ''
    sasAutofilledRef.current = false
  }

  const resetAll = () => {
    step.value = 'idle'
    resetSas()
    gfsUrl.value = ''
    peerHint.value = null
  }

  const modalTitle = mode.value === 'gfs' ? t('gfs.title') : t('pairing.title')

  return (
    <Modal open={open.value}
           onClose={() => { open.value = false }}
           title={modalTitle}>
      <div class="sh-pairing-flow">
        {mode.value === 'household' && (
          <StepIndicator current={step.value} />
        )}

        {mode.value === 'household' && (
          <>
            {step.value === 'idle' && (
              <div class="sh-pairing-start">
                <div class="sh-pairing-hero" aria-hidden="true">🔗</div>
                <h3 style={{ margin: 0 }}>{t('pairing.title')}</h3>
                <p class="sh-muted">{t('pairing.intro')}</p>
                <Button onClick={initiate}>{t('pairing.generate')}</Button>
              </div>
            )}
            {step.value === 'generating' && <Spinner />}
            {step.value === 'waiting' && (
              <div class="sh-pairing-qr">
                <p class="sh-muted">{t('pairing.show_qr')}</p>
                <QrCodeImg data={qrPayload.value} size={240} />
                <div class="sh-row" style={{ gap: 'var(--sh-space-xs)', justifyContent: 'center' }}>
                  <button type="button" class="sh-link"
                          onClick={copyPayload}>
                    Copy pairing link
                  </button>
                </div>
                <div class="sh-pairing-waiting" role="status">
                  <span class="sh-pairing-pulse" aria-hidden="true" />
                  <span>{t('pairing.waiting')}</span>
                </div>
                <SasInput autofilled={sasAutofilledRef.current} />
                <div class="sh-pairing-actions">
                  <Button onClick={verify}
                          disabled={verificationCode.value.length !== 6}>
                    {t('pairing.verify')}
                  </Button>
                  {!sasAutofilledRef.current && (
                    <button type="button" class="sh-link" onClick={resetSas}>
                      {t('pairing.clear_code')}
                    </button>
                  )}
                </div>
              </div>
            )}
            {step.value === 'verifying' && <Spinner />}
            {step.value === 'success' && (
              <div class="sh-pairing-success">
                <div class="sh-pairing-success-burst" aria-hidden="true">
                  <span>✓</span>
                </div>
                <h3 style={{ margin: 0 }}>{t('pairing.success')}</h3>
                <p class="sh-muted">
                  {peerHint.value
                    ? `Paired with ${peerHint.value}.`
                    : t('pairing.success_message')}
                </p>
                <Button onClick={() => { open.value = false }}>
                  {t('pairing.done')}
                </Button>
              </div>
            )}
            {step.value === 'failed' && (
              <div class="sh-pairing-failed">
                <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
                <h3 style={{ margin: 0 }}>{t('pairing.failed')}</h3>
                <p class="sh-muted">
                  {peerHint.value ?? t('pairing.failed_message')}
                </p>
                <Button onClick={resetAll}>{t('pairing.retry')}</Button>
              </div>
            )}
          </>
        )}

        {mode.value === 'gfs' && (
          <>
            {step.value === 'idle' && (
              <GfsUrlInput onSubmit={connectGfs} />
            )}
            {step.value === 'generating' && <Spinner />}
            {step.value === 'success' && (
              <div class="sh-pairing-success">
                <div class="sh-pairing-success-burst" aria-hidden="true">
                  <span>✓</span>
                </div>
                <h3 style={{ margin: 0 }}>{t('gfs.connected')}</h3>
                <p class="sh-muted">{t('gfs.pair_success')}</p>
                <Button onClick={() => { open.value = false }}>
                  {t('pairing.done')}
                </Button>
              </div>
            )}
            {step.value === 'failed' && (
              <div class="sh-pairing-failed">
                <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
                <h3 style={{ margin: 0 }}>{t('pairing.failed')}</h3>
                <p class="sh-muted">
                  {peerHint.value ?? t('gfs.pairing_failed')}
                </p>
                <Button onClick={resetAll}>{t('pairing.retry')}</Button>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
