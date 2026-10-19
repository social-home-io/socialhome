/**
 * A map pin as words: "47.3769° N, 8.5417° E" in the UI locale (4 dp —
 * the precision the server stores). Used where a space's map pin is set:
 * the create dialog and Settings › Location.
 */
import { formatLocale, t } from '@/i18n/i18n'

export function coordLabel(lat: number, lon: number): string {
  const nf = new Intl.NumberFormat(formatLocale(), { minimumFractionDigits: 4, maximumFractionDigits: 4 })
  const ns = lat < 0 ? t('zones.coord.s') : t('zones.coord.n')
  const ew = lon < 0 ? t('zones.coord.w') : t('zones.coord.e')
  return `${nf.format(Math.abs(lat))}° ${ns}, ${nf.format(Math.abs(lon))}° ${ew}`
}
