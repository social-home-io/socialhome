/**
 * viewerSheets — the sheets the highlight viewer opens (r3 design).
 *
 * Every sheet is a :class:`Modal` (focus trap, Esc closes the sheet and
 * never the viewer, arrows stay inside) with a heading and a 40 px ✕.
 * An open Modal pauses the viewer's timer (``openDialogCount``), so a
 * sheet never closes itself under a frame change (bug 7).
 *
 * - :func:`SeenBySheet` — the author's "Seen by N", each viewer with
 *   their reaction.
 * - :func:`ShareSheet` — Share to the household Feed or a space; asks
 *   first when that is wider than the highlight's audience (S6 / P6).
 * - :class:`ViewerSheet` + :func:`SheetRow` — the ⋯ menu's shape.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Avatar } from '@/components/Avatar'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { contentWrite } from '@/utils/contentWrite'
import { localSinceTime } from '@/utils/relativeTime'
import { connections, ensureConnections } from '@/store/connections'
import { householdUsers, namedPersonLabel, namedPersonPicture, type NamedPerson } from '@/store/householdUsers'
import { loadSpaces, spaces } from '@/store/spaces'
import { t } from '@/i18n/i18n'
import type { Highlight, Space, SpaceMemberProfile } from '@/types'
import { VIEWER_ICON } from './icons'
import { widensAudience, type ReachMember, type ShareTarget } from './shareReach'

export function ViewerSheet({ title, onClose, children }: {
  title: string
  onClose: () => void
  children: ComponentChildren
}) {
  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      class="sh-hlv-sheet"
      header={(id) => (
        <>
          <h2 id={id} class="sh-hlv-sheet__title">{title}</h2>
          <button type="button" class="sh-hlv-sheet__close" onClick={onClose} aria-label={t('modal.close')}>
            {VIEWER_ICON.close}
          </button>
        </>
      )}
    >
      {children}
    </Modal>
  )
}

export function SheetRow({ icon, title, sub, onClick, danger = false, disabled = false }: {
  icon: preact.JSX.Element
  title: ComponentChildren
  sub?: ComponentChildren
  onClick: () => void
  danger?: boolean
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      class={danger ? 'sh-hlv-row sh-hlv-row--danger' : 'sh-hlv-row'}
      onClick={onClick}
      disabled={disabled}
    >
      <span class="sh-hlv-row__ic">{icon}</span>
      <span class="sh-hlv-row__body">
        <span class="sh-hlv-row__t">{title}</span>
        {sub && <span class="sh-hlv-row__m">{sub}</span>}
      </span>
    </button>
  )
}

// ── Seen by ──────────────────────────────────────────────────────────────

export interface SeenView extends NamedPerson { viewer_user_id: string; viewed_at: string }
export interface SeenReaction extends NamedPerson { reactor_user_id: string; emoji: string }

interface SeenRow { id: string; person: NamedPerson; at: string | null; emoji: string | null }

/** One row per person: who watched (newest first), with their reaction;
 *  someone who reacted through a share card without a view row too. */
export function seenRows(views: readonly SeenView[], reactions: readonly SeenReaction[]): SeenRow[] {
  const rows = new Map<string, SeenRow>()
  for (const v of views) rows.set(v.viewer_user_id, { id: v.viewer_user_id, person: v, at: v.viewed_at, emoji: null })
  for (const r of reactions) {
    const row = rows.get(r.reactor_user_id)
    if (row) row.emoji = r.emoji
    else rows.set(r.reactor_user_id, { id: r.reactor_user_id, person: r, at: null, emoji: r.emoji })
  }
  return [...rows.values()].sort((a, b) => (b.at ?? '').localeCompare(a.at ?? ''))
}

