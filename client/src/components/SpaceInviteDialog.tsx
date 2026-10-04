/**
 * SpaceInviteDialog — mint, share and manage invite links for a space.
 *
 * Three artifacts come out of a mint: a copyable
 * ``socialhome://invite#…`` code (primary — paste into chat), a QR
 * (same-room handoff), and — when the link was also published to a
 * connection server — a plain HTTPS URL anyone can open in a browser.
 * The code embeds the issuer's stable ``instance_id`` so the receiver's
 * own Social Home can route the redeem over federation when the issuer
 * isn't on the receiver's instance.
 *
 * Why the published URL is safe where a bare deep-link isn't: it points
 * at the *connection server's* public join page, which only shows the
 * space name and the same paste-code. It never tries to redeem on the
 * issuer's instance — the receiver still joins from their own Social
 * Home. A link straight to the issuer's instance can't do that (no
 * account there), which is why this dialog has never offered one.
 *
 * The dialog is also the home of the **links list**: every live link
 * for the space, with who minted it, how many uses are left, when it
 * lapses, and a Revoke. Minting and auditing are the same job, so they
 * live behind the same door rather than in a separate settings tab.
 */
import type { SpaceRole } from '@/features/spaces/spaceRoles'
import { signal } from '@preact/signals'
import { useEffect } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { instanceConfig } from '@/store/instance'
import { buildInviteCode, gfsBaseFromInviteUrl } from '@/lib/spaceInviteCode'
import { relativeFutureTime } from '@/utils/relativeTime'
import type { GfsConnection } from '@/types'
import { Modal } from './Modal'
import { Button } from './Button'
import { QrCodeImg } from './QrCodeImg'
import { showToast } from './Toast'
import { confirmDialog } from './confirm'
import { t } from '@/i18n/i18n'
import { addBase } from '@/baseUrl'

/** Roles an invite link can seat someone as. ``owner`` is deliberately
 *  absent — ownership transfers are a separate, deliberate gesture, and
 *  the backend answers 422 for it. */
export type InviteRole = 'member' | 'subscriber' | 'admin'

/** Viewer's own role in the space — decides which roles they may hand
 *  out. Only the owner can mint an admin link; a moderator mints none
 *  (invites are settings authority). Nobody mints a moderator link. */
type ViewerRole = SpaceRole | undefined

interface InviteGfsRef {
  /** Local connection-server id the link was published through. */
  gfs_id: string
  /** The server's opaque handle for the parked invite blob. */
  gfs_token: string
  /** Public, visitor-facing URL: ``{base}/join/{gfs_token}``. */
  url: string
}

/** How a link is redeemed. ``gfs`` — through the connection-server relay,
 *  so a household that never met ours can join; ``internal`` — only
 *  households already paired or reachable over the mesh. */
export type InviteVia = 'gfs' | 'internal'

interface InviteTokenRow {
  token: string
  role: InviteRole
  /** Seats the link was minted with — the denominator of "2 of 5 left".
   *  Absent on a pre-0053 row; the list then shows the bare remainder. */
  uses?: number | null
  uses_remaining: number
  expires_at: string | null
  created_by: string
  created_at: string
  /** Server-built ``socialhome://invite#…``. Preferred over a locally
   *  built one: only the backend holds the §D2b bootstrap block (issuer
   *  key-wrap key + signature) a stranger's redeem needs. */
  code?: string | null
  gfs?: InviteGfsRef | null
  /** The link's type. Absent on an older backend (every link was ``gfs``). */
  via?: InviteVia
}

const EXPIRY_CHOICES = [
  { id: '1d', label: '1 day', ttl: 86_400 },
  { id: '7d', label: '7 days', ttl: 604_800 },
  { id: '30d', label: '30 days', ttl: 2_592_000 },
  // ``0`` is "never expires" — the route maps it onto the service's
  // ``None`` precisely because this picker sends it. (An explicit
  // ``null`` means the same thing; OMITTING the field is what takes the
  // server's 7-day default, which is the opposite of what the user
  // picked here.)
  { id: 'never', label: 'Never', ttl: 0 },
] as const

type ExpiryId = typeof EXPIRY_CHOICES[number]['id']

const MAX_USES = 100

const ROLE_CHOICES: { id: InviteRole; label: string; hint: string }[] = [
  { id: 'member', label: 'Member', hint: 'can post and take part' },
  { id: 'subscriber', label: 'Follower', hint: 'reads only' },
  { id: 'admin', label: 'Admin', hint: 'manages members and settings' },
]


