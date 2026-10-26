/**
 * MomentCard — one moment in the Moments list (§Momentum, UX r3).
 *
 * The household Feed's PostCard anatomy, flat:
 *
 * - **Who.** Avatar and name are ``PersonButton``s (→ person sheet); the
 *   meta line is the time (a link to the moment) and where it came from —
 *   "Berger · paired", "2 hops away", "via GFS"; nothing for your own
 *   household.
 * - **⋯ acts on the moment**, never the person: Reply, Copy text,
 *   Save / Saved, Report; on your own: Who sees this, Delete.
 * - **Reactions, honest about what is known** (:func:`reactionMode`):
 *   ``counted`` — one chip per emoji with its number, yours filled, where
 *   the count is complete (written in this household, the author's
 *   household sent its totals, or reactions stay here — then with the
 *   household-only note); ``mine-only`` — a paired author without totals
 *   yet: a single React / "😮 You" toggle, no partial numbers; ``own`` —
 *   a summary of the reactions to your moment.
 * - **Threads.** "Reply" opens the moment with the reply field (the one
 *   reply entry point); the newest reply sits under the card with a link
 *   to all of them.
 * - **Video** is a poster at its natural aspect with ▶ and its length; the
 *   player (native controls) comes on tap.
 *
 * User text (body, names, the reply) carries ``dir="auto"`` so an LTR
 * moment reads right in an RTL UI and the other way round.
 */
import type { ComponentChildren } from 'preact'
import { useSignal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Avatar } from '@/components/Avatar'
import { openLightbox } from '@/components/ImageLightbox'
import { Modal } from '@/components/Modal'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { PersonButton } from '@/components/PersonButton'
import { openReport } from '@/components/ReportDialog'
import { showToast } from '@/components/Toast'
import { VideoMedia } from '@/components/VideoMedia'
import { confirmDialog } from '@/components/confirm'
import { currentUser } from '@/store/auth'
import { namedPersonLabel, namedPersonPicture } from '@/store/householdUsers'
import { formatLocale, isOne, t } from '@/i18n/i18n'
import type { Moment } from '@/types'
import { renderHashtagged } from './hashtags'
import { setMomentSaved } from './MomentSaveToggle'

/** The reactions a moment offers (the moment page uses the same six). */
export const MOMENT_REACTIONS = ['❤️', '🔥', '😂', '😮', '😢', '👏'] as const

const CONTENT_TRUNCATE_AT = 280
const REPLY_TRUNCATE_AT = 140

export type ReactionMode = 'own' | 'counted' | 'mine-only'

/** How a card shows reactions — counts only where they are complete. */
export function reactionMode(m: Moment, me: string | undefined): ReactionMode {
  if (m.author_user_id === me) return 'own'
  if (m.received_via === 'self') return 'counted'
  if (m.reaction_totals_at != null) return 'counted'
  if (m.reactions_reach === 'household') return 'counted'
  return 'mine-only'
}

/** The fields an optimistic reaction change touches. */
export function withReaction(
  m: Moment, next: string | null,
): Pick<Moment, 'my_reaction' | 'reaction_counts' | 'reaction_count'> {
  const counts: Record<string, number> = { ...(m.reaction_counts ?? {}) }
  const prev = m.my_reaction ?? null
  if (prev) {
    counts[prev] = (counts[prev] ?? 0) - 1
    if (counts[prev] <= 0) delete counts[prev]
  }
  if (next) counts[next] = (counts[next] ?? 0) + 1
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  return { my_reaction: next, reaction_counts: counts, reaction_count: total }
}

/** ``5 min`` / ``5 Min.`` / ``5j`` — the UI language's narrow form. */
export function relativeTime(iso: string): string {
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  const m = Math.floor((Date.now() - dt.getTime()) / 60_000)
  if (m < 1) return t('moment.time.now')
  if (m < 60) return narrowUnit(m, 'minute')
  const h = Math.floor(m / 60)
  if (h < 24) return narrowUnit(h, 'hour')
  const d = Math.floor(h / 24)
  if (d < 7) return narrowUnit(d, 'day')
  return dt.toLocaleDateString(formatLocale(), { month: 'short', day: 'numeric' })
}

const NARROW_FALLBACK = {
  minute: 'time.minutes_ago_short',
  hour: 'time.hours_ago_short',
  day: 'time.days_ago_short',
} as const

function narrowUnit(n: number, unit: 'minute' | 'hour' | 'day'): string {
  try {
    return new Intl.NumberFormat(formatLocale(), {
      style: 'unit', unit, unitDisplay: 'narrow',
    }).format(n)
  } catch {
    // An engine without unit formatting: the translated short form.
    return t(NARROW_FALLBACK[unit], { n: String(n) })
  }
}

