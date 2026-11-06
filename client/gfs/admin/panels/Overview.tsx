import { useEffect, useState } from 'preact/hooks'
import { api } from '../api'
import { MediaCacheCard, type SpaceMediaStats } from './MediaCache'

interface OverviewData {
  clients: { active: number; pending: number; banned: number }
  spaces: { active: number; pending: number; banned: number }
  open_reports: number
  /** The space media cache; absent (or null) on an older server. */
  space_media?: SpaceMediaStats | null
}

export function OverviewPanel() {
  const [data, setData] = useState<OverviewData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    api<OverviewData>('GET', '/admin/api/overview')
      .then(setData)
      .catch((e) => setErr((e as Error).message))
  }, [])
  if (err) return <p class="error">{err}</p>
  if (!data) return <p class="muted">Loading…</p>
  return (
    <>
      <h2>Overview</h2>
      <div class="grid-3">
        <div class="card">
          <div>Clients active</div>
          <div class="val">{data.clients.active}</div>
          <div class="muted">{data.clients.pending} pending</div>
        </div>
        <div class="card">
          <div>Spaces active</div>
          <div class="val">{data.spaces.active}</div>
          <div class="muted">{data.spaces.pending} pending</div>
        </div>
        <div class="card">
          <div>Open reports</div>
          <div class="val">{data.open_reports}</div>
        </div>
      </div>
      {data.space_media ? <MediaCacheCard stats={data.space_media} /> : null}
    </>
  )
}
