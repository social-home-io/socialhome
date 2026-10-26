/**
 * gfsNames — the display names of this household's GFS connections, for
 * "Public on Demo GFS" on the moment page. Read once per session; a failed
 * read leaves the generic "your GFS" wording.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import type { GfsConnection, MomentPublicRegistration } from '@/types'

export const gfsNames = signal<Record<string, string>>({})
let loading: Promise<void> | null = null

export function loadGfsNames(): Promise<void> {
  if (loading) return loading
  loading = api.get('/api/gfs/connections')
    .then((rows: GfsConnection[] | null) => {
      gfsNames.value = Object.fromEntries((rows ?? []).map((g) => [g.id, g.display_name]))
    })
    .catch(() => { loading = null })
  return loading
}

/** "Public on Demo GFS" / "Public on your GFS" / "Public on your 2 GFS
 *  servers" — or, with ``also``, the reply switch's "Also public on …". */
export function publicOnLabel(regs: MomentPublicRegistration[], also = false): string {
  if (regs.length <= 1) {
    const name = regs.length === 1 ? gfsNames.value[regs[0].gfs_id] : undefined
    if (name) return t(also ? 'moment.composer.public_on' : 'moment.detail.status_public_on', { gfs: name })
    return t(also ? 'moment.composer.public_on_one' : 'moment.detail.status_public_on_one')
  }
  return t(
    also ? 'moment.composer.public_on_many' : 'moment.detail.status_public_on_many',
    { n: String(regs.length) },
  )
}

/** Tests only. */
export function _resetGfsNamesForTests(): void {
  loading = null
  gfsNames.value = {}
}
