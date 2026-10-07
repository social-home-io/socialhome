import { useCallback, useEffect, useState } from 'preact/hooks'
import { api, pillClass } from '../api'

interface Client {
  instance_id: string
  display_name: string
  status: string
}

const FILTERS: Array<{ key: string; label: string }> = [
  { key: '',        label: 'All' },
  { key: 'active',  label: 'Active' },
  { key: 'pending', label: 'Pending' },
  { key: 'banned',  label: 'Banned' },
]

export function ClientsPanel() {
  const [filter, setFilter] = useState('')
  const [list, setList] = useState<Client[]>([])
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const qs = filter ? `?status=${encodeURIComponent(filter)}` : ''
      const data = await api<Client[]>('GET', `/admin/api/clients${qs}`)
      setList(data)
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }, [filter])

  useEffect(() => { void reload() }, [reload])

  const action = async (id: string, name: string) => {
    await api('POST', `/admin/api/clients/${encodeURIComponent(id)}/${name}`)
    await reload()
  }

  return (
    <>
      <h2>Clients</h2>
      <div style={{ marginBottom: '10px' }}>
        {FILTERS.map((f) => (
          <button
            key={f.key}
            class={`secondary${filter === f.key ? ' is-active' : ''}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
      </div>
      {err && <p class="error">{err}</p>}
      <table>
        <thead>
          <tr>
            <th>Display</th>
            <th>Instance ID</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {list.length === 0 && (
            <tr><td colSpan={4} class="muted">No clients.</td></tr>
          )}
          {list.map((c) => (
            <tr key={c.instance_id}>
              <td>{c.display_name || '—'}</td>
              <td style={{ fontFamily: 'monospace' }}>{c.instance_id}</td>
              <td><span class={pillClass(c.status)}>{c.status}</span></td>
              <td class="row-actions">
                {c.status === 'pending' && (
                  <>
                    <button class="primary" onClick={() => void action(c.instance_id, 'accept')}>
                      Accept
                    </button>
                    <button class="secondary" onClick={() => void action(c.instance_id, 'reject')}>
                      Reject
                    </button>
                  </>
                )}
                {c.status === 'active' && (
                  <button class="danger" onClick={() => void action(c.instance_id, 'ban')}>
                    Ban
                  </button>
                )}
                {c.status === 'banned' && (
                  <button class="secondary" onClick={() => void action(c.instance_id, 'unban')}>
                    Unban
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