export function SeenBySheet({ frameLabel, views, reactions, onClose }: {
  frameLabel: string
  views: readonly SeenView[]
  reactions: readonly SeenReaction[]
  onClose: () => void
}) {
  const rows = seenRows(views, reactions)
  return (
    <ViewerSheet
      title={t('highlight.viewer.seen_heading', { n: String(views.length), frame: frameLabel })}
      onClose={onClose}
    >
      {rows.length === 0
        ? <p class="sh-hlv-sheet__hint">{t('highlight.seen_by.empty')}</p>
        : (
          <ul class="sh-hlv-seen">
            {rows.map(r => {
              const name = namedPersonLabel(r.id, r.person)
              return (
                <li key={r.id} class="sh-hlv-seen__row">
                  <Avatar name={name} src={namedPersonPicture(r.id, r.person)} size={40} />
                  <span class="sh-hlv-row__body">
                    <span class="sh-hlv-row__t"><bdi dir="auto">{name}</bdi></span>
                    {r.at && <span class="sh-hlv-row__m"><time dateTime={r.at}>{localSinceTime(r.at)}</time></span>}
                  </span>
                  {r.emoji && (
                    <span class="sh-hlv-seen__emoji">
                      <span aria-hidden="true">{r.emoji}</span>
                      <span class="sr-only">{t('highlight.viewer.reacted_with', { emoji: r.emoji })}</span>
                    </span>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      <p class="sh-hlv-sheet__hint">{t('highlight.viewer.seen_hint')}</p>
    </ViewerSheet>
  )
}

// ── Share to Feed or a space ─────────────────────────────────────────────

/** Spaces the person can post a share into: not archived, not a follower. */
export function shareableSpaces(list: readonly Space[]): Space[] {
  return list.filter(s => !s.archived && s.my_role !== 'subscriber')
}

async function spaceMembers(spaceId: string): Promise<ReachMember[] | null> {
  try {
    const rows = await api.get(`/api/spaces/${encodeURIComponent(spaceId)}/members`) as SpaceMemberProfile[]
    return rows.map(r => ({ user_id: r.user_id, instance_id: r.instance_id ?? null }))
  } catch {
    return null
  }
}

async function pairedIds(): Promise<Set<string>> {
  await ensureConnections()
  return new Set(connections.value.filter(c => c.status === 'confirmed').map(c => c.instance_id))
}

export function ShareSheet({ highlight, me, onClose }: {
  highlight: Highlight
  me: string
  onClose: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  useEffect(() => { void loadSpaces() }, [])
  const list = shareableSpaces(spaces.value)

  const share = async (key: string, scope: 'household' | 'space', space: Space | null) => {
    if (busy) return
    setBusy(key)
    try {
      const target: ShareTarget = scope === 'household'
        ? { kind: 'household', members: [...householdUsers.value.keys()].map(user_id => ({ user_id })) }
        : { kind: 'space', spaceType: space!.space_type, members: await spaceMembers(space!.id) }
      const paired = highlight.audience_kind === 'all_paired' ? await pairedIds() : new Set<string>()
      if (widensAudience(highlight, me, target, paired)) {
        const ok = await confirmDialog(
          scope === 'household'
            ? t('highlight.viewer.share_wider_household')
            : t('highlight.viewer.share_wider_space', { space: space!.name }),
          { title: t('highlight.viewer.share_wider_title'), confirmLabel: t('highlight.viewer.share_confirm') },
        )
        if (!ok) return
      }
      const body: Record<string, unknown> = { scope }
      if (space) body.space_id = space.id
      const res = await contentWrite<{ post_id?: string }>(
        api.post(`/api/highlights/${encodeURIComponent(highlight.id)}/share`, body),
        { spaceId: space?.id ?? null },
      )
      onClose()
      if (!res.queued) showToast(t('highlight.shared'), 'success')
    } catch (err: unknown) {
      showToast(t('highlight.share_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <ViewerSheet title={t('highlight.viewer.share_heading')} onClose={onClose}>
      <p class="sh-hlv-sheet__hint">{t('highlight.viewer.share_note')}</p>
      <div class="sh-hlv-menu" role="group" aria-label={t('highlight.viewer.share_heading')}>
        <SheetRow
          icon={VIEWER_ICON.feed}
          title={t('highlight.viewer.share_feed')}
          sub={t('highlight.viewer.share_feed_sub')}
          disabled={busy !== null}
          onClick={() => void share('household', 'household', null)}
        />
        {list.map(s => (
          <SheetRow
            key={s.id}
            icon={s.emoji ? <span class="sh-hlv-row__emoji" aria-hidden="true">{s.emoji}</span> : VIEWER_ICON.space}
            title={<bdi dir="auto">{s.name}</bdi>}
            disabled={busy !== null}
            onClick={() => void share(s.id, 'space', s)}
          />
        ))}
      </div>
    </ViewerSheet>
  )
}
