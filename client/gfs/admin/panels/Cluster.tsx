import { useCallback, useEffect, useState } from 'preact/hooks'
import { api } from '../api'
import { normaliseTimestamp } from '@/utils/relativeTime'

interface ClusterNode {
  node_id: string
  url: string
  status: string
  last_seen: string | null
  connected_clients: number
  active_sync_sessions: number
  is_self: boolean
}

interface ClusterData {
  node_id: string
  status: string
  nodes: ClusterNode[]
}

/* Cluster status maps differently from the moderation pills: a node is
   ``online`` (active/green), ``offline`` (banned/red), or transitional
   (``syncing``/``single-node``/``unknown`` → pending/amber). pillClass()
   in api.ts only knows the moderation vocabulary, so we map locally. */
function nodePill(status: string): string {
  if (status === 'online') return 'pill active'
  if (status === 'offline') return 'pill banned'
  return 'pill pending'
}

export function ClusterPanel() {
  const [data, setData] = useState<ClusterData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [peerUrl, setPeerUrl] = useState('')

  const reload = useCallback(async () => {
    try {
      const d = await api<ClusterData>('GET', '/admin/api/cluster')
      setData(d)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [])

  useEffect(() => {
    void reload()
    const id = setInterval(() => void reload(), 10_000)
    return () => clearInterval(id)
  }, [reload])

  const addPeer = async () => {
    const url = peerUrl.trim()
    if (!url) return
    try {
      await api('POST', '/admin/api/cluster/peers', { url })
      setPeerUrl('')
      setErr(null)
      await reload()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  const removePeer = async (nodeId: string) => {
    try {
      await api('DELETE', `/admin/api/cluster/peers/${encodeURIComponent(nodeId)}`)
      setErr(null)
      await reload()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  const pingPeer = async (nodeId: string) => {
    try {
      await api('POST', `/admin/api/cluster/peers/${encodeURIComponent(nodeId)}/ping`)
      setErr(null)
      await reload()
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  if (err && !data) return <p class="error">{err}</p>
  if (!data) return <p class="muted">Loading…</p>

  return (
    <>
      <h2>Cluster</h2>
      <p class="muted">
        This node: <span style={{ fontFamily: 'monospace' }}>{data.node_id}</span>
        {' — '}
        <span class={nodePill(data.status)}>{data.status}</span>
      </p>
      {err && <p class="error">{err}</p>}
      {/* Scroll the wide node table inside its own box so a narrow
          (mobile) viewport never gets horizontal scroll on the page body. */}
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Node</th>
              <th>Status</th>
              <th>Connected clients</th>
              <th>Sync sessions</th>
              <th>Last seen</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.nodes.map((n) => (
              <tr key={n.node_id}>
                <td>
                  <span style={{ fontFamily: 'monospace' }}>{n.node_id}</span>
                  {n.is_self && <span class="muted"> (this node)</span>}
                </td>
                <td><span class={nodePill(n.status)}>{n.status}</span></td>
                <td>{n.connected_clients}</td>
                <td>{n.active_sync_sessions}</td>
                <td>{n.last_seen ? new Date(normaliseTimestamp(n.last_seen)).toLocaleString() : '—'}</td>
                <td class="row-actions">
                  {!n.is_self && (
                    <>
                      <button class="secondary" onClick={() => void pingPeer(n.node_id)}>
                        Ping
                      </button>
                      <button class="danger" onClick={() => void removePeer(n.node_id)}>
                        Remove
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
        <input
          type="text"
          placeholder="https://peer.example"
          style={{ flex: 1 }}
          value={peerUrl}
          onInput={(e) => setPeerUrl((e.currentTarget as HTMLInputElement).value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void addPeer() }}
        />
        <button class="primary" onClick={() => void addPeer()}>Add</button>
      </div>
    </>
  )
}
