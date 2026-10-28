/**
 * NotificationRowMenu — a row's "…" menu: fewer like this (redesign §4).
 *
 *   Mute this post                       (a post's rows; D10)
 *   <Space> notifications…               (a row in a space; its level, D9)
 *   Mute this chat…                      (a chat's rows; with a length)
 *   Turn off "<Kind>" push / Turn off "<Kind>"   (the row's kind, D4)
 *   ─────
 *   Notification settings
 *
 * Every change says what it did in a toast with **Undo**. Needs you rows
 * offer no "turn off" (only stepping down as admin stops them), and a kind
 * already Off offers nothing.
 *
 * A WAI-ARIA menu button: arrow keys move, Esc closes and gives focus back
 * to "…", a click outside closes. The two sub-lists (a space's level, a
 * chat's mute length) open in place with a Back item.
 *
 * The menu stays where it can be seen (:func:`menuPlacement`): on a phone a
 * row near the bottom opens it upward instead of under the bottom tab bar
 * or the keyboard, and its height is capped to the room on that side.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { addBase } from '@/baseUrl'
import { ASK_LENGTHS, levelLabel } from '@/components/NotifLevelPicker'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { setConversationMute } from '@/store/dms'
import {
  getSpaceLevel, isPostKey, kindChoice, loadNotifSettings, mutePost,
  notifSettings, setKind, setSpaceLevel, unmutePost,
  type NotifChoice, type NotifKind, type NotifLevel, type SpaceLevelState,
} from '@/store/notifSettings'
import { spaces } from '@/store/spaces'
import type { Notification } from '@/types'
import { isMutedForever, muteTimeLabel, type MuteDuration } from '@/utils/mute'
import { kindLabel } from '@/features/settings/NotificationSettings'
import { visibleBottom } from '@/components/OverflowMenu'

type View = 'main' | 'space' | 'chat'

/** Kinds a row's menu never turns down: chat messages make no rows, and
 *  Needs you can't be Off. */
const NO_TURN_DOWN: ReadonlySet<NotifKind> = new Set(['messages', 'needs_you'])

/** What "Turn this kind down" does from *now*: Push → In app, In app →
 *  Off; ``null`` when there is nothing further down. */
export function turnDown(now: NotifChoice | null): NotifChoice | null {
  if (now === 'push') return 'in_app'
  if (now === 'in_app') return 'off'
  return null
}

/** The chat a row is about (``dm:<conversation>``), or ``null``. */
export function chatOf(n: Notification): string | null {
  return n.object_key?.startsWith('dm:') ? n.object_key.slice(3) : null
}

/** The mute length that best restores a mute ending at *until*. */
export function closestDuration(until: string | null): MuteDuration {
  if (!until || isMutedForever(until)) return 'forever'
  const left = Date.parse(until) - Date.now()
  if (left <= 1.5 * 3600e3) return '1h'
  if (left <= 2 * 24 * 3600e3) return '8h'
  return '1w'
}

function spaceName(n: Notification): string | null {
  if (!n.space_id) return null
  return spaces.value.find(s => s.id === n.space_id)?.name
    ?? n.params?.space_name ?? null
}

/** Gap between the "…" and its menu. */
const GAP = 4
/** Margin kept free under the top bar. */
const EDGE = 8
/** Never squeeze the menu below this — a few items stay usable. */
const MIN_ROOM = 120

/** The highest y the menu may reach: under the sticky top bar, inside the
 *  visual viewport. */
function visibleTop(): number {
  const vv = window.visualViewport
  let top = vv ? vv.offsetTop : 0
  const bar = document.querySelector('.sh-topbar')?.getBoundingClientRect()
  if (bar && bar.height > 0 && bar.bottom > top) top = bar.bottom
  return top + EDGE
}

/**
 * Where the menu opens: below the "…" when *menuHeight* fits between it and
 * the visible bottom (above the bottom tab bar and the keyboard), else on
 * whichever side has more room. ``maxHeight`` caps it to that side's room.
 */
export function menuPlacement(
  trigger: { top: number, bottom: number },
  menuHeight: number,
  area: { top: number, bottom: number },
): { up: boolean, maxHeight: number } {
  const below = area.bottom - trigger.bottom - GAP
  const above = trigger.top - area.top - GAP
  const up = menuHeight > below && above > below
  return { up, maxHeight: Math.max(MIN_ROOM, Math.floor(up ? above : below)) }
}

const DOTS = (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">
    <circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" />
  </svg>
)

interface Props {
  n: Notification
  /** The row's sentence as plain text (the button's accessible name). */
  label: string
}

