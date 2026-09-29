/**
 * @-mention helpers shared by the composer autocomplete and the renderers
 * (§23.42).
 *
 * The backend's ``MentionParser`` (``socialhome/domain/mention.py``) owns
 * the grammar: ``@base`` (word chars, ``.``, ``-``; trailing ``.``/``-`` is
 * punctuation) with an optional ``@<user_id prefix>`` qualifier, never
 * glued to a preceding word char (so ``bob@example.com`` is not a
 * mention). ``GET /api/spaces/{id}/members`` hands us each member's exact
 * token (``mention``), so the SPA never invents one — it inserts what the
 * server resolves and highlights only tokens it knows.
 */
import type { SpaceMemberProfile } from '@/types'

/** A word char in the backend's sense (Python ``\w`` ≈ letters, digits,
 *  underscore — Unicode-aware). */
const W = '\\p{L}\\p{N}\\p{M}_'

/** Partial token ending at the cursor: ``@`` + up to 64 token chars. */
const TRIGGER_RE = new RegExp(`(?:^|[^${W}@])@([${W}.\\-]{0,64})$`, 'u')

/** A complete token in rendered text; group 1 = the leading boundary. */
const TOKEN_RE = new RegExp(
  `(^|[^${W}@])@([${W}][${W}.\\-]{0,63})(?:@([${W}]{1,64}))?`,
  'gu',
)

const MAX_RESULTS = 8

export interface MentionTrigger {
  /** Index of the ``@`` in the text. */
  start: number
  /** What the user typed after the ``@`` (may be empty). */
  query: string
}

export interface MentionCandidate {
  userId: string
  /** Token to insert (without ``@``). */
  token: string
  /** Name shown in the picker. */
  name: string
  pictureUrl: string | null
  /** Household label for a member from another household, else null. */
  household: string | null
}

/** Is the cursor at the end of a ``@partial`` token? */
export function findMentionTrigger(text: string, cursor: number): MentionTrigger | null {
  const m = TRIGGER_RE.exec(text.slice(0, cursor))
  if (!m) return null
  return { start: cursor - m[1].length - 1, query: m[1] }
}

function displayName(m: SpaceMemberProfile): string {
  return (
    m.space_display_name || m.personal_alias || m.display_name || m.mention || m.user_id
  )
}

/** Members matching ``query`` (prefix on the token, the shown name, or any
 *  word of it; case-insensitive), minus the viewer and anyone without a
 *  token. Token matches rank first; capped at 8. */
export function mentionCandidates(
  members: Iterable<SpaceMemberProfile>,
  query: string,
  excludeUserId: string | null | undefined,
): MentionCandidate[] {
  const q = query.toLocaleLowerCase()
  const ranked: { rank: number, c: MentionCandidate }[] = []
  for (const m of members) {
    if (!m.mention || m.user_id === excludeUserId) continue
    const name = displayName(m)
    const lname = name.toLocaleLowerCase()
    const token = m.mention.toLocaleLowerCase()
    let rank = -1
    if (token.startsWith(q)) rank = 0
    else if (lname.startsWith(q)) rank = 1
    else if (lname.split(/\s+/u).some(w => w.startsWith(q))) rank = 2
    if (rank < 0) continue
    ranked.push({
      rank,
      c: {
        userId: m.user_id,
        token: m.mention,
        name,
        pictureUrl: m.picture_url ?? null,
        household: m.instance_id ? (m.household_name || 'Other household') : null,
      },
    })
  }
  ranked.sort((a, b) => a.rank - b.rank)
  return ranked.slice(0, MAX_RESULTS).map(r => r.c)
}

/** Lower-cased set of every member's token — what the renderers highlight. */
export function mentionTokenSet(members: Iterable<SpaceMemberProfile>): Set<string> {
  const out = new Set<string>()
  for (const m of members) if (m.mention) out.add(m.mention.toLocaleLowerCase())
  return out
}

export interface MentionPart {
  /** Lower-cased token that matched the known set. */
  token: string
  /** As written, including the ``@``. */
  raw: string
}

/** Split plain ``text`` into literal strings and known mentions. Pure — the
 *  caller decides how to render each part (JSX, or escaped HTML). */
export function splitMentions(
  text: string,
  tokens: ReadonlySet<string>,
): (string | MentionPart)[] {
  if (tokens.size === 0 || !text.includes('@')) return [text]
  const parts: (string | MentionPart)[] = []
  let last = 0
  for (const m of text.matchAll(TOKEN_RE)) {
    const rawBase = m[2]
    const base = rawBase.replace(/[.-]+$/u, '')
    const qualifier = base === rawBase ? m[3] : undefined
    const token = (qualifier ? `${base}@${qualifier}` : base)
    if (!base || !tokens.has(token.toLocaleLowerCase())) continue
    const at = (m.index ?? 0) + m[1].length
    if (at > last) parts.push(text.slice(last, at))
    const raw = `@${token}`
    parts.push({ token: token.toLocaleLowerCase(), raw })
    last = at + raw.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts.length ? parts : [text]
}
