/**
 * HomeLocationSettings — Admin › Settings › Our home location.
 *
 * Where the household lives: the 🏠 on the Connections map, the "Use our
 * home" shortcut for a local community's area, and what a join to another
 * household's local community checks (rounded to ~1 km, sent only after
 * the person agreed).
 *
 * ``GET /api/admin/home-location`` says whether the platform owns it
 * (``from_platform`` — Home Assistant, ``home_location_sync``): then it is
 * shown read-only with "change it in Home Assistant". Otherwise
 * (standalone) the admin sets it — a tap on the map or a typed latitude /
 * longitude (``LocalAreaPicker`` ``mode="home"``) — with ``PUT``, or
 * removes it with ``DELETE``. A saved home goes straight into the
 * ``selfLat`` / ``selfLon`` signals every map and local-community check
 * reads (the server also sends a ``local.home_changed`` frame).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { selfLat, selfLon } from '@/store/connections'
import { coordLabel } from '@/utils/coordLabel'
import { LocalAreaMap } from './LocalAreaMap'
import { LocalAreaPicker, PIN_DOT_KM } from './LocalAreaPicker'

const URL = '/api/admin/home-location'

interface HomeLocationView {
  latitude: number | null
  longitude: number | null
  from_platform: boolean
}

type Home = { lat: number; lon: number }

function homeOf(v: HomeLocationView): Home | null {
  return v.latitude != null && v.longitude != null ? { lat: v.latitude, lon: v.longitude } : null
}

export function HomeLocationSettings() {
  const [view, setView] = useState<HomeLocationView | null>(null)
  const [failed, setFailed] = useState(false)
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)
  const cardRef = useRef<HTMLDivElement | null>(null)

  const load = async () => {
    setFailed(false)
    try {
      setView(await api.get<HomeLocationView>(URL))
    } catch {
      setFailed(true)
    }
  }
  useEffect(() => { void load() }, [])

  /** Mirror what the server now holds into the shared home signals. */
  const apply = (next: HomeLocationView) => {
    setView(next)
    const home = homeOf(next)
    selfLat.value = home?.lat ?? null
    selfLon.value = home?.lon ?? null
  }

  /** Set / Change / Remove swap places: keep focus on the card's actions. */
  const keepFocus = () => requestAnimationFrame(() => {
    const active = document.activeElement
    if (active && active !== document.body && active.isConnected) return
    cardRef.current?.querySelector<HTMLElement>('.sh-home-location__actions button')?.focus()
  })

  const save = async (next: Home | null) => {
    if (busy) return
    setBusy(true)
    try {
      apply(next
        ? await api.put<HomeLocationView>(URL, { latitude: next.lat, longitude: next.lon })
        : await api.delete<HomeLocationView>(URL))
      showToast(next ? t('home_location.saved') : t('home_location.removed'), 'success')
    } catch {
      showToast(t('home_location.save_failed'), 'error')
    } finally {
      setBusy(false)
      keepFocus()
    }
  }

  const home = view ? homeOf(view) : null
  const editable = view !== null && !view.from_platform
  return (
    <div class="sh-settings-subcard sh-home-location" id="home-location" ref={cardRef}
         aria-busy={busy || (view === null && !failed) ? 'true' : undefined}>
      <h3 class="sh-settings-panel-heading">🏠 {t('home_location.title')}</h3>
      <p class="sh-muted sh-settings-panel-blurb">{t('home_location.blurb')}</p>
      {failed ? (
        <div class="sh-home-location__error" role="alert">
          <p>{t('home_location.load_failed')}</p>
          <Button variant="secondary" onClick={() => void load()}>{t('common.try_again')}</Button>
        </div>
      ) : view === null ? (
        <p class="sh-muted">{t('common.loading')}</p>
      ) : (
        <>
          <p class="sh-home-location__where" role="status">
            {home ? coordLabel(home.lat, home.lon)
              : view.from_platform ? t('home_location.platform_none') : t('home_location.none')}
          </p>
          {home && (
            <LocalAreaMap area={{ lat: home.lat, lon: home.lon, radius_km: PIN_DOT_KM }}
                          height={180} zoneName={t('local_area.your_home')} zoneIcon="🏠"
                          ariaLabel={t('home_location.map_label')} />
          )}
          {view.from_platform && <p class="sh-ss-hint">{t('home_location.from_platform')}</p>}
          {editable && (
            <div class="sh-form-actions sh-home-location__actions">
              <Button variant="secondary" disabled={busy} onClick={() => setPicking(true)}>
                {home ? t('home_location.change') : t('home_location.set')}
              </Button>
              {home && (
                <Button variant="ghost" disabled={busy} onClick={() => void save(null)}>
                  {t('home_location.remove')}
                </Button>
              )}
            </div>
          )}
          <ul class="sh-home-location__privacy sh-ss-hint">
            <li>{t('home_location.stays_home')}</li>
            <li>{t('home_location.paired')}</li>
            <li>{t('home_location.local_join')}</li>
          </ul>
        </>
      )}
      {editable && (
        <LocalAreaPicker
          mode="home"
          open={picking}
          initial={home ? { ...home, radius_km: PIN_DOT_KM } : null}
          home={null}
          onClose={() => setPicking(false)}
          onSubmit={(next) => {
            setPicking(false)
            void save({ lat: next.lat, lon: next.lon })
          }}
        />
      )}
    </div>
  )
}