export function NotificationRowMenu({ n, label }: Props) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<View>('main')
  const [space, setSpace] = useState<SpaceLevelState | null>(null)
  const [fit, setFit] = useState<{ up: boolean, maxHeight: number } | null>(null)
  const wrap = useRef<HTMLDivElement | null>(null)
  const trigger = useRef<HTMLButtonElement | null>(null)
  const panel = useRef<HTMLDivElement | null>(null)
  const place = spaceName(n)
  const chatId = chatOf(n)
  const kind = (n.kind ?? null) as NotifKind | null

  const close = (refocus = true) => {
    setOpen(false)
    setView('main')
    if (refocus) trigger.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    if (notifSettings.value === null) void loadNotifSettings()
    if (n.space_id && place) {
      getSpaceLevel(n.space_id).then(setSpace).catch(() => setSpace(null))
    }
    const onDoc = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) close(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open, n.space_id, place])

  // Open where it can be seen, again when the view (and so the height)
  // changes, the window resizes or the keyboard comes up.
  useLayoutEffect(() => {
    if (!open) { setFit(null); return }
    const measure = () => {
      const r = trigger.current?.getBoundingClientRect()
      if (!r) return
      const h = panel.current?.scrollHeight ?? 0
      const next = menuPlacement(r, h, { top: visibleTop(), bottom: visibleBottom() })
      setFit(p => (p && p.up === next.up && p.maxHeight === next.maxHeight ? p : next))
    }
    measure()
    const vv = window.visualViewport
    window.addEventListener('resize', measure)
    vv?.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('resize', measure)
      vv?.removeEventListener('resize', measure)
    }
  }, [open, view])

  // Focus the first item whenever the menu opens or changes view — without
  // scrolling the page to it (the menu may just have flipped upward).
  useEffect(() => {
    if (!open) return
    panel.current?.querySelector<HTMLElement>('[role^="menuitem"]')?.focus({ preventScroll: true })
  }, [open, view])

  const onKey = (e: KeyboardEvent) => {
    const items = Array.from(
      panel.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]') ?? [],
    )
    const at = items.indexOf(document.activeElement as HTMLElement)
    let next = -1
    switch (e.key) {
      case 'Escape': e.preventDefault(); close(); return
      case 'Tab': close(false); return
      case 'ArrowDown': next = (at + 1) % items.length; break
      case 'ArrowUp': next = (at - 1 + items.length) % items.length; break
      case 'Home': next = 0; break
      case 'End': next = items.length - 1; break
      default: return
    }
    e.preventDefault()
    items[next]?.focus()
  }

  /** Run *act*, close, and offer *undo* in a toast saying *done*. */
  const run = async (act: () => Promise<unknown>, done: string, undo: () => Promise<unknown>) => {
    close()
    try {
      await act()
    } catch {
      showToast(t('notifications.menu.failed'), 'error')
      return
    }
    showToast(done, 'success', {
      action: {
        label: t('notifications.menu.undo'),
        onClick: () => {
          undo().catch(() => showToast(t('notifications.menu.failed'), 'error'))
        },
      },
    })
  }

  const muteThisPost = (key: string) => run(
    () => mutePost(key),
    t('notifications.menu.post_muted'),
    () => unmutePost(key),
  )

  const pickSpace = (level: NotifLevel, duration?: MuteDuration) => {
    if (!n.space_id || !place) return
    const sid = n.space_id
    const before = space
    const done = level !== 'muted'
      ? t('notifications.menu.space_level', { space: place, level: levelLabel(level) })
      : duration === 'forever' || !duration
        ? t('notifications.menu.space_quiet_forever', { space: place })
        : t('notifications.menu.space_quiet', {
          space: place, length: t(`notif.level.for_${duration}`),
        })
    void run(
      () => setSpaceLevel(sid, level, duration),
      done,
      () => (before
        ? before.level === 'muted'
          ? setSpaceLevel(sid, 'muted', closestDuration(before.muted_until))
          : setSpaceLevel(sid, before.level)
        : Promise.resolve()),
    )
  }

  const muteChat = (duration: MuteDuration) => {
    if (!chatId) return
    void run(
      () => setConversationMute(chatId, duration),
      duration === 'forever'
        ? t('notifications.menu.chat_muted_forever')
        : t('notifications.menu.chat_muted', { length: t(`notif.level.for_${duration}`) }),
      () => setConversationMute(chatId, null),
    )
  }

  const current = kind ? kindChoice(kind) : null
  const down = kind && !NO_TURN_DOWN.has(kind) ? turnDown(current) : null
  const turnKindDown = () => {
    if (!kind || !down || !current) return
    const name = kindLabel(kind)
    void run(
      () => setKind(kind, down),
      t(down === 'off' ? 'notifications.menu.kind_off_done' : 'notifications.menu.kind_in_app_done', { kind: name }),
      () => setKind(kind, current),
    )
  }

  const item = (text: string, onClick: () => void, sub?: string, extra?: Record<string, unknown>) => (
    <button type="button" role="menuitem" class="sh-notif-menu__item" tabIndex={-1} onClick={onClick} {...extra}>
      <span class="sh-notif-menu__label">{text}</span>
      {sub && <small class="sh-notif-menu__sub">{sub}</small>}
    </button>
  )

  const back = item(t('notifications.menu.back'), () => setView('main'))

  return (
    <div class="sh-notif-menu" ref={wrap} onKeyDown={onKey}>
      <button
        ref={trigger}
        type="button"
        class="sh-icon-btn sh-notif-menu__btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('notifications.menu.more_for', { what: label })}
        onClick={() => (open ? close() : setOpen(true))}
      >
        {DOTS}
      </button>
      {open && (
        <div
          ref={panel}
          class={`sh-notif-menu__panel${fit?.up ? ' sh-notif-menu__panel--up' : ''}`}
          style={fit ? { '--sh-notif-menu-room': `${fit.maxHeight}px` } : undefined}
          role="menu"
          aria-label={t('notifications.menu.title')}
        >
          {view === 'main' && (
            <>
              {(isPostKey(n.object_key) || (n.space_id && place) || chatId || down) && (
                <div class="sh-notif-menu__head" aria-hidden="true">{t('notifications.menu.fewer')}</div>
              )}
              {isPostKey(n.object_key) && item(
                t('notifications.menu.mute_post'),
                () => void muteThisPost(n.object_key as string),
                t('notifications.menu.mute_post_hint'),
              )}
              {n.space_id && place && item(
                t('notifications.menu.space', { space: place }),
                () => setView('space'),
                space
                  ? t('notifications.menu.space_now', { level: levelLabel(space.level) })
                  : undefined,
              )}
              {chatId && item(
                t('notifications.menu.mute_chat'),
                () => setView('chat'),
                t('notifications.menu.mute_chat_hint'),
              )}
              {kind && down && item(
                t(down === 'off' ? 'notifications.menu.kind_off' : 'notifications.menu.kind_push_off', { kind: kindLabel(kind) }),
                turnKindDown,
                t(down === 'off' ? 'notifications.menu.kind_off_hint' : 'notifications.menu.kind_push_off_hint'),
              )}
              {kind === 'needs_you' && (
                <p class="sh-notif-menu__note">{t('notifications.menu.needs_you_note')}</p>
              )}
              <div class="sh-notif-menu__sep" role="separator" />
              <a role="menuitem" tabIndex={-1} class="sh-notif-menu__item"
                 href={addBase('/settings#notifications')} onClick={() => close(false)}>
                <span class="sh-notif-menu__label">{t('notifications.menu.settings')}</span>
              </a>
            </>
          )}
          {view === 'space' && place && (
            <>
              {back}
              <div class="sh-notif-menu__head" aria-hidden="true">
                {t('notifications.menu.space', { space: place })}
              </div>
              {(['all', 'mentions'] as const).map(l => (
                <button key={l} type="button" role="menuitemradio" tabIndex={-1}
                        aria-checked={space?.level === l}
                        class="sh-notif-menu__item sh-notif-menu__item--radio"
                        onClick={() => pickSpace(l)}>
                  <span class="sh-notif-menu__check" aria-hidden="true">{space?.level === l ? '✓' : ''}</span>
                  <span class="sh-notif-menu__label">{levelLabel(l)}</span>
                </button>
              ))}
              <div class="sh-notif-menu__head" aria-hidden="true">
                {space?.level === 'muted' && space.muted_until
                  ? t('notifications.menu.quiet_now', {
                    until: isMutedForever(space.muted_until)
                      ? t('notifications.menu.until_on')
                      : muteTimeLabel(space.muted_until),
                  })
                  : t('notifications.menu.nothing_for')}
              </div>
              {ASK_LENGTHS.map(d => item(
                t(`notif.level.for_${d}`),
                () => pickSpace('muted', d),
                undefined,
                { key: d },
              ))}
            </>
          )}
          {view === 'chat' && (
            <>
              {back}
              <div class="sh-notif-menu__head" aria-hidden="true">{t('notifications.menu.nothing_for')}</div>
              {ASK_LENGTHS.map(d => item(
                t(`notif.level.for_${d}`),
                () => muteChat(d),
                undefined,
                { key: d },
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}
