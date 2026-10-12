/**
 * DiagnosticsRow — one file to attach to a bug report.
 *
 * Fetched through the ``api`` client (auth + ingress base) and saved via
 * a Blob: a bare ``<a download>`` at the endpoint would be an
 * unauthenticated navigation. The copy says plainly what is in it, since
 * "diagnostics" invites the worry that it might hold messages or keys.
 */
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { showToast } from '@/components/Toast'
import { AdvancedRow } from './AdvancedRow'
import { ConnIcon } from './ConnIcon'

export function DiagnosticsRow() {
  const [busy, setBusy] = useState(false)

  const download = async () => {
    setBusy(true)
    try {
      const data = await api.get('/api/admin/diagnostics')
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const stamp = new Date().toISOString().replace(/[:.]/g, '-')
      a.download = `socialhome-diagnostics-${stamp}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      // Revoke on the next tick — revoking synchronously can cancel the
      // download in some browsers before it has read the blob.
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch {
      showToast(t('connections.diag.failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AdvancedRow
      id="diag"
      icon="download"
      label={t('connections.adv.diag_title')}
      desc={t('connections.adv.diag_desc')}
    >
      <p>{t('connections.diag.note')}</p>
      <div>
        <button
          type="button"
          class="sh-btn sh-btn--secondary"
          disabled={busy}
          onClick={() => void download()}
        >
          <ConnIcon name="download" />
          {busy ? t('connections.diag.preparing') : t('connections.diag.download')}
        </button>
      </div>
    </AdvancedRow>
  )
}
