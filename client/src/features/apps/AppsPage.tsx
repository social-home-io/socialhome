/**
 * AppsPage — browse and manage Social Home Apps (§Apps).
 *
 * Admins get a two-tab layout:
 *  - **Installed** (default) — the household's installed apps. Admin-only
 *    settings (enable/disable, minimum age, uninstall) are tucked behind a
 *    per-card ⋯ overflow menu so the card face stays clean; a disabled app
 *    shows a "Disabled" chip so the exceptional state is still visible.
 *  - **Catalog** — available apps with an Install button (already-installed
 *    entries show an "Installed" chip).
 *
 * Non-admins see no tab chrome — just the enabled apps they can open.
 */
import { useEffect, useState } from 'preact/hooks'
import { t, isOne } from '@/i18n/i18n'
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
  loadInstalled,
  loadCatalog,
  loadUpdates,
  updateApp,
  installApp,
  uninstallApp,
  setEnabled,
  setMinAge,
  loadProtectionStatus,
  householdHasProtectedMinor,
  type InstalledApp,
  type AppUpdate,
  type CatalogEntry,
} from '@/store/apps'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { TabHeader } from '@/components/TabHeader'
import { ApiError } from '@/api'
import { addBase } from '@/baseUrl'

/**
 * Only render an icon `<img>` for a self-contained `data:` URI or an absolute
 * `https?:` URL. A relative path (e.g. `"icon.svg"`) would resolve against the
 * SPA origin and 404 — render the placeholder instead of a broken image.
 * (App bundles are served from a sandboxed opaque origin, so a relative bundle
 * path is never loadable from a host card anyway.)
 */
export function safeIconSrc(icon: string | null | undefined): string | null {
  if (!icon) return null
  return /^(data:|https?:\/\/)/i.test(icon) ? icon : null
}

type AppsTab = 'installed' | 'catalog'
const tabLabels = (): Record<AppsTab, string> => ({
  installed: t('apps.tab.installed'),
  catalog: t('apps.tab.catalog'),
})

export default function AppsPage() {
  useTitle(t('nav.apps'))
  const isAdmin = !!currentUser.value?.is_admin
  const [tab, setTab] = useState<AppsTab>('installed')

  useEffect(() => {
    void loadInstalled()
    void loadUpdates()
    if (isAdmin) {
      void loadCatalog()
      void loadProtectionStatus()
    }
  }, [])

  // Non-admins have no catalog and a single view — skip the tab chrome.
  if (!isAdmin) {
    return (
      <div class="sh-page sh-apps-page">
        <header class="sh-page-header">
          <h1 class="sh-page-title">{t('nav.apps')}</h1>
        </header>
        <InstalledSection isAdmin={false} onBrowse={() => {}} />
      </div>
    )
  }

  return (
    <div class="sh-page sh-apps-page">
      <header class="sh-page-header">
        <h1 class="sh-page-title">{t('nav.apps')}</h1>
      </header>

      <TabHeader<AppsTab>
        activeTab={tab}
        visibleTabs={['installed', 'catalog']}
        labels={tabLabels()}
        onSelectTab={setTab}
        ariaLabel={t('apps.sections_aria')}
        actions={tab === 'installed' ? <CheckUpdatesButton /> : undefined}
      />

      <div role="tabpanel" aria-label={tabLabels()[tab]}>
        {tab === 'installed' ? (
          <InstalledSection isAdmin onBrowse={() => setTab('catalog')} />
        ) : (
          <CatalogSection />
        )}
      </div>
    </div>
  )
}

// ─── Check-for-updates action (Installed tab header) ──────────────────────────

function CheckUpdatesButton() {
  const checking = updatesChecking.value
  const handleCheckUpdates = async () => {
    await loadUpdates(true)
    const count = updates.value.length
    showToast(
      count > 0
        ? t(isOne(count) ? 'apps.updates_available_one' : 'apps.updates_available', { n: String(count) })
        : t('apps.up_to_date'),
      'info',
    )
  }
  return (
    <Button
      variant="secondary"
      loading={checking}
      onClick={() => { void handleCheckUpdates() }}
    >
      {t('apps.check_updates')}
    </Button>
  )
}

// ─── Installed section ───────────────────────────────────────────────────────

