import { useCallback, useEffect, useRef, useState } from 'preact/hooks'
import { api, ApiError } from '../api'
import { normaliseTimestamp } from '@/utils/relativeTime'

/* Operator-approved cluster membership: a node joins only when its HELLO
   is signed with this GFS's own key (the shared seed) or with a key the
   operator approved here. So the panel shows this node's key (for the
   peer's operator), takes a key on add, and marks each row with where its
   key came from. Rotating a key = remove, then add again. */

type KeySource = 'own' | 'approved' | 'none'

interface ClusterNode {
  node_id: string
  url: string
  status: string
  last_seen: string | null
  connected_clients: number
  active_sync_sessions: number
  is_self: boolean
  public_key: string
  key_source: KeySource
}

interface ClusterData {
  node_id: string
  public_key: string
  status: string
  nodes: ClusterNode[]
}

type Field = 'node_id' | 'url' | 'public_key'
type FieldErrors = Partial<Record<Field, string>>

const HEX_KEY = /^[0-9a-f]{64}$/i

/* Cluster status maps differently from the moderation pills: a node is
   ``online`` (active/green), ``offline`` (banned/red), or transitional
   (``syncing``/``single-node``/``unknown`` → pending/amber). pillClass()
   in api.ts only knows the moderation vocabulary, so we map locally. */
function nodePill(status: string): string {
  if (status === 'online') return 'pill active'
  if (status === 'offline') return 'pill banned'
  return 'pill pending'
}

/** Short, human-comparable form of a hex key: first 8 + last 8 chars,
 *  grouped by four (``0123 4567 … 89ab cdef``). Operators read it aloud
 *  to each other to confirm they approved the right key. */
export function keyFingerprint(key: string | null | undefined): string {
  const k = (key ?? '').trim().toLowerCase()
  if (!k) return '—'
  if (k.length <= 16) return k.replace(/(.{4})(?=.)/g, '$1 ')
  const g = (s: string) => s.replace(/(.{4})(?=.)/g, '$1 ')
  return `${g(k.slice(0, 8))} … ${g(k.slice(-8))}`
}

const PEER_ERRORS: Record<string, string> = {
  invalid_node_id:
    "Enter the peer's node id as shown on its Cluster page: up to 128 letters, digits and . _ : / -",
  node_id_is_self:
    "That is this server's own node id. Enter the node id of the other GFS.",
  invalid_url:
    "Enter the peer's address, starting with http:// or https://, without a user name, ? or #.",
  invalid_public_key:
    "That isn't a valid public key. Copy the 64-character key from the peer's Cluster page.",
  key_mismatch:
    'This node is already approved with a different key. Remove it first to change its key.',
}

/** Plain-language message for an add-peer error code from the server. */
export function peerErrorMessage(code: string): string {
  return PEER_ERRORS[code] ?? "Couldn't add the peer. Check the values and try again."
}

/* Which form field a server error code points at, for aria-invalid. */
const ERROR_FIELD: Record<string, Field> = {
  invalid_node_id: 'node_id',
  node_id_is_self: 'node_id',
  invalid_url: 'url',
  invalid_public_key: 'public_key',
  key_mismatch: 'public_key',
}

function validate(nodeId: string, url: string, key: string): FieldErrors {
  const errs: FieldErrors = {}
  if (!nodeId.trim()) {
    errs.node_id = "Enter the peer's node id."
  } else if (nodeId.trim().length > 128) {
    errs.node_id = 'The node id can be at most 128 characters.'
  }
  let ok = false
  try {
    const u = new URL(url.trim())
    ok = (u.protocol === 'http:' || u.protocol === 'https:') && !!u.hostname
  } catch { /* not a URL */ }
  if (!ok) errs.url = 'The URL must start with http:// or https://.'
  if (!HEX_KEY.test(key.trim())) {
    errs.public_key = 'The public key must be 64 hex characters (0–9, a–f).'
  }
  return errs
}

const KEY_SOURCE_LABEL: Record<KeySource, string> = {
  own: "shares this server's key",
  approved: 'approved key',
  none: 'not approved',
}

function KeyCell({ node }: { node: ClusterNode }) {
  return (
    <td class="key-cell">
      <code class="fingerprint">{keyFingerprint(node.public_key)}</code>
      <div class={`key-source key-source-${node.key_source}`}>
        {node.is_self ? "this server's key" : KEY_SOURCE_LABEL[node.key_source]}
      </div>
      {!node.is_self && node.key_source === 'none' && (
        <div class="muted key-hint">
          If it has its own key, approve it again below with that key. If it
          shares this server's key, there's nothing to do: it rejoins by itself.
        </div>
      )}
    </td>
  )
}

