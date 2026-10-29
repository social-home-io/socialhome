/**
 * AppsPage — one Apps page that grows with the catalog (D1, R3 §2.1).
 *
 * Top to bottom, each part only when it has something:
 *  - **Needs you** for apps: game invites waiting for you; for admins the
 *    requests members sent ("Milo asked for Chess").
 *  - **Updates** (admins): Update, or "Needs a newer Social Home".
 *  - **In your household**: Open, who added it, and for admins a ⋯ menu
 *    (about, turn on / off, minimum age, uninstall).
 *  - **More apps**: name, one line, a rating chip only when it isn't
 *    "Everyone", and Details (the detail page holds the full trust surface
 *    and the add / ask buttons).
 *
 * Nothing added yet (today's first run): one featured card with the three
 * permission lines and **Add to household** (admins, through the consent
 * dialog) or **Ask an admin** (members). Search appears at 8 catalog apps,
 * filter chips at 12; the layout stays the same.
 *
 * ``?app=<id>`` (a "new app" notification) picks that app's entry out.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t, isOne, locale } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import {
  installedApps,
  catalog,
  updates,
  updatesChecking,
  appsLoading,
  catalogLoading,
  appsError,
  catalogError,
  catalogEnabled,
  catalogSourceHost,
  loadInstalled,
  loadCatalog,
  loadUpdates,
  uninstallApp,
  setEnabled,
  setMinAge,
  loadProtectionStatus,
  householdHasProtectedMinor,
  catalogFreshness,
  catalogReached,
  localized,
  type InstalledApp,
  type AppUpdate,
  type CatalogEntry,
} from '@/store/apps'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { NeedsYouSection } from '@/features/notifications/NeedsYouSection'
import { CatalogFreshnessNote, catalogFreshnessText } from './CatalogFreshnessNote'
import {
  AddAppDialog,
  AppIcon,
  AskButton,
  PermissionList,
  RatingChip,
  UpdateButton,
  aboutPath,
} from './AppDialogs'
import { permissionsOf, shortDay, sizeLabel, sourceLabel, versionLabel } from './appFacts'

/** Catalog size at which the search box appears (D1). */
export const SEARCH_AT = 8
/** Catalog size at which the filter chips appear (D1). */
export const FILTERS_AT = 12

/** The Needs-you rows the Apps page shows on top. */
const APP_NEEDS: ReadonlySet<string> = new Set(['app_challenge', 'app_challenge_continue', 'app_request'])

type Filter = 'all' | 'together' | 'solo'

