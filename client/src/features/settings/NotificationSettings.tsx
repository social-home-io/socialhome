/**
 * Settings → Notifications (redesign D4): one simple page.
 *
 * 1. **This device** (:func:`DeviceBlock`) — is push on for this browser,
 *    off, blocked (with the steps to unblock it) or not possible here.
 * 2. **What you hear about** (:func:`KindsBlock`) — eight kinds, each with
 *    one Push / In app / Off choice and a real example sentence.
 * 3. **Muted and changed** (:func:`MutedAndChanged`) — every space, chat
 *    and post not at its default or muted, with Unmute / Change: the way
 *    back from every mute made from a row's "…" menu or a space.
 *
 * The Home Assistant pieces (notify target, automations switch) and the
 * household chat's level are composed in by ``SettingsPage``.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useId } from 'preact/hooks'
import { signal } from '@preact/signals'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { ChipRadioGroup } from '@/components/ChipRadioGroup'
import { levelLabel, quietLabel } from '@/components/NotifLevelPicker'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { setConversationMute } from '@/store/dms'
import {
  KINDS, dropChange, loadNotifSettings, notifSettings, notifSettingsError,
  setKind, setSpaceLevel, unmutePost,
  type NotifChange, type NotifChoice, type NotifKind,
} from '@/store/notifSettings'
import { settingsSectionPath } from '@/features/spaces/spaceSettingsSections'
import { isMuteActive } from '@/utils/mute'
import {
  currentPushSubscription, disableWebPush, enableWebPush, webPushSupported,
} from '@/utils/webPush'

// ── This device ─────────────────────────────────────────────────────────

type PushState = 'on' | 'off' | 'blocked' | 'unsupported'

const pushState = signal<PushState>('off')
const pushBusy = signal(false)

function permissionDenied(): boolean {
  return typeof Notification !== 'undefined' && Notification.permission === 'denied'
}

async function refreshPushState(): Promise<void> {
  if (!webPushSupported()) { pushState.value = 'unsupported'; return }
  if (permissionDenied()) { pushState.value = 'blocked'; return }
  try {
    pushState.value = (await currentPushSubscription()) !== null ? 'on' : 'off'
  } catch {
    pushState.value = 'off'
  }
}

async function turnOn(): Promise<void> {
  pushBusy.value = true
  try {
    if (await enableWebPush()) {
      pushState.value = 'on'
      showToast(t('settings.notifications.push_enabled'), 'success')
    } else {
      // Declined: the browser may now block the site — say how to undo it.
      pushState.value = permissionDenied() ? 'blocked' : 'off'
      showToast(t('settings.notifications.blocked'), 'info')
    }
  } catch (err: unknown) {
    showToast(t('settings.notifications.enable_failed', {
      error: String((err as Error)?.message ?? err),
    }), 'error')
  } finally {
    pushBusy.value = false
  }
}

async function turnOff(): Promise<void> {
  pushBusy.value = true
  try {
    await disableWebPush()
    pushState.value = 'off'
    showToast(t('settings.notifications.push_disabled'), 'info')
  } catch {
    showToast(t('settings.notifications.disable_failed'), 'error')
  } finally {
    pushBusy.value = false
  }
}

const DEVICE_ICON = (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <rect width="14" height="20" x="5" y="2" rx="2" /><path d="M12 18h.01" />
  </svg>
)

/** This browser's push: on / off / blocked (with unblock steps) / not
 *  possible. *children*: more device rows (the Home Assistant app). */
