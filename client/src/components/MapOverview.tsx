/**
 * MapOverview — the People / Zones panel beside a person map (Home
 * Assistant 2026.10's map overview, frontend PR #54029).
 *
 * Wide enough (the host is a size container, see ``app.css``) the panel
 * sits beside the map and scrolls on its own; narrower it is a bottom
 * sheet over the lower part of the map — collapsed to its tab strip
 * until the grabber (a real button, ``aria-expanded``) or a tab — the
 * one already shown included — opens it. The map is drawn by the caller
 * through ``renderMap``, which gets the strip the sheet covers so a fit
 * keeps markers out from under it.
 *
 * One selection is shared with the map: a person id or ``zone:<id>``.
 * Picking a row selects; a selection (from either side) opens its
 * detail — a person's zone / state, last update and activity, or who is
 * in a zone right now. There is no history: we don't store where anyone
 * has been. Back clears the selection and puts focus back on the row.
 *
 * Empty tabs are hidden; when the chosen tab empties, the first tab
 * with items shows instead. Zones are listed by name, Home first.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { SegmentedSwitch } from './SegmentedSwitch'
import type { MapInsets } from './LocationMap'
import { personColorToken } from '@/utils/mapMarkers'
import { colorClass } from '@/utils/tokenColor'
import { zoneColor } from '@/utils/zoneColor'
import { formatRadius, sortZones } from '@/utils/zones'
import { relativeDocsTime } from '@/utils/relativeTime'
import { isOne, t } from '@/i18n/i18n'

export interface OverviewPerson {
  /** The id the map selects (``LocationMarker.id``). */
  id: string
  /** Picks the frame colour, as on the map; falls back to ``id``. */
  user_id?: string
  name: string
  avatar_url?: string | null
  /** Where they are, in words (``presenceLabel``) — null to say nothing
   *  (another household's member, whose state we can't know). */
  status: string | null
  /** The zone they are in now, if any — counts them in that zone. */
  zone_id?: string | null
  /** When their location last changed (UTC). */
  updated_at?: string | null
  /** App activity, when the surface knows it. */
  online?: 'online' | 'idle' | 'offline' | null
  last_seen_at?: string | null
}

export interface OverviewZone {
  id: string
  name: string
  radius_m: number
  color: string | null
  icon?: string | null
  is_home?: boolean
}

export type OverviewTab = 'people' | 'zones'

interface RectLike {
  top: number
  right: number
  bottom: number
  left: number
}

/** The part of ``map`` that ``panel`` covers: a sheet along the bottom
 *  (spanning the width) → ``bottom``; anything else over the map →
 *  ``right``; a panel beside it → nothing. */
export function overlayInsets(map: RectLike, panel: RectLike): MapInsets {
  const width = map.right - map.left
  const overlapX = Math.min(map.right, panel.right) - Math.max(map.left, panel.left)
  const overlapY = Math.min(map.bottom, panel.bottom) - Math.max(map.top, panel.top)
  if (width <= 0 || overlapX <= 0 || overlapY <= 0) return {}
  if (overlapX >= width - 1) return { bottom: Math.round(map.bottom - Math.max(map.top, panel.top)) }
  return { right: Math.round(map.right - Math.max(map.left, panel.left)) }
}

/** People in ``zoneId`` right now. */
function _occupants(people: readonly OverviewPerson[], zoneId: string): OverviewPerson[] {
  return people.filter((p) => p.zone_id === zoneId)
}

function _countLabel(n: number): string {
  if (n === 0) return t('location.overview.nobody')
  return t(isOne(n) ? 'location.overview.people_count_one' : 'location.overview.people_count', { n: String(n) })
}

function _initials(name: string): string {
  return name.trim().split(/\s+/).slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '').join('')
}

function _zoneFace(z: OverviewZone): string {
  const icon = z.icon?.trim()
  if (icon) return icon
  return Array.from(z.name.trim())[0]?.toUpperCase() ?? '?'
}

/** The person's map marker face: photo (initials under it, shown when
 *  the photo fails) framed in their colour. */
function PersonAvatar({ p, large = false }: { p: OverviewPerson, large?: boolean }) {
  const [failed, setFailed] = useState(false)
  return (
    <span
      class={`sh-map-avatar ${colorClass(personColorToken(p.user_id ?? p.id))}${large ? ' sh-map-overview__avatar--large' : ''}`}
      aria-hidden="true"
    >
      <span class="sh-map-avatar__initials">{_initials(p.name)}</span>
      {p.avatar_url && !failed && (
        <img src={p.avatar_url} alt="" loading="lazy" onError={() => setFailed(true)} />
      )}
    </span>
  )
}

function ZoneBadge({ z }: { z: OverviewZone }) {
  return (
    <span class="sh-map-overview__zone-badge" style={{ '--zone-colour': zoneColor(z) }} aria-hidden="true">
      {_zoneFace(z)}
    </span>
  )
}

