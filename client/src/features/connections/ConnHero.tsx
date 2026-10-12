/**
 * ConnHero — the network summary at the top of the page.
 *
 * Variants (``heroVariant`` in ``model.ts``):
 *   problem  "1 of 3 households needs a look" + a sentence about the worst
 *            one + chips that jump to the affected card;
 *   healthy  "Everyone's connected" + a check line;
 *   empty    "Your household, ready to meet the neighbors" + (admin) the
 *            SetupWarning when nobody could reach us yet.
 * Admins get the CTAs; members get the AdminContactNote instead. Version
 * data (chips "needs an update" / "You're on v57") is admin-only.
 */
import type { ComponentChildren } from 'preact'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { ConnChip } from './ConnChip'
import { ConnIcon } from './ConnIcon'
import { HeroConstellation } from './HeroConstellation'
import { formatSince, type Household } from './model'
import { fillNodes } from './richText'

/** The hero's one-sentence explanation of a problem state. */
export function heroSubText(households: Household[], now: number = Date.now()): string | null {
  const down = households.filter((h) => h.status === 'down')
  const warn = households.filter((h) => h.status === 'warn')
  if (down.length > 1) return t('connections.hero.sub_down_many', { n: String(down.length) })
  if (down.length === 1) {
    const c = down[0].conn
    const since = formatSince(c.unreachable_since, 'long', formatLocale(), now)
    return since
      ? t('connections.hero.sub_down', { name: c.display_name, when: since })
      : t('connections.hero.sub_down_now', { name: c.display_name })
  }
  if (warn.length > 1) return t('connections.hero.sub_behind_many', { n: String(warn.length) })
  if (warn.length === 1) return t('connections.hero.sub_behind', { name: warn[0].conn.display_name })
  return null
}

function plural(key: string, n: number, extra: Record<string, string> = {}): string {
  return t(isOne(n) ? `${key}_one` : key, { n: String(n), ...extra })
}

interface Props {
  variant: 'problem' | 'healthy' | 'empty'
  households: Household[]
  isAdmin: boolean
  /** Any GFS connected → one GFS tile in the constellation. */
  gfs: boolean
  /** Our protocol version (admin compat data); 0 = unknown. */
  ours: number
  onPair: () => void
  /** Omitted when there is nobody to introduce us (0 households). */
  onPairVia?: () => void
  /** Focus a household card (problem chips). */
  onJump: (instanceId: string) => void
  setupWarning?: ComponentChildren
  memberNote?: ComponentChildren
}

export function ConnHero(props: Props) {
  const { variant, households, isAdmin, gfs, ours, onPair, onPairVia, onJump, setupWarning, memberNote } = props
  const total = households.length
  const down = households.filter((h) => h.status === 'down')
  const warn = households.filter((h) => h.status === 'warn')
  const problems = down.length + warn.length
  const version = isAdmin && ours > 0 ? `v${ours}` : null

  let headline: ComponentChildren
  let sub: string | null = null
  let status: ComponentChildren = null
  if (variant === 'problem') {
    headline = fillNodes(
      t('connections.hero.problem', { n: String(problems), total: String(total) }),
      { em: <em>{t(isOne(problems) ? 'connections.hero.problem_em_one' : 'connections.hero.problem_em')}</em> },
    )
    sub = heroSubText(households)
    status = (
      <ul class="sh-conn-chips" aria-label={t('connections.hero.chips_label')}>
        {down.length > 0 && (
          <li>
            <ConnChip tone="danger" size="md" icon="off" onClick={() => onJump(down[0].conn.instance_id)}>
              {t('connections.hero.chip_unreachable', { n: String(down.length) })}
            </ConnChip>
          </li>
        )}
        {warn.length > 0 && (
          <li>
            <ConnChip tone="warn" size="md" icon="up" onClick={() => onJump(warn[0].conn.instance_id)}>
              {plural('connections.hero.chip_behind', warn.length)}
            </ConnChip>
          </li>
        )}
        {version && (
          <li>
            <ConnChip size="md">
              {fillNodes(t('connections.hero.your_version'), { v: <strong>{version}</strong> })}
            </ConnChip>
          </li>
        )}
      </ul>
    )
  } else if (variant === 'healthy') {
    headline = fillNodes(t('connections.hero.healthy'), { em: <em>{t('connections.hero.healthy_em')}</em> })
    const count = plural('connections.hero.households', total)
    status = (
      <p class="sh-conn-hero__ok">
        <ConnIcon name="check" />
        <span>
          {version ? `${count} · ${t('connections.hero.your_version', { v: version })}` : count}
        </span>
      </p>
    )
  } else {
    headline = fillNodes(t('connections.hero.empty'), { em: <em>{t('connections.hero.empty_em')}</em> })
    sub = t('connections.hero.empty_sub')
  }

  return (
    <section class="sh-conn-hero" aria-labelledby="sh-conn-hero-title">
      <div class="sh-conn-hero__top">
        <span class="sh-conn-eyebrow">{t('connections.hero.eyebrow')}</span>
        <h2 id="sh-conn-hero-title" class="sh-conn-hero__headline">{headline}</h2>
      </div>
      <div class="sh-conn-hero__art">
        <HeroConstellation households={households} gfs={gfs && variant !== 'empty'} empty={variant === 'empty'} />
      </div>
      <div class="sh-conn-hero__rest">
        {sub && <p class="sh-conn-hero__sub">{sub}</p>}
        {status}
        {setupWarning}
        {isAdmin ? (
          <div class="sh-conn-hero__actions">
            <button type="button" class="sh-btn sh-btn--primary" onClick={onPair}>
              <ConnIcon name="qr" />
              {t('connections.hero.pair')}
            </button>
            {onPairVia && (
              <button
                type="button"
                class="sh-btn sh-btn--secondary"
                aria-label={t('connections.hero.via_long')}
                onClick={onPairVia}
              >
                <ConnIcon name="users" />
                <span class="sh-conn-long">{t('connections.hero.via_long')}</span>
                <span class="sh-conn-short" aria-hidden="true">{t('connections.hero.via_short')}</span>
              </button>
            )}
          </div>
        ) : memberNote}
      </div>
    </section>
  )
}
