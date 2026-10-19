/**
 * SpaceNeedsYou — the "Needs you" card under the space hero, for owners,
 * admins and moderators. One card, a 3 px primary start border, a
 * sentence-case heading with a filled count, neutral icons, one row per
 * thing waiting, each with a chevron:
 *
 * - each open **proposal** (owners / admins) — opens the vote dialog. Its
 *   line says the tally and whether the viewer already voted: the
 *   proposer and anyone who voted are never asked again (bug 9);
 * - items **waiting for review** and open **reports** → Moderation;
 * - **join requests** (owners / admins) → Moderation;
 * - member households on an **older version** (owners / admins) — opens
 *   the details (which features lag, whom the space waits on).
 *
 * Nothing waiting → no card. The counts come from the space detail's
 * ``attention`` (``store/spaceAttention``); the page re-reads it on the
 * frames that change them, and after a vote here.
 */
import type { JSX } from 'preact'
import { useState } from 'preact/hooks'
import { addBase } from '@/baseUrl'
import { isOne, t } from '@/i18n/i18n'
import { Modal } from '@/components/Modal'
import {
  SpaceProposalDialog, canVoteOn, describeProposal,
} from '@/components/SpaceProposalDialog'
import { SpaceVersionDetails, type SpaceCompat } from '@/components/SpaceVersionNotice'
import { needsYouTotal, type AttentionProposal, type SpaceAttention } from '@/store/spaceAttention'
import { spaceSectionPath } from './spaceTabs'

interface Props {
  spaceId: string
  attention: SpaceAttention | null
  /** Member households behind on the protocol (owners / admins). */
  compat: SpaceCompat | null
  /** Settings authority — may vote on proposals. */
  canVote: boolean
  isOwner: boolean
  /** Re-read the space detail (after a vote). */
  onChanged: () => void
}

const SVG = {
  viewBox: '0 0 24 24', width: 20, height: 20, fill: 'none', stroke: 'currentColor',
  'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  'aria-hidden': 'true', focusable: 'false',
} as const

const ICONS = {
  globe: <svg {...SVG}><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></svg>,
  trash: <svg {...SVG}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></svg>,
  person: <svg {...SVG}><circle cx="12" cy="8" r="3.5" /><path d="M5 20a7 7 0 0 1 14 0" /></svg>,
  shield: <svg {...SVG}><path d="M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z" /></svg>,
  flag: <svg {...SVG}><path d="M5 21V4M5 4h11l-2 4 2 4H5" /></svg>,
  people: (
    <svg {...SVG}>
      <circle cx="9" cy="9" r="3.2" /><path d="M3 20a6 6 0 0 1 12 0" />
      <circle cx="17" cy="7" r="2.4" /><path d="M14.5 13.5A4.6 4.6 0 0 1 21 19" />
    </svg>
  ),
  plug: <svg {...SVG}><path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4" /></svg>,
  chevron: <svg {...SVG} class="sh-flip-rtl"><path d="m9 6 6 6-6 6" /></svg>,
} satisfies Record<string, JSX.Element>

function proposalIcon(p: AttentionProposal): JSX.Element {
  if (p.action === 'dissolve') return ICONS.trash
  if (p.action === 'set_public_tier') return ICONS.globe
  return ICONS.person
}

/** "1 of 2 approvals · You voted" / "… · Needs your vote" / "Waiting for
 *  the space owner". */
export function proposalMeta(p: AttentionProposal, canVote: boolean, isOwner: boolean): string {
  const tally = t('space.needs.tally', { approvals: String(p.approvals), required: String(p.required) })
  if (p.voted_by_me) return `${tally} · ${t('space.needs.you_voted')}`
  if (canVoteOn(p, canVote, isOwner)) return `${tally} · ${t('space.needs.your_vote')}`
  return p.owner_only ? t('space.proposal.waiting_owner') : `${tally} · ${t('space.needs.waiting_admins')}`
}

/** "2 households need an update" — or, when the lagging households
 *  aren't named, that some features don't reach everyone yet. */