export function DeviceBlock({ children }: { children?: ComponentChildren }) {
  useEffect(() => { void refreshPushState() }, [])
  const state = pushState.value
  return (
    <section class="sh-settings-section sh-notif-set" aria-labelledby="sh-notif-device-h">
      <h2 id="sh-notif-device-h">{t('settings.notif.device.title')}</h2>
      <p class="sh-muted sh-notif-set__lead">{t('settings.notif.device.lead')}</p>
      <div class="sh-notif-set__box">
        <div class="sh-notif-set__row sh-notif-device" data-state={state}>
          <span class="sh-notif-device__icon">{DEVICE_ICON}</span>
          <span class="sh-notif-set__text">
            <b>{t(`settings.notif.device.${state}`)}</b>
            <span>{t(`settings.notif.device.${state}_hint`)}</span>
          </span>
          {state === 'on' && (
            <Button variant="secondary" loading={pushBusy.value} onClick={() => void turnOff()}>
              {t('settings.notif.device.turn_off')}
            </Button>
          )}
          {state === 'off' && (
            <Button loading={pushBusy.value} onClick={() => void turnOn()}>
              {t('settings.notif.device.turn_on')}
            </Button>
          )}
        </div>
        {state === 'blocked' && (
          <ol class="sh-notif-device__steps" aria-label={t('settings.notif.device.unblock_title')}>
            <li>{t('settings.notif.device.unblock_1')}</li>
            <li>{t('settings.notif.device.unblock_2')}</li>
            <li>{t('settings.notif.device.unblock_3')}</li>
          </ol>
        )}
        {children}
      </div>
    </section>
  )
}

// ── What you hear about ─────────────────────────────────────────────────

/** A kind's name, in the UI language. */
export function kindLabel(kind: NotifKind): string {
  return t(`settings.notif.kind.${kind}`)
}

/** A choice's label: Push / In app / Off. */
export function choiceLabel(choice: NotifChoice): string {
  return t(`settings.notif.choice.${choice}`)
}

function KindRow({ kind, choice, choices }: {
  kind: NotifKind, choice: NotifChoice, choices: NotifChoice[]
}) {
  const id = useId()
  const pick = async (next: NotifChoice) => {
    if (next === choice) return
    try {
      await setKind(kind, next)
    } catch {
      showToast(t('settings.notif.save_failed'), 'error')
    }
  }
  return (
    <div class="sh-notif-set__row sh-notif-kind" data-kind={kind}>
      <span class="sh-notif-set__text">
        <b id={id}>{kindLabel(kind)}</b>
        <span>{t(`settings.notif.kind.${kind}_hint`)}</span>
        <span class="sh-notif-kind__example">{t(`settings.notif.kind.${kind}_example`)}</span>
      </span>
      <ChipRadioGroup
        variant="segmented"
        class="sh-notif-kind__choice"
        labelledBy={id}
        value={choice}
        options={choices.map(c => ({ value: c, label: choiceLabel(c) }))}
        onChange={(c) => void pick(c)}
      />
    </div>
  )
}

export function KindsBlock() {
  const s = notifSettings.value
  return (
    <section class="sh-settings-section sh-notif-set" aria-labelledby="sh-notif-kinds-h">
      <h2 id="sh-notif-kinds-h">{t('settings.notif.kinds.title')}</h2>
      <p class="sh-muted sh-notif-set__lead">{t('settings.notif.kinds.lead')}</p>
      {s === null ? (
        notifSettingsError.value ? (
          <p class="sh-muted" role="alert">
            {t('settings.notif.load_failed')}{' '}
            <Button variant="secondary" onClick={() => void loadNotifSettings()}>
              {t('common.retry')}
            </Button>
          </p>
        ) : <p class="sh-muted">{t('common.loading')}</p>
      ) : (
        <div class="sh-notif-set__box">
          {KINDS.map(kind => {
            const k = s.kinds.find(x => x.kind === kind)
            return k
              ? <KindRow key={kind} kind={kind} choice={k.choice} choices={k.choices} />
              : null
          })}
        </div>
      )}
    </section>
  )
}

// ── Muted and changed ───────────────────────────────────────────────────

/** "Space" / "Local community"… — the kind of place a line is. */
function placeLabel(c: NotifChange): string {
  return t(`settings.notif.changed.place_${c.target}_${c.place}`)
}

