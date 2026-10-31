/**
 * StoreCard — one app in the Store grid.
 *
 * Icon, name, where it comes from ("From Social Home", D6), the
 * description, what it can do in Social Home's own words (``appFacts`` —
 * never the catalog's), then a footer: who it is for (Everyone / 13+),
 * players for a group game, size and the date of the latest version.
 *
 * Actions, by state:
 *  - added and on → "✓ Installed" + **Open**;
 *  - added but turned off → "Off" + **Details** (an admin turns it on
 *    from Your apps or the detail page);
 *  - needs a newer Social Home → says so + **Details**;
 *  - not added → **Details** + **Install** (admins, through the consent
 *    dialog) or **Ask an admin** / "Asked" (members, D4).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t } from '@/i18n/i18n'
import { catalogSourceHost, localized, type CatalogEntry, type InstalledApp } from '@/store/apps'
import { Button } from '@/components/Button'
import { AddAppDialog, AppIcon, AskButton, PermissionList, aboutPath } from './AppDialogs'
import { permissionsOf, ratingLabel, sizeLabel, sourceLabel, versionDay } from './appFacts'

export function StoreCard({ entry, installed, isAdmin, lang, picked = false }: {
  entry: CatalogEntry
  /** The household's copy, when it has the app. */
  installed: InstalledApp | null
  isAdmin: boolean
  lang: string
  /** ``?app=<id>`` picked this app out: scroll to it and focus it. */
  picked?: boolean
}) {
  const { route } = useLocation()
  const [consentOpen, setConsentOpen] = useState(false)
  const ref = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!picked || !ref.current) return
    ref.current.scrollIntoView?.({ block: 'center' })
    ref.current.focus({ preventScroll: true })
  }, [picked])
  // Install became Open: once the card shows it, put focus there.
  const [justAdded, setJustAdded] = useState(false)
  useEffect(() => {
    if (!justAdded || !installed?.enabled) return
    ref.current?.querySelector<HTMLElement>('[data-open]')?.focus()
    setJustAdded(false)
  }, [justAdded, installed?.enabled])

  const text = localized(entry, lang)
  const perms = permissionsOf(installed, entry)
  const size = sizeLabel(entry.bundle_size)
  const day = versionDay(entry.latest_version)
  const minAge = entry.min_age ?? 0
  const players = entry.players
  const headingId = `sh-store-card-${entry.app_id}`

  const details = (
    <Button
      variant="secondary"
      onClick={() => route(aboutPath(entry.app_id))}
      aria-label={t('apps.details_named', { name: text.name })}
    >
      {t('apps.details')}
    </Button>
  )

  let actions
  if (installed?.enabled) {
    actions = (
      <>
        <span class="sh-store-chip sh-store-chip--ok">
          <span aria-hidden="true" class="sh-store-chip__glyph">✓</span>{t('apps.store.installed')}
        </span>
        <Button
          variant="secondary"
          data-open
          onClick={() => route(`/apps/${encodeURIComponent(entry.app_id)}`)}
          aria-label={t('apps.open_named', { name: text.name })}
        >
          {t('apps.open')}
        </Button>
      </>
    )
  } else if (installed) {
    actions = <><span class="sh-store-chip">{t('apps.off')}</span>{details}</>
  } else if (entry.compatible === false) {
    actions = <><span class="sh-store-card__note">{t('apps.needs_newer')}</span>{details}</>
  } else if (isAdmin) {
    actions = (
      <>
        {details}
        <Button
          onClick={() => setConsentOpen(true)}
          aria-label={t('apps.store.install_named', { name: text.name })}
        >
          {t('apps.store.install')}
        </Button>
      </>
    )
  } else {
    actions = <>{details}<AskButton entry={entry} name={text.name} variant="primary" /></>
  }

  return (
    <article
      ref={ref}
      class={`sh-store-card${picked ? ' sh-store-card--picked' : ''}`}
      aria-labelledby={headingId}
      tabIndex={picked ? -1 : undefined}
    >
      <div class="sh-store-card__head">
        <AppIcon src={entry.icon_url} />
        <div class="sh-store-card__title">
          <h3 class="sh-store-card__name" id={headingId}>{text.name}</h3>
          <p class="sh-store-card__pub">{sourceLabel(catalogSourceHost.value)}</p>
        </div>
      </div>
      {text.description && <p class="sh-store-card__desc">{text.description}</p>}
      <PermissionList perms={perms} name={text.name} />
      <div class="sh-store-card__foot">
        <p class="sh-store-card__meta">
          <span
            class={`sh-store-chip${minAge > 0 ? ' sh-store-chip--age' : ''}`}
            title={minAge > 0 ? t('apps.min_age.title', { age: String(minAge) }) : undefined}
          >
            {ratingLabel(minAge)}
          </span>
          {players && (
            <span class="sh-store-card__fact">
              {players.min === players.max
                ? t('space.games.players_exact', { n: String(players.max) })
                : t('space.games.players_range', { min: String(players.min), max: String(players.max) })}
            </span>
          )}
          {size && <span class="sh-store-card__fact">{size}</span>}
          {/* A CalVer version reads as the day it came out ("v1.2" otherwise). */}
          <span class="sh-store-card__fact" title={day.startsWith('v') ? undefined : t('apps.store.updated', { date: day })}>{day}</span>
        </p>
        <div class="sh-store-card__actions">{actions}</div>
      </div>
      {/* Kept while open: adding the app turns this card into "Installed"
        * before the dialog closes. */}
      {isAdmin && (!installed || consentOpen) && (
        <AddAppDialog
          entry={entry}
          name={text.name}
          permissions={perms}
          open={consentOpen}
          onClose={() => setConsentOpen(false)}
          // Install became Open: put focus there, not on the page.
          onAdded={() => setJustAdded(true)}
        />
      )}
    </article>
  )
}
