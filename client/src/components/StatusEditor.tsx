/**
 * StatusEditor — the caller's status: one emoji, one line of text and an
 * optional "clear after" (§23.8).
 *
 * Saves through ``PATCH /api/me`` (``status_emoji`` / ``status_text`` /
 * ``status_clear_after``). The server validates (80 chars of text, one
 * emoji, a deadline in the future) and answers 422 with a readable
 * detail, which is shown as a toast while the editor stays open. On
 * success ``currentUser`` takes the returned profile, so the page shows
 * the new status without waiting for the ``user.status_changed`` frame.
 */
import { useState } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { currentUser } from '@/store/auth'
import type { User, UserStatus } from '@/types'
import { Button } from './Button'
import { showToast } from './Toast'
import { formatLocale, t } from '@/i18n/i18n'

/** ``keep`` = leave an existing deadline as it is (only offered while
 *  the current status has one). */
export type ClearAfterChoice = 'none' | '30m' | '1h' | '4h' | 'today' | 'keep'

const CHOICES: { id: Exclude<ClearAfterChoice, 'keep'>; labelKey: string }[] = [
  { id: 'none', labelKey: 'status.clear_never' },
  { id: '30m', labelKey: 'status.clear_30m' },
  { id: '1h', labelKey: 'status.clear_1h' },
  { id: '4h', labelKey: 'status.clear_4h' },
  { id: 'today', labelKey: 'status.clear_today' },
]

/** Matches the server caps in ``socialhome/domain/user.py``. */
export const STATUS_TEXT_MAX = 80
/** Code points; a single emoji can span several (skin tone, ZWJ). The
 *  input's ``maxLength`` counts UTF-16 units, hence the headroom. */
const STATUS_EMOJI_MAX_UNITS = 32

/** "3:30 PM" for today, "Tue 3:30 PM" otherwise. */
export function formatClearsAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const sameDay = d.toDateString() === new Date().toDateString()
  return d.toLocaleString(formatLocale(), sameDay
    ? { hour: 'numeric', minute: '2-digit' }
    : { weekday: 'short', hour: 'numeric', minute: '2-digit' })
}

/** The status as others see it right now: ``null`` when unset or
 *  already past its deadline. */
export function liveStatus(s: UserStatus | null | undefined): UserStatus | null {
  if (!s || (!s.emoji && !s.text)) return null
  if (s.expires_at && Date.parse(s.expires_at) <= Date.now()) return null
  return s
}

/** What the status fields hold while being edited. */
export interface StatusDraft {
  emoji: string
  text: string
  clearAfter: ClearAfterChoice
}

/** The fields' starting point for *status* (the saved one). */
export function statusDraftFrom(status: UserStatus | null | undefined): StatusDraft {
  const live = liveStatus(status)
  return {
    emoji: live?.emoji ?? '',
    text: live?.text ?? '',
    clearAfter: live?.expires_at ? 'keep' : 'none',
  }
}

/** Whether *draft* says the same as *saved* (whitespace aside). Two
 *  empty statuses are equal whatever their "clear after". */
export function sameStatus(draft: StatusDraft, saved: StatusDraft): boolean {
  const de = draft.emoji.trim(), dt = draft.text.trim()
  const se = saved.emoji.trim(), st = saved.text.trim()
  if (!de && !dt && !se && !st) return true
  return de === se && dt === st && draft.clearAfter === saved.clearAfter
}

/** The ``PATCH /api/me`` body that saves *draft*. ``keepUntil`` is the
 *  saved deadline the "Until …" chip keeps. An empty draft clears. */
export function statusPatchBody(
  draft: StatusDraft,
  keepUntil: string | null,
): Record<string, string | null> {
  const emoji = draft.emoji.trim()
  const text = draft.text.trim()
  if (!emoji && !text) return { status_emoji: null, status_text: null }
  const clear_after = draft.clearAfter === 'keep'
    ? keepUntil
    : draft.clearAfter === 'none' ? null : draft.clearAfter
  return {
    status_emoji: emoji || null,
    status_text: text || null,
    status_clear_after: clear_after,
  }
}

/**
 * StatusFields — the emoji + text inputs and the "clear after" chips,
 * controlled. ``StatusEditor`` wraps them with its own Save; Settings ›
 * Profile embeds them in its one form (decision 8: one editor, two
 * entry points).
 */
