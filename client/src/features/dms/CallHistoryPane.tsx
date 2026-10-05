/**
 * CallHistoryPane — per-conversation call history (§26.8 + admin drill-down).
 *
 * Renders ``GET /api/conversations/{id}/calls``. One row per historical
 * call, grouped by day. Each row exposes the initiator, call type,
 * duration, status, and (if present) an average RTT / loss badge so
 * users can spot poor-quality calls at a glance.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import {
  householdDisplayName,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { t, formatLocale } from '@/i18n/i18n'

interface CallRow {
  call_id: string
  conversation_id: string
  initiator_user_id: string
  callee_user_id: string | null
  call_type: 'audio' | 'video'
  status: 'ringing' | 'active' | 'ended' | 'declined' | 'missed'
  participant_user_ids: string[]
  started_at: string
  connected_at: string | null
  ended_at: string | null
  duration_seconds: number | null
  avg_rtt_ms: number | null
  avg_loss_pct: number | null
}

// Status labels: t(`calls.state.${status}`) — calls.state.ringing / .active /
// .ended / .declined / .missed (CallsTab adds .in_progress).
const calls = signal<CallRow[]>([])
const loading = signal(true)

function formatDuration(sec: number | null): string {
  if (sec == null || sec <= 0) return ''
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return m > 0
    ? t('calls.history.duration_min_sec', { m: String(m), s: String(s) })
    : t('calls.history.duration_sec', { s: String(s) })
}

function dayKey(iso: string): string {
  return new Date(iso).toLocaleDateString(formatLocale(), {
    weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
  })
}

function statusIcon(status: string): string {
  switch (status) {
    case 'missed':   return '❌'
    case 'declined': return '✖️'
    case 'ended':    return '✅'
    case 'active':   return '🟢'
    default:         return '📞'
  }
}

export default function CallHistoryPane() {
  const { params } = useRoute()
  const loc = useLocation()
  const convId = params.id

  useEffect(() => {
    loading.value = true
    void loadHouseholdUsers()  // resolve initiator names from raw user_ids
    api.get(`/api/conversations/${convId}/calls`).then((r: { calls: CallRow[] }) => {
      calls.value = r.calls
      loading.value = false
    }).catch(() => { loading.value = false })
  }, [convId])

  if (loading.value) return <Spinner />
  if (calls.value.length === 0) {
    return (
      <div class="sh-call-history-empty">
        <p class="sh-muted">{t('calls.history.empty')}</p>
        <Button onClick={() => loc.route(`/dms/${convId}`)}>{t('common.back')}</Button>
      </div>
    )
  }

  // Group rows by calendar day.
  const groups = new Map<string, CallRow[]>()
  for (const c of calls.value) {
    const key = dayKey(c.started_at)
    const arr = groups.get(key) ?? []
    arr.push(c)
    groups.set(key, arr)
  }

  return (
    <div class="sh-call-history">
      <header class="sh-call-history-header">
        <Button onClick={() => loc.route(`/dms/${convId}`)}>← {t('common.back')}</Button>
        <h2>{t('calls.history.title')}</h2>
      </header>
      {[...groups.entries()].map(([day, rows]) => (
        <section key={day}>
          <h3 class="sh-day-header">{day}</h3>
          {rows.map(c => (
            <div key={c.call_id} class={`sh-call-row sh-call-${c.status}`}>
              <span class="sh-call-icon" aria-hidden="true">
                {statusIcon(c.status)} {c.call_type === 'video' ? '📹' : '📞'}
              </span>
              <span class="sh-call-who">{householdDisplayName(c.initiator_user_id)}</span>
              <span class="sh-call-dur">{formatDuration(c.duration_seconds)}</span>
              <span class="sh-call-status">{t(`calls.state.${c.status}`)}</span>
              {c.avg_rtt_ms != null && (
                <span class={`sh-call-quality ${qBadge(c.avg_rtt_ms, c.avg_loss_pct)}`}>
                  {Math.round(c.avg_rtt_ms)}ms
                  {c.avg_loss_pct != null
                    && ` · ${t('calls.history.loss', { pct: c.avg_loss_pct.toFixed(1) })}`}
                </span>
              )}
            </div>
          ))}
        </section>
      ))}
    </div>
  )
}

function qBadge(rtt: number, loss: number | null): string {
  const l = loss ?? 0
  if (rtt > 300 || l > 5) return 'sh-q-poor'
  if (rtt > 150 || l > 1) return 'sh-q-fair'
  return 'sh-q-good'
}