function lineName(c: NotifChange): string {
  if (c.target === 'post') {
    return c.snippet ? `“${c.snippet}”` : t('settings.notif.changed.a_post')
  }
  if (c.name) return c.name
  return c.place === 'household'
    ? t('settings.notifications.household_chat')
    : t('settings.notif.changed.a_chat')
}

function stateLabel(c: NotifChange): string {
  if (c.target === 'post') return t('settings.notif.changed.post_muted')
  if (c.level === 'muted') {
    return quietLabel(
      c.muted_until,
      c.muted_until && isMuteActive(c.muted_until) && !c.muted_until.startsWith('9999-')
        ? c.then_level
        : null,
    )
  }
  return levelLabel(c.level)
}

/** Where "Change" goes: the space's notification settings, the chat. */
function changeHref(c: NotifChange): string | null {
  if (c.target === 'space') return addBase(settingsSectionPath(c.id, 'notifications'))
  if (c.target === 'chat') {
    if (c.place === 'space' && c.space_id) {
      return addBase(settingsSectionPath(c.space_id, 'notifications'))
    }
    if (c.place === 'household') return '#household-chat'
    return addBase(`/dms/${c.id}`)
  }
  return null
}

async function unmute(c: NotifChange): Promise<void> {
  try {
    if (c.target === 'post') {
      await unmutePost(c.id)
    } else if (c.target === 'space') {
      await setSpaceLevel(c.id, c.then_level ?? c.default_level)
    } else {
      await setConversationMute(c.id, null)
      if (c.then_level === c.default_level || c.then_level === null) dropChange('chat', c.id)
      else void loadNotifSettings()
    }
    showToast(t('settings.notif.changed.unmuted', { name: lineName(c) }), 'success')
  } catch {
    showToast(t('settings.notif.save_failed'), 'error')
  }
}

function ChangeLine({ c }: { c: NotifChange }) {
  const muted = c.level === 'muted'
  const href = changeHref(c)
  return (
    <li class="sh-notif-set__row sh-notif-changed" data-target={c.target}>
      <span class="sh-notif-set__text">
        <b>{lineName(c)}</b>
        <span class="sh-notif-changed__meta">
          <span>{c.target === 'post' && c.space_name ? c.space_name : placeLabel(c)}</span>
          <span class={`sh-notif-changed__state${muted ? ' sh-notif-changed__state--muted' : ''}`}>
            {stateLabel(c)}
          </span>
        </span>
      </span>
      {muted ? (
        <Button variant="secondary" onClick={() => void unmute(c)}
                aria-label={t('settings.notif.changed.unmute_named', { name: lineName(c) })}>
          {t('settings.notif.changed.unmute')}
        </Button>
      ) : href && (
        <a class="sh-btn sh-btn--secondary" href={href}
           aria-label={t('settings.notif.changed.change_named', { name: lineName(c) })}>
          {t('settings.notif.changed.change')}
        </a>
      )}
    </li>
  )
}

export function MutedAndChanged({ children }: { children?: ComponentChildren }) {
  const s = notifSettings.value
  const lines = s?.changed ?? []
  return (
    <section class="sh-settings-section sh-notif-set" aria-labelledby="sh-notif-changed-h">
      <h2 id="sh-notif-changed-h">{t('settings.notif.changed.title')}</h2>
      <p class="sh-muted sh-notif-set__lead">{t('settings.notif.changed.lead')}</p>
      {s !== null && (lines.length === 0 ? (
        <p class="sh-notif-set__empty">{t('settings.notif.changed.empty')}</p>
      ) : (
        <ul class="sh-notif-set__box sh-notif-changed__list">
          {lines.map(c => <ChangeLine key={`${c.target}:${c.id}`} c={c} />)}
        </ul>
      ))}
      {children}
    </section>
  )
}