/* A value with a Copy button. When the clipboard API is missing or
   refused (plain-http admin, locked-down browser) the value is selected
   instead, so the operator only has to press their copy shortcut. */
function CopyableValue({ label, value, onDone }: {
  label: string
  value: string
  onDone: (msg: string) => void
}) {
  const ref = useRef<HTMLElement>(null)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      onDone(`${label} copied`)
    } catch {
      const sel = window.getSelection()
      if (sel && ref.current) sel.selectAllChildren(ref.current)
      onDone(`Couldn't copy automatically. The ${label.toLowerCase()} is selected, so you can copy it by hand.`)
    }
  }
  return (
    <>
      <code class="wrap" ref={ref}>{value}</code>
      <button
        type="button"
        class="secondary"
        aria-label={`Copy ${label.toLowerCase()}`}
        onClick={() => void copy()}
      >Copy</button>
    </>
  )
}

function SelfNode({ data }: { data: ClusterData }) {
  const [copied, setCopied] = useState('')
  return (
    <div class="self-node">
      <h3>This node</h3>
      <p class="muted">
        Give these two values to the operator of a peer GFS so they can approve this node.
      </p>
      <dl>
        <dt>Node id</dt>
        <dd>
          <CopyableValue label="Node id" value={data.node_id} onDone={setCopied} />
        </dd>
        <dt>Public key</dt>
        <dd>
          <CopyableValue label="Public key" value={data.public_key} onDone={setCopied} />
        </dd>
        <dt>Fingerprint</dt>
        <dd><code class="fingerprint">{keyFingerprint(data.public_key)}</code></dd>
        <dt>Status</dt>
        <dd><span class={nodePill(data.status)}>{data.status}</span></dd>
      </dl>
      <p class="muted copy-status" role="status" aria-live="polite">{copied}</p>
    </div>
  )
}

