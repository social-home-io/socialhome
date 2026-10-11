/**
 * PrivateChannelPicker — the owner of a PRIVATE space picks the GFSs that
 * carry its private channel (shown under the "Use the GFS" toggle).
 *
 * A private space is never listed anywhere: its members exchange items
 * over an opaque channel on the GFSs the owner picks here (at most
 * ``max``). Members send each item to every one of them — anonymously at
 * the ones their household isn't connected to.
 *
 * Controlled: the parent (space settings) holds the selection and saves
 * it with the rest of the settings (``PUT /api/spaces/{id}/gfs-servers``
 * after the config PATCH). This component loads the choices once
 * (``GET /api/spaces/{id}/gfs-servers``) and reports them through
 * ``onLoaded``.
 */
import { useEffect, useState } from 'preact/hooks'
import type { JSX } from 'preact'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { Button } from './Button'
import { GfsChecklist, defaultGfsSelection, gfsHost, type GfsChoice } from './GfsChecklist'
import type { SpaceGfsServersView } from '@/types'

/** What the parent needs to save a change: the choices on offer, the
 *  selection the server holds, and the starting selection. */
export interface PrivateChannelChoices {
  choices: GfsChoice[]
  saved: string[]
  initial: string[]
  max: number
}

/** The choices and the starting selection of a view: every server already
 *  chosen — whatever its state, with a note when it can't be used right now,
 *  so the owner can always keep or untick it — then every other connection
 *  that can carry a channel. A connection without a ``channel_capable`` flag
 *  (the option isn't on yet, so the server didn't check) is offered — the
 *  save checks it. */
export function channelChoices(
  view: SpaceGfsServersView | null | undefined,
): PrivateChannelChoices {
  const chosen: GfsChoice[] = (view?.mode === 'channel' ? (view.chosen ?? []) : [])
    .map(c => ({
      id: c.gfs_id,
      display_name: c.display_name,
      inbox_url: c.inbox_url,
      note: c.connection_status !== 'active'
        ? t('space.private_gfs.channel_suspended')
        : (!c.channel_capable ? t('space.private_gfs.channel_unsupported') : undefined),
    }))
  const chosenIds = new Set(chosen.map(c => c.id))
  // One stable order (name, then host) — a row never jumps when its server
  // gets suspended or comes back.
  const choices = [
    ...chosen,
    ...(view?.available ?? [])
      .filter(a => a.channel_capable !== false && !chosenIds.has(a.gfs_id))
      .map(a => ({ id: a.gfs_id, display_name: a.display_name, inbox_url: a.inbox_url })),
  ].sort((x, y) =>
    x.display_name.localeCompare(y.display_name)
    || gfsHost(x.inbox_url).localeCompare(gfsHost(y.inbox_url)))
  const saved = view?.mode === 'channel' ? (view.gfs_ids ?? []) : []
  const initial = saved.length > 0 ? saved : defaultGfsSelection(choices)
  return { choices, saved, initial, max: view?.max ?? 8 }
}

/** Whether two id lists hold the same servers (order doesn't matter). */
export function sameGfsSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every(id => b.includes(id))
}

export function PrivateChannelPicker({
  spaceId,
  selected,
  onChange,
  onLoaded,
  error = null,
  reloadKey = 0,
}: {
  spaceId: string
  selected: string[] | null
  onChange: (ids: string[]) => void
  onLoaded: (choices: PrivateChannelChoices) => void
  error?: string | null
  /** Bump to re-read the server's choice (after a save). */
  reloadKey?: number
}): JSX.Element {
  const [loaded, setLoaded] = useState<PrivateChannelChoices | null>(null)
  const [failed, setFailed] = useState(false)

  const load = async () => {
    try {
      const view = await api.get<SpaceGfsServersView>(`/api/spaces/${spaceId}/gfs-servers`)
      const next = channelChoices(view)
      setLoaded(next)
      setFailed(false)
      onLoaded(next)
    } catch {
      setFailed(true)
    }
  }
  useEffect(() => { void load() }, [spaceId, reloadKey])

  if (failed) {
    return (
      <p class="sh-muted" data-testid="private-channel-load-failed">
        {t('space.gfs_listing.load_failed')}{' '}
        <Button variant="secondary" onClick={() => void load()}>
          {t('common.retry')}
        </Button>
      </p>
    )
  }
  if (loaded === null) return <p class="sh-muted">{t('common.loading')}</p>
  if (loaded.choices.length === 0) {
    return (
      <p class="sh-muted" data-testid="private-channel-none">
        {t('space.private_gfs.channels_none')}
      </p>
    )
  }
  return (
    <GfsChecklist
      name="private-channel"
      legend={t('space.private_gfs.channels_legend')}
      icon="🛰️"
      intro={t('space.private_gfs.channels_intro')}
      choices={loaded.choices}
      selected={selected ?? loaded.initial}
      onChange={onChange}
      max={loaded.max}
      error={error}
    />
  )
}