export default function AppsPage() {
  useTitle(t('nav.apps'))
  const isAdmin = !!currentUser.value?.is_admin
  // ``?app=<id>`` — a "new app" notification picks the app's entry out.
  const picked = useLocation().query.app ?? null
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')

  useEffect(() => {
    void loadInstalled()
    void loadUpdates()
    void loadCatalog()
    if (isAdmin) void loadProtectionStatus()
  }, [])

  const lang = locale.value || 'en'
  const entries = catalog.value
  const byId = new Map(entries.map(e => [e.app_id, e]))
  const installed = isAdmin ? installedApps.value : installedApps.value.filter(a => a.enabled)
  const installedIds = new Set(installedApps.value.map(a => a.app_id))
  const more = entries.filter(e => !installedIds.has(e.app_id))

  const needle = query.trim().toLocaleLowerCase()
  const matches = (name: string, description: string) =>
    !needle || name.toLocaleLowerCase().includes(needle) || description.toLocaleLowerCase().includes(needle)
  const passes = (e: CatalogEntry) => {
    const perms = permissionsOf(null, e)
    if (filter === 'together' && !perms.includes('people')) return false
    if (filter === 'solo' && perms.includes('people')) return false
    const text = localized(e, lang)
    return matches(text.name, text.description)
  }
  const shownInstalled = installed.filter((a) => {
    const entry = byId.get(a.app_id)
    const text = entry ? localized(entry, lang) : { name: a.name, description: '' }
    return matches(text.name, text.description)
  })
  const shownMore = more.filter(passes)

  const firstRun = installedApps.value.length === 0 && !appsLoading.value && !appsError.value
  const featured = firstRun && !needle && filter === 'all'
    ? (more.find(e => e.compatible !== false) ?? more[0] ?? null)
    : null
  const rest = shownMore.filter(e => e !== featured)
  const showSearch = entries.length >= SEARCH_AT
  const showFilters = entries.length >= FILTERS_AT

  return (
    <div class="sh-page sh-apps-page">
      <header class="sh-page-header sh-apps-head">
        <h1 class="sh-page-title">{t('nav.apps')}</h1>
        {showSearch && (
          <input
            type="search"
            class="sh-input sh-apps-search"
            placeholder={t('apps.search_placeholder', { n: String(entries.length) })}
            aria-label={t('apps.search_aria')}
            value={query}
            onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
          />
        )}
      </header>
      {firstRun && (
        <p class="sh-apps-intro">
          {catalogSourceHost.value
            ? t('apps.intro_operator', { host: catalogSourceHost.value })
            : t('apps.intro')}
        </p>
      )}

      <CatalogFreshnessNote
        retrying={isAdmin && updatesChecking.value}
        onRetry={isAdmin ? () => { void retryCatalog() } : undefined}
      />

      <NeedsYouSection only={APP_NEEDS} />

      {isAdmin && updates.value.length > 0 && (
        <UpdatesSection list={updates.value} byId={byId} lang={lang} />
      )}

      {appsLoading.value && (
        <div class="sh-apps-loading" aria-live="polite">{t('common.loading')}</div>
      )}
      {appsError.value && (
        <div class="sh-apps-error" role="alert">
          <p>{appsError.value}</p>
          <Button onClick={() => { void loadInstalled() }}>{t('common.retry')}</Button>
        </div>
      )}

      {shownInstalled.length > 0 && (
        <section class="sh-apps-section" aria-labelledby="sh-apps-h-household">
          <div class="sh-apps-section__head">
            <h2 class="sh-apps-section__title" id="sh-apps-h-household">{t('apps.section.household')}</h2>
            {isAdmin && <CheckUpdatesButton />}
          </div>
          <ul class="sh-apps-list">
            {shownInstalled.map(app => (
              <InstalledRow
                key={app.app_id}
                app={app}
                entry={byId.get(app.app_id) ?? null}
                isAdmin={isAdmin}
                picked={app.app_id === picked}
                update={updates.value.find(u => u.app_id === app.app_id) ?? null}
                lang={lang}
              />
            ))}
          </ul>
        </section>
      )}

      {featured && (
        <section class="sh-apps-section" aria-labelledby="sh-apps-h-first">
          <h2 class="sh-apps-section__title" id="sh-apps-h-first">{t('apps.section.nothing_yet')}</h2>
          <FeaturedCard entry={featured} isAdmin={isAdmin} lang={lang} />
        </section>
      )}

      {(rest.length > 0 || (showFilters && filter !== 'all')) && (
        <section class="sh-apps-section" aria-labelledby="sh-apps-h-more">
          <h2 class="sh-apps-section__title" id="sh-apps-h-more">
            {t('apps.section.more')}
            <span class="sh-apps-section__count"> · {rest.length}</span>
          </h2>
          {showFilters && <FilterChips value={filter} onChange={setFilter} />}
          {rest.length > 0
            ? (
              <ul class="sh-apps-list">
                {rest.map(e => <MoreRow key={e.app_id} entry={e} isAdmin={isAdmin} lang={lang} />)}
              </ul>
            )
            : <p class="sh-muted">{t('apps.filter_empty')}</p>}
        </section>
      )}

      {needle && shownInstalled.length === 0 && shownMore.length === 0 && (
        <p class="sh-apps-empty">{t('apps.search_empty', { query: query.trim() })}</p>
      )}

      <CatalogState empty={entries.length === 0 && installedApps.value.length === 0} />

      {firstRun && entries.length > 0 && entries.length < 4 && (
        <p class="sh-apps-note">{t('apps.more_on_the_way')}</p>
      )}
      {isAdmin && householdHasProtectedMinor.value && (installed.length > 0 || entries.length > 0) && (
        <p class="sh-apps-note">{t('apps.protected_note')}</p>
      )}
    </div>
  )
}

/** Admin "Retry" on the stale strip: check again, and refresh the
 *  catalog list once the app list answers. */
async function retryCatalog(): Promise<void> {
  await loadUpdates(true)
  if (catalogReached()) void loadCatalog()
}

/** Catalog loading / failed / turned off / empty — said once, under the lists. */
function CatalogState({ empty }: { empty: boolean }) {
  if (catalogLoading.value && catalog.value.length === 0) {
    return <div class="sh-apps-loading" aria-live="polite">{t('apps.catalog_loading')}</div>
  }
  if (catalogError.value && catalog.value.length === 0) {
    return (
      <div class="sh-apps-error" role="alert">
        <p>{t('apps.catalog_failed')}</p>
        <Button variant="secondary" onClick={() => { void loadCatalog() }}>{t('common.retry')}</Button>
      </div>
    )
  }
  if (!catalogEnabled.value) {
    return <p class="sh-apps-note">{t('apps.catalog_disabled')}</p>
  }
  if (empty && !catalogLoading.value && !appsLoading.value) {
    return <p class="sh-apps-empty">{t('apps.catalog_empty')}</p>
  }
  return null
}