function InstalledSection({
  isAdmin,
  onBrowse,
}: {
  isAdmin: boolean
  onBrowse: () => void
}) {
  const loading      = appsLoading.value
  const error        = appsError.value
  const apps         = installedApps.value
  const updateList   = updates.value

  if (loading) {
    return <div class="sh-apps-loading" aria-live="polite">{t('common.loading')}</div>
  }

  if (error) {
    return (
      <div class="sh-apps-error" role="alert">
        <p>{error}</p>
        <Button onClick={() => { void loadInstalled() }}>{t('common.retry')}</Button>
      </div>
    )
  }

  const visible = isAdmin ? apps : apps.filter(a => a.enabled)
  const updateMap = new Map<string, AppUpdate>(updateList.map(u => [u.app_id, u]))

  if (visible.length === 0) {
    return (
      <div class="sh-apps-empty">
        <p class="sh-muted">{t('apps.empty_installed')}</p>
        {isAdmin && (
          <Button variant="secondary" onClick={onBrowse}>
            {t('apps.browse_catalog')}
          </Button>
        )}
      </div>
    )
  }

  return (
    <div class="sh-apps-grid">
      {visible.map(app => (
        <AppCard
          key={app.app_id}
          app={app}
          isAdmin={isAdmin}
          update={updateMap.get(app.app_id) ?? null}
        />
      ))}
    </div>
  )
}

const minAgeOptions = (): { value: number; label: string }[] => [
  { value: 0,  label: t('apps.min_age.everyone') },
  { value: 13, label: '13+' },
  { value: 16, label: '16+' },
  { value: 18, label: '18+' },
]

function AppCard({
  app,
  isAdmin,
  update,
}: {
  app: InstalledApp
  isAdmin: boolean
  update: AppUpdate | null
}) {
  const [uninstallOpen, setUninstallOpen] = useState(false)
  const [togglingEnabled, setTogglingEnabled] = useState(false)
  const [uninstalling, setUninstalling]   = useState(false)
  const [updating, setUpdating]           = useState(false)
  const [settingMinAge, setSettingMinAge] = useState(false)

  const handleToggle = async () => {
    setTogglingEnabled(true)
    try {
      await setEnabled(app.app_id, !app.enabled)
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.update_failed'), 'error')
    } finally {
      setTogglingEnabled(false)
    }
  }

  const handleMinAgeChange = async (value: number) => {
    if (value === app.min_age) return
    setSettingMinAge(true)
    try {
      await setMinAge(app.app_id, value)
      showToast(value === 0 ? t('apps.min_age.removed') : t('apps.min_age.set', { age: String(value) }), 'success')
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.min_age.failed'), 'error')
    } finally {
      setSettingMinAge(false)
    }
  }

  const handleUninstall = async () => {
    setUninstalling(true)
    try {
      await uninstallApp(app.app_id)
      showToast(t('apps.uninstalled', { name: app.name }), 'info')
      setUninstallOpen(false)
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.uninstall_failed'), 'error')
    } finally {
      setUninstalling(false)
    }
  }

  const handleUpdate = async () => {
    if (!update) return
    setUpdating(true)
    try {
      await updateApp(app.app_id)
      showToast(t('apps.updated', { name: app.name, version: update.latest_version }), 'success')
    } catch (err: unknown) {
      showToast((err as Error).message ?? t('apps.update_failed'), 'error')
    } finally {
      setUpdating(false)
    }
  }

  return (
    <div class="sh-welcome-card sh-app-card">
      <div class="sh-app-card__header">
        {safeIconSrc(app.icon) ? (
          <img src={safeIconSrc(app.icon)!} alt="" class="sh-app-card__icon" aria-hidden="true" />
        ) : (
          <span class="sh-app-card__icon-placeholder" aria-hidden="true">📦</span>
        )}
        <div class="sh-app-card__meta">
          <span class="sh-app-card__name">{app.name}</span>
          <span class="sh-muted sh-app-card__version">v{app.version}</span>
        </div>
        {isAdmin && !app.enabled && (
          <span class="sh-chip sh-chip--muted sh-app-card__disabled-chip">{t('apps.disabled')}</span>
        )}
        {isAdmin && (
          <AppCardMenu
            app={app}
            togglingEnabled={togglingEnabled}
            settingMinAge={settingMinAge}
            onToggleEnabled={() => { void handleToggle() }}
            onSetMinAge={(v) => { void handleMinAgeChange(v) }}
            onUninstall={() => setUninstallOpen(true)}
          />
        )}
      </div>

      {(app.capabilities.length > 0 || app.min_age > 0) && (
        <div class="sh-app-card__caps">
          {app.capabilities.map(cap => (
            <span key={cap} class="sh-chip sh-chip--muted">{cap}</span>
          ))}
          {app.min_age > 0 && (
            <span class="sh-age-chip" title={t('apps.min_age.title', { age: String(app.min_age) })}>
              {app.min_age}+
            </span>
          )}
        </div>
      )}

      {update && (
        <div class="sh-app-card__update-row">
          <span class="sh-chip sh-chip--update" aria-label={t('apps.update_available_aria', { version: update.latest_version })}>
            {t('apps.update_available', { version: update.latest_version })}
          </span>
          {isAdmin && (
            <Button
              variant="primary"
              loading={updating}
              onClick={() => { void handleUpdate() }}
            >
              {t('apps.update')}
            </Button>
          )}
        </div>
      )}

      <div class="sh-app-card__actions">
        {app.enabled ? (
          <Button
            variant="primary"
            onClick={() => { window.location.href = addBase(`/apps/${encodeURIComponent(app.app_id)}`) }}
          >
            {t('apps.open')}
          </Button>
        ) : (
          isAdmin && (
            <span class="sh-muted sh-app-card__disabled-hint">
              {t('apps.disabled_hint')}
            </span>
          )
        )}
      </div>

      <Modal
        open={uninstallOpen}
        onClose={() => setUninstallOpen(false)}
        title={t('apps.uninstall_title', { name: app.name })}
      >
        <p>{boldName(t('apps.uninstall_body', { name: '\u0000' }), app.name)}</p>
        <div class="sh-modal-actions">
          <Button variant="secondary" onClick={() => setUninstallOpen(false)}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger" loading={uninstalling} onClick={() => { void handleUninstall() }}>
            {t('apps.uninstall')}
          </Button>
        </div>
      </Modal>
    </div>
  )
}

