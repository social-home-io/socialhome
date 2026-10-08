/**
 * SpaceSettings — space admin settings panel (§23.91).
 * Includes a Federation section for GFS publish/unpublish.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { clearLocalDissolve, markLocalDissolve } from '@/store/spaces'
import { Button } from './Button'
import { ProtectedNotice, isRestricted } from './ProtectedNotice'
import { ConfirmDialog } from './ConfirmDialog'
import { EmojiField } from './EmojiField'
import { RadioCardGroup } from './RadioCardGroup'
import { ChipRadioGroup } from './ChipRadioGroup'
import { joinOptionsForVisibility } from './spaceModeOptions'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'
import type {
  Space, GfsConnection, GfsPublishMode, GfsSpacePublication, SpaceAccessLevel,
} from '@/types'
import {
  ACCESS_FEATURES,
  accessLevel,
  featureLabel,
  levelLabel,
  levelOptions,
  peersTooOldHouseholds,
  type AccessFeature,
  type BehindHousehold,
} from '@/features/spaces/spaceAccess'

// Post types an admin can enable/disable for the space feed (§23.49) —
// the same set the space composer offers, in composer order, so each
// toggle maps 1:1 to a button members see. Types the backend tracks but
// that aren't composed via the picker (transcript / event /
// highlight_share) are intentionally absent here and preserved untouched
// on save (see ``save``), so toggling these never disables them.
// Each entry: [type, icon, label key].
// For ``i18n:check``: t('space.post_type.text') t('space.post_type.image')
// t('space.post_type.video') t('space.post_type.file') t('space.post_type.poll')
// t('space.post_type.schedule') t('space.post_type.location')
// t('space.post_type.highlight_share')
const SPACE_POST_TYPES: [string, string, string][] = [
  ['text', '🔤', 'space.post_type.text'],
  ['image', '📷', 'space.post_type.image'],
  ['video', '🎬', 'space.post_type.video'],
  ['file', '📄', 'space.post_type.file'],
  ['poll', '📊', 'space.post_type.poll'],
  ['schedule', '📅', 'space.post_type.schedule'],
  ['location', '📍', 'space.post_type.location'],
  ['highlight_share', '⭕', 'space.post_type.highlight_share'],
]
const SPACE_POST_TYPE_KEYS = SPACE_POST_TYPES.map(([k]) => k)

// Post types an admin may keep past the retention horizon. The sweep
// (``space_retention_scheduler``) soft-deletes old ``space_posts`` whose
// ``type`` is NOT in ``retention_exempt_types``, so each key is a real
// ``PostType`` value. Deliberately absent: ``text`` / ``transcript`` (that
// is what retention is for — exempting them would switch it off) and
// ``highlight_share`` (the highlight expires on its own schedule, so a kept
// card would only show "Highlight has ended"). Any of those an API client
// set is preserved untouched on save.
const RETENTION_EXEMPT_TYPES: [string, string][] = [
  ['image', 'space.retention_keep_image'],
  ['video', 'space.retention_keep_video'],
  ['file', 'space.retention_keep_file'],
  ['poll', 'space.retention_keep_poll'],
  ['schedule', 'space.retention_keep_schedule'],
  ['event', 'space.retention_keep_event'],
  ['bazaar', 'space.retention_keep_bazaar'],
  ['location', 'space.retention_keep_location'],
]

const showDissolve = signal(false)
const gfsServers = signal<GfsConnection[]>([])
const publications = signal<GfsSpacePublication[]>([])
const federationLoading = signal(false)
// GFS connection ids whose publish/unpublish request is in flight, so the
// per-row Button can show a spinner and be disabled — blocks a double-click
// from firing duplicate publish/unpublish requests.
const pendingPublish = signal<Set<string>>(new Set())
// GFS connection id awaiting publish confirmation (publishing makes a space
// world-discoverable, so it's gated behind a confirm like dissolve). ``null``
// = no dialog open.
const confirmPublishGfs = signal<string | null>(null)

/** A household named by a 409 ``PRIVATE_GFS_LINK_MEMBERS``. */
interface LinkMemberHousehold {
  instance_id: string
  display_name: string
  members: { user_id: string; display_name: string }[]
}

/** The households a 409 ``PRIVATE_GFS_LINK_MEMBERS`` names, or ``null``
 *  for any other error. Defensive about the shape: a nameless row falls
 *  back to its id rather than rendering blank. */
function privateGfsLinkMembers(err: unknown): LinkMemberHousehold[] | null {
  const e = err as { code?: unknown; extra?: { households?: unknown } } | null
  if (!e || e.code !== 'PRIVATE_GFS_LINK_MEMBERS') return null
  const raw = Array.isArray(e.extra?.households) ? e.extra.households : []
  return raw.flatMap((h): LinkMemberHousehold[] => {
    if (!h || typeof h !== 'object') return []
    const r = h as Record<string, unknown>
    const id = typeof r.instance_id === 'string' ? r.instance_id : ''
    if (!id) return []
    const members = (Array.isArray(r.members) ? r.members : []).flatMap(
      (m): LinkMemberHousehold['members'] => {
        if (!m || typeof m !== 'object') return []
        const mm = m as Record<string, unknown>
        const uid = typeof mm.user_id === 'string' ? mm.user_id : ''
        if (!uid) return []
        const name = typeof mm.display_name === 'string' && mm.display_name
          ? mm.display_name : uid
        return [{ user_id: uid, display_name: name }]
      },
    )
    return [{
      instance_id: id,
      display_name: typeof r.display_name === 'string' && r.display_name
        ? r.display_name : id,
      members,
    }]
  })
}

async function loadFederationData(spaceId: string) {
  federationLoading.value = true
  try {
    const [servers, pubs] = await Promise.all([
      api.get<GfsConnection[]>('/api/gfs/connections'),
      api.get<GfsSpacePublication[]>(`/api/spaces/${spaceId}/publications`),
    ])
    gfsServers.value = servers
    publications.value = pubs
  } catch {
    gfsServers.value = []
    publications.value = []
  }
  federationLoading.value = false
}

function isPublished(gfsId: string): boolean {
  return publications.value.some(p => p.gfs_connection_id === gfsId)
}