function versionMeta(compat: SpaceCompat): string {
  const behind = compat.behind_members.length
  if (behind === 0) return t('space.needs.version_meta_any')
  return t(isOne(behind) ? 'space.needs.version_meta_one' : 'space.needs.version_meta', { count: String(behind) })
}


export function SpaceNeedsYou({ spaceId, attention: a, compat, canVote, isOwner, onChanged }: Props) {
  const [proposalId, setProposalId] = useState<string | null>(null)
  const [versionOpen, setVersionOpen] = useState(false)
  // The same number as More's badge (``needsYouTotal``). The card still
  // lists a proposal I made or voted on (its tally) and households on an
  // older version, which wait for nobody's decision.
  const count = needsYouTotal(a, spaceId, { canVote, isOwner })
  if (count === 0 && !compat && !(a?.proposals.length)) return null
  const moderation = addBase(spaceSectionPath(spaceId, 'moderation'))
  const open = a?.proposals.find((p) => p.id === proposalId) ?? null

  const body = (icon: JSX.Element, title: string, meta: string) => (
    <>
      <span class="sh-needs-you__icon">{icon}</span>
      <span class="sh-needs-you__text">
        <span class="sh-needs-you__title">{title}</span>
        <span class="sh-needs-you__meta">{meta}</span>
      </span>
      <span class="sh-needs-you__chevron">{ICONS.chevron}</span>
    </>
  )

  return (
    <section class="sh-needs-you" aria-labelledby={`needs-you-${spaceId}`}>
      <h2 class="sh-needs-you__heading" id={`needs-you-${spaceId}`}>
        {t('space.needs.title')}
        {count > 0 && (
          <>
            <span class="sh-space-badge sh-space-badge--needs" aria-hidden="true">{count > 99 ? '99+' : count}</span>
            <span class="sr-only">{', '}{t('space.needs.count', { count: String(count) })}</span>
          </>
        )}
      </h2>
      <ul class="sh-needs-you__list">
        {a?.proposals.map((p) => (
          <li key={p.id}>
            <button type="button" class="sh-needs-you__row" aria-haspopup="dialog"
                    data-row="proposal" onClick={() => setProposalId(p.id)}>
              {body(proposalIcon(p), t('space.needs.proposal', { action: describeProposal(p) }),
                    proposalMeta(p, canVote, isOwner))}
            </button>
          </li>
        ))}
        {a && a.review > 0 && (
          <li>
            <a class="sh-needs-you__row" href={moderation} data-row="review">
              {body(ICONS.shield,
                    t(isOne(a.review) ? 'space.needs.review_one' : 'space.needs.review', { count: String(a.review) }),
                    t('space.needs.review_meta'))}
            </a>
          </li>
        )}
        {a && a.reports > 0 && (
          <li>
            <a class="sh-needs-you__row" href={moderation} data-row="reports">
              {body(ICONS.flag,
                    t(isOne(a.reports) ? 'space.more.reports_one' : 'space.more.reports', { count: String(a.reports) }),
                    t('space.needs.reports_meta'))}
            </a>
          </li>
        )}
        {a && a.joinRequests > 0 && (
          <li>
            <a class="sh-needs-you__row" href={moderation} data-row="join">
              {body(ICONS.people,
                    t(isOne(a.joinRequests) ? 'space.more.join_one' : 'space.more.join', { count: String(a.joinRequests) }),
                    t('space.needs.join_meta'))}
            </a>
          </li>
        )}
        {compat && (
          <li>
            <button type="button" class="sh-needs-you__row" aria-haspopup="dialog"
                    data-row="version" onClick={() => setVersionOpen(true)}>
              {body(ICONS.plug, t('space.version_banner.title'), versionMeta(compat))}
            </button>
          </li>
        )}
      </ul>
      {open && (
        <SpaceProposalDialog
          spaceId={spaceId}
          proposal={open}
          canVote={canVote}
          isOwner={isOwner}
          onClose={() => setProposalId(null)}
          onVoted={onChanged}
        />
      )}
      {versionOpen && compat && (
        <Modal open onClose={() => setVersionOpen(false)} title={t('space.version_banner.title')}>
          <SpaceVersionDetails compat={compat} />
        </Modal>
      )}
    </section>
  )
}