/** The connection server drops a parked invite blob after this long, so a
 *  published `/join/...` web URL stops resolving even when the local link
 *  itself never lapses. */
const PUBLISHED_LINK_MAX_DAYS = 30

const open = signal(false)
const spaceId = signal('')
const displayHint = signal<string | null>(null)
const viewerRole = signal<ViewerRole>(undefined)
const role = signal<InviteRole>('member')
const uses = signal(1)
const expiry = signal<ExpiryId>('7d')
const publish = signal(false)
const publishTo = signal<string | null>(null)
/** Per-connection reason the server refused to publish (422 detail),
 *  keyed by connection id — rendered on the option so the picker
 *  explains itself instead of repeating a toast. */
const publishBlocked = signal<Record<string, string>>({})
const servers = signal<GfsConnection[]>([])
/** The space's tier and whether a private one uses the connection server
 *  (``features.private_gfs``). ``null`` until the detail read lands. */
const spaceType = signal<string | null>(null)
const privateGfs = signal(false)
/** Link type picked for a PRIVATE space; ``null`` everywhere else (public
 *  and global links are always connection-server links, and the POST then
 *  leaves ``via`` to the server). */
const via = signal<InviteVia | null>(null)
const created = signal<InviteTokenRow | null>(null)
const loading = signal(false)

const links = signal<InviteTokenRow[]>([])
const linksLoading = signal(false)
const linksError = signal(false)
/** ``user_id`` → display name for the space's members, so a row can say
 *  "by Maximiliana" instead of the 32-character opaque id the API
 *  stores. A minter who has since left the space is not in the map and
 *  keeps the id — wrong-but-honest beats inventing a name. */
const memberNames = signal<Record<string, string>>({})

/**
 * Open the invite dialog for ``sid``. ``hint`` is the space's display
 * name — pass it when the caller already has it (avoids an extra
 * fetch). ``actorRole`` is the opener's own role in the space; it gates
 * the Admin option. Omitted (or anything but ``owner``), the dialog
 * plays safe and offers Member / Follower only.
 */
export function openSpaceInvite(
  sid: string,
  hint: string | null = null,
  actorRole: ViewerRole = undefined,
) {
  spaceId.value = sid
  displayHint.value = hint
  viewerRole.value = actorRole
  created.value = null
  role.value = 'member'
  uses.value = 1
  expiry.value = '7d'
  publish.value = false
  publishTo.value = null
  publishBlocked.value = {}
  links.value = []
  linksError.value = false
  memberNames.value = {}
  spaceType.value = null
  privateGfs.value = false
  via.value = null
  open.value = true
}

/** Record the space detail the dialog reads on open. A private space gets
 *  the server's default link type: internal while it doesn't use the
 *  connection server, a connection-server link once it does. */
function applySpaceDetail(data: {
  space_type?: string
  features?: { private_gfs?: boolean } | null
}) {
  spaceType.value = data.space_type ?? null
  privateGfs.value = Boolean(data.features?.private_gfs)
  via.value = spaceType.value === 'private'
    ? (privateGfs.value ? 'gfs' : 'internal')
    : null
}

/** Pick a link type. An internal link is never published to a connection
 *  server (the server answers 422), so that toggle resets with it. */
function chooseVia(next: InviteVia) {
  via.value = next
  if (next === 'internal') publish.value = false
}

function ttlFor(id: ExpiryId): number {
  return EXPIRY_CHOICES.find(c => c.id === id)!.ttl
}

/** The code to show for ``row``. Prefers the backend's own — it is the
 *  only side that can add the bootstrap block — and falls back to one
 *  built here for an older backend that returns the token alone. */
function codeFor(row: InviteTokenRow): string {
  if (row.code) return row.code
  const gfsUrl = row.gfs ? gfsBaseFromInviteUrl(row.gfs.url) : null
  return buildInviteCode({
    token: row.token,
    space_id: spaceId.value || null,
    space_display_hint: displayHint.value,
    // Stable instance id so the receiver can decide whether they can
    // redeem locally (same instance), over federation (CONFIRMED peer),
    // or need to pair first.
    issuer_instance_id: instanceConfig.value?.instance_id ?? null,
    // Published link → name the connection server that carries the
    // blob, so a household that has never met ours can relay its
    // redeem through it (§D2b).
    via_gfs: gfsUrl ? { gfs_url: gfsUrl } : null,
    expires_at: row.expires_at,
  })
}