/** The publication row for this GFS, if any — exposes ``.status`` so the
 *  UI can distinguish live (``active``) from held (``pending``) and
 *  rejected (``banned``) publications, not just the boolean. */
function publicationFor(gfsId: string): GfsSpacePublication | undefined {
  return publications.value.find(p => p.gfs_connection_id === gfsId)
}

/**
 * Build the GFS-side public URL for this space.
 *
 * The GFS exposes a server-rendered ``/spaces/{space_id}`` page (see
 * ``socialhome/global_server/public.py``). ``inbox_url`` is stored on
 * the connection as the GFS root (``https://gfs.example.com``) — the
 * routes ``/gfs/...`` are concatenated onto it for federation calls,
 * so stripping a trailing slash is enough to land on the public root
 * for browser links.
 */
function publicSpaceUrl(inboxUrl: string, spaceId: string): string {
  return `${inboxUrl.replace(/\/+$/, '')}/spaces/${spaceId}`
}

async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    showToast(t('space.gfs.public_link_copied'), 'success')
  } catch {
    // Older browsers / locked-down WebViews fall through to a manual
    // selection prompt rather than silently failing.
    showToast(t('space.gfs.public_link_copy_failed'), 'info')
  }
}

function setPending(gfsId: string, on: boolean) {
  const next = new Set(pendingPublish.value)
  if (on) next.add(gfsId)
  else next.delete(gfsId)
  pendingPublish.value = next
}

/**
 * Run the publish/unpublish mutation. Unpublish fires straight away;
 * publish is funnelled through a confirm dialog (``confirmPublishGfs``)
 * because it makes the space world-discoverable. The per-row in-flight
 * flag guards against a double-click firing a duplicate request and is
 * always cleared in ``finally``.
 */
async function togglePublish(spaceId: string, gfsId: string) {
  if (!isPublished(gfsId)) {
    // Defer the actual POST to the confirm handler.
    confirmPublishGfs.value = gfsId
    return
  }
  if (pendingPublish.value.has(gfsId)) return
  setPending(gfsId, true)
  try {
    await api.delete(`/api/spaces/${spaceId}/publish/${gfsId}`)
    publications.value = publications.value.filter(p => p.gfs_connection_id !== gfsId)
    showToast(t('space.unpublish_from_gfs'), 'success')
  } catch (e: any) {
    showToast(e.message || t('common.error'), 'error')
  } finally {
    setPending(gfsId, false)
  }
}

/** Confirmed publish — pushes the returned publication (carrying
 *  ``status`` so the row reflects active-vs-pending immediately). */
async function doPublish(spaceId: string, gfsId: string) {
  if (pendingPublish.value.has(gfsId)) return
  setPending(gfsId, true)
  try {
    const pub = await api.post<GfsSpacePublication>(`/api/spaces/${spaceId}/publish/${gfsId}`)
    publications.value = [...publications.value, pub]
    showToast(t('space.publish_to_gfs'), 'success')
  } catch (e: any) {
    showToast(e.message || t('common.error'), 'error')
  } finally {
    setPending(gfsId, false)
  }
}

