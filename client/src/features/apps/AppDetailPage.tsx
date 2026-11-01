/**
 * AppDetailPage — ``/apps/:appId/about``: what an app can do, before and
 * after it is added (R3 §2.1 / §2.2). Never launches the app
 * (``/apps/:appId`` does).
 *
 *  - Hero: icon, name, source (D6: "From Social Home" or the operator's
 *    host — never "verified"), rating, languages, description, and the one
 *    action that fits: **Add to household** (admins, through the consent
 *    dialog), **Ask an admin** / **Ask a grown-up** (members), **Open**
 *    once added, or "Needs a newer Social Home".
 *  - What it can do — the plain-word permissions in full sentences — and
 *    the fixed "It can never…" list.
 *  - About (version as a date, size, publisher, rating, languages, needs,
 *    comes from), In your household (who added it), What's new.
 *  - Admins manage an added app here too: Update, turn on / off, minimum
 *    age, uninstall.
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation, useRoute } from 'preact-iso'
import { t, isOne, locale } from '@/i18n/i18n'
import { addBase } from '@/baseUrl'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import {
  installedApps,
  catalog,
  updates,
  appsLoading,
  catalogLoading,
  catalogLoaded,
  catalogSourceHost,
  householdHasProtectedMinor,
  loadInstalled,
  loadCatalog,
  loadUpdates,
  loadProtectionStatus,
  localized,
  setEnabled,
  dismissRequests,
  type AppUpdate,
  type CatalogEntry,
  type InstalledApp,
} from '@/store/apps'
import {
  AddAppDialog,
  AppIcon,
  AskButton,
  PermissionList,
  RatingChip,
  UpdateButton,
  whoCanOpen,
} from './AppDialogs'
import { AppMenu, UninstallDialog } from './AppsPage'
import { dropHandled, loadNeedsYou, needsYou } from '@/features/notifications/needsYou'
import {
  changelogNote,
  languageNames,
  neverList,
  permissionsOf,
  ratingLabel,
  shortDay,
  sizeLabel,
  sourceLabel,
  versionLabel,
} from './appFacts'

export default function AppDetailPage() {
  const appId = decodeURIComponent(useRoute().params.appId ?? '')
  const { route } = useLocation()
  const isAdmin = !!currentUser.value?.is_admin
  const lang = locale.value || 'en'

  useEffect(() => {
    void loadInstalled()
    void loadCatalog()
    if (isAdmin) {
      void loadUpdates()
      void loadProtectionStatus()
      void loadNeedsYou()
    }
  }, [appId, isAdmin])

  const entry = catalog.value.find(e => e.app_id === appId) ?? null
  const installed = installedApps.value.find(a => a.app_id === appId) ?? null
  const name = entry ? localized(entry, lang).name : installed?.name ?? ''
  useTitle(name || t('nav.apps'))

  const back = (
    <a class="sh-apps-back" href={addBase('/apps')}>
      <span aria-hidden="true">←</span>{t('nav.apps')}
    </a>
  )

  if (!entry && !installed) {
    const waiting = appsLoading.value || catalogLoading.value || !catalogLoaded.value
    return (
      <div class="sh-page sh-app-detail">
        {back}
        {waiting
          ? <div class="sh-apps-loading" aria-live="polite">{t('common.loading')}</div>
          : (
            <div class="sh-apps-empty">
              <h1 class="sh-page-title">{t('apps.detail.missing_title')}</h1>
              <p>{t('apps.detail.missing_body')}</p>
            </div>
          )}
      </div>
    )
  }

  const description = entry ? localized(entry, lang).description : ''
  const perms = permissionsOf(installed, entry)
  const minAge = installed ? installed.min_age : (entry?.min_age ?? 0)
  const langs = languageNames(entry?.languages)
  const size = sizeLabel(entry?.bundle_size)
  const update = updates.value.find(u => u.app_id === appId) ?? null

  return (
    <div class="sh-page sh-app-detail">
      {back}
      <section class="sh-app-detail__hero" aria-labelledby="sh-app-detail-name">
        <AppIcon src={entry?.icon_url ?? installed?.icon} size="lg" />
        <div class="sh-app-detail__hero-body">
          <h1 class="sh-app-detail__name" id="sh-app-detail-name">{name}</h1>
          <p class="sh-app-meta">
            {entry && sourceLabel(catalogSourceHost.value)}
            <RatingChip minAge={minAge} />
            {langs.length > 0 && <> · {langs.join(', ')}</>}
          </p>
          {description && <p class="sh-app-detail__desc">{description}</p>}
          <HeroAction
            entry={entry}
            installed={installed}
            name={name}
            isAdmin={isAdmin}
            size={size}
            onOpen={() => route(`/apps/${encodeURIComponent(appId)}`)}
          />
        </div>
      </section>

      <div class="sh-app-detail__grid">
        <div class="sh-app-detail__main">
          <section class="sh-app-detail__card" aria-labelledby="sh-app-detail-can">
            <h2 class="sh-app-detail__h" id="sh-app-detail-can">{t('apps.detail.can_do', { name })}</h2>
            <PermissionList perms={perms} name={name} detailed engines={installed?.engines ?? entry?.engines ?? []} />
            <div class="sh-app-never">
              <h3 class="sh-app-never__h">{t('apps.never.title', { name })}</h3>
              <ul class="sh-app-never__list">
                {neverList().map(line => <li key={line}>{line}</li>)}
              </ul>
            </div>
          </section>
          {entry?.changelog && entry.changelog.length > 0 && (
            <section class="sh-app-detail__card" id="whats-new" aria-labelledby="sh-app-detail-new">
              <h2 class="sh-app-detail__h" id="sh-app-detail-new">{t('apps.whats_new')}</h2>
              <ul class="sh-app-changelog">
                {entry.changelog.map(c => {
                  const note = changelogNote(c, lang)
                  return (
                    <li key={c.version}>
                      <h3 class="sh-app-changelog__v">
                        {versionLabel(c.version)}
                        {/* A same-day fix (``YYYY.M.D.N``) reads apart from the day's first. */}
                        {c.version.split('.').length > 3 && <span class="sh-app-changelog__n"> · {c.version}</span>}
                      </h3>
                      {note && <p class="sh-app-changelog__note">{note}</p>}
                    </li>
                  )
                })}
              </ul>
            </section>
          )}
        </div>
        <aside class="sh-app-detail__side">
          <section class="sh-app-detail__card" aria-labelledby="sh-app-detail-about">
            <h2 class="sh-app-detail__h" id="sh-app-detail-about">{t('apps.detail.about')}</h2>
            <dl class="sh-app-facts">
              <Fact label={t('apps.fact.version')} value={versionLabel(installed?.version ?? entry?.latest_version ?? '')} />
              {size && <Fact label={t('apps.fact.size')} value={size} />}
              {entry?.publisher && <Fact label={t('apps.fact.publisher')} value={entry.publisher} />}
              <Fact label={t('apps.fact.rating')} value={ratingLabel(minAge)} />
              {langs.length > 0 && <Fact label={t('apps.fact.languages')} value={langs.join(', ')} />}
              {entry?.min_core_version && (
                <Fact label={t('apps.fact.needs')} value={t('apps.fact.needs_value', { version: entry.min_core_version })} />
              )}
              {entry && (
                <Fact
                  label={t('apps.fact.source')}
                  value={catalogSourceHost.value
                    ? t('apps.fact.source_operator', { host: catalogSourceHost.value })
                    : t('apps.fact.source_social_home')}
                />
              )}
            </dl>
            {/* Text, never a link: nothing here is fetched. */}
            {entry?.homepage && (
              <p class="sh-app-detail__homepage">
                {t('apps.fact.homepage', { url: entry.homepage.replace(/^https:\/\//, '') })}
              </p>
            )}
          </section>
          <section class="sh-app-detail__card" aria-labelledby="sh-app-detail-home">
            <h2 class="sh-app-detail__h" id="sh-app-detail-home">{t('apps.section.household')}</h2>
            <HouseholdFacts installed={installed} minAge={minAge} isAdmin={isAdmin} />
          </section>
          {isAdmin && installed && (
            <ManageCard app={installed} name={name} icon={entry?.icon_url} update={update?.compatible !== false ? update : null} />
          )}
          {isAdmin && !installed && entry && needsYou.value.some(
            n => n.type === 'app_request' && n.object_key === `app:${entry.app_id}`,
          ) && <NotNow appId={entry.app_id} />}
        </aside>
      </div>
    </div>
  )
}

function Fact({ label, value }: { label: string, value: string }) {
  return (
    <div class="sh-app-facts__row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

function HeroAction({ entry, installed, name, isAdmin, size, onOpen }: {
  entry: CatalogEntry | null
  installed: InstalledApp | null
  name: string
  isAdmin: boolean
  size: string
  onOpen: () => void
}) {
  const [consentOpen, setConsentOpen] = useState(false)
  if (installed) {
    if (!installed.enabled) {
      return <p class="sh-app-detail__note">{t('apps.detail.turned_off')}</p>
    }
    return (
      <div class="sh-app-detail__actions">
        <Button onClick={onOpen} aria-label={t('apps.open_named', { name })}>{t('apps.open')}</Button>
      </div>
    )
  }
  if (!entry) return null
  if (entry.compatible === false) {
    return (
      <p class="sh-app-detail__note" role="note">
        {entry.min_core_version
          ? t('apps.needs_newer_version', { version: entry.min_core_version })
          : t('apps.needs_newer')}
      </p>
    )
  }
  if (!isAdmin) {
    return (
      <div class="sh-app-detail__actions">
        <AskButton entry={entry} name={name} variant="primary" />
        <span class="sh-app-detail__hint">{t('apps.detail.member_hint')}</span>
      </div>
    )
  }
  return (
    <div class="sh-app-detail__actions">
      <Button onClick={() => setConsentOpen(true)}>{t('apps.install')}</Button>
      <span class="sh-app-detail__hint">
        {size ? t('apps.detail.add_hint_size', { size }) : t('apps.detail.add_hint')}
      </span>
      <AddAppDialog
        entry={entry}
        name={name}
        permissions={permissionsOf(null, entry)}
        open={consentOpen}
        onClose={() => setConsentOpen(false)}
      />
    </div>
  )
}

function HouseholdFacts({ installed, minAge, isAdmin }: {
  installed: InstalledApp | null
  minAge: number
  isAdmin: boolean
}) {
  const protectedLine = (isAdmin && householdHasProtectedMinor.value) || currentUser.value?.protected
    ? <> {t('apps.consent.protected')}</>
    : null
  if (!installed) {
    return <p class="sh-app-detail__text">{t('apps.detail.not_installed')} {whoCanOpen(minAge)}{protectedLine}</p>
  }
  const day = shortDay(installed.installed_at)
  return (
    <>
      <p class="sh-app-detail__text sh-app-detail__added">
        {installed.installed_by_name
          ? t('apps.added_by', { name: installed.installed_by_name, date: day })
          : day ? t('apps.added_on', { date: day }) : null}
      </p>
      <p class="sh-app-detail__text">
        {installed.enabled ? <>{whoCanOpen(installed.min_age)}{protectedLine}</> : t('apps.detail.turned_off')}
      </p>
    </>
  )
}

/** Admins: update, turn on / off, minimum age and uninstall. */
function ManageCard({ app, name, icon, update }: {
  app: InstalledApp
  name: string
  icon?: string | null
  update: AppUpdate | null
}) {
  const { route } = useLocation()
  const [toggling, setToggling] = useState(false)
  const [uninstallOpen, setUninstallOpen] = useState(false)
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
  const asks = update?.new_permissions?.length ?? 0
  return (
    <section class="sh-app-detail__card" aria-labelledby="sh-app-detail-manage">
      <div class="sh-app-detail__manage-head">
        <h2 class="sh-app-detail__h" id="sh-app-detail-manage">{t('apps.detail.manage')}</h2>
        <AppMenu app={app} name={name} toggling={toggling} onToggle={() => { void toggle() }} onUninstall={() => setUninstallOpen(true)} />
      </div>
      {update && (
        <p class="sh-app-detail__text sh-app-detail__update">
          <strong>{t('apps.update_title', { name })}</strong>
          {' · '}
          {versionLabel(update.current_version)} → {versionLabel(update.latest_version)}
          {' · '}
          {asks > 0
            ? t(isOne(asks) ? 'apps.update_new_permission_one' : 'apps.update_new_permissions', { n: String(asks) })
            : t('apps.update_same_permissions')}
        </p>
      )}
      <div class="sh-app-detail__actions">
        {update && <UpdateButton appId={app.app_id} name={name} update={update} />}
        <Button variant="secondary" loading={toggling} onClick={() => { void toggle() }}>
          {app.enabled ? t('apps.turn_off') : t('apps.turn_on')}
        </Button>
      </div>
      <UninstallDialog
        app={app}
        name={name}
        icon={icon}
        open={uninstallOpen}
        onClose={() => setUninstallOpen(false)}
        onDone={() => route('/apps')}
      />
    </section>
  )
}

/** Admins, for an app members asked for: "Not now" answers the requests. */
function NotNow({ appId }: { appId: string }) {
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      await dismissRequests(appId)
      dropHandled([`app:${appId}`])
      showToast(t('apps.not_now_done'), 'info')
    } catch {
      showToast(t('apps.not_now_failed'), 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section class="sh-app-detail__card sh-app-detail__card--quiet">
      <p class="sh-app-detail__text">{t('apps.not_now_hint')}</p>
      <Button variant="secondary" loading={busy} onClick={() => { void run() }}>{t('apps.not_now')}</Button>
    </section>
  )
}