async function copy(text: string, label: string) {
  try {
    await navigator.clipboard.writeText(text)
    showToast(`${label} copied!`, 'success')
  } catch {
    showToast(
      `Could not copy — select the ${label.toLowerCase()} to copy manually.`,
      'error',
    )
  }
}

async function loadLinks() {
  const sid = spaceId.value
  if (!sid) return
  linksLoading.value = true
  linksError.value = false
  try {
    const r = await api.get(`/api/spaces/${sid}/invite-tokens`) as {
      tokens?: InviteTokenRow[]
    }
    if (spaceId.value !== sid) return
    links.value = r.tokens ?? []
  } catch {
    if (spaceId.value !== sid) return
    linksError.value = true
  } finally {
    linksLoading.value = false
  }
}

/** "3 of 5 uses left" when the mint size is known, "3 uses left"
 *  otherwise. The remaining count alone hid how generous a link was. */
function usesLabel(row: InviteTokenRow): string {
  const left = row.uses_remaining
  const total = row.uses ?? null
  const noun = left === 1 ? 'use' : 'uses'
  if (total && total !== left) return `${left} of ${total} ${noun} left`
  return `${left} ${noun} left`
}

/** Display name for the link's minter, falling back to the raw id. */
function creatorName(userId: string): string {
  return memberNames.value[userId] ?? userId
}

async function loadMemberNames() {
  const sid = spaceId.value
  if (!sid) return
  try {
    const rows = await api.get(`/api/spaces/${sid}/members`) as {
      user_id: string
      display_name?: string | null
      space_display_name?: string | null
    }[]
    if (spaceId.value !== sid) return
    const map: Record<string, string> = {}
    for (const m of rows ?? []) {
      const name = m.space_display_name || m.display_name
      if (name) map[m.user_id] = name
    }
    memberNames.value = map
  } catch {
    // Non-fatal: rows fall back to the raw id.
  }
}

async function loadServers() {
  try {
    const r = await api.get('/api/gfs/connections') as GfsConnection[]
    servers.value = (r ?? []).filter(g => g.status === 'active')
  } catch {
    // Non-fatal: no picker, minting still works. A household with no
    // reachable connection server simply doesn't see the toggle.
    servers.value = []
  }
}