// ─── Check for updates (kept until updates arrive as a Needs-you row) ───────

function CheckUpdatesButton() {
  const checking = updatesChecking.value
  const handleCheckUpdates = async () => {
    await loadUpdates(true)
    // "All apps are up to date" only after the app list answered.
    const unreached = catalogFreshnessText(catalogFreshness.value)
    if (unreached) {
      showToast(unreached, 'error')
      return
    }
    const count = updates.value.length
    showToast(
      count > 0
        ? t(isOne(count) ? 'apps.updates_available_one' : 'apps.updates_available', { n: String(count) })
        : t('apps.up_to_date'),
      'info',
    )
  }
  return (
    <Button variant="secondary" loading={checking} onClick={() => { void handleCheckUpdates() }}>
      {t('apps.check_updates')}
    </Button>
  )
}

// ─── Updates (admins) ────────────────────────────────────────────────────────

function UpdatesSection({ list, byId, lang }: {
  list: AppUpdate[]
  byId: Map<string, CatalogEntry>
  lang: string
}) {
  return (
    <section class="sh-apps-section" aria-labelledby="sh-apps-h-updates">
      <h2 class="sh-apps-section__title" id="sh-apps-h-updates">{t('apps.section.updates')}</h2>
      <ul class="sh-apps-list">
        {list.map((u) => {
          const entry = byId.get(u.app_id)
          const name = entry ? localized(entry, lang).name : u.name
          return <UpdateRow key={u.app_id} update={u} name={name} icon={entry?.icon_url ?? null} />
        })}
      </ul>
    </section>
  )
}

function UpdateRow({ update, name, icon }: { update: AppUpdate, name: string, icon: string | null }) {
  const { route } = useLocation()
  const asks = update.new_permissions?.length ?? 0
  return (
    <li class="sh-apps-row sh-apps-row--update">
      <AppIcon src={icon} />
      <div class="sh-apps-row__main">
        <h3 class="sh-apps-row__name">{t('apps.update_title', { name })}</h3>
        <p class="sh-apps-row__meta">
          {versionLabel(update.current_version)} → {versionLabel(update.latest_version)}
          {update.compatible !== false && (
            <>
              {' · '}
              {asks > 0
                ? <span class="sh-apps-row__asks">{t(isOne(asks) ? 'apps.update_new_permission_one' : 'apps.update_new_permissions', { n: String(asks) })}</span>
                : t('apps.update_same_permissions')}
            </>
          )}
        </p>
      </div>
      <div class="sh-apps-row__actions">
        <Button
          variant="secondary"
          onClick={() => route(`${aboutPath(update.app_id)}#whats-new`)}
          aria-label={t('apps.whats_new_named', { name })}
        >
          {t('apps.whats_new')}
        </Button>
        {update.compatible === false
          ? <span class="sh-apps-row__note">{t('apps.needs_newer')}</span>
          : <UpdateButton appId={update.app_id} name={name} update={update} />}
      </div>
    </li>
  )
}

// ─── In your household ───────────────────────────────────────────────────────

const minAgeOptions = (): { value: number; label: string }[] => [
  { value: 0,  label: t('apps.min_age.everyone') },
  { value: 13, label: '13+' },
  { value: 16, label: '16+' },
  { value: 18, label: '18+' },
]