export function SpaceSettings({
  space,
  onUpdate,
  isRemoteSpace = false,
  isOwner = false,
}: {
  space: Space
  onUpdate: () => void
  /** True when this space is hosted on another household (we hold a stub).
   *  General config, archive, dissolve + tier proposals all forward to the
   *  host, but GFS publication is the host's own concern — hide it. */
  isRemoteSpace?: boolean
  /** The caller is the space owner. Owner-only settings (the connection
   *  server publish mode) render only then — the server answers anyone
   *  else with 403. */
  isOwner?: boolean
}) {
  // ``useSignal`` (not ``signal()``) so the underlying signal instance
  // is stable across renders. Plain ``signal(initial)`` inside a
  // component body recreates a fresh signal on every render, which
  // silently drops typed values when sibling state changes trigger a
  // re-render — caught the hard way during the retention-days work.
  const name = useSignal(space.name)
  const description = useSignal(space.description || '')
  const emoji = useSignal(space.emoji || '')
  const joinMode = useSignal(space.join_mode)
  // Publication tier (space_type) is quorum-gated (v_16) — changing it is a
  // *proposal*, separate from the rest of the config form below.
  const tierChoice = useSignal(space.space_type)
  const locationEnabled = useSignal(Boolean(space.features?.location))
  const locationMode = useSignal<'gps' | 'zone_only'>(
    space.features?.location_mode ?? 'gps',
  )
  // Per-space feature visibility (§23.91). Admins toggle these to hide
  // tabs that aren't used in the space; existing data (pages, events,
  // tasks, stickies, gallery albums) stays in storage and reappears
  // when the flag flips back. Defaults mirror the SpaceFeatures
  // dataclass — every tab on by default. Pre-0008/0009 spaces with
  // a column at 0 are backfilled to 1 by the migrations.
  const featurePages = useSignal(space.features?.pages ?? true)
  const featureCalendar = useSignal(space.features?.calendar ?? true)
  // Opt-in: the backend's ``SpaceFeatures.timetable`` defaults off.
  const featureTimetable = useSignal(Boolean(space.features?.timetable))
  const featureTodo = useSignal(space.features?.todo ?? true)
  const featureStickies = useSignal(space.features?.stickies ?? true)
  const featureGallery = useSignal(space.features?.gallery ?? true)
  const featureBazaar = useSignal(space.features?.bazaar ?? true)
  // The readability opt-in. OFF by default: with it off the space is
  // listed in a directory but nothing of its content is published — no GFS
  // relay, no content key for a follower, no subscribe accepted. It is
  // independent of the join mode above (invite-only + followers-on is a
  // broadcast space).
  const allowSubscribers = useSignal(
    Boolean(space.features?.allow_subscribers),
  )
  // Owner-only (v_50): how members publish over a connection server.
  // Absent → ``trusted``, the server default.
  const gfsPublishMode = useSignal<GfsPublishMode>(
    space.features?.gfs_publish_mode ?? 'trusted',
  )
  // Owner-only, PRIVATE spaces: whether the space uses a connection
  // server at all (``features.private_gfs``). Off by default.
  const isPrivate = space.space_type === 'private'
  const showPrivateGfs = isOwner && isPrivate
  const privateGfs = useSignal(Boolean(space.features?.private_gfs))
  // A 409 PRIVATE_GFS_LINK_MEMBERS: the households that joined through a
  // connection-server link and must be removed before it can go off.
  const linkMembers = useSignal<LinkMemberHousehold[] | null>(null)
  // Shown only where members actually publish over a connection server.
  // A public / global space: when followers may read its posts (the
  // backend's ``_publicly_readable``) — the live followers switch counts,
  // and a space already strict keeps it visible so the owner can always
  // switch back. A private space: exactly while it uses the connection
  // server (the live switch above); with it off the choice means nothing.
  // Only these tiers may be listed on a GFS directory.
  const publishableTier = space.space_type === 'public' || space.space_type === 'global'
  const showGfsPublishMode = isOwner && (
    isPrivate
      ? privateGfs.value
      : (
        (
          (space.space_type === 'public' || space.space_type === 'global')
          && allowSubscribers.value
        )
        || (space.features?.gfs_publish_mode ?? 'trusted') !== 'trusted'
      )
  )
  // Subscriber-engagement opt-ins (§23.49) — admins flip these when
  // they want followers to be able to react / comment without being
  // promoted to full members.  Posts always remain member-only.
  const allowSubscriberComment = useSignal(
    Boolean(space.features?.allow_subscriber_comment),
  )
  const allowSubscriberReact = useSignal(
    Boolean(space.features?.allow_subscriber_react),
  )
  // Owner opt-in (delegated-admin epic, Phase 1a). Defaults OFF
  // (least-privilege): authorises the space's admins to act on the
  // owner's behalf (moderate / invite / publish) while the owner is
  // offline. This flag is the persisted policy switch only; the
  // seed-share behaviour it gates ships in a later task.
  // §23.42 — whether owners/admins may page every member with @here. Off by
  // default; plain members' @here never notifies either way.
  const allowHere = useSignal(Boolean(space.allow_here_mention))
  const delegatedAdminAuthority = useSignal(
    Boolean(space.features?.delegated_admin_authority),
  )
  // Per-space post-type allow-list (§23.49). A missing list means a
  // freshly-stubbed space the host config hasn't reached yet — treat
  // that as all-allowed so the checkboxes don't render everything off.
  const allowedPostTypes = space.features?.allowed_post_types
  const postTypeEnabled = useSignal<Record<string, boolean>>(
    Object.fromEntries(
      SPACE_POST_TYPE_KEYS.map((k) => [
        k,
        !allowedPostTypes || allowedPostTypes.includes(k),
      ]),
    ),
  )
  // Who may add / change things per feature (§4.3 access levels). The
  // server enforces them on every household; the selects only offer the
  // levels the server applies today (Reviewed is posts-only).
  const accessLevels = useSignal<Record<AccessFeature, SpaceAccessLevel>>(
    Object.fromEntries(
      ACCESS_FEATURES.map(f => [f, accessLevel(space.features, f)]),
    ) as Record<AccessFeature, SpaceAccessLevel>,
  )
  // A 409 PEERS_TOO_OLD: the body we tried, and the households that can't
  // enforce a raised level yet — the admin applies anyway or backs out.
  const peersTooOld = useSignal<{
    body: Record<string, unknown>
    households: BehindHousehold[]
  } | null>(null)
  // Retention is "delete posts older than N days". ``null`` means
  // "keep forever" — that's the legacy default and what fresh spaces
  // ship with. The text input is empty in that case; entering 0 or
  // clearing the field flips it back to "forever". The backend
  // service normalises any non-positive value to ``null``.
  const [retentionDays, setRetentionDays] = useState<string>(
    space.retention_days != null ? String(space.retention_days) : '',
  )
  const [exemptTypes, setExemptTypes] = useState<string[]>(
    space.retention_exempt_types ?? [],
  )
  // Mirrors the save logic below: a positive whole number turns the sweep on.
  const retentionOn = (parseInt(retentionDays, 10) || 0) > 0

  useEffect(() => {
    // GFS publication is host-local; on a remote stub there's nothing to load.
    if (!isRemoteSpace) loadFederationData(space.id)
  }, [space.id, isRemoteSpace])

  const save = async () => {
    const previousMode = space.features?.location_mode ?? 'gps'
    const modeChanged = locationEnabled.value
      && locationMode.value !== previousMode
    // Rebuild allowed_post_types from the checkboxes, but PRESERVE any
    // type the UI doesn't manage (transcript / event / highlight_share)
    // exactly as the space already had it — otherwise saving settings
    // would silently strip them.
    const existingAllowed =
      space.features?.allowed_post_types
      ?? SPACE_POST_TYPE_KEYS.slice()
    const preserved = existingAllowed.filter(
      (t) => !SPACE_POST_TYPE_KEYS.includes(t),
    )
    const chosen = SPACE_POST_TYPE_KEYS.filter((k) => postTypeEnabled.value[k])
    if (chosen.length === 0) {
      showToast(t('space.post_types.none_on'), 'error')
      return
    }
    const allowedPostTypesPayload = [...preserved, ...chosen].sort()

    // Send ONLY what the admin changed. Re-sending a field the form merely
    // displayed is not harmless: on a space hosted by another household our
    // copy can lag the host (and before retention federated it never had
    // the value at all), so saving a rename used to push
    // ``retention_days: 0`` and switch the host's cleanup off. Absent means
    // "leave it alone" on every path (this PATCH route and the host's
    // forwarded-edit handler).
    const body: Record<string, unknown> = {}
    if (name.value !== space.name) body.name = name.value
    // '' clears the description / icon (the backend stores it as null).
    if (description.value !== (space.description || '')) {
      body.description = description.value
    }
    if (emoji.value !== (space.emoji || '')) body.emoji = emoji.value
    if (joinMode.value !== space.join_mode) body.join_mode = joinMode.value
    if (allowHere.value !== Boolean(space.allow_here_mention)) {
      body.allow_here_mention = allowHere.value
    }
    // Retention: empty / zero / negative means "keep forever", sent as the
    // 0 sentinel the backend normalises to ``retention_days = null``. Input
    // that isn't a number at all is left out.
    const parsedRetention = parseInt(retentionDays, 10)
    const retentionValue: number | null | undefined =
      retentionDays.trim() === ''
        ? null
        : Number.isFinite(parsedRetention)
          ? (parsedRetention > 0 ? parsedRetention : null)
          : undefined
    if (
      retentionValue !== undefined
      && retentionValue !== (space.retention_days ?? null)
    ) {
      body.retention_days = retentionValue ?? 0
    }
    // Only send the exempt list while retention is on — with it off the
    // checkboxes are hidden, so the stored choice is left as it was.
    if (retentionOn) {
      const exempt = [...new Set(exemptTypes)].sort()
      const stored = [...new Set(space.retention_exempt_types ?? [])].sort()
      if (exempt.join(',') !== stored.join(',')) {
        body.retention_exempt_types = exempt
      }
    }
    // Features go as a partial block — the backend merges it onto the
    // space's current features. Baselines mirror the toggle defaults above.
    const f = space.features
    const featureValues: Array<[string, unknown, unknown]> = [
      ['pages', featurePages.value, f?.pages ?? true],
      ['calendar', featureCalendar.value, f?.calendar ?? true],
      ['timetable', featureTimetable.value, Boolean(f?.timetable)],
      ['todo', featureTodo.value, f?.todo ?? true],
      ['stickies', featureStickies.value, f?.stickies ?? true],
      ['gallery', featureGallery.value, f?.gallery ?? true],
      ['bazaar', featureBazaar.value, f?.bazaar ?? true],
      ['location', locationEnabled.value, Boolean(f?.location)],
      ['location_mode', locationMode.value, previousMode],
      ['allow_subscribers', allowSubscribers.value, Boolean(f?.allow_subscribers)],
      [
        'allow_subscriber_comment',
        allowSubscriberComment.value,
        Boolean(f?.allow_subscriber_comment),
      ],
      [
        'allow_subscriber_react',
        allowSubscriberReact.value,
        Boolean(f?.allow_subscriber_react),
      ],
      [
        'delegated_admin_authority',
        delegatedAdminAuthority.value,
        Boolean(f?.delegated_admin_authority),
      ],
      [
        'gfs_publish_mode',
        gfsPublishMode.value,
        f?.gfs_publish_mode ?? 'trusted',
      ],
      ['private_gfs', privateGfs.value, Boolean(f?.private_gfs)],
    ]
    for (const feature of ACCESS_FEATURES) {
      featureValues.push([
        `${feature}_access`,
        accessLevels.value[feature],
        accessLevel(f, feature),
      ])
    }
    const features: Record<string, unknown> = {}
    for (const [key, value, baseline] of featureValues) {
      if (value !== baseline) features[key] = value
    }
    if (
      allowedPostTypesPayload.join(',') !== [...existingAllowed].sort().join(',')
    ) {
      features.allowed_post_types = allowedPostTypesPayload
    }
    if (Object.keys(features).length > 0) body.features = features

    if (Object.keys(body).length === 0) {
      showToast(t('space.settings.no_changes'), 'info')
      return
    }
    await sendPatch(body, modeChanged)
  }

  /** PATCH the config. A 409 ``PEERS_TOO_OLD`` (a raised access level some
   *  member household can't enforce yet) opens the apply-anyway prompt
   *  instead of an error toast. */
  const sendPatch = async (body: Record<string, unknown>, modeChanged = false) => {
    try {
      const res = await api.patch<{ forwarded?: boolean }>(`/api/spaces/${space.id}`, body)
      if (res?.forwarded) {
        // Hosted by another household: the edit went to the host, which
        // applies it (and may keep a raised access level back until every
        // member household can enforce it). Show the level in force here,
        // never the ask; the config broadcast refreshes the page.
        const features = body.features as Record<string, unknown> | undefined
        const askedAccess = ACCESS_FEATURES.some(f => features?.[`${f}_access`] !== undefined)
        accessLevels.value = Object.fromEntries(
          ACCESS_FEATURES.map(f => [f, accessLevel(space.features, f)]),
        ) as Record<AccessFeature, SpaceAccessLevel>
        showToast(
          askedAccess ? t('space.access.forwarded') : t('space.settings.forwarded'),
          'info',
        )
        onUpdate()
        return
      }
      if (modeChanged) {
        showToast(
          locationMode.value === 'zone_only'
            ? t('space.location.zone_on_toast')
            : t('space.location.gps_on_toast'),
          'success',
        )
      } else {
        showToast(t('space.settings.saved'), 'success')
      }
      onUpdate()
    } catch (e: any) {
      const households = body.force === true ? null : peersTooOldHouseholds(e)
      if (households) {
        peersTooOld.value = { body, households }
        return
      }
      const stranded = privateGfsLinkMembers(e)
      if (stranded) {
        // Nothing was saved: the option stays on until these households
        // are gone. Show the switch as it really is, and who blocks it.
        privateGfs.value = true
        linkMembers.value = stranded
        showToast(t('space.private_gfs.link_members_toast'), 'error')
        return
      }
      showToast(e.message || t('space.settings.save_failed'), 'error')
    }
  }

  /** The features whose access row shows (a disabled feature has none). */
  const shownAccessFeatures = ACCESS_FEATURES.filter(feature => ({
    posts: true,
    pages: featurePages.value,
    tasks: featureTodo.value,
    stickies: featureStickies.value,
    calendar: featureCalendar.value,
  })[feature])

  const dissolve = async () => {
    // A sole admin's dissolve executes inside this request, so the
    // server's ``dissolved`` frame arrives before the response: mark it
    // as ours so the live handlers leave the toast + redirect to us.
    markLocalDissolve(space.id)
    try {
      // Dissolving is gated behind multi-admin approval (v_16): this opens
      // a proposal. It executes immediately only when the caller is the
      // sole admin (majority of 1); otherwise it needs other admins to
      // approve from the banner on the space.
      const res = await api.post<{
        proposal?: { status?: string; needed?: number }
      }>(`/api/spaces/${space.id}/proposals`, { action: 'dissolve' })
      if (res?.proposal?.status === 'executed') {
        showToast(t('space.dissolve.done'), 'info')
        // ``addBase`` keeps the hard navigate inside the ingress prefix.
        window.location.href = addBase('/spaces')
      } else {
        // Only proposed: when the other admins approve later, the
        // dissolve frame must still move this tab off the space.
        clearLocalDissolve(space.id)
        showToast(t('space.dissolve.proposed'), 'info')
      }
    } catch (e: any) {
      clearLocalDissolve(space.id)
      showToast(e.message || t('space.dissolve.failed'), 'error')
    }
  }

  const proposeTier = async () => {
    if (tierChoice.value === space.space_type) return
    try {
      // Publication-tier changes are quorum-gated (v_16): this opens a
      // proposal. Solo-admin spaces apply immediately; otherwise it needs a
      // majority to approve from the banner on the space.
      const res = await api.post<{
        proposal?: { status?: string }
      }>(`/api/spaces/${space.id}/proposals`, {
        action: 'set_public_tier',
        space_type: tierChoice.value,
      })
      if (res?.proposal?.status === 'executed') {
        showToast(t('space.tier.changed'), 'success')
        onUpdate()
      } else {
        showToast(t('space.tier.proposed'), 'info')
      }
    } catch (e: any) {
      showToast(e.message || t('space.tier.failed'), 'error')
    }
  }

  const setArchived = async (archived: boolean) => {
    try {
      if (archived) await api.post(`/api/spaces/${space.id}/archive`)
      else await api.delete(`/api/spaces/${space.id}/archive`)
      showToast(
        archived ? t('space.archive.archived_toast') : t('space.archive.unarchived_toast'),
        'success',
      )
      onUpdate()
    } catch (e: any) {
      showToast(e.message || t('space.archive.failed'), 'error')
    }
  }

  return (
    <div class="sh-space-settings">
      <h3>{t('space.settings.title')}</h3>
      <div class="sh-form">
        <label>{t('space.settings.name')} <input value={name.value} onInput={(e) => name.value = (e.target as HTMLInputElement).value} /></label>
        <label>{t('space.settings.description')} <textarea value={description.value} onInput={(e) => description.value = (e.target as HTMLTextAreaElement).value} rows={2} /></label>
        <EmojiField value={emoji} openKey="space-settings-icon" />
        <RadioCardGroup
          legend={t('space.join.legend')}
          name="space-settings-join-mode"
          value={joinMode.value}
          options={joinOptionsForVisibility(space.space_type)}
          onChange={(v) => joinMode.value = v as typeof joinMode.value}
        />
        <fieldset class="sh-form-fieldset">
          <legend>🗓 {t('space.retention.legend')}</legend>
          <label>
            {t('space.retention.label')}
            <input
              type="number"
              min={0}
              max={3650}
              inputMode="numeric"
              value={retentionDays}
              placeholder={t('space.retention.placeholder')}
              onInput={(e) => {
                setRetentionDays((e.target as HTMLInputElement).value)
              }}
            />
            <span class="sh-muted"> {t('space.retention.days_hint')}</span>
          </label>
          <p class="sh-muted">{t('space.retention.applies')}</p>
          {retentionOn && (
            <fieldset
              class="sh-form-fieldset sh-retention-keep"
              data-testid="retention-exempt-types"
            >
              <legend>{t('space.retention_keep_legend')}</legend>
              <p class="sh-muted" style={{ marginTop: 0 }}>
                {t('space.retention_keep_hint')}
              </p>
              {RETENTION_EXEMPT_TYPES.map(([key, labelKey]) => (
                <label class="sh-toggle-row" key={key}>
                  <input
                    type="checkbox"
                    value={key}
                    checked={exemptTypes.includes(key)}
                    onChange={(e) => {
                      const on = (e.target as HTMLInputElement).checked
                      setExemptTypes((prev) => on
                        ? [...prev.filter((x) => x !== key), key]
                        : prev.filter((x) => x !== key))
                    }}
                  />
                  {t(labelKey)}
                </label>
              ))}
            </fieldset>
          )}
        </fieldset>
        <fieldset class="sh-form-fieldset" data-testid="space-features">
          <legend>🧩 {t('space.features.legend')}</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            {t('space.features.hint')}
          </p>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featurePages.value}
              onChange={(e) => {
                featurePages.value = (e.target as HTMLInputElement).checked
              }}
            />
            📄 {t('nav.pages')}
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureCalendar.value}
              onChange={(e) => {
                featureCalendar.value = (e.target as HTMLInputElement).checked
              }}
            />
            🗓 {t('nav.calendar')}
          </label>
          <label class="sh-toggle-row sh-toggle-row--sub">
            <input
              type="checkbox"
              checked={featureTimetable.value}
              onChange={(e) => {
                featureTimetable.value = (e.target as HTMLInputElement).checked
              }}
            />
            <span class="sh-toggle-row__text">
              <span>🏫 {t('space.feature.timetable')}</span>
              <span class="sh-toggle-row__sub">{t('space.feature.timetable_sub')}</span>
            </span>
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureTodo.value}
              onChange={(e) => {
                featureTodo.value = (e.target as HTMLInputElement).checked
              }}
            />
            ✅ {t('nav.tasks')}
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureStickies.value}
              onChange={(e) => {
                featureStickies.value = (e.target as HTMLInputElement).checked
              }}
            />
            📝 {t('nav.stickies')}
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureGallery.value}
              onChange={(e) => {
                featureGallery.value = (e.target as HTMLInputElement).checked
              }}
            />
            🖼 {t('nav.gallery')}
          </label>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={featureBazaar.value}
              onChange={(e) => {
                featureBazaar.value = (e.target as HTMLInputElement).checked
              }}
            />
            🛍 {t('nav.bazaar')}
          </label>
        </fieldset>
        <fieldset class="sh-form-fieldset" data-testid="space-access">
          <legend>🔐 {t('space.access.heading')}</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>{t('space.access.intro')}</p>
          {shownAccessFeatures.map(feature => {
            const current = accessLevels.value[feature]
            const id = `sh-access-${space.id}-${feature}`
            // Every level is offered — Reviewed works with members from
            // other households (federated moderation, v_43). A household
            // too old for it is named by the PEERS_TOO_OLD prompt.
            return (
              <div key={feature} class="sh-access-row">
                <label for={id} class="sh-access-row__label">{featureLabel(feature)}</label>
                <select
                  id={id}
                  class="sh-select sh-access-row__select"
                  data-feature={feature}
                  value={current}
                  onChange={(e) => {
                    accessLevels.value = {
                      ...accessLevels.value,
                      [feature]: (e.target as HTMLSelectElement).value as SpaceAccessLevel,
                    }
                  }}
                >
                  {levelOptions().map(level => (
                    <option key={level} value={level}>{levelLabel(level)}</option>
                  ))}
                </select>
              </div>
            )
          })}
          <p class="sh-muted sh-access-explain">{t('space.access.explain')}</p>
        </fieldset>
        <fieldset class="sh-form-fieldset" data-testid="space-post-types">
          <legend>📮 {t('space.post_types.legend')}</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            {t('space.post_types.hint')}
          </p>
          {SPACE_POST_TYPES.map(([key, icon, labelKey]) => (
            <label key={key} class="sh-toggle-row">
              <input
                type="checkbox"
                checked={postTypeEnabled.value[key]}
                onChange={(e) => {
                  postTypeEnabled.value = {
                    ...postTypeEnabled.value,
                    [key]: (e.target as HTMLInputElement).checked,
                  }
                }}
              />
              {icon} {t(labelKey)}
            </label>
          ))}
        </fieldset>

        <fieldset class="sh-form-fieldset">
          <legend>📍 {t('space.location.legend')}</legend>
          <label class="sh-toggle-row">
            <input
              type="checkbox"
              checked={locationEnabled.value}
              onChange={(e) => {
                locationEnabled.value = (e.target as HTMLInputElement).checked
              }}
            />
            {t('space.location.toggle')}
          </label>
          {locationEnabled.value && (
            <>
              <fieldset class="sh-mode-fieldset" aria-label={t('space.location.mode_legend')}>
                <legend>{t('space.location.mode_legend')}</legend>
                <label class={`sh-mode-option ${locationMode.value === 'gps' ? 'sh-mode-option--selected' : ''}`}>
                  <input
                    type="radio"
                    name={`location-mode-${space.id}`}
                    value="gps"
                    checked={locationMode.value === 'gps'}
                    onChange={() => { locationMode.value = 'gps' }}
                  />
                  <span class="sh-mode-option__body">
                    <span class="sh-mode-option__title">
                      🛰️ {t('space.location.gps')}
                    </span>
                    <span class="sh-muted">{t('space.location.gps_hint')}</span>
                  </span>
                </label>
                <label class={`sh-mode-option ${locationMode.value === 'zone_only' ? 'sh-mode-option--selected' : ''}`}>
                  <input
                    type="radio"
                    name={`location-mode-${space.id}`}
                    value="zone_only"
                    checked={locationMode.value === 'zone_only'}
                    onChange={() => { locationMode.value = 'zone_only' }}
                  />
                  <span class="sh-mode-option__body">
                    <span class="sh-mode-option__title">
                      🔒 {t('space.location.zone')}
                      <span class="sh-mode-option__badge">{t('space.location.zone_badge')}</span>
                    </span>
                    <span class="sh-muted">{t('space.location.zone_hint')}</span>
                  </span>
                </label>
              </fieldset>
              <p class="sh-muted">
                <a href={addBase(`/spaces/${space.id}/zones`)}>{t('space.location.manage_zones')} →</a>
                {locationMode.value === 'zone_only'
                  && ` ${t('space.location.zones_required')}`}
              </p>
            </>
          )}
          <p class="sh-muted">{t('space.location.ha_zones_note')}</p>
        </fieldset>

        {/* Followers (subscribers). The first switch decides whether they
         *  may exist at ALL — it is what makes a public / global space
         *  publicly readable, independently of the join mode. The two
         *  below loosen what an existing follower may do. Posting
         *  top-level content stays member-only either way. */}
        <fieldset class="sh-form-fieldset">
          <legend>🔔 {t('space.followers.legend')}</legend>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            {t('space.followers.intro')}
          </p>
          <label>
            <input
              type="checkbox"
              checked={allowSubscribers.value}
              onChange={(e) => {
                allowSubscribers.value =
                  (e.target as HTMLInputElement).checked
              }}
            />
            {t('space.followers.allow')}
          </label>
          <label>
            <input
              type="checkbox"
              disabled={!allowSubscribers.value}
              checked={allowSubscriberReact.value}
              onChange={(e) => {
                allowSubscriberReact.value =
                  (e.target as HTMLInputElement).checked
              }}
            />
            {t('space.followers.react')}
          </label>
          <label>
            <input
              type="checkbox"
              disabled={!allowSubscribers.value}
              checked={allowSubscriberComment.value}
              onChange={(e) => {
                allowSubscriberComment.value =
                  (e.target as HTMLInputElement).checked
              }}
            />
            {t('space.followers.comment')}
          </label>
          <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
            {allowSubscribers.value
              ? t('space.followers.members_only_note')
              : t('space.followers.off_note')}
          </p>
        </fieldset>

        {/* Whether a PRIVATE space uses the connection server at all —
         *  owner-only. Off: internal links only, no server traffic. */}
        {showPrivateGfs && (
          <fieldset class="sh-form-fieldset sh-private-gfs" data-testid="private-gfs">
            <legend>🛰️ {t('space.private_gfs.legend')}</legend>
            <p class="sh-muted" style={{ marginTop: 0 }} data-testid="private-gfs-intro">
              {t('space.private_gfs.intro')}
            </p>
            <label class="sh-toggle-row">
              <input
                type="checkbox"
                checked={privateGfs.value}
                data-testid="private-gfs-toggle"
                onChange={(e) => {
                  privateGfs.value = (e.target as HTMLInputElement).checked
                  // A new choice; the old refusal no longer describes it.
                  linkMembers.value = null
                }}
              />
              {t('space.private_gfs.toggle')}
            </label>
            <dl class="sh-gfs-publish-mode__help">
              <dt>{t('space.private_gfs.on')}</dt>
              <dd>{t('space.private_gfs.on_help')}</dd>
              <dt>{t('space.private_gfs.off')}</dt>
              <dd>{t('space.private_gfs.off_help')}</dd>
            </dl>
            <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
              {t('space.private_gfs.owner_only')}
            </p>
            {!privateGfs.value && Boolean(space.features?.private_gfs)
              && !linkMembers.value && (
              <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}
                 data-testid="private-gfs-off-note">
                {t('space.private_gfs.off_note')}
              </p>
            )}
            {linkMembers.value && (
              <div class="sh-private-gfs__blocked" role="alert"
                   data-testid="private-gfs-link-members">
                <p class="sh-private-gfs__blocked-title">
                  {t('space.private_gfs.link_members_title')}
                </p>
                <p class="sh-muted" style={{ margin: 0 }}>
                  {t('space.private_gfs.link_members_body')}
                </p>
                <ul class="sh-private-gfs__households">
                  {linkMembers.value.map(h => (
                    <li key={h.instance_id}>
                      <strong>{h.display_name}</strong>
                      {h.members.length > 0 && (
                        <span class="sh-muted">
                          {' — '}
                          {h.members.map(m => m.display_name).join(', ')}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
                <a href={addBase(`/spaces/${space.id}?tab=members`)}
                   class="sh-private-gfs__members-link">
                  {t('space.private_gfs.link_members_open')}
                </a>
              </div>
            )}
          </fieldset>
        )}

        {/* Connection server publish mode (v_50) — owner-only, and only
         *  where members publish over a connection server at all. */}
        {showGfsPublishMode && (
          <fieldset class="sh-form-fieldset sh-gfs-publish-mode">
            <legend id="space-settings-gfs-publish-legend">
              📡 {t('space.gfs_publish.legend')}
            </legend>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              {isPrivate
                ? t('space.gfs_publish.private_intro')
                : t('space.gfs_publish.intro')}
            </p>
            <ChipRadioGroup<GfsPublishMode>
              labelledBy="space-settings-gfs-publish-legend"
              value={gfsPublishMode.value}
              onChange={(v) => { gfsPublishMode.value = v }}
              options={[
                { value: 'trusted', label: t('space.gfs_publish.trusted') },
                { value: 'strict', label: t('space.gfs_publish.strict') },
              ]}
            />
            <dl class="sh-gfs-publish-mode__help">
              <dt>{t('space.gfs_publish.trusted')}</dt>
              <dd>{t('space.gfs_publish.trusted_help')}</dd>
              <dt>{t('space.gfs_publish.strict')}</dt>
              <dd>{t('space.gfs_publish.strict_help')}</dd>
            </dl>
            {gfsPublishMode.value === 'strict'
              && (space.features?.gfs_publish_mode ?? 'trusted') !== 'strict' && (
              <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
                {t('space.gfs_publish.strict_switch_note')}
              </p>
            )}
          </fieldset>
        )}

        {/* @here (§23.42). Admin config like the rest; federates to every
         *  member household, each of which re-checks the author's role. */}
        <fieldset class="sh-form-fieldset">
          {/* ``bdi``: "@here" stays "@here" on a right-to-left page. */}
          <legend>📣 <bdi>@here</bdi></legend>
          <label>
            <input
              type="checkbox"
              checked={allowHere.value}
              onChange={(e) => {
                allowHere.value = (e.target as HTMLInputElement).checked
              }}
            />
            {t('space.here.toggle')}
          </label>
          <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
            {t('space.here.hint')}
          </p>
        </fieldset>

        {/* Delegated admin authority — owner-only policy switch
         *  (delegated-admin epic, Phase 1a). Off by default; only
         *  meaningful for a space hosted here, so hidden on a remote stub. */}
        {!isRemoteSpace && (
          <fieldset class="sh-form-fieldset">
            <legend>🛡️ {t('space.admin_authority.legend')}</legend>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              {t('space.admin_authority.intro')}
            </p>
            <label>
              <input
                type="checkbox"
                checked={delegatedAdminAuthority.value}
                onChange={(e) => {
                  delegatedAdminAuthority.value =
                    (e.target as HTMLInputElement).checked
                }}
              />
              {t('space.admin_authority.toggle')}
            </label>
            <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
              {t('space.admin_authority.hint')}
            </p>
          </fieldset>
        )}

        <div class="sh-form-actions">
          <Button onClick={save}>{t('space.settings.save')}</Button>
        </div>
      </div>

      <hr />
      <h3>{t('space.tier.heading')}</h3>
      <p class="sh-muted" style={{ marginTop: 0 }}>{t('space.tier.intro')}</p>
      <div class="sh-form">
        <label>{t('space.tier.label')}
          <select
            value={tierChoice.value}
            onChange={(e) =>
              (tierChoice.value = (e.target as HTMLSelectElement)
                .value as Space['space_type'])
            }
          >
            <option value="private">{t('space.tier.private')}</option>
            <option value="household">{t('space.tier.household')}</option>
            <option value="public" disabled={isRestricted('public_spaces')}>
              {t('space.tier.public')}
            </option>
            <option value="global" disabled={isRestricted('public_spaces')}>
              {t('space.tier.global')}
            </option>
          </select>
        </label>
        {isRestricted('public_spaces') && (
          <ProtectedNotice capability="public_spaces" />
        )}
        <div class="sh-form-actions">
          <Button
            variant="secondary"
            disabled={tierChoice.value === space.space_type}
            onClick={proposeTier}
          >
            {tierChoice.value === space.space_type
              ? t('space.tier.current')
              : t('space.tier.propose')}
          </Button>
        </div>
      </div>

      {!isRemoteSpace && <hr />}
      {!isRemoteSpace && <h3>{t('space.federation')}</h3>}
      {!isRemoteSpace && (federationLoading.value ? (
        <p class="sh-muted">{t('common.loading')}</p>
      ) : gfsServers.value.length === 0 ? (
        <p class="sh-muted">{t('space.no_gfs_connections')}</p>
      ) : (
        <div class="sh-federation-list">
          {!publishableTier && (
            <p class="sh-muted" data-testid="gfs-publish-tier-note">
              {t('space.gfs_publish_needs_public')}
            </p>
          )}
          {/* You can only publish to a GFS that has accepted your household
              (``active``); pending/suspended connections get no publish row.
              The backend now returns those non-active connections too, so we
              filter here and surface the held count below. */}
          {gfsServers.value.filter(g => g.status === 'active').map(gfs => {
            const pub = publicationFor(gfs.id)
            const published = pub != null
            // Only a live (``active``) publication has a resolvable public
            // page; a pending/banned space's GFS page 404s, so suppress the
            // link in those states.
            const isLive = pub?.status === 'active'
            const isPending = pub?.status === 'pending'
            const inFlight = pendingPublish.value.has(gfs.id)
            const publicUrl = publicSpaceUrl(gfs.inbox_url, space.id)
            // Status label honesty: green only when actually live; pending and
            // rejected get muted treatments so the admin never mistakes a
            // held/removed publication for a discoverable one.
            const statusLabel = !published
              ? t('space.not_published')
              : isLive
                ? t('space.published')
                : isPending
                  ? t('space.publish_pending')
                  : t('space.publish_rejected')
            const statusClass = isLive
              ? 'sh-text-success'
              : isPending
                ? 'sh-text-warning'
                : 'sh-muted'
            return (
              <div key={gfs.id} class="sh-federation-row" data-testid={`gfs-row-${gfs.id}`}>
                <div class="sh-connection-info">
                  <span class={`sh-status-dot sh-status-dot--${gfs.status === 'active' ? 'active' : gfs.status === 'suspended' ? 'unreachable' : 'pending'}`} />
                  <strong>{gfs.display_name}</strong>
                  <span class="sh-muted">{gfs.inbox_url}</span>
                </div>
                {isLive && (
                  <div class="sh-federation-public-url">
                    <span class="sh-muted">🔗 {t('space.gfs.public_link')}</span>
                    <a
                      href={publicUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      class="sh-federation-public-url__link"
                      title={publicUrl}
                    >
                      {publicUrl}
                    </a>
                    <button
                      type="button"
                      class="sh-federation-public-url__copy"
                      onClick={() => void copyToClipboard(publicUrl)}
                      aria-label={t('space.gfs.copy_public_link')}
                      title={t('space.gfs.copy')}
                    >
                      📋
                    </button>
                  </div>
                )}
                {isPending && (
                  <p class="sh-muted sh-federation-pending-hint">
                    {t('space.publish_pending_hint')}
                  </p>
                )}
                <div class="sh-federation-actions">
                  <span class={statusClass}>{statusLabel}</span>
                  {/* Only public / global spaces may be listed on a GFS (the
                      server refuses the rest with SPACE_NOT_PUBLIC). An old
                      listing can still be withdrawn. */}
                  {(published || publishableTier) && (
                    <Button
                      variant={published ? 'danger' : 'primary'}
                      loading={inFlight}
                      onClick={() => togglePublish(space.id, gfs.id)}
                    >
                      {published ? t('gfs.unpublish') : t('gfs.publish')}
                    </Button>
                  )}
                </div>
              </div>
            )
          })}
          {gfsServers.value.some(g => g.status !== 'active') && (
            <p class="sh-muted">
              {t('space.gfs_pending_note', {
                n: String(gfsServers.value.filter(g => g.status !== 'active').length),
              })}
            </p>
          )}
        </div>
      ))}

      <hr />
      <h3>{t('space.archive.heading')}</h3>
      {space.archived ? (
        space.archived_reason === 'dissolved' ? (
          // Remote-terminated: the owner host dissolved the space. The
          // server rejects unarchiving, so offer no Unarchive button.
          <p class="sh-muted" style={{ marginTop: 0 }}>
            <strong>{t('space.archive.dissolved_lead')}</strong>{' '}
            {t('space.archive.readonly_copy')}
          </p>
        ) : space.archived_reason === 'removed' ? (
          <p class="sh-muted" style={{ marginTop: 0 }}>
            <strong>{t('space.archive.removed_lead')}</strong>{' '}
            {t('space.archive.readonly_copy')}
          </p>
        ) : (
          <>
            <p class="sh-muted" style={{ marginTop: 0 }}>
              <strong>{t('space.archive.archived_lead')}</strong>{' '}
              {t('space.archive.archived_body')}
            </p>
            <Button variant="secondary" onClick={() => setArchived(false)}>{t('space.archive.unarchive')}</Button>
          </>
        )
      ) : (
        <>
          <p class="sh-muted" style={{ marginTop: 0 }}>
            {t('space.archive.intro')}
          </p>
          <Button variant="secondary" onClick={() => setArchived(true)}>{t('space.archive.archive')}</Button>
        </>
      )}

      <hr />
      <h3>{t('space.dissolve.heading')}</h3>
      <p class="sh-muted">{t('space.dissolve.intro')}</p>
      <Button variant="danger" onClick={() => showDissolve.value = true}>{t('space.dissolve.button')}</Button>
      <ConfirmDialog
        open={confirmPublishGfs.value !== null}
        title={t('space.gfs.publish_confirm_title')}
        message={t('space.gfs.publish_confirm_body')}
        confirmLabel={t('gfs.publish')}
        onConfirm={() => {
          const gfsId = confirmPublishGfs.value
          confirmPublishGfs.value = null
          if (gfsId) doPublish(space.id, gfsId)
        }}
        onCancel={() => { confirmPublishGfs.value = null }}
      />
      <ConfirmDialog
        open={peersTooOld.value !== null}
        title={t('space.access.peers_too_old.title')}
        message={t('space.access.peers_too_old.body', {
          households: (peersTooOld.value?.households ?? [])
            .map(h => h.display_name)
            .join(', '),
        })}
        confirmLabel={t('space.access.peers_too_old.apply')}
        onConfirm={() => {
          const pending = peersTooOld.value
          peersTooOld.value = null
          if (pending) void sendPatch({ ...pending.body, force: true })
        }}
        onCancel={() => { peersTooOld.value = null }}
      />
      <ConfirmDialog open={showDissolve.value} title={t('space.dissolve.confirm_title')}
        message={t('space.dissolve.confirm_body')}
        confirmLabel={t('space.dissolve.confirm_ok')} destructive
        onConfirm={() => { showDissolve.value = false; dissolve() }}
        onCancel={() => showDissolve.value = false} />
    </div>
  )
}
