/**
 * On this day — one card at the top of the household gallery (owner
 * decision D14): photos taken on today's date in an earlier year, from one
 * album open to everyone at home (never a private album, never a space's).
 * It opens the photo in the viewer. Never a notification.
 *
 * Each person decides for themselves: ✕ hides it for today, the ⋯ menu
 * leaves that album out for good or turns the card off. Every choice has
 * an Undo, and :func:`OnThisDayNote` at the foot of the album list turns it
 * back on (or lets the left-out albums back in).
 *
 * The server keeps the settings (``/api/gallery/on-this-day``); "today" is
 * this browser's own day.
 */
import { signal } from '@preact/signals'
import { useState } from 'preact/hooks'
import { t, isOne } from '@/i18n/i18n'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { showToast } from '@/components/Toast'
import { OverflowMenu } from '@/components/OverflowMenu'
import { dayKeyOf, formatDayKey } from '@/utils/calendar'
import { albumDisplayName, type NamedAlbum } from './albumName'
import { galleryPath } from './galleryRoutes'

interface CardItem { id: string; thumbnail_url: string; item_type: 'photo' | 'video' }

interface CardAlbum extends NamedAlbum { id: string }

export interface OnThisDayCard {
  album: CardAlbum
  date: string
  years_ago: number
  count: number
  items: CardItem[]
}

export interface OnThisDaySettings {
  enabled: boolean
  hidden_today: boolean
  excluded_album_ids: string[]
}

interface OnThisDayState { card: OnThisDayCard | null; settings: OnThisDaySettings }

/** The last answer (``null`` before the first, or when it failed — then
 *  there is simply no card). */
export const onThisDay = signal<OnThisDayState | null>(null)

const URL = '/api/gallery/on-this-day'

/** Read today's card. A failure shows nothing — it is only a memory. */
export async function loadOnThisDay(): Promise<void> {
  try {
    onThisDay.value = await api.get(`${URL}?date=${dayKeyOf(new Date())}`) as OnThisDayState
  } catch {
    // Offline or the gallery is off: no card, and no error for a nicety.
  }
}

/** Change a setting; the card follows the answer. ``false`` on failure
 *  (a toast said so). */
async function change(body: Record<string, unknown>): Promise<boolean> {
  try {
    const r = await api.patch(URL, { ...body, date: dayKeyOf(new Date()) }) as
      { settings: OnThisDaySettings }
    const prev = onThisDay.value
    onThisDay.value = { card: prev?.card ?? null, settings: r.settings }
    return true
  } catch {
    showToast(t('gallery.otd.save_failed'), 'error')
    return false
  }
}

/** Drop the card now, then confirm with the server; put it back if that
 *  fails. Undo restores it and reads the card again. */
async function hideWith(body: Record<string, unknown>, undo: Record<string, unknown>, message: string) {
  const before = onThisDay.value
  if (before) onThisDay.value = { ...before, card: null }
  if (!(await change(body))) {
    onThisDay.value = before
    return
  }
  showToast(message, 'info', {
    action: {
      label: t('common.undo'),
      onClick: () => { void change(undo).then((ok) => { if (ok) void loadOnThisDay() }) },
    },
  })
}

function yearsAgo(n: number): string {
  return t(isOne(n) ? 'gallery.otd.years_ago_one' : 'gallery.otd.years_ago', { n: String(n) })
}

export function OnThisDayCardView() {
  const state = onThisDay.value
  const card = state?.card
  const [menuOpen, setMenuOpen] = useState(false)
  if (!card || card.items.length === 0) return null
  const name = albumDisplayName(card.album)
  const date = formatDayKey(card.date, { day: 'numeric', month: 'long', year: 'numeric' })
  const first = card.items[0]
  const href = addBase(galleryPath(undefined, card.album.id, first.id))
  const meta = t(isOne(card.count) ? 'gallery.otd.meta_one' : 'gallery.otd.meta', {
    n: String(card.count), album: name,
  })
  return (
    <section class="sh-otd" aria-label={t('gallery.otd.title')}>
      <a class="sh-otd__link" href={href}>
        <span class="sh-otd__stack" aria-hidden="true">
          {card.items.map(i => (
            <img key={i.id} src={i.thumbnail_url} alt="" loading="lazy" decoding="async" />
          ))}
        </span>
        <span class="sh-otd__text">
          <span class="sh-otd__ago">{yearsAgo(card.years_ago)}</span>
          <h2 class="sh-otd__date">{date}</h2>
          <span class="sh-otd__meta">{meta}</span>
        </span>
      </a>
      <div class="sh-otd__actions">
        <OverflowMenu
          label={t('gallery.otd.more')}
          open={menuOpen}
          onOpenChange={setMenuOpen}
          menuWidth={260}
          items={[
            {
              label: t('gallery.otd.exclude_album', { album: name }),
              onSelect: () => void hideWith(
                { exclude_album_id: card.album.id },
                { include_album_id: card.album.id },
                t('gallery.otd.excluded', { album: name }),
              ),
            },
            {
              label: t('gallery.otd.turn_off'),
              onSelect: () => void hideWith(
                { enabled: false }, { enabled: true }, t('gallery.otd.turned_off'),
              ),
            },
          ]}
        >
          ⋯
        </OverflowMenu>
        <button
          type="button"
          class="sh-otd__close"
          aria-label={t('gallery.otd.hide_today')}
          title={t('gallery.otd.hide_today')}
          onClick={() => void hideWith(
            { hide_today: true }, { hide_today: false }, t('gallery.otd.hidden_today'),
          )}
        >
          <span aria-hidden="true">✕</span>
        </button>
      </div>
    </section>
  )
}

/** At the foot of the album list: how to bring On this day back after
 *  turning it off, or after leaving albums out. Nothing when it is on and
 *  nothing is left out. */
export function OnThisDayNote() {
  const settings = onThisDay.value?.settings
  if (!settings) return null
  const left = settings.excluded_album_ids.length
  if (settings.enabled && left === 0) return null
  const turnOn = async () => {
    if (await change({ enabled: true })) void loadOnThisDay()
  }
  const letBack = async () => {
    if (await change({ include_all: true })) void loadOnThisDay()
  }
  return (
    <p class="sh-otd-note sh-muted">
      {settings.enabled
        ? t(isOne(left) ? 'gallery.otd.left_out_one' : 'gallery.otd.left_out', { n: String(left) })
        : t('gallery.otd.off_note')}{' '}
      <button type="button" class="sh-link-button" onClick={() => void (settings.enabled ? letBack() : turnOn())}>
        {settings.enabled ? t('gallery.otd.let_back') : t('gallery.otd.turn_on')}
      </button>
    </p>
  )
}