function InstalledRow({ app, entry, isAdmin, picked, update, lang }: {
  app: InstalledApp
  entry: CatalogEntry | null
  isAdmin: boolean
  picked: boolean
  update: AppUpdate | null
  lang: string
}) {
  const { route } = useLocation()
  const rowRef = useRef<HTMLLIElement>(null)
  // The entry a notification opened: bring it into view and put focus
  // on it, so a screen reader lands on the app the row named.
  useEffect(() => {
    if (!picked || !rowRef.current) return
    rowRef.current.scrollIntoView?.({ block: 'center' })
    rowRef.current.focus({ preventScroll: true })
  }, [picked])
  const [uninstallOpen, setUninstallOpen] = useState(false)
  const [toggling, setToggling] = useState(false)
  const name = entry ? localized(entry, lang).name : app.name
  const added = app.installed_by_name
    ? t('apps.added_by', { name: app.installed_by_name, date: shortDay(app.installed_at) })
    : shortDay(app.installed_at) ? t('apps.added_on', { date: shortDay(app.installed_at) }) : ''

  const toggle = async () => {
    setToggling(true)
    try {
      await setEnabled(app.app_id, !app.enabled)
    } catch {
      showToast(app.enabled ? t('apps.disable_failed') : t('apps.enable_failed'), 'error')
    } finally {
      setToggling(false)
    }
  }

  return (
    <li
      ref={rowRef}
      class={`sh-apps-row${picked ? ' sh-apps-row--picked' : ''}${app.enabled ? '' : ' sh-apps-row--off'}`}
      tabIndex={picked ? -1 : undefined}
    >
      <AppIcon src={entry?.icon_url ?? app.icon} />
      <div class="sh-apps-row__main">
        <h3 class="sh-apps-row__name">
          <a href={addBase(aboutPath(app.app_id))} class="sh-apps-row__link">{name}</a>
          {isAdmin && !app.enabled && <span class="sh-chip sh-chip--muted">{t('apps.off')}</span>}
          <RatingChip minAge={app.min_age} />
        </h3>
        {(added || update) && (
          <p class="sh-apps-row__meta">
            {added}
            {isAdmin && update && <>{added && ' · '}<span class="sh-apps-row__update">{t('apps.update_ready')}</span></>}
          </p>
        )}
      </div>
      <div class="sh-apps-row__actions">
        {app.enabled
          ? (
            <Button
              onClick={() => route(`/apps/${encodeURIComponent(app.app_id)}`)}
              aria-label={t('apps.open_named', { name })}
            >
              {t('apps.open')}
            </Button>
          )
          : isAdmin && (
            <Button variant="secondary" loading={toggling} onClick={() => { void toggle() }}
              aria-label={t('apps.turn_on_named', { name })}
            >
              {t('apps.turn_on')}
            </Button>
          )}
        {isAdmin && (
          <AppMenu
            app={app}
            name={name}
            toggling={toggling}
            onAbout={() => route(aboutPath(app.app_id))}
            onToggle={() => { void toggle() }}
            onUninstall={() => setUninstallOpen(true)}
          />
        )}
      </div>
      {isAdmin && (
        <UninstallDialog app={app} name={name} open={uninstallOpen} onClose={() => setUninstallOpen(false)} />
      )}
    </li>
  )
}

/** The uninstall confirmation (PR 13 redesigns it). */
export function UninstallDialog({ app, name, open, onClose, onDone }: {
  app: InstalledApp
  name: string
  open: boolean
  onClose: () => void
  onDone?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      await uninstallApp(app.app_id)
      showToast(t('apps.uninstalled', { name }), 'info')
      onClose()
      onDone?.()
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.uninstall_failed'), 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal open={open} onClose={onClose} title={t('apps.uninstall_title', { name })}>
      <p>{boldName(t('apps.uninstall_body', { name: '\u0000' }), name)}</p>
      <div class="sh-modal-actions">
        <Button variant="secondary" onClick={onClose}>{t('common.cancel')}</Button>
        <Button variant="danger" loading={busy} onClick={() => { void run() }}>{t('apps.uninstall')}</Button>
      </div>
    </Modal>
  )
}

/** Render a translated sentence with the ``\u0000`` placeholder swapped
 *  for the bold app name, so word order stays the translator's. */
function boldName(sentence: string, name: string) {
  const [before, after = ''] = sentence.split('\u0000')
  return <>{before}<strong>{name}</strong>{after}</>
}

/**
 * The ⋯ menu holding the low-frequency admin settings. The shared
 * ``OverflowMenu`` gives it the full menu keyboard model. The minimum-age
 * choices are ``menuitemradio`` items, only when the household has a
 * protected account (no point configuring a gate nobody is subject to).
 */
