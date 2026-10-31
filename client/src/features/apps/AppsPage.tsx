/**
 * AppsPage — "Apps" with a **Your apps | Store** switch beside the title
 * (owner's mockup, 2026-10-31).
 *
 * **Your apps**, top to bottom, each part only when it has something:
 *  - **Needs you** for apps: game invites waiting for you; for admins the
 *    requests members sent ("Milo asked for Chess").
 *  - **Updates** (admins): "has an update", from → to, a chip when it asks
 *    for a new permission, What's new and Update / Review update.
 *  - **In your household**: Open, who added it and when, Off + Turn on,
 *    and for admins a ⋯ menu (about, turn on / off, minimum age,
 *    uninstall). Search at 8 apps (D1).
 *  - **More apps**: a few apps the household could add, and a way to the
 *    Store.
 *
 * **Store**: search, then a card grid (3 / 2 / 1 columns) — what each
 * app can do in Social Home's own words, who it is for, size and date,
 * and Install (admins, through the consent dialog) or Ask an admin
 * (members, D4). Filter chips at 12 apps.
 *
 * The view is a deep link (``?tab=store``), remembered per viewer, and
 * ``?app=<id>`` (a "new app" notification) opens the view holding that
 * app and picks its entry out (``appsTab.ts``).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { ComponentChildren } from 'preact'
import { useLocation } from 'preact-iso'
import { t, isOne, locale, formatLocale } from '@/i18n/i18n'
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
  catalogLoaded,
  catalogSourceHost,
  loadInstalled,
  loadCatalog,
  loadUpdates,
  uninstallApp,
  loadUninstallPreview,
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
  type UninstallPreview,
} from '@/store/apps'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { NeedsYouSection } from '@/features/notifications/NeedsYouSection'
import { CatalogFreshnessNote, catalogFreshnessText } from './CatalogFreshnessNote'
import { AppIcon, RatingChip, UpdateButton, aboutPath } from './AppDialogs'
import { permissionsOf, shortDay, versionDay } from './appFacts'
import { AppsTabs, panelId, tabId } from './AppsTabs'
import { pickTab, rememberTab, type AppsTab } from './appsTab'
import { StoreCard } from './StoreCard'

/** Your apps: the household's app count at which its search box appears
 *  (D1). The Store always has one. */
export const SEARCH_AT = 8
/** Catalog size at which the Store's filter chips appear (D1). */
export const FILTERS_AT = 12
/** How many apps the "More apps" teaser on Your apps shows. */
export const TEASER_MAX = 3

/** The Needs-you rows the Apps page shows on top. */
const APP_NEEDS: ReadonlySet<string> = new Set(['app_challenge', 'app_challenge_continue', 'app_challenge_waiting', 'app_request'])

type Filter = 'all' | 'together' | 'solo'