function RemoveConfirm({ node, onConfirm, onCancel }: {
  node: ClusterNode
  onConfirm: () => void
  onCancel: () => void
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { cancelRef.current?.focus() }, [node.node_id])
  /* A ``none`` row may be a shared-key sibling (comes back by itself) or a
     node with its own key (needs approving) — say both, as the row does. */
  const warning = node.key_source === 'own'
    ? "It shares this server's signing key, so it will join again by itself the next time it contacts this server."
    : node.key_source === 'none'
      ? "If it shares this server's key, it joins again by itself. If it has its own key, it can't sync with this server until you approve it again with that key."
      : "It can't sync with this server until you add it again with its public key."
  return (
    <div
      class="confirm-box"
      role="alertdialog"
      aria-labelledby="remove-title"
      aria-describedby="remove-desc"
      onKeyDown={(e) => { if (e.key === 'Escape') onCancel() }}
    >
      <p id="remove-title"><strong>Remove <code class="wrap">{node.node_id}</code> from the cluster?</strong></p>
      <p id="remove-desc" class="muted">{warning}</p>
      <div class="actions">
        <button type="button" class="danger" onClick={onConfirm}>Remove node</button>
        <button type="button" class="secondary" ref={cancelRef} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

function AddPeerForm({ onAdded }: { onAdded: () => Promise<void> }) {
  const [nodeId, setNodeId] = useState('')
  const [url, setUrl] = useState('')
  const [key, setKey] = useState('')
  const [fieldErrs, setFieldErrs] = useState<FieldErrors>({})
  const [serverErr, setServerErr] = useState<{ msg: string; field?: Field } | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: Event) => {
    e.preventDefault()
    const errs = validate(nodeId, url, key)
    setFieldErrs(errs)
    setServerErr(null)
    if (Object.keys(errs).length) return
    setBusy(true)
    try {
      await api('POST', '/admin/api/cluster/peers', {
        node_id: nodeId.trim(),
        url: url.trim(),
        public_key: key.trim(),
      })
      setNodeId('')
      setUrl('')
      setKey('')
      await onAdded()
    } catch (err) {
      const code = err instanceof ApiError ? err.code : null
      setServerErr(code
        ? { msg: peerErrorMessage(code), field: ERROR_FIELD[code] }
        : { msg: `Couldn't add the peer: ${(err as Error).message}` })
    } finally {
      setBusy(false)
    }
  }

  const input = (
    field: Field,
    label: string,
    ariaLabel: string,
    value: string,
    set: (v: string) => void,
    extra: Record<string, string>,
  ) => {
    const err = fieldErrs[field]
    // A server complaint about this field is shown under it (not at the
    // bottom of the form) and describes the input like a local one.
    const srvErr = serverErr?.field === field ? serverErr.msg : null
    const invalid = !!err || !!srvErr
    const describedBy = err ? `peer-${field}-err` : srvErr ? `peer-${field}-srv-err` : undefined
    return (
      <div class="field">
        <label for={`peer-${field}`}>{label}</label>
        <input
          id={`peer-${field}`}
          type="text"
          aria-label={ariaLabel}
          aria-invalid={invalid ? 'true' : 'false'}
          aria-describedby={describedBy}
          autocomplete="off"
          autocapitalize="off"
          spellcheck={false}
          value={value}
          onInput={(e) => {
            set((e.currentTarget as HTMLInputElement).value)
            // An edit answers the previous complaint — don't leave a stale
            // red border or message next to what the operator just typed.
            if (fieldErrs[field]) setFieldErrs({ ...fieldErrs, [field]: undefined })
            if (serverErr) setServerErr(null)
          }}
          {...extra}
        />
        {err && <p id={`peer-${field}-err`} class="error field-error">{err}</p>}
        {srvErr && (
          <p id={`peer-${field}-srv-err`} class="error field-error" role="alert">{srvErr}</p>
        )}
      </div>
    )
  }

  return (
    <form class="add-peer" noValidate onSubmit={(e) => void submit(e)}>
      <h3>Approve a peer node</h3>
      <p class="muted">
        Nodes that share this server's signing key join automatically. Only add
        nodes that have their own key: copy the values from that node's Cluster page.
      </p>
      {input('node_id', 'Node id', 'Peer node id', nodeId, setNodeId,
        { placeholder: 'gfs-2' })}
      {input('url', 'URL', 'Peer URL', url, setUrl,
        { placeholder: 'https://gfs-2.example', inputMode: 'url' })}
      {input('public_key', 'Public key', 'Peer public key', key, setKey,
        { placeholder: '64 hex characters', class: 'mono' })}
      {serverErr && !serverErr.field && <p class="error" role="alert">{serverErr.msg}</p>}
      <button class="primary" type="submit" disabled={busy}>Approve node</button>
    </form>
  )
}

export function ClusterPanel() {
  const [data, setData] = useState<ClusterData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<ClusterNode | null>(null)

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

  const removePeer = async (nodeId: string) => {
    setConfirming(null)
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
    <div class="cluster-panel">
      <h2>Cluster</h2>
      <SelfNode data={data} />
      <h3>Nodes</h3>
      {err && <p class="error">{err}</p>}
      {confirming && (
        <RemoveConfirm
          node={confirming}
          onConfirm={() => void removePeer(confirming.node_id)}
          onCancel={() => setConfirming(null)}
        />
      )}
      {/* Scroll the wide node table inside its own box so a narrow
          (mobile) viewport never gets horizontal scroll on the page body. */}
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Node</th>
              <th>Key</th>
              <th>Status</th>
              <th>Clients</th>
              <th>Sync sessions</th>
              <th>Last seen</th>
              <th><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {data.nodes.map((n) => (
              <tr key={n.node_id}>
                <td class="node-cell">
                  <code class="node-id">{n.node_id}</code>
                  {n.is_self && <span class="muted"> (this node)</span>}
                  {n.url && <div class="muted node-url">{n.url}</div>}
                </td>
                <KeyCell node={n} />
                <td><span class={nodePill(n.status)}>{n.status}</span></td>
                <td>{n.connected_clients}</td>
                <td>{n.active_sync_sessions}</td>
                <td>{n.last_seen ? new Date(normaliseTimestamp(n.last_seen)).toLocaleString() : '—'}</td>
                <td class="row-actions">
                  {!n.is_self && (
                    <>
                      {n.key_source !== 'none' && (
                        <button type="button" class="secondary" onClick={() => void pingPeer(n.node_id)}>
                          Ping
                        </button>
                      )}
                      <button type="button" class="danger" onClick={() => setConfirming(n)}>
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
      <AddPeerForm onAdded={reload} />
    </div>
  )
}
