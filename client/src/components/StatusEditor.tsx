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

/** ``keep`` = leave an existing deadline as it is (only offered while
 *  the current status has one). */
export type ClearAfterChoice = 'none' | '30m' | '1h' | '4h' | 'today' | 'keep'

const CHOICES: { id: Exclude<ClearAfterChoice, 'keep'>; label: string }[] = [
  { id: 'none', label: 'Never' },
  { id: '30m', label: '30 min' },
  { id: '1h', label: '1 hour' },
  { id: '4h', label: '4 hours' },
  { id: 'today', label: 'Today' },
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
  return d.toLocaleString(undefined, sameDay
    ? { hour: 'numeric', minute: '2-digit' }
    : { weekday: 'short', hour: 'numeric', minute: '2-digit' })
}

function liveStatus(s: UserStatus | null | undefined): UserStatus | null {
  if (!s || (!s.emoji && !s.text)) return null
  if (s.expires_at && Date.parse(s.expires_at) <= Date.now()) return null
  return s
}

export function StatusEditor({ onSave, onCancel }: {
  onSave?: (user: User) => void
  onCancel?: () => void
}) {
  const initial = liveStatus(currentUser.value?.status)
  const [emoji, setEmoji] = useState(initial?.emoji ?? '')
  const [text, setText] = useState(initial?.text ?? '')
  const [clearAfter, setClearAfter] = useState<ClearAfterChoice>(
    initial?.expires_at ? 'keep' : 'none',
  )
  const [busy, setBusy] = useState(false)
  const empty = !emoji.trim() && !text.trim()

  const submit = async (body: Record<string, string | null>, done: string) => {
    setBusy(true)
    try {
      const user = await api.patch('/api/me', body) as User
      currentUser.value = user
      showToast(done, done === 'Status cleared' ? 'info' : 'success')
      onSave?.(user)
    } catch (err) {
      showToast(
        err instanceof ApiError ? err.message : 'Could not save your status.',
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const save = () => {
    if (empty) return
    const clear_after = clearAfter === 'keep'
      ? initial?.expires_at ?? null
      : clearAfter === 'none' ? null : clearAfter
    void submit({
      status_emoji: emoji.trim() || null,
      status_text: text.trim() || null,
      status_clear_after: clear_after,
    }, 'Status updated')
  }

  const clear = () => {
    setEmoji(''); setText(''); setClearAfter('none')
    void submit({ status_emoji: null, status_text: null }, 'Status cleared')
  }

  return (
    <form
      class="sh-status-editor"
      onSubmit={(e) => { e.preventDefault(); save() }}
    >
      <div class="sh-status-row">
        <input
          class="sh-status-emoji"
          aria-label="Status emoji"
          value={emoji}
          placeholder="😊"
          maxLength={STATUS_EMOJI_MAX_UNITS}
          onInput={(e) => setEmoji((e.target as HTMLInputElement).value)}
        />
        <input
          class="sh-input sh-status-text"
          aria-label="Status text"
          value={text}
          placeholder="What's your status?"
          maxLength={STATUS_TEXT_MAX}
          onInput={(e) => setText((e.target as HTMLInputElement).value)}
        />
      </div>
      <div class="sh-status-expiry" role="radiogroup" aria-label="Clear status after">
        <span class="sh-status-expiry__label">Clear after</span>
        <div class="sh-expiry-options">
          {initial?.expires_at && (
            <button
              type="button"
              role="radio"
              aria-checked={clearAfter === 'keep'}
              class={clearAfter === 'keep' ? 'sh-chip sh-chip--active' : 'sh-chip'}
              onClick={() => setClearAfter('keep')}
            >
              {`Until ${formatClearsAt(initial.expires_at)}`}
            </button>
          )}
          {CHOICES.map(c => (
            <button
              key={c.id}
              type="button"
              role="radio"
              aria-checked={clearAfter === c.id}
              class={clearAfter === c.id ? 'sh-chip sh-chip--active' : 'sh-chip'}
              onClick={() => setClearAfter(c.id)}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>
      <div class="sh-status-actions">
        {onCancel && (
          <Button type="button" variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
        {initial && (
          <Button type="button" variant="secondary" onClick={clear} disabled={busy}>
            Clear status
          </Button>
        )}
        <Button type="submit" disabled={busy || empty}>Set status</Button>
      </div>
    </form>
  )
}
