/**
 * The facts an app shows people — in plain words, in their language.
 *
 * Permission text is always Social Home's own (never the catalog's), so a
 * publisher can't soften "sends invites and moves to other households"
 * (D2). Versions are CalVer and read as a date ("29 Oct 2026"); sizes as
 * "16 KB"; languages by their own names ("English, Deutsch").
 *
 * Keys looked up dynamically (``apps.perm.${p}``):
 *   apps.perm.storage apps.perm.storage_detail
 *   apps.perm.people apps.perm.people_detail
 *   apps.perm.federation apps.perm.federation_detail
 */
import { formatLocale, t } from '@/i18n/i18n'
import type { AppPermission, CatalogChangelogEntry, CatalogEntry, InstalledApp } from '@/store/apps'

/** The short line ("Saves its own data for each person"). */
export function permissionTitle(p: AppPermission): string {
  return t(`apps.perm.${p}`)
}

/** The full sentence for the detail page and the consent dialog. */
export function permissionDetail(p: AppPermission, name: string): string {
  return t(`apps.perm.${p}_detail`, { name })
}

/** The glyph next to each permission (decorative). */
export const PERMISSION_GLYPH: Record<AppPermission, string> = {
  storage: '▣',
  people: '☺\uFE0E',
  federation: '⇄',
}

/** The fixed "It can never…" list (true for every app: sandbox, no
 *  network, no token). */
export function neverList(): string[] {
  // Each line must be true of every app today: no token reaches the
  // sandboxed frame (sign-in), the bridge offers no posts / chats / photos
  // / location (content), the bundle CSP is ``default-src 'none'`` +
  // ``connect-src 'none'`` (internet), and (D3) the bridge opens no
  // session — only Social Home's invite sheet does — while ``app.send``
  // reaches only games the server knows for that person (unasked).
  return [
    t('apps.never.sign_in'),
    t('apps.never.content'),
    t('apps.never.internet'),
    t('apps.never.unasked'),
  ]
}

/** "Everyone" for no rating, else "13+". */
export function ratingLabel(minAge: number | null | undefined): string {
  return minAge && minAge > 0 ? `${minAge}+` : t('apps.min_age.everyone')
}

/** A CalVer version (``2026.10.29`` / ``2026.10.29.1``) as a date,
 *  anything else as ``v1.2.3``. */
export function versionLabel(version: string): string {
  const m = /^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.\d+)?$/.exec(version)
  if (!m) return `v${version}`
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  if (Number.isNaN(d.getTime())) return `v${version}`
  return d.toLocaleDateString(formatLocale(), { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
}

/** An ISO timestamp as a short day ("12 Oct"). */
export function shortDay(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString(formatLocale(), { day: 'numeric', month: 'short' })
}

/** Bytes as "16 KB" / "1.2 MB". */
export function sizeLabel(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return ''
  const fmt = (n: number, unit: 'kilobyte' | 'megabyte') =>
    new Intl.NumberFormat(formatLocale(), { style: 'unit', unit, maximumFractionDigits: unit === 'kilobyte' ? 0 : 1 }).format(n)
  if (bytes < 1024 * 1024) return fmt(Math.max(1, Math.round(bytes / 1024)), 'kilobyte')
  return fmt(bytes / (1024 * 1024), 'megabyte')
}

/** Each language by its own name ("English, Deutsch"). */
export function languageNames(codes: readonly string[] | undefined): string[] {
  return (codes ?? []).map((code) => {
    try {
      const name = new Intl.DisplayNames([code], { type: 'language' }).of(code)
      return name ? name.charAt(0).toLocaleUpperCase(code) + name.slice(1) : code
    } catch {
      return code
    }
  })
}

/** D6: "From Social Home", or "From <host>, set by your server operator". */
export function sourceLabel(host: string | null): string {
  return host ? t('apps.source.operator', { host }) : t('apps.source.social_home')
}

/** A changelog entry's note in the viewer's language (``pt-BR`` → ``pt``),
 *  else English, else whatever it has; ``''`` when it has none. */
export function changelogNote(entry: CatalogChangelogEntry, lang: string): string {
  const notes = entry.notes ?? {}
  const base = lang.split('-')[0]
  return notes[lang] ?? notes[base] ?? notes.en ?? Object.values(notes)[0] ?? ''
}

/** The permissions to show for an app: what the household agreed to when
 *  it is installed, else what the catalog says it asks for. */
export function permissionsOf(
  installed: InstalledApp | null | undefined,
  entry: CatalogEntry | null | undefined,
): AppPermission[] {
  if (installed?.permissions) return installed.permissions
  if (entry?.permissions) return entry.permissions
  // v1 data: the raw capabilities, ``people`` implied by ``federation``.
  const caps = new Set(installed?.capabilities ?? entry?.capabilities ?? [])
  if (caps.has('federation')) caps.add('people')
  return (['storage', 'people', 'federation'] as AppPermission[]).filter(p => caps.has(p))
}