function _personLine(p: OverviewPerson): string {
  const ago = p.updated_at ? relativeDocsTime(p.updated_at) : null
  return [p.status, ago].filter(Boolean).join(' · ')
}

function _activity(p: OverviewPerson): string | null {
  if (p.online === 'online') return t('dms.status.online')
  if (p.online === 'idle') return t('dms.status.idle')
  if (p.online === 'offline') {
    return p.last_seen_at
      ? t('dms.status.last_seen', { ago: relativeDocsTime(p.last_seen_at) })
      : t('dms.status.offline')
  }
  return null
}

export interface MapOverviewProps {
  people: readonly OverviewPerson[]
  zones: readonly OverviewZone[]
  /** Person id, ``zone:<id>``, or null — shared with the map. */
  selectedId: string | null
  onSelect: (id: string | null) => void
  /** Draws the map; ``insets`` is the strip a bottom sheet covers. */
  renderMap: (insets: MapInsets) => ComponentChildren
}

export function MapOverview({ people, zones, selectedId, onSelect, renderMap }: MapOverviewProps) {
  const uid = useId()
  const bodyId = `${uid}-body`
  const [chosenTab, setChosenTab] = useState<OverviewTab>('people')
  const [expanded, setExpanded] = useState(false)
  const [insets, setInsets] = useState<MapInsets>({})
  const mapRef = useRef<HTMLDivElement | null>(null)
  const panelRef = useRef<HTMLElement | null>(null)
  const headingRef = useRef<HTMLHeadingElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  /** The detail was opened from a row here: move focus into it. */
  const focusDetailRef = useRef(false)
  /** Row to put focus back on once the list shows again. */
  const returnFocusRef = useRef<string | null>(null)

  const sortedPeople = [...people].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
  const sortedZones = sortZones(zones)
  const tabs: OverviewTab[] = [
    ...(people.length > 0 ? ['people' as const] : []),
    ...(zones.length > 0 ? ['zones' as const] : []),
  ]
  const tab: OverviewTab | undefined = tabs.includes(chosenTab) ? chosenTab : tabs[0]

  const selectedZone = selectedId?.startsWith('zone:')
    ? zones.find((z) => `zone:${z.id}` === selectedId) ?? null
    : null
  const selectedPerson = selectedId && !selectedZone
    ? people.find((p) => p.id === selectedId) ?? null
    : null
  const detailKind: OverviewTab | null = selectedZone ? 'zones' : selectedPerson ? 'people' : null
  const hasDetail = detailKind != null
  const hasPanel = tabs.length > 0

  // A selection (from the map or a row) opens its detail: show the sheet
  // and the tab the item lives on, so Back lands where it belongs.
  useEffect(() => {
    if (!detailKind) return
    setChosenTab(detailKind)
    setExpanded(true)
  }, [selectedId, detailKind])

  useEffect(() => {
    if (hasDetail) {
      if (focusDetailRef.current) headingRef.current?.focus()
      focusDetailRef.current = false
      return
    }
    const back = returnFocusRef.current
    returnFocusRef.current = null
    if (!back) return
    const row = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[data-overview-id]') ?? [])
      .find((el) => el.dataset.overviewId === back)
    row?.focus()
  }, [hasDetail, selectedId])

  // What the sheet covers of the map, kept current as either resizes.
  useLayoutEffect(() => {
    const measure = () => {
      const m = mapRef.current?.getBoundingClientRect()
      const p = panelRef.current?.getBoundingClientRect()
      const next = m && p ? overlayInsets(m, p) : {}
      setInsets((prev) => (
        prev.bottom === next.bottom && prev.right === next.right ? prev : next
      ))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    if (mapRef.current) ro.observe(mapRef.current)
    if (panelRef.current) ro.observe(panelRef.current)
    return () => ro.disconnect()
  }, [hasPanel])

  const pick = (id: string) => {
    focusDetailRef.current = true
    onSelect(id)
  }
  const back = () => {
    returnFocusRef.current = selectedId
    onSelect(null)
  }

  const personRow = (p: OverviewPerson) => (
    <li key={p.id}>
      <button type="button" class="sh-map-overview__row" data-overview-id={p.id}
              onClick={() => pick(p.id)}>
        <PersonAvatar p={p} />
        <span class="sh-map-overview__row-text">
          <span class="sh-map-overview__row-name">{p.name}</span>
          {_personLine(p) && <span class="sh-map-overview__row-sub">{_personLine(p)}</span>}
        </span>
      </button>
    </li>
  )

  let header: ComponentChildren = null
  let body: ComponentChildren = null
  if (selectedPerson) {
    const p = selectedPerson
    const activity = _activity(p)
    header = (
      <div class="sh-map-overview__detail-head">
        <button type="button" class="sh-map-overview__back" onClick={back}
                aria-label={t('location.overview.back_to_people')}>
          <span aria-hidden="true">‹</span>
        </button>
        <PersonAvatar p={p} />
        <h3 ref={headingRef} tabIndex={-1} class="sh-map-overview__title">{p.name}</h3>
      </div>
    )
    body = (
      <dl class="sh-map-overview__facts">
        <div>
          <dt>{t('location.overview.where')}</dt>
          <dd>{p.status ?? t('location.overview.shared_position')}</dd>
        </div>
        {p.updated_at && (
          <div>
            <dt>{t('location.overview.last_update')}</dt>
            <dd><time dateTime={p.updated_at}>{relativeDocsTime(p.updated_at)}</time></dd>
          </div>
        )}
        {activity && (
          <div>
            <dt>{t('location.overview.activity')}</dt>
            <dd>
              <span class={`sh-map-overview__online sh-map-overview__online--${p.online}`} aria-hidden="true" />
              {activity}
            </dd>
          </div>
        )}
      </dl>
    )
  } else if (selectedZone) {
    const z = selectedZone
    const here = _occupants(sortedPeople, z.id)
    header = (
      <div class="sh-map-overview__detail-head">
        <button type="button" class="sh-map-overview__back" onClick={back}
                aria-label={t('location.overview.back_to_zones')}>
          <span aria-hidden="true">‹</span>
        </button>
        <ZoneBadge z={z} />
        <h3 ref={headingRef} tabIndex={-1} class="sh-map-overview__title">{z.name}</h3>
      </div>
    )
    body = (
      <>
        <p class="sh-map-overview__zone-meta">
          {t('location.overview.radius', { radius: formatRadius(z.radius_m) })}
          {' · '}
          {_countLabel(here.length)}
        </p>
        <h4 class="sh-map-overview__subhead">{t('location.overview.here_now')}</h4>
        {here.length === 0
          ? <p class="sh-map-overview__empty">{t('location.overview.nobody_here')}</p>
          : <ul class="sh-map-overview__list">{here.map(personRow)}</ul>}
      </>
    )
  } else if (tab) {
    header = tabs.length > 1 ? (
      <SegmentedSwitch<OverviewTab>
        class="sh-map-overview__tabs"
        options={tabs}
        value={tab}
        labels={{
          people: `${t('location.overview.people')} · ${people.length}`,
          zones: `${t('location.overview.zones')} · ${zones.length}`,
        }}
        onChange={(next) => { setChosenTab(next); setExpanded(true) }}
        ariaLabel={t('location.overview.show')}
      />
    ) : (
      <h3 class="sh-map-overview__single">
        {t(tab === 'people' ? 'location.overview.people' : 'location.overview.zones')}
      </h3>
    )
    body = tab === 'people' ? (
      <ul class="sh-map-overview__list">{sortedPeople.map(personRow)}</ul>
    ) : (
      <ul class="sh-map-overview__list">
        {sortedZones.map((z) => (
          <li key={z.id}>
            <button type="button" class="sh-map-overview__row" data-overview-id={`zone:${z.id}`}
                    onClick={() => pick(`zone:${z.id}`)}>
              <ZoneBadge z={z} />
              <span class="sh-map-overview__row-text">
                <span class="sh-map-overview__row-name">{z.name}</span>
                <span class="sh-map-overview__row-sub">{formatRadius(z.radius_m)}</span>
              </span>
              <span class="sh-map-overview__count">{_countLabel(_occupants(people, z.id).length)}</span>
            </button>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <div class="sh-map-overview-host">
      <div
        class={`sh-map-overview${hasPanel ? ' has-panel' : ''}${expanded ? ' is-expanded' : ''}`}
        style={{ '--sh-map-overview-inset': `${insets.bottom ?? 0}px` }}
      >
        <div ref={mapRef} class="sh-map-overview__map">{renderMap(insets)}</div>
        {hasPanel && (
          <section ref={panelRef} class="sh-map-overview__panel" aria-label={t('location.overview.label')}>
            <button
              type="button"
              class="sh-map-overview__grabber"
              aria-expanded={expanded}
              aria-controls={bodyId}
              aria-label={expanded ? t('location.overview.hide_list') : t('location.overview.show_list')}
              onClick={() => setExpanded(!expanded)}
            >
              <span class="sh-map-overview__grabber-bar" aria-hidden="true" />
            </button>
            {/* Any tap on the tab strip opens a collapsed sheet — the tab
                already shown too, which the switch itself ignores. */}
            <div class="sh-map-overview__head"
                 onClick={hasDetail ? undefined : () => setExpanded(true)}>
              {header}
            </div>
            <div ref={listRef} id={bodyId} class="sh-map-overview__body">{body}</div>
          </section>
        )}
      </div>
    </div>
  )
}
