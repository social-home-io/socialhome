/**
 * NetworkMap — visual map of paired instances + GFS connections (§23.71).
 *
 * Renders a force-directed-ish radial layout: this household at the
 * centre, paired peers as outer nodes, edges showing connection
 * health (reachable / unreachable / pending). The visualisation is
 * SVG so it stays accessible and copy-pastable.
 *
 * Data lives at GET /api/connections (federation_repo lists all
 * RemoteInstance rows). When the backend exposes /api/connections
 * the component renders live data; otherwise it falls back to a
 * placeholder explaining what would appear here.
 */
import { useEffect } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { api } from '@/api'
import { Spinner } from '@/components/Spinner'
import { t } from '@/i18n/i18n'
import { connections, type Connection as StoreConnection } from '@/store/connections'

interface Connection extends StoreConnection {
  status?: 'confirmed' | 'pending_sent' | 'pending_received' | 'unpairing'
  paired_at?: string | null
  source?: 'manual' | 'space_session'
}

const loading = signal(true)

const stats = computed(() => {
  const all = connections.value as Connection[]
  return {
    total:       all.length,
    confirmed:   all.filter(c => (c.status ?? 'confirmed') === 'confirmed').length,
    reachable:   all.filter(c => c.reachable).length,
    pending:     all.filter(c => (c.status ?? '').startsWith('pending')).length,
  }
})

export default function NetworkMap() {
  useEffect(() => { void loadConnections() }, [])

  if (loading.value) return <Spinner />

  return (
    <div class="sh-network-map">
      <header class="sh-row sh-justify-between">
        <h2>{t('network_map.title')}</h2>
        <span class="sh-muted">
          {t('network_map.stats', {
            paired: String(stats.value.confirmed),
            reachable: String(stats.value.reachable),
            pending: String(stats.value.pending),
          })}
        </span>
      </header>

      {connections.value.length === 0 ? (
        <p class="sh-muted">
          {t('network_map.empty')}
        </p>
      ) : (
        <svg
          class="sh-network-map-svg"
          viewBox="-160 -160 320 320"
          aria-label={t('network_map.aria', { n: String(stats.value.total) })}
        >
          {/* Self in the centre. */}
          <circle cx={0} cy={0} r={18} class="sh-network-self" />
          <text x={0} y={4} text-anchor="middle">{t('network_map.you')}</text>

          {(connections.value as Connection[]).map((c, i) => {
            const list = connections.value as Connection[]
            const angle = (i * 2 * Math.PI) / list.length
            const x = Math.cos(angle) * 120
            const y = Math.sin(angle) * 120
            const cls = c.reachable
              ? 'sh-network-edge-ok'
              : 'sh-network-edge-down'
            const status = c.status ?? 'confirmed'
            return (
              <g key={c.instance_id}>
                <line
                  x1={0}
                  y1={0}
                  x2={x}
                  y2={y}
                  class={cls}
                  stroke-dasharray={status !== 'confirmed' ? '4 4' : undefined}
                />
                <circle
                  cx={x}
                  cy={y}
                  r={14}
                  class={c.reachable
                    ? 'sh-network-peer-ok'
                    : 'sh-network-peer-down'}
                />
                <title>
                  {c.display_name ?? c.instance_id} ({status},
                  {c.reachable ? ' reachable' : ' unreachable'})
                </title>
              </g>
            )
          })}
        </svg>
      )}
    </div>
  )
}

async function loadConnections() {
  loading.value = true
  try {
    connections.value = await api.get('/api/connections') as Connection[]
  } catch {
    connections.value = []
  } finally {
    loading.value = false
  }
}
