/**
 * AutoPairDialog — transitive auto-pair "via a trusted peer" flow
 * (§11 extension).
 *
 * UX: pick one of your already-paired households as the vouching
 * peer, enter the target instance's id (and an optional friendly
 * name). The backend sends a PAIRING_INTRO_AUTO through the vouching
 * peer; the target's auto-accept policy completes the pair without
 * any admin interaction — the UI shows a spinner + "Paired!" via a
 * WS ``pairing.confirmed`` frame.
 *
 * If the target's policy is off, the request falls back to the
 * admin-approval queue; we surface that outcome as "Waiting for the
 * other side".
 */
import { signal } from '@preact/signals'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

interface PeerOption {
  instance_id: string
  display_name: string
}

const open = signal(false)
const peers = signal<PeerOption[]>([])
let onPairedCb: (() => void) | null = null

export function openAutoPair(availablePeers: PeerOption[]): void {
  peers.value = availablePeers
  open.value = true
}

interface Props {
  onPaired?: () => void
}

export function AutoPairDialog({ onPaired }: Props) {
  onPairedCb = onPaired ?? null
  const [viaPeer, setViaPeer] = useState('')
  const [targetId, setTargetId] = useState('')
  const [targetName, setTargetName] = useState('')
  const [step, setStep] = useState<
    'form' | 'pending' | 'paired' | 'queued' | 'failed'
  >('form')
  const [error, setError] = useState<string | null>(null)
  const [, setToken] = useState<string | null>(null)

  useEffect(() => {
    if (peers.value.length === 1 && !viaPeer) {
      setViaPeer(peers.value[0].instance_id)
    }
  }, [peers.value])

  useEffect(() => {
    if (!open.value) return
    const off = ws.on('pairing.confirmed', (e) => {
      if (step !== 'pending') return
      const d = e.data as { instance_id?: string }
      if (d.instance_id === targetId) {
        setStep('paired')
        showToast(t('connections.requests.paired', { name: targetName || d.instance_id }), 'success')
        onPairedCb?.()
      }
    })
    return () => { off() }
  }, [open.value, step, targetId, targetName])

  const reset = () => {
    setViaPeer(peers.value.length === 1 ? peers.value[0].instance_id : '')
    setTargetId('')
    setTargetName('')
    setStep('form')
    setError(null)
    setToken(null)
  }

  const close = () => {
    open.value = false
    reset()
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!viaPeer || !targetId.trim()) return
    setStep('pending')
    setError(null)
    try {
      const resp = await api.post('/api/pairing/auto-pair-via', {
        via_instance_id:     viaPeer,
        target_instance_id:  targetId.trim(),
        target_display_name: targetName.trim() || undefined,
      }) as { status: string; token: string }
      setToken(resp.token)
      // Give the federation round-trip 10 s before we assume the
      // target has the auto-accept policy off (fall-through queue).
      setTimeout(() => {
        setStep(prev => prev === 'pending' ? 'queued' : prev)
      }, 10_000)
    } catch (err: unknown) {
      setStep('failed')
      setError((err as Error).message ?? t('autopair.request_failed'))
    }
  }

  return (
    <Modal open={open.value} onClose={close}
           title={t('connections.pair_via_peer')}>
      <div class="sh-form sh-auto-pair-dialog">
        {step === 'form' && (
          <>
            <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-sm)' }}>
              {t('autopair.intro')}
            </p>

            <label>
              {t('autopair.via_label')}
              <select value={viaPeer}
                      required
                      onChange={(e) =>
                        setViaPeer((e.target as HTMLSelectElement).value)}>
                <option value="" disabled>{t('autopair.via_placeholder')}</option>
                {peers.value.map(p => (
                  <option key={p.instance_id} value={p.instance_id}>
                    {p.display_name}
                  </option>
                ))}
              </select>
            </label>

            <label>
              {t('autopair.target_label')}
              <input type="text" value={targetId}
                     required maxLength={128}
                     placeholder="abcdef0123456789…"
                     onInput={(e) =>
                       setTargetId((e.target as HTMLInputElement).value)} />
              <span class="sh-muted"
                    style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                {t('autopair.target_hint')}
              </span>
            </label>

            <label>
              {t('autopair.name_label')}
              <input type="text" value={targetName} maxLength={80}
                     placeholder={t('autopair.name_placeholder')}
                     onInput={(e) =>
                       setTargetName((e.target as HTMLInputElement).value)} />
            </label>

            <div class="sh-form-actions">
              <Button variant="secondary" type="button" onClick={close}>
                {t('common.cancel')}
              </Button>
              <Button onClick={submit}
                      disabled={!viaPeer || !targetId.trim()}>
                {t('autopair.send')}
              </Button>
            </div>
          </>
        )}

        {step === 'pending' && (
          <div class="sh-auto-pair-pending">
            <div class="sh-pairing-pulse" aria-hidden="true" />
            <h3 style={{ margin: 0 }}>{t('autopair.pending_title', { via: peerName(viaPeer) })}</h3>
            <p class="sh-muted">
              {t('autopair.pending_body', {
                via: peerName(viaPeer),
                target: targetName || t('autopair.other_household'),
              })}
            </p>
          </div>
        )}

        {step === 'paired' && (
          <div class="sh-pairing-success">
            <div class="sh-pairing-success-burst" aria-hidden="true">
              <span>✓</span>
            </div>
            <h3 style={{ margin: 0 }}>{t('autopair.paired_title')}</h3>
            <p class="sh-muted">
              {t('autopair.paired_body', { name: targetName || targetId })}
            </p>
            <Button onClick={close}>{t('pairing.done')}</Button>
          </div>
        )}

        {step === 'queued' && (
          <div class="sh-auto-pair-queued">
            <div class="sh-pairing-hero" aria-hidden="true">⏳</div>
            <h3 style={{ margin: 0 }}>{t('autopair.queued_title')}</h3>
            <p class="sh-muted">
              {t('autopair.queued_body', { target: targetName || t('autopair.other_household') })}
            </p>
            <Button onClick={close}>{t('common.ok')}</Button>
          </div>
        )}

        {step === 'failed' && (
          <div class="sh-pairing-failed">
            <div class="sh-pairing-fail-mark" aria-hidden="true">⚠</div>
            <h3 style={{ margin: 0 }}>{t('autopair.failed_title')}</h3>
            <p class="sh-muted">{error ?? t('autopair.unknown_error')}</p>
            <Button onClick={reset}>{t('common.try_again')}</Button>
          </div>
        )}
      </div>
    </Modal>
  )
}

function peerName(id: string): string {
  return peers.value.find(p => p.instance_id === id)?.display_name ?? id.slice(0, 8)
}
