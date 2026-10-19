/**
 * Saving one space setting the moment it is picked (owner decision 8:
 * only Basics is a form; every switch, radio and select elsewhere on the
 * settings page saves on its own, with a toast).
 *
 * ``patchSpace`` sends exactly the field(s) the control owns — never a
 * whole form — so a co-admin's stale copy of an unrelated field can't
 * ride along (on a space hosted elsewhere our copy can lag the host).
 *
 * Outcomes:
 *   - ``saved``     — applied here; toast ``okToast`` (or "Saved").
 *   - ``forwarded`` — hosted by another household: the host applies it and
 *                     the config broadcast refreshes the page.
 *   - ``failed``    — refused / offline; the toast says why and the caller
 *                     puts the control back.
 *
 * A 409 ``PEERS_TOO_OLD`` (a raised access level some member household
 * can't enforce yet) asks "Apply anyway?" and resends with ``force``; a 409
 * ``PRIVATE_GFS_LINK_MEMBERS`` hands the blocking households to
 * ``onLinkMembers``.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { peersTooOldHouseholds } from '@/features/spaces/spaceAccess'

export type PatchOutcome = 'saved' | 'forwarded' | 'failed'

/** A household named by a 409 ``PRIVATE_GFS_LINK_MEMBERS``. */
export interface LinkMemberHousehold {
  instance_id: string
  display_name: string
  members: { user_id: string; display_name: string }[]
}

/** The households a 409 ``PRIVATE_GFS_LINK_MEMBERS`` names, or ``null``
 *  for any other error. Defensive about the shape: a nameless row falls
 *  back to its id rather than rendering blank. */
export function privateGfsLinkMembers(err: unknown): LinkMemberHousehold[] | null {
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

export interface PatchOptions {
  /** Toast on success; defaults to "Saved". */
  okToast?: string
  /** Called after the server took it (saved or forwarded). */
  onUpdate?: () => void
  /** A 409 PRIVATE_GFS_LINK_MEMBERS: who blocks turning the GFS off. */
  onLinkMembers?: (households: LinkMemberHousehold[]) => void
}

/** ``PATCH /api/spaces/{id}`` with ``body`` and report how it went. */
export async function patchSpace(
  spaceId: string,
  body: Record<string, unknown>,
  opts: PatchOptions = {},
): Promise<PatchOutcome> {
  try {
    const res = await api.patch<{ forwarded?: boolean }>(`/api/spaces/${spaceId}`, body)
    if (res?.forwarded) {
      const features = body.features as Record<string, unknown> | undefined
      const askedAccess = Object.keys(features ?? {}).some(k => k.endsWith('_access'))
      showToast(
        askedAccess ? t('space.access.forwarded') : t('space.settings.forwarded'),
        'info',
      )
      opts.onUpdate?.()
      return 'forwarded'
    }
    showToast(opts.okToast ?? t('space.settings_page.saved'), 'success')
    opts.onUpdate?.()
    return 'saved'
  } catch (e: unknown) {
    const households = body.force === true ? null : peersTooOldHouseholds(e)
    if (households) {
      const ok = await confirmDialog(
        t('space.access.peers_too_old.body', {
          households: households.map(h => h.display_name).join(', '),
        }),
        {
          title: t('space.access.peers_too_old.title'),
          confirmLabel: t('space.access.peers_too_old.apply'),
        },
      )
      return ok ? patchSpace(spaceId, { ...body, force: true }, opts) : 'failed'
    }
    const stranded = privateGfsLinkMembers(e)
    if (stranded) {
      opts.onLinkMembers?.(stranded)
      showToast(t('space.private_gfs.link_members_toast'), 'error')
      return 'failed'
    }
    showToast((e as Error)?.message || t('space.settings.save_failed'), 'error')
    return 'failed'
  }
}

/** Local state that follows the server's value: a control shows its
 *  pick at once, and snaps to the stored value whenever a reload (or
 *  another admin's change) brings a new one. */
export function useServerValue<T>(server: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(server)
  const key = JSON.stringify(server)
  useEffect(() => { setValue(server) }, [key])
  return [value, setValue]
}
