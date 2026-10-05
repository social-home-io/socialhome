/**
 * GuardianAuditLog — read-only view of a minor's audit trail (§CP).
 *
 * Backed by `GET /api/cp/minors/{minor_id}/audit-log`. Only guardians
 * of the minor and household admins may read it; the backend enforces
 * the ACL and returns 403 otherwise.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { formatLocale, t } from '@/i18n/i18n'

interface AuditEntry {
  id:          string
  minor_id:    string
  guardian_id: string
  action:      string
  detail:      string | null
  occurred_at: string
}

const entries = signal<AuditEntry[]>([])
const loading = signal(false)
const forbidden = signal(false)

export async function loadAuditLog(minorId: string) {
  loading.value = true
  forbidden.value = false
  try {
    const body = await api.get(`/api/cp/minors/${minorId}/audit-log`) as
      { entries: AuditEntry[] }
    entries.value = body.entries
  } catch (err: any) {
    if (err?.status === 403) {
      forbidden.value = true
    } else {
      showToast(t('cp.audit.load_failed', { error: String(err?.message || err) }), 'error')
    }
    entries.value = []
  } finally {
    loading.value = false
  }
}

function _parseDetail(s: string | null): string {
  if (!s) return ''
  try {
    const parsed = JSON.parse(s)
    const pairs = Object.entries(parsed)
      .map(([k, v]) => `${k}=${String(v)}`)
    return pairs.join(', ')
  } catch {
    return s
  }
}

export function GuardianAuditLog({ minorId }: { minorId: string }) {
  useEffect(() => {
    void loadAuditLog(minorId)
  }, [minorId])

  if (loading.value) return <Spinner />
  if (forbidden.value) {
    return (
      <p class="sh-muted sh-error">
        {t('cp.audit.forbidden')}
      </p>
    )
  }
  if (entries.value.length === 0) {
    return <p class="sh-muted">{t('cp.audit.empty')}</p>
  }

  return (
    <ol class="sh-audit-log" aria-label={t('cp.audit.aria')}>
      {entries.value.map((e) => (
        <li key={e.id} class="sh-audit-row">
          <time class="sh-muted">
            {new Date(e.occurred_at).toLocaleString(formatLocale())}
          </time>
          <strong>{e.action.replace('_', ' ')}</strong>
          <span class="sh-muted">{t('cp.audit.by', { name: e.guardian_id })}</span>
          {e.detail && (
            <span class="sh-audit-detail">{_parseDetail(e.detail)}</span>
          )}
        </li>
      ))}
    </ol>
  )
}
