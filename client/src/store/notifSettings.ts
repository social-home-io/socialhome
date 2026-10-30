/**
 * Notification settings (redesign D4, D9, D10) — ``/api/me/notification-
 * settings``.
 *
 * - **Kinds:** eight, each Push / In app / Off (:data:`KINDS`). The server
 *   honours them on every row.
 * - **Muted and changed:** every space, chat and post set off its default
 *   or muted — the way back from every mute made elsewhere.
 * - **Space levels:** ``PUT /api/spaces/{id}/notif-prefs`` with a duration
 *   for "Nothing" (:func:`setSpaceLevel`).
 *
 * One cached copy (:data:`notifSettings`) feeds Settings → Notifications and
 * the row "…" menu.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import type { NotifKind } from '@/types'
import type { MuteDuration } from '@/utils/mute'

export type { NotifKind }

export type NotifChoice = 'push' | 'in_app' | 'off'

/** The eight kinds, in Settings order. */
export const KINDS: readonly NotifKind[] = [
  'messages', 'replies', 'needs_you', 'plans', 'about_you', 'games', 'reactions', 'activity',
]

export interface KindSetting {
  kind: NotifKind
  choice: NotifChoice
  default: NotifChoice
  choices: NotifChoice[]
}

/** A space's or a chat's level. ``muted`` = "Nothing". */
export type NotifLevel = 'all' | 'mentions' | 'muted'

/** One line of "Muted and changed". */
export interface NotifChange {
  target: 'space' | 'chat' | 'post'
  /** Space id, conversation id, or the post's ``object_key``. */
  id: string
  level: NotifLevel
  default_level: NotifLevel
  /** Space type, chat kind (dm / group / household / space) or post kind. */
  place: string
  name: string | null
  muted_until: string | null
  then_level: NotifLevel | null
  space_id: string | null
  space_name: string | null
  snippet: string | null
}

export interface NotifSettingsState {
  kinds: KindSetting[]
  changed: NotifChange[]
}

/** The latest settings; ``null`` before the first answer. */
export const notifSettings = signal<NotifSettingsState | null>(null)
/** The last load failed (Settings shows a retry). */
export const notifSettingsError = signal(false)

export async function loadNotifSettings(): Promise<void> {
  try {
    const body = await api.get('/api/me/notification-settings') as NotifSettingsState
    notifSettings.value = {
      kinds: Array.isArray(body?.kinds) ? body.kinds : [],
      changed: Array.isArray(body?.changed) ? body.changed : [],
    }
    notifSettingsError.value = false
  } catch {
    notifSettingsError.value = true
  }
}

/** The choice that applies for *kind* (``null`` before the first load). */
export function kindChoice(kind: NotifKind): NotifChoice | null {
  return notifSettings.value?.kinds.find(k => k.kind === kind)?.choice ?? null
}

/** Set *kind* to *choice*. Optimistic; a failure rolls back and throws. */
export async function setKind(kind: NotifKind, choice: NotifChoice): Promise<void> {
  const before = notifSettings.value
  if (before) {
    notifSettings.value = {
      ...before,
      kinds: before.kinds.map(k => (k.kind === kind ? { ...k, choice } : k)),
    }
  }
  try {
    const body = await api.patch('/api/me/notification-settings', { kind, choice }) as {
      kinds?: KindSetting[]
    }
    if (notifSettings.value && Array.isArray(body?.kinds)) {
      notifSettings.value = { ...notifSettings.value, kinds: body.kinds }
    }
  } catch (err) {
    notifSettings.value = before
    throw err
  }
}

/** Mute the post *objectKey* (``post:<id>`` / ``space_post:<id>``). */
export async function mutePost(objectKey: string): Promise<void> {
  await api.post('/api/me/notification-settings/muted-posts', { object_key: objectKey })
}

/** Unmute the post *objectKey*; drops its "Muted and changed" line. */
export async function unmutePost(objectKey: string): Promise<void> {
  await api.delete(
    `/api/me/notification-settings/muted-posts/${encodeURIComponent(objectKey)}`,
  )
  dropChange('post', objectKey)
}

/** Whether a row's ``object_key`` names a post that can be muted. */
export function isPostKey(key: string | null | undefined): key is string {
  return !!key && /^(post|space_post):[A-Za-z0-9_.-]+$/.test(key)
}

/** A space's level as ``GET``/``PUT /api/spaces/{id}/notif-prefs`` say. */
export interface SpaceLevelState {
  level: NotifLevel
  is_default: boolean
  default_level: NotifLevel
  muted_until: string | null
  then_level: NotifLevel | null
}

function asLevel(v: unknown, fallback: NotifLevel): NotifLevel {
  return v === 'all' || v === 'mentions' || v === 'muted' ? v : fallback
}

export function parseSpaceLevel(body: Partial<SpaceLevelState> | null): SpaceLevelState {
  const def = asLevel(body?.default_level, 'all')
  return {
    level: asLevel(body?.level, def),
    is_default: body?.is_default === true,
    default_level: def,
    muted_until: body?.muted_until ?? null,
    then_level: body?.then_level ? asLevel(body.then_level, def) : null,
  }
}

export async function getSpaceLevel(spaceId: string): Promise<SpaceLevelState> {
  return parseSpaceLevel(await api.get(`/api/spaces/${spaceId}/notif-prefs`))
}

/** Set a space's level; "Nothing" takes *duration* (default: until turned
 *  back on). */
export async function setSpaceLevel(
  spaceId: string,
  level: NotifLevel,
  duration?: MuteDuration,
): Promise<SpaceLevelState> {
  const body = level === 'muted'
    ? { level, duration: duration ?? 'forever' }
    : { level }
  const saved = parseSpaceLevel(await api.put(`/api/spaces/${spaceId}/notif-prefs`, body))
  syncSpaceChange(spaceId, saved)
  return saved
}

/** Keep the cached "Muted and changed" line of a space in step. */
function syncSpaceChange(spaceId: string, s: SpaceLevelState): void {
  const cur = notifSettings.value
  if (!cur) return
  const at = cur.changed.find(c => c.target === 'space' && c.id === spaceId)
  const off = s.level !== s.default_level || s.muted_until !== null
  if (!off) {
    dropChange('space', spaceId)
    return
  }
  if (at) {
    notifSettings.value = {
      ...cur,
      changed: cur.changed.map(c => (c === at
        ? { ...c, level: s.level, muted_until: s.muted_until, then_level: s.then_level }
        : c)),
    }
  }
}

/** Drop one "Muted and changed" line from the cache. */
export function dropChange(target: NotifChange['target'], id: string): void {
  const cur = notifSettings.value
  if (!cur) return
  notifSettings.value = {
    ...cur,
    changed: cur.changed.filter(c => !(c.target === target && c.id === id)),
  }
}