/** Render a translated sentence with the ``\u0000`` placeholder swapped
 *  for the bold app name, so word order stays the translator's. */
function boldName(sentence: string, name: string) {
  const [before, after = ''] = sentence.split('\u0000')
  return <>{before}<strong>{name}</strong>{after}</>
}

// ─── Per-card admin overflow menu ─────────────────────────────────────────────

/**
 * The ⋯ menu holding the low-frequency admin settings. Reuses the shared
 * `.sh-post-overflow` / `.sh-post-menu` kebab pattern (same a11y + visuals as
 * PostCard / EventOverflowMenu). The minimum-age choices are
 * `role="menuitemradio"` — the correct ARIA for a single-choice set — rather
 * than a native `<select>` jammed into a popover.
 */
function AppCardMenu({
  app,
  togglingEnabled,
  settingMinAge,
  onToggleEnabled,
  onSetMinAge,
  onUninstall,
}: {
  app: InstalledApp
  togglingEnabled: boolean
  settingMinAge: boolean
  onToggleEnabled: () => void
  onSetMinAge: (value: number) => void
  onUninstall: () => void
}) {
  const [open, setOpen] = useState(false)
  const close = () => setOpen(false)

  return (
    <div class="sh-post-overflow-wrap sh-app-card__menu">
      <button
        type="button"
        class="sh-post-overflow"
        aria-label={t('apps.settings_aria', { name: app.name })}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
        // Match PostCard/EventOverflowMenu: a short blur delay lets a click on
        // a menu item (which steals focus) fire before the menu closes.
        onBlur={() => setTimeout(close, 120)}
        onKeyDown={(e) => { if (e.key === 'Escape') close() }}
      >
        ⋯
      </button>
      {open && (
        <div class="sh-post-menu sh-app-menu" role="menu" onKeyDown={(e) => { if (e.key === 'Escape') close() }}>
          <button
            type="button"
            role="menuitem"
            disabled={togglingEnabled}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { onToggleEnabled(); close() }}
          >
            {app.enabled ? t('apps.disable') : t('apps.enable')}
          </button>

          {/* Age gate — only when the household actually has a protected
           *  minor (matches #536: no point configuring a gate nobody is
           *  subject to). Admin-authoritative value. */}
          {householdHasProtectedMinor.value && (
            <>
              <div class="sh-app-menu__sep" role="separator" />
              <div class="sh-app-menu__label" id={`minage-label-${app.app_id}`}>
                {t('apps.min_age.label')}
              </div>
              <div role="group" aria-labelledby={`minage-label-${app.app_id}`}>
                {minAgeOptions().map(opt => (
                  <button
                    key={opt.value}
                    type="button"
                    role="menuitemradio"
                    aria-checked={app.min_age === opt.value}
                    disabled={settingMinAge}
                    class="sh-app-menu__radio"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { onSetMinAge(opt.value); close() }}
                  >
                    <span class="sh-app-menu__radio-mark" aria-hidden="true">
                      {app.min_age === opt.value ? '●' : '○'}
                    </span>
                    {opt.label}
                  </button>
                ))}
              </div>
            </>
          )}

          <div class="sh-app-menu__sep" role="separator" />
          <button
            type="button"
            role="menuitem"
            class="sh-post-menu-danger"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => { onUninstall(); close() }}
          >
            {t('apps.uninstall_menu')}
          </button>
        </div>
      )}
    </div>
  )
}