/** ``0:04`` — whole seconds, rounded (a 3.6 s clip is 0:04, not 0:03). */
export function clipLength(ms: number): string {
  const s = Math.max(1, Math.round(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** "Berger · paired" / "2 hops away" / "via GFS" — or nothing (your own
 *  household). */
function provenance(m: Moment): string | null {
  if (m.received_via === 'gfs') return t('moment.inbox.via_gfs_chip')
  if (m.received_via !== 'household') return null
  const hops = m.hop_count ?? 1
  if (hops > 1) return t('moment.card.hops_away', { n: String(hops) })
  const household = m.author?.household
  return household
    ? t('moment.card.paired', { household })
    : t('moment.card.paired_unnamed')
}

interface Props {
  m: Moment
  me: string | undefined
  /** Merge a change (reaction, save) into the host's list. */
  onPatch: (id: string, patch: Partial<Moment>) => void
  /** The author deleted it. */
  onDeleted: (id: string) => void
}

export function MomentCard({ m, me, onPatch, onDeleted }: Props) {
  const loc = useLocation()
  const expanded = useSignal(false)
  const whoSees = useSignal(false)
  const mine = m.author_user_id === me
  const authorName = mine ? t('moment.card.you') : namedPersonLabel(m.author_user_id, m.author)
  const realName = namedPersonLabel(m.author_user_id, m.author)
  const authorPicture = namedPersonPicture(m.author_user_id, m.author)
  const hint = () => ({
    display_name: realName,
    picture_url: authorPicture,
    ...(m.author?.bio ? { bio: m.author.bio } : {}),
  })
  const where = provenance(m)
  const longContent = m.content.length > CONTENT_TRUNCATE_AT
  const text = longContent && !expanded.value
    ? m.content.slice(0, CONTENT_TRUNCATE_AT) + '…'
    : m.content
  const openTag = (tag: string, ev: MouseEvent) => {
    ev.preventDefault()
    loc.route(`/momentum?tag=${encodeURIComponent(tag)}`)
  }

  const copyText = async () => {
    try {
      await navigator.clipboard.writeText(m.content)
      showToast(t('moment.card.copied'), 'success')
    } catch {
      showToast(t('moment.card.copy_failed'), 'error')
    }
  }
  const remove = async () => {
    if (!await confirmDialog(t('moment.detail.delete_confirm'), { destructive: true })) return
    try {
      await api.delete(`/api/moments/${m.id}`)
      showToast(t('moment.detail.deleted'), 'info')
      onDeleted(m.id)
    } catch (err: unknown) {
      showToast(t('highlight.viewer.delete_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }
  const toggleSave = async () => {
    const next = m.saved_by_me !== true
    if (await setMomentSaved(m.id, next)) onPatch(m.id, { saved_by_me: next })
  }

  const items: MenuItem[] = [
    { key: 'reply', label: t('moment.card.reply'), onSelect: () => loc.route(`/momentum/${m.id}/reply`) },
  ]
  if (m.content) items.push({ key: 'copy', label: t('moment.card.copy_text'), onSelect: () => void copyText() })
  if (mine) {
    items.push({ key: 'who', label: t('moment.card.who_sees'), onSelect: () => { whoSees.value = true } })
    items.push({ key: 'delete', label: t('common.delete'), danger: true, onSelect: () => void remove() })
  } else {
    if (m.saved_by_me || m.can_save) {
      items.push({
        key: 'save',
        label: t(m.saved_by_me ? 'moment.save.saved' : 'moment.save.save'),
        onSelect: () => void toggleSave(),
      })
    }
    items.push({ key: 'report', label: t('report.action'), onSelect: () => openReport('moment', m.id) })
  }

  return (
    <article class="sh-moment-card" aria-label={t('moment.card.aria', { name: realName })}>
      <header class="sh-moment-card__head">
        <PersonButton userId={m.author_user_id} hint={hint} class="sh-person-btn--avatar" decorative>
          <Avatar name={realName} src={authorPicture} size={40} />
        </PersonButton>
        <div class="sh-moment-card__who">
          <PersonButton userId={m.author_user_id} hint={hint}
            class="sh-person-btn--name sh-moment-card__namebtn">
            <bdi class="sh-moment-card__name" dir="auto">{authorName}</bdi>
          </PersonButton>
          <span class="sh-moment-card__meta">
            <a href={addBase(`/momentum/${m.id}`)} class="sh-moment-card__time">
              <time dateTime={m.created_at}>{relativeTime(m.created_at)}</time>
            </a>
            {where && (
              <span title={m.received_via === 'gfs' ? t('moment.inbox.via_gfs') : undefined}>
                {' · '}<bdi dir="auto">{where}</bdi>
              </span>
            )}
          </span>
        </div>
        <OverflowMenu label={t('moment.card.actions')} items={items}
          triggerClass="sh-moment-card__more" wrapClass="sh-moment-card__menu">
          <span aria-hidden="true">⋯</span>
        </OverflowMenu>
      </header>

      {m.content && (
        <p class="sh-moment-card__text" dir="auto">
          {renderHashtagged(text, openTag)}
          {longContent && !expanded.value && (
            <button type="button" class="sh-moment-card__showmore"
              onClick={() => { expanded.value = true }}>
              {t('moment.inbox.show_more')}
            </button>
          )}
        </p>
      )}

      {m.media_type === 'image' && m.media_url && (
        <button
          type="button"
          class="sh-moment-card__media"
          aria-label={t('moment.open_photo')}
          onClick={() => openLightbox({
            items: [{ url: m.media_url!, item_type: 'photo', caption: m.content || null }],
          })}
        >
          <img src={m.media_url} loading="lazy"
            alt={m.content ? '' : t('moment.photo_from', { name: realName })} />
        </button>
      )}
      {m.media_type === 'video' && m.media_url && <MomentVideo m={m} />}

      <footer class="sh-moment-card__foot">
        <Reactions m={m} me={me} onPatch={onPatch} />
        <a class="sh-moment-card__reply-btn" href={addBase(`/momentum/${m.id}/reply`)}>
          <ReplyIcon />
          <span>{t('moment.card.reply')}</span>
        </a>
      </footer>
      {reactionMode(m, me) === 'counted' && m.reactions_reach === 'household' && (
        <p id={`sh-moment-reach-${m.id}`} class="sh-muted sh-moment-card__reach">
          {t('moment.detail.reactions_household_only')}
        </p>
      )}

      {m.reply_count > 0 && <Thread m={m} me={me} />}

      {mine && (
        <Modal open={whoSees.value} onClose={() => { whoSees.value = false }}
          title={t('moment.card.who_sees')}>
          <p>{t(currentUser.value?.protected ? 'moment.card.who_sees_protected' : 'moment.card.who_sees_body')}</p>
          {m.is_public && <p>{t('moment.card.who_sees_public')}</p>}
        </Modal>
      )}
    </article>
  )
}

function ReplyIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function Reactions({ m, me, onPatch }: Pick<Props, 'm' | 'me' | 'onPatch'>) {
  const mode = reactionMode(m, me)
  const counts = m.reaction_counts ?? {}
  const total = m.reaction_count

  if (mode === 'own') {
    if (total <= 0) return <span class="sh-moment-card__rx" />
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([e]) => e)
    return (
      <span class="sh-moment-card__rx">
        <a class="sh-moment-card__rxsum" href={addBase(`/momentum/${m.id}`)}>
          <span class="sh-moment-card__emoji" aria-hidden="true">{top.join('')}</span>
          {t(isOne(total) ? 'moment.yours.reactions_one' : 'moment.yours.reactions', { n: String(total) })}
        </a>
      </span>
    )
  }

  const react = async (next: string | null) => {
    const before = {
      my_reaction: m.my_reaction ?? null,
      reaction_counts: m.reaction_counts,
      reaction_count: m.reaction_count,
    }
    onPatch(m.id, withReaction(m, next))
    try {
      if (next) await api.put(`/api/moments/${m.id}/reaction`, { emoji: next })
      else await api.delete(`/api/moments/${m.id}/reaction`)
    } catch (err: unknown) {
      onPatch(m.id, before)
      showToast(t('highlight.viewer.reaction_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }
  const noteId = m.reactions_reach === 'household' ? `sh-moment-reach-${m.id}` : undefined
  const mineEmoji = m.my_reaction ?? null

  const menu = (label: string, content: ComponentChildren, pressed: boolean) => (
    <OverflowMenu
      label={label}
      bareTrigger
      triggerClass={pressed ? 'sh-moment-rx sh-moment-rx--mine' : 'sh-moment-rx sh-moment-rx--add'}
      menuClass="sh-moment-rxmenu"
      wrapClass="sh-moment-rxwrap"
      triggerDescribedBy={noteId}
      items={[
        ...MOMENT_REACTIONS.map((e): MenuItem => ({
          key: e, label: e, checked: e === mineEmoji, class: 'sh-moment-rxmenu__emoji',
          onSelect: () => { if (e !== mineEmoji) void react(e) },
        })),
        ...(mineEmoji ? [{ key: 'remove', label: t('moment.card.remove_reaction'), onSelect: () => void react(null) }] : []),
      ]}
    >
      {content}
    </OverflowMenu>
  )

  if (mode === 'mine-only') {
    return (
      <span class="sh-moment-card__rx">
        {mineEmoji
          ? menu(t('moment.card.your_reaction', { emoji: mineEmoji }), (
              <><span class="sh-moment-card__emoji" aria-hidden="true">{mineEmoji}</span>
                <span>{t('moment.card.you')}</span></>
            ), true)
          : menu(t('moment.card.react'), <ReactFace />, false)}
      </span>
    )
  }

  const entries = Object.entries(counts).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1])
  return (
    <span class="sh-moment-card__rx">
      {entries.map(([emoji, n]) => {
        const isMine = emoji === mineEmoji
        return (
          <button
            key={emoji}
            type="button"
            class={isMine ? 'sh-moment-rx sh-moment-rx--mine' : 'sh-moment-rx'}
            aria-pressed={isMine}
            aria-label={t(isMine ? 'moment.card.rx_chip_mine' : 'moment.card.rx_chip', { emoji, n: String(n) })}
            aria-describedby={noteId}
            onClick={() => void react(isMine ? null : emoji)}
          >
            <span class="sh-moment-card__emoji" aria-hidden="true">{emoji}</span>
            <span aria-hidden="true">{n}</span>
          </button>
        )
      })}
      {!mineEmoji && menu(t('moment.card.react'), <ReactFace />, false)}
    </span>
  )
}

function ReactFace() {
  return (
    <>
      <span class="sh-moment-card__emoji" aria-hidden="true">🙂</span>
      <span>{t('moment.card.react')}</span>
    </>
  )
}

function MomentVideo({ m }: { m: Moment }) {
  const playing = useSignal(false)
  const len = m.duration_ms ? clipLength(m.duration_ms) : null
  const notReady = m.media_status === 'processing' || m.media_status === 'failed'
  if (notReady) {
    return (
      <VideoMedia src={m.media_url!} poster={m.media_thumbnail_url}
        mediaStatus={m.media_status} class="sh-moment-card__video" />
    )
  }
  if (playing.value) {
    return (
      <video class="sh-moment-card__video" src={m.media_url!} poster={m.media_thumbnail_url}
        controls autoPlay playsInline preload="metadata" />
    )
  }
  return (
    <button
      type="button"
      class="sh-moment-card__media sh-moment-card__poster"
      aria-label={len ? t('moment.card.play_video_len', { len }) : t('moment.card.play_video')}
      onClick={() => { playing.value = true }}
    >
      {m.media_thumbnail_url
        ? <img src={m.media_thumbnail_url} alt="" loading="lazy" />
        : <span class="sh-moment-card__poster-blank" />}
      <span class="sh-moment-card__play" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" focusable="false">
          <path d="M8 5.5v13l11-6.5z" />
        </svg>
      </span>
      {len && <span class="sh-moment-card__len" aria-hidden="true">{len}</span>}
    </button>
  )
}

function Thread({ m, me }: { m: Moment; me: string | undefined }) {
  const r = m.latest_reply
  const label = t(isOne(m.reply_count) ? 'moment.detail.replies_one' : 'moment.detail.replies', { n: String(m.reply_count) })
  const name = r ? (r.author_user_id === me ? t('moment.card.you') : namedPersonLabel(r.author_user_id, r.author)) : ''
  const replyText = r && r.content.length > REPLY_TRUNCATE_AT
    ? r.content.slice(0, REPLY_TRUNCATE_AT) + '…'
    : r?.content ?? ''
  return (
    <div class="sh-moment-card__thread">
      <a class="sh-moment-card__replies" href={addBase(`/momentum/${m.id}`)}>{label}</a>
      {r && (
        <div class="sh-moment-card__reply">
          <Avatar name={namedPersonLabel(r.author_user_id, r.author)}
            src={namedPersonPicture(r.author_user_id, r.author)} size={24} />
          <div class="sh-moment-card__reply-body">
            <span class="sh-moment-card__reply-head">
              <bdi dir="auto" class="sh-moment-card__reply-name">{name}</bdi>
              <span class="sh-muted"> · {relativeTime(r.created_at)}</span>
            </span>
            {replyText
              ? <span class="sh-moment-card__reply-text" dir="auto">{replyText}</span>
              : <span class="sh-muted">{t(r.media_type === 'video' ? 'moment.card.reply_video' : 'moment.card.reply_photo')}</span>}
          </div>
        </div>
      )}
    </div>
  )
}