export function StatusFields({ value, keepUntil, onChange, onClear, disabled, invalid, describedBy }: {
  value: StatusDraft
  /** The saved deadline; offers the "Until …" chip while set. */
  keepUntil: string | null
  onChange: (next: StatusDraft) => void
  /** Shown as "Clear status" while the fields hold something. */
  onClear?: () => void
  disabled?: boolean
  invalid?: boolean
  describedBy?: string
}) {
  const set = (patch: Partial<StatusDraft>) => onChange({ ...value, ...patch })
  const hasValue = !!value.emoji.trim() || !!value.text.trim()
  return (
    <>
      <div class="sh-status-row">
        <input
          class="sh-status-emoji"
          aria-label={t('status.emoji_aria')}
          value={value.emoji}
          placeholder="😊"
          maxLength={STATUS_EMOJI_MAX_UNITS}
          disabled={disabled}
          aria-invalid={invalid ? true : undefined}
          aria-describedby={describedBy}
          onInput={(e) => set({ emoji: (e.target as HTMLInputElement).value })}
        />
        <input
          class="sh-input sh-status-text"
          aria-label={t('status.text_aria')}
          value={value.text}
          placeholder={t('status.placeholder_text')}
          maxLength={STATUS_TEXT_MAX}
          disabled={disabled}
          aria-invalid={invalid ? true : undefined}
          aria-describedby={describedBy}
          onInput={(e) => set({ text: (e.target as HTMLInputElement).value })}
        />
      </div>
      <div class="sh-status-expiry" role="radiogroup" aria-label={t('status.clear_after_aria')}>
        <span class="sh-status-expiry__label">{t('status.clear_after')}</span>
        <div class="sh-expiry-options">
          {keepUntil && (
            <button
              type="button"
              role="radio"
              aria-checked={value.clearAfter === 'keep'}
              class={value.clearAfter === 'keep' ? 'sh-chip sh-chip--active' : 'sh-chip'}
              disabled={disabled}
              onClick={() => set({ clearAfter: 'keep' })}
            >
              {t('status.until', { time: formatClearsAt(keepUntil) })}
            </button>
          )}
          {CHOICES.map(c => (
            <button
              key={c.id}
              type="button"
              role="radio"
              aria-checked={value.clearAfter === c.id}
              class={value.clearAfter === c.id ? 'sh-chip sh-chip--active' : 'sh-chip'}
              disabled={disabled}
              onClick={() => set({ clearAfter: c.id })}
            >
              {t(c.labelKey)}
            </button>
          ))}
        </div>
      </div>
      {onClear && hasValue && (
        <button
          type="button"
          class="sh-status-clear"
          disabled={disabled}
          onClick={onClear}
        >
          {t('status.clear_status')}
        </button>
      )}
    </>
  )
}

export function StatusEditor({ onSave, onCancel }: {
  onSave?: (user: User) => void
  onCancel?: () => void
}) {
  const initial = liveStatus(currentUser.value?.status)
  const [draft, setDraft] = useState<StatusDraft>(() => statusDraftFrom(initial))
  const [busy, setBusy] = useState(false)
  const keepUntil = initial?.expires_at ?? null
  const empty = !draft.emoji.trim() && !draft.text.trim()

  const submit = async (body: Record<string, string | null>, cleared: boolean) => {
    setBusy(true)
    try {
      const user = await api.patch('/api/me', body) as User
      currentUser.value = user
      showToast(
        cleared ? t('status.cleared') : t('status.updated'),
        cleared ? 'info' : 'success',
      )
      onSave?.(user)
    } catch (err) {
      showToast(
        err instanceof ApiError ? err.message : t('status.save_failed'),
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const save = () => {
    if (empty) return
    void submit(statusPatchBody(draft, keepUntil), false)
  }

  const clear = () => {
    setDraft({ emoji: '', text: '', clearAfter: 'none' })
    void submit({ status_emoji: null, status_text: null }, true)
  }

  return (
    <form
      class="sh-status-editor"
      onSubmit={(e) => { e.preventDefault(); save() }}
    >
      <StatusFields value={draft} keepUntil={keepUntil} onChange={setDraft} />
      <div class="sh-status-actions">
        {onCancel && (
          <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
            {t('common.cancel')}
          </Button>
        )}
        {initial && (
          <Button type="button" variant="secondary" onClick={clear} disabled={busy}>
            {t('status.clear_status')}
          </Button>
        )}
        <Button type="submit" disabled={busy || empty}>{t('status.set')}</Button>
      </div>
    </form>
  )
}