// ─── Catalog / Browse section ─────────────────────────────────────────────────

function CatalogSection() {
  const loading = catalogLoading.value
  const error   = catalogError.value
  const entries = catalog.value
  const installed = installedApps.value

  if (loading) {
    return <div class="sh-apps-loading" aria-live="polite">{t('apps.catalog_loading')}</div>
  }

  if (error) {
    return (
      <div class="sh-apps-error" role="alert">
        <p>{error}</p>
        <Button onClick={() => { void loadCatalog() }}>{t('common.retry')}</Button>
      </div>
    )
  }

  if (entries.length === 0) {
    return (
      <div class="sh-apps-empty">
        <p class="sh-muted">{t('apps.catalog_empty')}</p>
      </div>
    )
  }

  return (
    <div class="sh-apps-catalog-list">
      {entries.map(entry => (
        <CatalogRow
          key={entry.app_id}
          entry={entry}
          alreadyInstalled={installed.some(a => a.app_id === entry.app_id)}
        />
      ))}
    </div>
  )
}

function CatalogRow({
  entry,
  alreadyInstalled,
}: {
  entry: CatalogEntry
  alreadyInstalled: boolean
}) {
  const [installing, setInstalling] = useState(false)

  const handleInstall = async () => {
    setInstalling(true)
    try {
      await installApp(entry.app_id)
    } catch (err: unknown) {
      const msg = err instanceof ApiError && err.detail
        ? err.message
        : (err as Error).message ?? t('apps.install_failed')
      showToast(msg, 'error')
    } finally {
      setInstalling(false)
    }
  }

  return (
    <div class="sh-welcome-card sh-catalog-row">
      <div class="sh-catalog-row__header">
        {safeIconSrc(entry.icon_url) ? (
          <img src={safeIconSrc(entry.icon_url)!} alt="" class="sh-app-card__icon" aria-hidden="true" />
        ) : (
          <span class="sh-app-card__icon-placeholder" aria-hidden="true">📦</span>
        )}
        <div class="sh-catalog-row__meta">
          <span class="sh-catalog-row__name">{entry.name}</span>
          <span class="sh-muted">v{entry.latest_version}</span>
        </div>
        {alreadyInstalled ? (
          <span class="sh-chip sh-chip--success">{t('apps.installed')}</span>
        ) : (
          <Button
            variant="primary"
            loading={installing}
            onClick={() => { void handleInstall() }}
          >
            {t('apps.install')}
          </Button>
        )}
      </div>
      {entry.description && (
        <p class="sh-catalog-row__desc sh-muted">{entry.description}</p>
      )}
      {entry.capabilities.length > 0 && (
        <div class="sh-app-card__caps">
          {entry.capabilities.map(cap => (
            <span key={cap} class="sh-chip sh-chip--muted">{cap}</span>
          ))}
        </div>
      )}
    </div>
  )
}
