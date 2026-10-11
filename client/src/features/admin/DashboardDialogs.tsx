/**
 * The two secret dialogs of the Admin › Dashboard tab: the household
 * dashboard password and the tablets' privacy-lock PIN.
 *
 * Both validate in the dialog (the server re-checks: 422
 * ``DASHBOARD_PASSWORD_TOO_SHORT`` / ``DASHBOARD_PIN_INVALID``) and both
 * ask for the secret twice — neither can be read back later, so a typo
 * would lock the admin out of their own tablet.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { FormError } from '@/components/FormError'
import { Modal } from '@/components/Modal'
import { t } from '@/i18n/i18n'

/** Mirrors ``MIN_PASSWORD_LENGTH`` in ``domain/household_dashboard.py``. */
export const MIN_DASHBOARD_PASSWORD = 8
/** Mirrors ``MIN_PIN_LENGTH`` / ``MAX_PIN_LENGTH`` there. */
const PIN_RE = /^[0-9]{4,6}$/

/** A short password with one kind of character is allowed but worth a nudge. */
export function isWeakPassword(value: string): boolean {
  if (value.length < MIN_DASHBOARD_PASSWORD) return false
  const kinds = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/]
    .filter(re => re.test(value)).length
  return value.length < 12 && kinds < 3
}

interface SecretDialogProps {
  open: boolean
  /** A secret already exists — the copy says "change", not "set". */
  replacing: boolean
  onClose: () => void
  /** Called after the server accepted the new secret. */
  onSaved: () => void
}

export function DashboardPasswordDialog({ open, replacing, onClose, onSaved }: SecretDialogProps) {
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // A fresh, empty form every time the dialog opens.
  useEffect(() => {
    if (open) { setPw(''); setPw2(''); setError(null); setBusy(false) }
  }, [open])

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (pw.length < MIN_DASHBOARD_PASSWORD) {
      setError(t('admin.dashboard.password.too_short', { min: String(MIN_DASHBOARD_PASSWORD) }))
      return
    }
    if (pw !== pw2) {
      setError(t('admin.dashboard.password.mismatch'))
      return
    }
    setError(null)
    setBusy(true)
    try {
      await api.put('/api/admin/dashboard/password', { password: pw })
      onSaved()
    } catch (e: unknown) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const title = replacing
    ? t('admin.dashboard.password.change_title')
    : t('admin.dashboard.password.set_title')
  return (
    <Modal open={open} onClose={onClose} title={title}>
      <form class="sh-form sh-dashboard-secret" onSubmit={(e) => void submit(e)} noValidate>
        <p class="sh-muted">
          {replacing
            ? t('admin.dashboard.password.change_intro')
            : t('admin.dashboard.password.set_intro')}
        </p>
        <label for="sh-dash-pw">{t('admin.dashboard.password.new')}</label>
        <input
          id="sh-dash-pw"
          type="password"
          autocomplete="new-password"
          value={pw}
          disabled={busy}
          aria-describedby="sh-dash-pw-hint sh-dash-pw-error"
          onInput={(e) => setPw((e.target as HTMLInputElement).value)}
        />
        <span id="sh-dash-pw-hint" class="sh-form-hint" aria-live="polite">
          {isWeakPassword(pw)
            ? t('admin.dashboard.password.weak')
            : t('admin.dashboard.password.rule', { min: String(MIN_DASHBOARD_PASSWORD) })}
        </span>
        <label for="sh-dash-pw2">{t('admin.dashboard.password.repeat')}</label>
        <input
          id="sh-dash-pw2"
          type="password"
          autocomplete="new-password"
          value={pw2}
          disabled={busy}
          aria-describedby="sh-dash-pw-error"
          onInput={(e) => setPw2((e.target as HTMLInputElement).value)}
        />
        <FormError id="sh-dash-pw-error" message={error} />
        <div class="sh-modal-actions">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={busy}>
            {t('admin.dashboard.password.save')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export function DashboardPinDialog({ open, replacing, onClose, onSaved }: SecretDialogProps) {
  const [pin, setPin] = useState('')
  const [pin2, setPin2] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) { setPin(''); setPin2(''); setError(null); setBusy(false) }
  }, [open])

  const submit = async (ev: Event) => {
    ev.preventDefault()
    if (!PIN_RE.test(pin)) {
      setError(t('admin.dashboard.pin.invalid'))
      return
    }
    if (pin !== pin2) {
      setError(t('admin.dashboard.pin.mismatch'))
      return
    }
    setError(null)
    setBusy(true)
    try {
      await api.put('/api/admin/dashboard/pin', { pin })
      onSaved()
    } catch (e: unknown) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  // Digits only: anything else typed or pasted is dropped as it arrives.
  // The DOM value is rewritten too: when the cleaned value equals the
  // current state, Preact skips the re-render and the stray character stays.
  const digits = (e: Event): string => {
    const el = e.target as HTMLInputElement
    const clean = el.value.replace(/\D/g, '').slice(0, 6)
    if (el.value !== clean) el.value = clean
    return clean
  }
  const title = replacing ? t('admin.dashboard.pin.change_title') : t('admin.dashboard.pin.set_title')
  return (
    <Modal open={open} onClose={onClose} title={title}>
      <form class="sh-form sh-dashboard-secret" onSubmit={(e) => void submit(e)} noValidate>
        <p class="sh-muted">{t('admin.dashboard.pin.dialog_intro')}</p>
        <label for="sh-dash-pin">{t('admin.dashboard.pin.new')}</label>
        <input
          id="sh-dash-pin"
          class="sh-dashboard-pin-input"
          type="password"
          inputMode="numeric"
          pattern="[0-9]*"
          autocomplete="off"
          maxLength={6}
          value={pin}
          disabled={busy}
          aria-describedby="sh-dash-pin-hint sh-dash-pin-error"
          onInput={(e) => setPin(digits(e))}
        />
        <span id="sh-dash-pin-hint" class="sh-form-hint">{t('admin.dashboard.pin.rule')}</span>
        <label for="sh-dash-pin2">{t('admin.dashboard.pin.repeat')}</label>
        <input
          id="sh-dash-pin2"
          class="sh-dashboard-pin-input"
          type="password"
          inputMode="numeric"
          pattern="[0-9]*"
          autocomplete="off"
          maxLength={6}
          value={pin2}
          disabled={busy}
          aria-describedby="sh-dash-pin-error"
          onInput={(e) => setPin2(digits(e))}
        />
        <FormError id="sh-dash-pin-error" message={error} />
        <div class="sh-modal-actions">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={busy}>
            {t('admin.dashboard.pin.save')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
