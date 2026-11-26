/**
 * SpaceBanList — who is banned from a space, in Settings › Moderation
 * (owner decision C, 2026-11-26: the list lives there, not under Members).
 *
 * Owner / admins only (``GET /api/spaces/{id}/bans``). Each row names the
 * person or household, its reason and date, and lifts the ban in one click
 * — except a ban the owner made, which only the owner lifts: an admin sees
 * why instead of a button that would fail. A banned person's row also
 * offers banning their whole household (owner decision B) when it is
 * known, not the owner's household and not banned already.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { formatLocale, t } from '@/i18n/i18n'
import { personLabel } from '@/utils/names'
import { Avatar } from './Avatar'
import { BanDialog, banErrorCopy, type BanTarget } from './BanDialog'
import { Button } from './Button'
import { LoadErrorState } from './LoadErrorState'
import { showToast } from './Toast'

export interface Ban {
  /** ``person``: one person's ban; ``household``: a whole household's. */
  kind?: 'person' | 'household'
  /** A person ban's id (absent on a household ban). */
  user_id?: string
  /** A household ban's household — or a banned person's household, when
   *  known (``null`` otherwise). */
  instance_id?: string | null
  /** That household's name, null if unknown here. */
  household_name?: string | null
  banned_by?: string
  banned_at: string
  reason?: string | null
  /** The space owner made this ban: only the owner lifts it. */
  banned_by_owner?: boolean
  /** Server-resolved: the per-space name, else the person's household
   *  name, else the space roster's — null when nobody here knows them. */
  display_name?: string | null
  /** The viewer's private nickname for them, if any. */
  personal_alias?: string | null
  /** Signed; null with no picture or across a guardian block. */
  picture_url?: string | null
}

export function isHouseholdBan(b: Ban): boolean {
  return b.kind === 'household'
}

/** A banned person's label: the name the server resolved (a remote
 *  person's too), else the viewer's nickname, else "Someone". A banned
 *  household: its name, else "A household". */
export function banName(b: Ban): string {
  if (isHouseholdBan(b)) return b.household_name || t('space.members.unknown_household')
  return personLabel(b.display_name, b.personal_alias)
}

function bannedOn(iso: string): string {
  // SQLite's naive UTC "YYYY-MM-DD HH:MM:SS".
  const d = new Date(iso.includes('T') ? iso : `${iso.replace(' ', 'T')}Z`)
  if (Number.isNaN(d.getTime())) return ''
  return t('bans.banned_on', {
    date: d.toLocaleDateString(formatLocale(), { day: 'numeric', month: 'short', year: 'numeric' }),
  })
}

export function SpaceBanList({ spaceId, viewerIsOwner, ownerInstanceId }: {
  spaceId: string
  /** The viewer owns the space: every ban is theirs to lift. */
  viewerIsOwner: boolean
  /** The owner household — never offered as a household to ban. */
  ownerInstanceId: string | null
}) {
  const [bans, setBans] = useState<Ban[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [banning, setBanning] = useState<BanTarget | null>(null)
  const [lifting, setLifting] = useState<string | null>(null)

  const reload = () => {
    setFailed(false)
    api.get(`/api/spaces/${spaceId}/bans`)
      .then((rows) => setBans(Array.isArray(rows) ? rows as Ban[] : []))
      .catch(() => setFailed(true))
  }
  useEffect(reload, [spaceId])

  if (failed) return <LoadErrorState message={t('bans.load_failed')} onRetry={reload} />
  if (bans === null) return <p class="sh-muted">{t('common.loading')}</p>

  const householdsBanned = new Set(bans.filter(isHouseholdBan).map(b => b.instance_id))
  const key = (b: Ban) => (isHouseholdBan(b) ? `h:${b.instance_id}` : `p:${b.user_id}`)

  const unban = async (b: Ban) => {
    setLifting(key(b))
    try {
      await api.delete(isHouseholdBan(b)
        ? `/api/spaces/${spaceId}/household-bans/${encodeURIComponent(b.instance_id ?? '')}`
        : `/api/spaces/${spaceId}/bans/${encodeURIComponent(b.user_id ?? '')}`)
      showToast(isHouseholdBan(b)
        ? t('ban.unbanned_household', { household: banName(b) })
        : t('ban.unbanned_person', { name: banName(b) }), 'success')
      reload()
    } catch (e: unknown) {
      showToast(banErrorCopy(e), 'error')
    } finally {
      setLifting(null)
    }
  }

  return (
    <div class="sh-ban-list" data-testid="space-bans">
      {bans.length === 0 && <p class="sh-muted" data-testid="bans-empty">{t('bans.empty')}</p>}
      {bans.map(b => {
        const name = banName(b)
        const ownerOnly = !viewerIsOwner && b.banned_by_owner === true
        const household = !isHouseholdBan(b) && b.instance_id
          && b.instance_id !== ownerInstanceId && !householdsBanned.has(b.instance_id)
          ? b.instance_id : null
        const when = bannedOn(b.banned_at)
        return (
          <div key={key(b)} class="sh-member-row sh-member-row--banned"
               data-ban-kind={isHouseholdBan(b) ? 'household' : 'person'}>
            <Avatar name={name} src={b.picture_url ?? null} size={32} />
            <div class="sh-member-info">
              <span class="sh-member-name">{name}</span>
              <span class="sh-badge sh-badge--danger">
                {t(isHouseholdBan(b) ? 'space.members.household_banned_badge' : 'space.members.banned_badge')}
              </span>
              {!isHouseholdBan(b) && b.household_name && (
                <span class="sh-muted">{b.household_name}</span>
              )}
              {b.reason && <span class="sh-ban-reason">{b.reason}</span>}
              {when && <span class="sh-muted sh-member-joined">{when}</span>}
              {ownerOnly && <span class="sh-muted sh-ban-owner-only">{t('error.owner_ban')}</span>}
            </div>
            <div class="sh-ban-row-actions">
              {household && (
                <Button variant="secondary" data-action="ban-their-household"
                        onClick={() => setBanning({
                          kind: 'household', spaceId, instanceId: household,
                          household: b.household_name ?? null, people: [],
                        })}>
                  {t('bans.ban_household')}
                </Button>
              )}
              {!ownerOnly && (
                <Button variant="secondary" loading={lifting === key(b)}
                        disabled={lifting === key(b)}
                        aria-label={t('space.members.unban_aria', { name })}
                        onClick={() => void unban(b)}>
                  {t('space.members.unban')}
                </Button>
              )}
            </div>
          </div>
        )
      })}
      <BanDialog target={banning} onClose={() => setBanning(null)}
                 onBanned={() => { setBanning(null); reload() }} />
    </div>
  )
}
