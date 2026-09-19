/**
 * ``socialhome://invite#<base64url(JSON)>`` — single-line, chat-safe
 * space-invite handoff.
 *
 * Mirrors the §11 pairing scheme (``socialhome://pair#…``). The JSON
 * payload sits in the URL fragment so a stray paste into a browser
 * address bar never sends the token to anyone's server logs —
 * fragments stay client-side.
 *
 * Payload fields:
 *
 *  - ``token`` — the bearer credential the receiver redeems against
 *    the issuer's instance. Mandatory.
 *  - ``space_id`` — for client-side preview / post-join navigation.
 *    Optional for back-compat with bare-token pastes.
 *  - ``space_display_hint`` — human-readable space name so the join
 *    card can render "You're about to join Pascal's family" without
 *    a round-trip to the issuer. Optional.
 *  - ``issuer_instance_id`` — the base32 instance id of the issuing
 *    Social Home. Lets the receiver decide whether they can redeem
 *    locally (same instance), over federation (CONFIRMED peer), or
 *    need to pair first. **Never** an HTTPS URL — a clickable URL
 *    would land the receiver on the issuer's instance where they
 *    can't redeem against their own account. A future GFS-mediated
 *    redirect can make a real link work; until then the code paste
 *    is the only path.
 *  - ``via_gfs`` — for GFS-published spaces, the GFS reference the
 *    receiver can use to redeem if their household is paired with
 *    that GFS. Optional; ``null`` for private peer-to-peer spaces.
 *  - ``issuer_identity_pk`` / ``issuer_keywrap_pk`` /
 *    ``issuer_keywrap_sig`` / ``issuer_proto_version`` /
 *    ``expires_at`` — the §D2b bootstrap block, present on a code
 *    minted for someone the issuing household has never federated
 *    with. Public keys only, never an address: the identity key the
 *    instance id derives from, the static X25519 key-wrap key, and
 *    the signature binding the two. The receiver seals its redeem to
 *    that key-wrap key and relays the blob by instance id through the
 *    connection server named in ``via_gfs``. All optional — an older
 *    code simply has none of them and redeems over the direct /
 *    mesh paths as before.
 *
 * Backend redeems travel through the §24.11 federation pipeline
 * (``SPACE_INVITE_TOKEN_REDEEM`` family) when issuer ≠ receiver.
 */
import { base64UrlEncode, base64UrlDecode } from './base64Url'

export interface SpaceInviteGfsRef {
  /** Base URL of the GFS the space is published on. */
  gfs_url: string
  /** Space id under that GFS (may differ from the local space id).
   *  Optional: the mint response (``POST /api/spaces/{id}/invite-tokens``
   *  → ``gfs``) carries no such id, and the redeem path only ever reads
   *  ``gfs_url`` (see ``SpaceJoinByCodeDialog``), so a code minted by
   *  the SPA omits it rather than inventing one. Older codes that carry
   *  it keep decoding unchanged. */
  gfs_space_id?: string | null
}

/**
 * Derive a connection server's **base** URL from the public invite-link
 * URL it returns (``{base}/join/{gfs_token}``).
 *
 * The mint response hands back the visitor-facing link; the invite code's
 * ``via_gfs.gfs_url`` needs the server root, because that is what the
 * redeeming household POSTs ``/gfs/envelope`` to. Returns ``null`` when
 * the URL isn't parseable or doesn't end in the documented
 * ``/join/{token}`` shape — the caller then simply omits ``via_gfs``
 * rather than shipping a guess a stranger's redeem would fail against.
 */
export function gfsBaseFromInviteUrl(url: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  const segments = parsed.pathname.split('/').filter(Boolean)
  if (segments.length < 2) return null
  if (segments[segments.length - 2] !== 'join') return null
  const base = segments.slice(0, -2).join('/')
  return `${parsed.origin}${base ? `/${base}` : ''}`
}

export interface SpaceInvitePayload {
  token: string
  space_id?: string | null
  space_display_hint?: string | null
  issuer_instance_id?: string | null
  via_gfs?: SpaceInviteGfsRef | null
  /** Issuer's Ed25519 identity public key (hex) — its instance id derives
   *  from this, so a substituted key can't keep the advertised id. */
  issuer_identity_pk?: string | null
  /** Issuer's static X25519 key-wrap public key (hex) — what the redeem
   *  request is sealed to. */
  issuer_keywrap_pk?: string | null
  /** base64url signature binding ``issuer_keywrap_pk`` to
   *  ``issuer_identity_pk``; verified before anything is sealed. */
  issuer_keywrap_sig?: string | null
  /** Issuer's advertised federation protocol version — there is no peer
   *  row to ask, so the blob carries it. */
  issuer_proto_version?: number | null
  /** When the invite stops being redeemable (tz-aware ISO-8601). */
  expires_at?: string | null
}

export function buildInviteCode(payload: SpaceInvitePayload): string {
  return `socialhome://invite#${base64UrlEncode(JSON.stringify(payload))}`
}

const URI_PREFIX = 'socialhome://invite#'

// Matches a bare invite token. The backend mints tokens via
// ``secrets.token_hex(16)`` (or similar), so this is conservative —
// 16+ hex chars without any other punctuation.
const BARE_TOKEN_RE = /^[a-f0-9]{16,64}$/i

function isPayloadValid(raw: unknown): raw is SpaceInvitePayload {
  if (!raw || typeof raw !== 'object') return false
  const obj = raw as Record<string, unknown>
  if (typeof obj.token !== 'string' || !obj.token) return false
  return true
}

/**
 * Decode a pasted invite string. Accepts, in order of preference:
 *
 *   1. ``socialhome://invite#<base64url(JSON)>`` — the canonical wire
 *      shape this module emits.
 *   2. Raw multi-field JSON — forward-compat with codes shared via
 *      other transports (chat that mangles the URI scheme).
 *   3. A bare hex token — back-compat with operators who pasted just
 *      the UUID from an older share dialog. We synthesise a minimal
 *      ``{token}`` payload so the join card can still call the API.
 *
 * Returns ``null`` for anything else (garbage, empty, wrong scheme).
 */
export function decodeInviteCode(raw: string): SpaceInvitePayload | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  if (trimmed.startsWith(URI_PREFIX)) {
    const json = base64UrlDecode(trimmed.slice(URI_PREFIX.length))
    if (!json) return null
    try {
      const parsed = JSON.parse(json) as unknown
      return isPayloadValid(parsed) ? parsed : null
    } catch {
      return null
    }
  }
  if (trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(trimmed) as unknown
      return isPayloadValid(parsed) ? parsed : null
    } catch {
      return null
    }
  }
  if (BARE_TOKEN_RE.test(trimmed)) {
    return { token: trimmed }
  }
  return null
}