export default function AppsPage() {
  useTitle(t('nav.apps'))
  const isAdmin = !!currentUser.value?.is_admin
  const { query: q, route } = useLocation()
  // ``?app=<id>`` — a "new app" notification picks the app's entry out.
  const picked = q.app ?? null
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  // The view in use. ``null`` until the lists answered (the default
  // depends on them); then pinned, so adding the household's first app
  // from the Store doesn't throw the page over to Your apps. A new
  // ``?tab=`` (a link) wins again.
  const [chosen, setChosen] = useState<AppsTab | null>(null)
  // The page asked for the household's list (``appsLoading`` is still
  // ``false`` on the very first render, before that).
  const [asked, setAsked] = useState(false)
  useEffect(() => { setChosen(null) }, [q.tab])

  useEffect(() => {
    void loadInstalled()
    setAsked(true)
    void loadUpdates()
    void loadCatalog()
    if (isAdmin) void loadProtectionStatus()
  }, [])

  const lang = locale.value || 'en'
  const entries = catalog.value
  const byId = new Map(entries.map(e => [e.app_id, e]))
  const installed = isAdmin ? installedApps.value : installedApps.value.filter(a => a.enabled)
  const installedById = new Map(installedApps.value.map(a => [a.app_id, a]))
  const more = entries.filter(e => !installedById.has(e.app_id))
  const listed = asked && !appsLoading.value
  const nothingAdded = listed && installedApps.value.length === 0 && !appsError.value
  const catalogAnswered = catalogLoaded.value || !!catalogError.value || !catalogEnabled.value

  const tab = chosen ?? pickTab({
    query: q.tab,
    picked,
    installedIds: new Set(installed.map(a => a.app_id)),
    catalogIds: new Set(entries.map(e => e.app_id)),
    nothingAdded,
  })
  useEffect(() => {
    if (chosen === null && !q.tab && listed && catalogAnswered) setChosen(tab)
  }, [chosen, q.tab, listed, catalogAnswered, tab])
  const select = (next: AppsTab) => {
    setChosen(next)
    rememberTab(next)
    // Each view has its own search: a word typed in one never hides the
    // other's list.
    setQuery('')
    setFilter('all')
    // Replaces the entry (a switch isn't a page); drops ``?app=``.
    route(`/apps?tab=${next}`, true)
  }
  // "See all in the Store": switch, and land on the Store tab.
  const toStore = () => {
    select('store')
    setTimeout(() => document.getElementById(tabId('store'))?.focus(), 0)
  }

  const needle = query.trim().toLocaleLowerCase()
  const matches = (name: string, description: string) =>
    !needle || name.toLocaleLowerCase().includes(needle) || description.toLocaleLowerCase().includes(needle)
  const showSearch = tab === 'store' ? entries.length > 0 : installed.length >= SEARCH_AT
  const showFilters = entries.length >= FILTERS_AT

  return (
    <div class="sh-page sh-apps-page">
      <header class="sh-page-header sh-apps-head">
        <div class="sh-apps-head__title">
          <h1 class="sh-page-title">{t('nav.apps')}</h1>
          <AppsTabs value={tab} onChange={select} />
        </div>
        {showSearch && (
          <input
            type="search"
            class="sh-input sh-apps-search"
            placeholder={t('apps.search_placeholder', { n: String(tab === 'store' ? entries.length : installed.length) })}
            aria-label={t('apps.search_aria')}
            value={query}
            onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
          />
        )}
      </header>

      <CatalogFreshnessNote
        retrying={isAdmin && updatesChecking.value}
        onRetry={isAdmin ? () => { void retryCatalog() } : undefined}
      />

      <NeedsYouSection only={APP_NEEDS} />

      <div role="tabpanel" id={panelId(tab)} aria-labelledby={tabId(tab)} tabIndex={0} class="sh-apps-panel">
        {tab === 'yours'
          ? (
            <YourApps
              installed={installed.filter((a) => {
                const entry = byId.get(a.app_id)
                const text = entry ? localized(entry, lang) : { name: a.name, description: '' }
                return matches(text.name, text.description ?? '')
              })}
              all={installed}
              more={more}
              byId={byId}
              isAdmin={isAdmin}
              picked={picked}
              lang={lang}
              searching={!!needle}
              query={query.trim()}
              onStore={toStore}
            />
          )
          : (
            <Store
              entries={entries.filter((e) => {
                const perms = permissionsOf(null, e)
                if (showFilters && filter === 'together' && !perms.includes('people')) return false
                if (showFilters && filter === 'solo' && perms.includes('people')) return false
                const text = localized(e, lang)
                return matches(text.name, text.description ?? '')
              })}
              installedById={installedById}
              isAdmin={isAdmin}
              picked={picked}
              lang={lang}
              query={query.trim()}
              filter={showFilters ? filter : null}
              onFilter={setFilter}
              nothingAdded={nothingAdded}
              total={entries.length}
            />
          )}
      </div>
    </div>
  )
}

// ─── Your apps ───────────────────────────────────────────────────────────────

