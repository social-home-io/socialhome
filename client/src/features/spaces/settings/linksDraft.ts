/**
 * Quick links as part of the Basics draft (owner decision 8: Basics is
 * one form with one save bar). Adding, editing, removing and reordering
 * links only changes the draft; Save applies it, Discard drops it.
 *
 * The API has no batch endpoint, so Save is a sequence of single calls —
 * deletes, then edits / moves, then new links — each one on its own.
 * One failing call never undoes the others: afterwards the draft is
 * compared with what the server now holds, so a link whose call failed
 * simply stays unsaved (the save bar keeps counting it) and the others
 * are saved. Each call is idempotent against the state it was computed
 * from, so pressing Save again retries exactly what is left.
 */

/** A stored link, as ``GET /api/spaces/{id}/links`` lists it (in order). */
export interface StoredLink {
  id: string
  label: string
  url: string
  position: number
}

/** A link in the draft. ``id`` is set once the server holds it; ``key``
 *  is stable for the row's lifetime (rendering, failure marks). */
export interface DraftLink {
  key: string
  id: string | null
  label: string
  url: string
}

let _seq = 0
/** A fresh row key for a link added in this draft. */
export function newDraftKey(): string {
  _seq += 1
  return `new-${_seq}`
}

/** The draft that equals the stored links. */
export function draftOf(stored: readonly StoredLink[]): DraftLink[] {
  return stored.map(l => ({ key: l.id, id: l.id, label: l.label, url: l.url }))
}

/** A link the server would accept (``validate_space_link_url``): a name,
 *  and an absolute http(s) address with a host, no spaces or control
 *  characters and no user name / password. */
export function validLink(label: string, url: string): 'ok' | 'required' | 'url' {
  const u = url.trim()
  if (!label.trim() || !u) return 'required'
  // eslint-disable-next-line no-control-regex
  if (/[\s\u0000-\u001f\u007f]/.test(u)) return 'url'
  try {
    const p = new URL(u)
    const web = p.protocol === 'http:' || p.protocol === 'https:'
    return web && p.hostname && !p.username && !p.password ? 'ok' : 'url'
  } catch {
    return 'url'
  }
}

/** Rows Save can't send (no name, or not an http(s) address). */
export function invalidLinks(draft: readonly DraftLink[]): DraftLink[] {
  return draft.filter(l => validLink(l.label, l.url) !== 'ok')
}

export interface LinksDiff {
  deletes: StoredLink[]
  /** Edited (label / url) and / or moved links: the PATCH body. */
  patches: Array<{ link: DraftLink; body: Partial<Pick<StoredLink, 'label' | 'url' | 'position'>> }>
  creates: Array<{ link: DraftLink; position: number }>
  /** What the save bar counts: each added, removed or edited link, plus
   *  one for a new order of the links that stay. */
  count: number
}

export function diffLinks(stored: readonly StoredLink[], draft: readonly DraftLink[]): LinksDiff {
  const byId = new Map(stored.map(l => [l.id, l]))
  const kept = new Set(draft.flatMap(l => (l.id && byId.has(l.id) ? [l.id] : [])))
  const deletes = stored.filter(l => !kept.has(l.id))
  // Order changed = the links that stay are in another relative order.
  const storedOrder = stored.filter(l => kept.has(l.id)).map(l => l.id)
  const draftOrder = draft.flatMap(l => (l.id && kept.has(l.id) ? [l.id] : []))
  const moved = storedOrder.join('\n') !== draftOrder.join('\n')
  const added = draft.some(l => !(l.id && kept.has(l.id)))
  // Positions are rewritten (to the draft index) only when the list
  // itself changes shape — never as a change of its own.
  const renumber = moved || added || deletes.length > 0
  const patches: LinksDiff['patches'] = []
  const creates: LinksDiff['creates'] = []
  let edits = 0
  draft.forEach((link, position) => {
    const was = link.id ? byId.get(link.id) : undefined
    if (!was) {
      creates.push({ link, position })
      return
    }
    const body: LinksDiff['patches'][number]['body'] = {}
    if (link.label.trim() !== was.label) body.label = link.label.trim()
    if (link.url.trim() !== was.url) body.url = link.url.trim()
    if (body.label !== undefined || body.url !== undefined) edits += 1
    if (renumber && was.position !== position) body.position = position
    if (Object.keys(body).length) patches.push({ link, body })
  })
  return {
    deletes,
    patches,
    creates,
    count: deletes.length + creates.length + edits + (moved ? 1 : 0),
  }
}

/** The four calls Save makes — ``api`` in the app, a stub in tests. */
export interface LinksApi {
  delete: (path: string) => Promise<unknown>
  patch: (path: string, body: unknown) => Promise<unknown>
  post: (path: string, body: unknown) => Promise<unknown>
  get: (path: string) => Promise<unknown>
}

export interface LinksSaveResult {
  /** What the server holds afterwards (re-read; the old list when even
   *  that read failed). */
  stored: StoredLink[]
  /** The draft with the ids of the links that were created. */
  draft: DraftLink[]
  /** Rows whose call failed — their change is still in the draft. A
   *  failed delete names the stored link. */
  failed: Array<{ key: string; label: string }>
}

export async function saveLinks(
  api: LinksApi,
  spaceId: string,
  stored: readonly StoredLink[],
  draft: readonly DraftLink[],
): Promise<LinksSaveResult> {
  const base = `/api/spaces/${spaceId}/links`
  const { deletes, patches, creates } = diffLinks(stored, draft)
  const failed: LinksSaveResult['failed'] = []
  const next = draft.map(l => ({ ...l }))
  // What the server holds after each successful call — used only if the
  // final re-read fails, so a retry never repeats a call that worked.
  let local: StoredLink[] = stored.map(l => ({ ...l }))
  // One call at a time: concurrent position swaps raced before.
  for (const link of deletes) {
    try {
      await api.delete(`${base}/${link.id}`)
      local = local.filter(l => l.id !== link.id)
    } catch {
      failed.push({ key: link.id, label: link.label })
    }
  }
  for (const { link, body } of patches) {
    try {
      await api.patch(`${base}/${link.id}`, body)
      local = local.map(l => (l.id === link.id ? { ...l, ...body } : l))
    } catch {
      failed.push({ key: link.key, label: link.label.trim() || link.url })
    }
  }
  for (const { link, position } of creates) {
    try {
      const made = await api.post(base, { label: link.label.trim(), url: link.url.trim(), position }) as StoredLink
      const row = next.find(l => l.key === link.key)
      if (row && made?.id) {
        row.id = made.id
        local.push({ id: made.id, label: link.label.trim(), url: link.url.trim(), position })
      }
    } catch {
      failed.push({ key: link.key, label: link.label.trim() || link.url })
    }
  }
  let fresh = [...local].sort((a, b) => a.position - b.position)
  try {
    fresh = ((await api.get(base)) as { links?: StoredLink[] }).links ?? []
  } catch { /* the local picture stands; Save retries only what failed */ }
  return { stored: fresh, draft: next, failed }
}