export function AppMenu({ app, name, toggling, onAbout, onToggle, onUninstall }: {
  app: InstalledApp
  name: string
  toggling: boolean
  onAbout?: () => void
  onToggle: () => void
  onUninstall: () => void
}) {
  const [settingAge, setSettingAge] = useState(false)
  const setAge = async (value: number) => {
    if (value === app.min_age) return
    setSettingAge(true)
    try {
      await setMinAge(app.app_id, value)
      showToast(value === 0 ? t('apps.min_age.removed') : t('apps.min_age.set', { age: String(value) }), 'success')
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.min_age.failed'), 'error')
    } finally {
      setSettingAge(false)
    }
  }
  const items: MenuItem[] = [
    ...(onAbout ? [{ key: 'about', label: t('apps.about_menu'), onSelect: onAbout }] : []),
    {
      key: 'toggle',
      label: app.enabled ? t('apps.turn_off') : t('apps.turn_on'),
      disabled: toggling,
      onSelect: onToggle,
    },
    ...(householdHasProtectedMinor.value
      ? minAgeOptions().map((opt): MenuItem => ({
          key: `age-${opt.value}`,
          label: t('apps.min_age.option', { age: opt.label }),
          checked: app.min_age === opt.value,
          disabled: settingAge,
          onSelect: () => { void setAge(opt.value) },
        }))
      : []),
    { key: 'uninstall', label: t('apps.uninstall_menu'), danger: true, onSelect: onUninstall },
  ]
  return (
    <OverflowMenu
      label={t('apps.settings_aria', { name })}
      items={items}
      wrapClass="sh-app-card__menu"
      menuClass="sh-app-menu"
    >
      ⋯
    </OverflowMenu>
  )
}

// ─── More apps ───────────────────────────────────────────────────────────────

function FilterChips({ value, onChange }: { value: Filter, onChange: (f: Filter) => void }) {
  const chips: { key: Filter, label: string }[] = [
    { key: 'all', label: t('apps.filter.all') },
    { key: 'together', label: t('apps.filter.together') },
    { key: 'solo', label: t('apps.filter.solo') },
  ]
  return (
    <div class="sh-apps-filters" role="group" aria-label={t('apps.filter.aria')}>
      {chips.map(c => (
        <button
          key={c.key}
          type="button"
          class={`sh-chip sh-apps-filter${value === c.key ? ' sh-apps-filter--on' : ''}`}
          aria-pressed={value === c.key}
          onClick={() => onChange(c.key)}
        >
          {c.label}
        </button>
      ))}
    </div>
  )
}

function MoreRow({ entry, isAdmin, lang }: { entry: CatalogEntry, isAdmin: boolean, lang: string }) {
  const { route } = useLocation()
  const text = localized(entry, lang)
  return (
    <li class="sh-apps-row">
      <AppIcon src={entry.icon_url} />
      <div class="sh-apps-row__main">
        <h3 class="sh-apps-row__name">
          {text.name}
          <RatingChip minAge={entry.min_age} />
        </h3>
        {text.description && <p class="sh-apps-row__desc">{text.description}</p>}
        {entry.compatible === false && <p class="sh-apps-row__note">{t('apps.needs_newer')}</p>}
      </div>
      <div class="sh-apps-row__actions">
        {!isAdmin && entry.requested && (
          <span class="sh-chip sh-chip--muted sh-app-asked">{t('apps.ask.asked')}</span>
        )}
        <Button
          variant="secondary"
          onClick={() => route(aboutPath(entry.app_id))}
          aria-label={t('apps.details_named', { name: text.name })}
        >
          {t('apps.details')}
        </Button>
      </div>
    </li>
  )
}

/** First run: the one app, with what it can do and the way to add it. */
function FeaturedCard({ entry, isAdmin, lang }: { entry: CatalogEntry, isAdmin: boolean, lang: string }) {
  const { route } = useLocation()
  const [consentOpen, setConsentOpen] = useState(false)
  const text = localized(entry, lang)
  const perms = permissionsOf(null, entry)
  const size = sizeLabel(entry.bundle_size)
  return (
    <div class="sh-apps-featured">
      <AppIcon src={entry.icon_url} size="lg" />
      <div class="sh-apps-featured__body">
        <h3 class="sh-apps-featured__name">{text.name}</h3>
        <p class="sh-app-meta">
          {sourceLabel(catalogSourceHost.value)}
          <RatingChip minAge={entry.min_age} />
          {size && <> · {size}</>}
        </p>
        {text.description && <p class="sh-apps-featured__desc">{text.description}</p>}
        <PermissionList perms={perms} name={text.name} />
        <div class="sh-apps-featured__actions">
          {entry.compatible === false
            ? <span class="sh-apps-row__note">{t('apps.needs_newer')}</span>
            : isAdmin
              ? <Button onClick={() => setConsentOpen(true)}>{t('apps.add_to_household')}</Button>
              : <AskButton entry={entry} name={text.name} variant="primary" />}
          <Button
            variant="secondary"
            onClick={() => route(aboutPath(entry.app_id))}
            aria-label={t('apps.details_named', { name: text.name })}
          >
            {t('apps.details')}
          </Button>
        </div>
      </div>
      {isAdmin && (
        <AddAppDialog
          entry={entry}
          name={text.name}
          permissions={perms}
          open={consentOpen}
          onClose={() => setConsentOpen(false)}
        />
      )}
    </div>
  )
}