function YourApps({ installed, all, more, byId, isAdmin, picked, lang, searching, query, onStore }: {
  /** The household's apps the search lets through. */
  installed: InstalledApp[]
  /** All the household's apps this person sees. */
  all: InstalledApp[]
  /** Catalog apps the household hasn't added. */
  more: CatalogEntry[]
  byId: Map<string, CatalogEntry>
  isAdmin: boolean
  picked: string | null
  lang: string
  searching: boolean
  query: string
  onStore: () => void
}) {
  const teaser = more.filter(e => e.compatible !== false).slice(0, TEASER_MAX)
  // The household has no apps at all (a member's list leaves out apps
  // that are off, so this asks the whole list).
  const empty = installedApps.value.length === 0 && !appsLoading.value && !appsError.value
  return (
    <>
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

      {installed.length > 0 && (
        <section class="sh-apps-section" aria-labelledby="sh-apps-h-household">
          <div class="sh-apps-section__head">
            <h2 class="sh-apps-section__title" id="sh-apps-h-household">{t('apps.section.household')}</h2>
            {isAdmin && <CheckUpdatesButton />}
          </div>
          <ul class="sh-apps-list">
            {installed.map(app => (
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

      {searching && installed.length === 0 && all.length > 0 && (
        <p class="sh-apps-empty">{t('apps.search_empty', { query })}</p>
      )}

      {empty && (
        <section class="sh-apps-section sh-apps-none" aria-labelledby="sh-apps-h-none">
          <h2 class="sh-apps-none__title" id="sh-apps-h-none">{t('apps.section.nothing_yet')}</h2>
          <p class="sh-apps-none__text">
            {catalogSourceHost.value
              ? t('apps.intro_operator', { host: catalogSourceHost.value })
              : t('apps.intro')}
          </p>
          {catalogEnabled.value && <Button onClick={onStore}>{t('apps.store.browse')}</Button>}
        </section>
      )}

      {!searching && teaser.length > 0 && !empty && (
        <section class="sh-apps-section" aria-labelledby="sh-apps-h-more">
          <div class="sh-apps-section__head">
            <h2 class="sh-apps-section__title" id="sh-apps-h-more">{t('apps.section.more')}</h2>
            <button type="button" class="sh-apps-link" onClick={onStore}>
              {t('apps.store.see_all')}
            </button>
          </div>
          <ul class="sh-apps-list">
            {teaser.map(e => <MoreRow key={e.app_id} entry={e} isAdmin={isAdmin} lang={lang} />)}
          </ul>
        </section>
      )}

      {/* The app list turned off or failing: why there is nothing to add. */}
      <CatalogState empty={false} quiet />

      {isAdmin && householdHasProtectedMinor.value && all.length > 0 && (
        <p class="sh-apps-note">{t('apps.protected_note')}</p>
      )}
    </>
  )
}

// ─── Store ───────────────────────────────────────────────────────────────────

function Store({ entries, installedById, isAdmin, picked, lang, query, filter, onFilter, nothingAdded, total }: {
  /** The catalog apps search and filter let through. */
  entries: CatalogEntry[]
  installedById: Map<string, InstalledApp>
  isAdmin: boolean
  picked: string | null
  lang: string
  query: string
  /** ``null``: too few apps for filter chips. */
  filter: Filter | null
  onFilter: (f: Filter) => void
  nothingAdded: boolean
  total: number
}) {
  return (
    <>
      {nothingAdded && total > 0 && (
        <p class="sh-apps-intro">
          {catalogSourceHost.value
            ? t('apps.intro_operator', { host: catalogSourceHost.value })
            : t('apps.intro')}
        </p>
      )}
      {filter && <FilterChips value={filter} onChange={onFilter} />}
      {entries.length > 0 && (
        <ul class="sh-store-grid" aria-label={t('apps.tab.store')}>
          {entries.map(e => (
            <li key={e.app_id}>
              <StoreCard
                entry={e}
                installed={installedById.get(e.app_id) ?? null}
                isAdmin={isAdmin}
                lang={lang}
                picked={e.app_id === picked}
              />
            </li>
          ))}
        </ul>
      )}
      {total > 0 && entries.length === 0 && (
        query
          ? <p class="sh-apps-empty">{t('apps.search_empty', { query })}</p>
          : <p class="sh-apps-empty">{t('apps.filter_empty')}</p>
      )}

      <CatalogState empty={total === 0} />

      {nothingAdded && total > 0 && total < 4 && (
        <p class="sh-apps-note">{t('apps.more_on_the_way')}</p>
      )}
      {/* The intro already says it on a first visit. */}
      {total > 0 && !nothingAdded && (
        <p class="sh-apps-note sh-store-sealed">
          <span aria-hidden="true">🔒 </span>{t('apps.store.sealed')}
        </p>
      )}
      {isAdmin && householdHasProtectedMinor.value && total > 0 && (
        <p class="sh-apps-note">{t('apps.protected_note')}</p>
      )}
    </>
  )
}

/** Admin "Retry" on the stale strip: check again, and refresh the
 *  catalog list once the app list answers. */
async function retryCatalog(): Promise<void> {
  await loadUpdates(true)
  if (catalogReached()) void loadCatalog()
}

/** Catalog loading / failed / turned off / empty — said once, under the lists. */
function CatalogState({ empty, quiet = false }: {
  empty: boolean
  /** Say nothing while it loads (Your apps doesn't wait on it). */
  quiet?: boolean
}) {
  if (catalogLoading.value && catalog.value.length === 0) {
    if (quiet) return null
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
        <p class="sh-apps-row__meta sh-apps-row__meta--facts">
          <span class="sh-nowrap">{versionDay(update.current_version)} → {versionDay(update.latest_version)}</span>
          {update.compatible !== false && (
            asks > 0
              ? <span class="sh-store-chip sh-store-chip--asks">{t(isOne(asks) ? 'apps.update_new_permission_one' : 'apps.update_new_permissions', { n: String(asks) })}</span>
              : <span>{t('apps.update_same_permissions')}</span>
          )}
        </p>
      </div>
      <div class="sh-apps-row__actions">
        <button
          type="button"
          class="sh-apps-link"
          onClick={() => route(`${aboutPath(update.app_id)}#whats-new`)}
          aria-label={t('apps.whats_new_named', { name })}
        >
          {t('apps.whats_new')}
        </button>
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
        {(added || update || !app.enabled) && (
          <p class="sh-apps-row__meta sh-apps-row__meta--facts">
            {added && <span>{added}</span>}
            {!app.enabled && <span>{t('apps.off_kept')}</span>}
            {isAdmin && update && <span class="sh-apps-row__update">{t('apps.update_ready')}</span>}
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
        <UninstallDialog app={app} name={name} icon={entry?.icon_url} open={uninstallOpen} onClose={() => setUninstallOpen(false)} />
      )}
    </li>
  )
}

/**
 * The uninstall dialog (D9): what is lost — people with saved data, games
 * and open invites with other households and who will be told — counted by
 * ``GET /api/apps/{id}/uninstall-preview``. **Turn off instead** is the
 * focused default (it keeps everything); **Uninstall and delete** is the
 * destructive second choice. A failed count still lets the admin decide,
 * with the general warning.
 */
export function UninstallDialog({ app, name, icon, open, onClose, onDone }: {
  app: InstalledApp
  name: string
  icon?: string | null
  open: boolean
  onClose: () => void
  onDone?: () => void
}) {
  const [busy, setBusy] = useState<'uninstall' | 'off' | null>(null)
  const [preview, setPreview] = useState<UninstallPreview | null>(null)
  const [failed, setFailed] = useState(false)
  const keepRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    let live = true
    setPreview(null)
    setFailed(false)
    loadUninstallPreview(app.app_id)
      .then(p => { if (live) setPreview(p) })
      .catch(() => { if (live) setFailed(true) })
    // The safe choice has the focus (after the dialog's own focus move).
    const id = setTimeout(() => keepRef.current?.querySelector<HTMLButtonElement>('[data-keep]')?.focus(), 0)
    return () => { live = false; clearTimeout(id) }
  }, [open, app.app_id])

  const uninstall = async () => {
    setBusy('uninstall')
    try {
      await uninstallApp(app.app_id)
      showToast(t('apps.uninstalled', { name }), 'info')
      onClose()
      onDone?.()
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.uninstall_failed'), 'error')
    } finally {
      setBusy(null)
    }
  }
  const turnOff = async () => {
    setBusy('off')
    try {
      await setEnabled(app.app_id, false)
      showToast(t('apps.uninstall.turned_off', { name }), 'success')
      onClose()
    } catch {
      showToast(t('apps.disable_failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('apps.uninstall_title', { name })}
      class="sh-app-consent sh-app-uninstall"
      role="alertdialog"
      header={(titleId) => (
        <>
          <span class="sh-app-uninstall__title">
            <AppIcon src={icon ?? app.icon} size="sm" />
            <h2 id={titleId}>{t('apps.uninstall_title', { name })}</h2>
          </span>
          <button type="button" class="sh-modal-close" onClick={onClose} aria-label={t('modal.close')}>
            &times;
          </button>
        </>
      )}
    >
      <p class="sh-app-uninstall__lead">{t('apps.uninstall.lead', { name })}</p>
      <UninstallLosses preview={preview} failed={failed} name={name} />
      {app.enabled
        ? <p class="sh-app-uninstall__keep">{withBold(t('apps.uninstall.keep', { turn_off: '\u0000' }), t('apps.uninstall.turn_it_off'))}</p>
        : <p class="sh-app-uninstall__keep">{t('apps.uninstall.keep_off', { name })}</p>}
      <div class="sh-modal-actions sh-app-uninstall__actions" ref={keepRef}>
        {/* Destructive first in the DOM (left on a wide screen, below on a
          * phone — CSS); the safe choice is the one focused. */}
        <Button variant="secondary" loading={busy === 'uninstall'} disabled={busy !== null} onClick={() => { void uninstall() }}>
          {t('apps.uninstall.confirm')}
        </Button>
        {app.enabled
          ? <Button data-keep loading={busy === 'off'} disabled={busy !== null} onClick={() => { void turnOff() }}>{t('apps.uninstall.turn_off_instead')}</Button>
          : <Button data-keep onClick={onClose}>{t('apps.uninstall.keep_button', { name })}</Button>}
      </div>
    </Modal>
  )
}

/** "Melanie", "Melanie and Pascal", "A, B and C" in the UI language. */
function joinNames(names: string[]): string {
  try {
    return new Intl.ListFormat(formatLocale(), { type: 'conjunction' }).format(names)
  } catch {
    return names.join(', ')
  }
}

/** The lines of what an uninstall loses. */
export function UninstallLosses({ preview, failed, name }: {
  preview: UninstallPreview | null
  failed: boolean
  name: string
}) {
  if (failed) return <p class="sh-app-uninstall__note">{t('apps.uninstall.unknown')}</p>
  if (!preview) return <p class="sh-app-uninstall__note" role="status">{t('apps.uninstall.checking')}</p>
  const { people, games, invites, told, told_more: more, untold } = preview
  const tables = preview.tables ?? 0
  if (people === 0 && games === 0 && invites === 0 && tables === 0) {
    return <p class="sh-app-uninstall__note">{t('apps.uninstall.nothing', { name })}</p>
  }
  const plural = (key: string, n: number, extra: Record<string, string> = {}) =>
    t(isOne(n) ? `${key}_one` : key, { n: String(n), ...extra })
  const names = [...told, ...(more > 0 ? [plural('apps.uninstall.told_more', more)] : [])]
  const lines: { key: string, glyph: string, text: ComponentChildren }[] = []
  if (people > 0) {
    lines.push({
      key: 'people',
      glyph: '▣',
      text: withBold(plural('apps.uninstall.people', people, { who: '\u0000' }), plural('apps.uninstall.people_n', people)),
    })
  }
  if (games > 0) {
    const tell = untold >= games
      ? t('apps.uninstall.untold_all')
      : [
          names.length > 0 ? t(names.length === 1 ? 'apps.uninstall.told_one' : 'apps.uninstall.told', { names: joinNames(names) }) : '',
          untold > 0 ? plural('apps.uninstall.untold', untold) : '',
        ].filter(Boolean).join(' ')
    lines.push({
      key: 'games',
      glyph: '⇄',
      text: <>{withBold(plural('apps.uninstall.games', games, { what: '\u0000' }), plural('apps.uninstall.games_n', games))}{tell && <> {tell}</>}</>,
    })
  }
  if (tables > 0) {
    lines.push({
      key: 'tables',
      glyph: '⇶',
      text: withBold(plural('apps.uninstall.tables', tables, { what: '\u0000' }), plural('apps.uninstall.tables_n', tables)),
    })
  }
  if (invites > 0) {
    lines.push({
      key: 'invites',
      glyph: '✉',
      text: <>
        {withBold(plural('apps.uninstall.invites', invites, { what: '\u0000' }), plural('apps.uninstall.invites_n', invites))}
        {games === 0 && names.length > 0 && <> {t(names.length === 1 ? 'apps.uninstall.told_one' : 'apps.uninstall.told', { names: joinNames(names) })}</>}
      </>,
    })
  }
  return (
    <ul class="sh-app-perms sh-app-perms--detailed sh-app-uninstall__losses">
      {lines.map(l => (
        <li key={l.key} class="sh-app-perms__item">
          <span class="sh-app-perms__glyph" aria-hidden="true">{l.glyph}</span>
          <span class="sh-app-perms__text sh-app-uninstall__line">{l.text}</span>
        </li>
      ))}
    </ul>
  )
}

/** Render a translated sentence with the ``\u0000`` placeholder swapped
 *  for *bold* in bold, so word order stays the translator's. */
function withBold(sentence: string, bold: ComponentChildren) {
  const [before, after = ''] = sentence.split('\u0000')
  return <>{before}<strong>{bold}</strong>{after}</>
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
      // The household list clips its rows (rounded card, overflow hidden),
      // so the menu floats above the page instead of hanging off the row.
      floating
      menuWidth={240}
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