export function SpaceInviteDialog() {
  // Everything the dialog needs on open: the space name (only when the
  // caller didn't hand one over — it goes into the code as the preview
  // hint), the live links, and the connection servers that can host a
  // published link.
  useEffect(() => {
    if (!open.value || !spaceId.value) return
    let cancelled = false
    // Always read the detail: besides the name hint, a private space's
    // link-type choice depends on whether it uses the connection server.
    api.get(`/api/spaces/${spaceId.value}`).then((data) => {
      if (cancelled) return
      const d = data as Parameters<typeof applySpaceDetail>[0] & { name?: string }
      if (d.name && !displayHint.value) displayHint.value = d.name
      applySpaceDetail(d)
    }).catch(() => {
      // Non-fatal: the hint is optional, and without the tier the dialog
      // offers today's link and lets the server pick the type.
    })
    void loadLinks()
    void loadServers()
    void loadMemberNames()
    return () => { cancelled = true }
  }, [open.value, spaceId.value])

  const row = created.value
  const code = row ? codeFor(row) : ''
  const roleChoices = ROLE_CHOICES.filter(
    c => c.id !== 'admin' || viewerRole.value === 'owner',
  )
  const blockedReason = publishTo.value
    ? publishBlocked.value[publishTo.value]
    : undefined

  const isPrivate = spaceType.value === 'private'
  // Publishing parks the link on a connection server: never for an
  // internal link.
  const canPublish = servers.value.length > 0 && via.value !== 'internal'

  const createToken = async () => {
    loading.value = true
    const publishedTo = canPublish && publish.value ? publishTo.value : null
    try {
      const body: {
        role: InviteRole
        uses: number
        ttl_seconds: number
        publish_to_gfs?: string
        via?: InviteVia
      } = {
        role: role.value,
        uses: uses.value,
        ttl_seconds: ttlFor(expiry.value),
      }
      if (via.value) body.via = via.value
      if (publishedTo) body.publish_to_gfs = publishedTo
      const result = await api.post(
        `/api/spaces/${spaceId.value}/invite-tokens`,
        body,
      ) as InviteTokenRow
      created.value = result
      // Newest first — matches the order the list endpoint returns.
      links.value = [result, ...links.value]
    } catch (e: unknown) {
      if (e instanceof ApiError && e.code === 'PRIVATE_GFS_OFF') {
        // The owner turned the connection server off since we read the
        // space: show it as it is now, so the next try is an internal link.
        privateGfs.value = false
        chooseVia('internal')
      } else if (e instanceof ApiError && e.status === 422 && publishedTo) {
        // The server named a reason (unknown connection server, or one
        // that can't serve invite links yet). Pin it to that option so
        // the picker carries the explanation.
        publishBlocked.value = {
          ...publishBlocked.value,
          [publishedTo]: e.detail
            || "This connection server can't host invite links yet.",
        }
      }
      showToast(
        (e as Error)?.message ?? 'Failed to create invite',
        'error',
      )
    } finally {
      loading.value = false
    }
  }

  const revoke = async (target: InviteTokenRow) => {
    const ok = await confirmDialog(
      'This link stops working immediately, here and on the connection '
      + 'server. People who already joined with it stay.',
      {
        title: 'Revoke this invite link?',
        confirmLabel: 'Revoke link',
        destructive: true,
      },
    )
    if (!ok) return
    const before = links.value
    links.value = links.value.filter(l => l.token !== target.token)
    if (created.value?.token === target.token) created.value = null
    try {
      await api.delete(
        `/api/spaces/${spaceId.value}/invite-tokens/${target.token}`,
      )
      showToast('Link revoked.', 'success')
    } catch (e: unknown) {
      // Put it back — a revoke that didn't land must not look like one
      // that did, or the owner walks away believing a live link is dead.
      links.value = before
      showToast(
        (e as Error)?.message ?? 'Could not revoke that link.',
        'error',
      )
    }
  }

  return (
    <Modal open={open.value} onClose={() => open.value = false}
           title="Invite to space">
      <div class="sh-invite-dialog">
        {!row ? (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Make a link or code to share. Whoever gets it joins from
              their own Social Home.
            </p>

            <fieldset class="sh-invite-fieldset">
              <legend>They join as</legend>
              <div class="sh-invite-roles">
                {roleChoices.map(c => (
                  <label
                    key={c.id}
                    class={`sh-invite-role ${role.value === c.id ? 'sh-invite-role--active' : ''}`}
                  >
                    <input
                      type="radio"
                      name="sh-invite-role"
                      value={c.id}
                      checked={role.value === c.id}
                      onChange={() => { role.value = c.id }}
                      data-testid={`invite-role-${c.id}`}
                    />
                    <span class="sh-invite-role__label">{c.label}</span>
                    <span class="sh-invite-role__hint">{c.hint}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            {isPrivate && (
              <fieldset class="sh-invite-fieldset" data-testid="invite-via">
                <legend>{t('invite.via.legend')}</legend>
                <div class="sh-invite-roles">
                  <label
                    class={[
                      'sh-invite-role',
                      via.value === 'gfs' ? 'sh-invite-role--active' : '',
                      privateGfs.value ? '' : 'sh-invite-role--disabled',
                    ].filter(Boolean).join(' ')}
                  >
                    <input
                      type="radio"
                      name="sh-invite-via"
                      value="gfs"
                      checked={via.value === 'gfs'}
                      disabled={!privateGfs.value}
                      aria-describedby={privateGfs.value ? undefined : 'sh-invite-via-gfs-off'}
                      onChange={() => chooseVia('gfs')}
                      data-testid="invite-via-gfs"
                    />
                    <span class="sh-invite-role__label">{t('invite.via.gfs')}</span>
                    <span class="sh-invite-role__hint">{t('invite.via.gfs_hint')}</span>
                  </label>
                  <label
                    class={`sh-invite-role ${via.value === 'internal' ? 'sh-invite-role--active' : ''}`}
                  >
                    <input
                      type="radio"
                      name="sh-invite-via"
                      value="internal"
                      checked={via.value === 'internal'}
                      onChange={() => chooseVia('internal')}
                      data-testid="invite-via-internal"
                    />
                    <span class="sh-invite-role__label">{t('invite.via.internal')}</span>
                    <span class="sh-invite-role__hint">{t('invite.via.internal_hint')}</span>
                  </label>
                </div>
                {!privateGfs.value && (
                  <p class="sh-muted" id="sh-invite-via-gfs-off"
                     style={{ margin: 'var(--sh-space-xs) 0 0', fontSize: 'var(--sh-font-size-xs)' }}
                     data-testid="invite-via-gfs-off">
                    {viewerRole.value === 'owner' ? (
                      <>
                        {t('invite.via.gfs_off_owner')}{' '}
                        <a href={addBase(`/spaces/${spaceId.value}/settings`)}
                           onClick={() => { open.value = false }}>
                          {t('invite.via.open_settings')}
                        </a>
                      </>
                    ) : t('invite.via.gfs_off')}
                  </p>
                )}
              </fieldset>
            )}

            <label class="sh-form-field">
              <span>How many people can use this link?</span>
              <input
                type="number"
                min={1}
                max={MAX_USES}
                inputMode="numeric"
                value={uses.value}
                data-testid="invite-uses"
                onInput={(e) => {
                  const n = parseInt((e.target as HTMLInputElement).value, 10)
                  uses.value = Number.isNaN(n)
                    ? 1
                    : Math.min(MAX_USES, Math.max(1, n))
                }}
              />
            </label>

            <label class="sh-form-field">
              <span>Stops working after</span>
              <select
                value={expiry.value}
                data-testid="invite-expiry"
                onChange={(e) => {
                  expiry.value = (e.target as HTMLSelectElement).value as ExpiryId
                }}
              >
                {EXPIRY_CHOICES.map(c => (
                  <option key={c.id} value={c.id}>{c.label}</option>
                ))}
              </select>
            </label>
            {expiry.value === 'never' && (
              <p class="sh-muted" style={{ marginTop: 0, fontSize: 'var(--sh-font-size-xs)' }}
                 data-testid="invite-never-hint">
                A link that never lapses keeps working until its uses run
                out or you revoke it — anyone it was ever forwarded to can
                still join.
              </p>
            )}

            {canPublish && (
              <div class="sh-invite-publish">
                <label class="sh-invite-publish__toggle">
                  <input
                    type="checkbox"
                    checked={publish.value}
                    data-testid="invite-publish-toggle"
                    onChange={(e) => {
                      publish.value = (e.target as HTMLInputElement).checked
                      if (publish.value && !publishTo.value) {
                        publishTo.value = servers.value[0].id
                      }
                    }}
                  />
                  <span>
                    Also publish to{' '}
                    {servers.value.length === 1
                      ? servers.value[0].display_name
                      : 'a connection server'}
                  </span>
                </label>
                {publish.value && servers.value.length > 1 && (
                  <select
                    class="sh-invite-publish__picker"
                    value={publishTo.value ?? ''}
                    data-testid="invite-publish-picker"
                    onChange={(e) => {
                      publishTo.value = (e.target as HTMLSelectElement).value
                    }}
                  >
                    {servers.value.map(g => (
                      <option
                        key={g.id}
                        value={g.id}
                        disabled={!!publishBlocked.value[g.id]}
                      >
                        {g.display_name}
                        {publishBlocked.value[g.id] ? ' — unavailable' : ''}
                      </option>
                    ))}
                  </select>
                )}
                {publish.value && blockedReason && (
                  <p class="sh-error" role="alert"
                     data-testid="invite-publish-blocked">
                    {blockedReason}
                  </p>
                )}
                {publish.value && !blockedReason && (
                  <p class="sh-muted"
                     style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                    Publishing gets you a plain web link you can send to
                    someone who has never heard of Social Home. The server
                    only ever shows the space name and the code.
                  </p>
                )}
              </div>
            )}

            <div class="sh-form-actions sh-invite-dialog__submit"
                 data-testid="invite-submit-row">
              <Button onClick={createToken} loading={loading.value}>
                Create invite link
              </Button>
            </div>
          </>
        ) : (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              Good for {row.uses_remaining}{' '}
              {row.uses_remaining === 1 ? 'use' : 'uses'}
              {row.expires_at
                ? `, lapses ${relativeFutureTime(row.expires_at)}`
                : ', never lapses'}
              . They join as{' '}
              {ROLE_CHOICES.find(c => c.id === row.role)?.label.toLowerCase()
                ?? row.role}.
            </p>

            {row.via === 'internal' && (
              <p class="sh-muted"
                 style={{ marginTop: 0, fontSize: 'var(--sh-font-size-xs)' }}
                 data-testid="invite-created-internal">
                {t('invite.via.created_internal')}
              </p>
            )}

            {row.gfs && !row.expires_at && (
              <p class="sh-muted"
                 style={{ marginTop: 0, fontSize: 'var(--sh-font-size-xs)' }}
                 data-testid="invite-published-never-hint">
                The web link is the exception: the connection server only
                holds a published invite for {PUBLISHED_LINK_MAX_DAYS} days,
                after which that URL stops opening. The code below keeps
                working until its uses run out or you revoke it.
              </p>
            )}

            <div class="sh-invite-artifact sh-invite-artifact--primary">
              <div class="sh-invite-artifact-label">
                Invite code · paste into chat
              </div>
              <code class="sh-invite-link" data-testid="invite-code">{code}</code>
              <div class="sh-form-actions">
                <Button onClick={() => copy(code, 'Code')}>
                  Copy code
                </Button>
              </div>
            </div>

            {row.gfs && (
              <div class="sh-invite-artifact">
                <div class="sh-invite-artifact-label">
                  🌐 Web link · published
                </div>
                <code class="sh-invite-link" data-testid="invite-link-url">
                  {row.gfs.url}
                </code>
                <p class="sh-muted"
                   style={{ margin: 0, fontSize: 'var(--sh-font-size-xs)' }}>
                  Anyone with this link sees the space name and can request
                  the code — they join from their own Social Home.
                </p>
                <div class="sh-form-actions">
                  <Button onClick={() => copy(row.gfs!.url, 'Link')}>
                    Copy link
                  </Button>
                </div>
              </div>
            )}

            <div class="sh-invite-artifact sh-invite-artifact--qr">
              <div class="sh-invite-artifact-label">
                QR · scan with another device
              </div>
              <QrCodeImg data={code} size={180} alt="Invite QR code" />
            </div>

            <div class="sh-form-actions">
              <Button variant="secondary" onClick={() => { created.value = null }}>
                Make another
              </Button>
            </div>
          </>
        )}

        <section class="sh-invite-links" data-testid="invite-links">
          <h3 class="sh-invite-links__title">Active links</h3>
          {linksLoading.value && links.value.length === 0 ? (
            <p class="sh-muted">Loading links…</p>
          ) : linksError.value ? (
            <div class="sh-invite-links__error" data-testid="invite-links-error">
              <p class="sh-muted" style={{ margin: 0 }}>
                Couldn't load the links for this space.
              </p>
              <Button variant="secondary" onClick={() => void loadLinks()}>
                Try again
              </Button>
            </div>
          ) : links.value.length === 0 ? (
            <p class="sh-muted" data-testid="invite-links-empty">
              No active links.
            </p>
          ) : (
            links.value.map(l => (
              <div key={l.token} class="sh-invite-link-row-item"
                   data-testid={`invite-link-row-${l.token}`}>
                <div class="sh-invite-link-row-item__main">
                  <span class={l.role === 'admin' ? 'sh-chip sh-chip--honey' : 'sh-chip'}>
                    {ROLE_CHOICES.find(c => c.id === l.role)?.label ?? l.role}
                  </span>
                  {l.via && (
                    <span class="sh-chip sh-invite-via-badge"
                          data-testid={`invite-via-badge-${l.token}`}>
                      {l.via === 'internal'
                        ? t('invite.via.badge_internal')
                        : t('invite.via.badge_gfs')}
                    </span>
                  )}
                  {l.gfs && (
                    <span class="sh-invite-link-row-item__web"
                          title="Published as a web link">
                      🌐
                    </span>
                  )}
                  <span class="sh-muted">
                    {usesLabel(l)}
                    {' · '}
                    {l.expires_at
                      ? `lapses ${relativeFutureTime(l.expires_at)}`
                      : 'never lapses'}
                    {' · '}
                    by {creatorName(l.created_by)}
                  </span>
                </div>
                <div class="sh-invite-link-row-item__actions">
                  {l.gfs && (
                    <Button variant="secondary"
                            onClick={() => copy(l.gfs!.url, 'Link')}>
                      Copy link
                    </Button>
                  )}
                  <Button variant="danger"
                          data-testid={`invite-revoke-${l.token}`}
                          onClick={() => void revoke(l)}>
                    Revoke
                  </Button>
                </div>
              </div>
            ))
          )}
        </section>
      </div>
    </Modal>
  )
}
