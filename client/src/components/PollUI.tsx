/**
 * PollUI — reply-poll voting surface (§9 / §23.52).
 *
 * Rendered by :mod:`PostCard` when ``post.type === 'poll'``. Lazily
 * fetches the summary from ``/api/posts/{id}/poll`` (household) or
 * ``/api/spaces/{space_id}/posts/{id}/poll`` (space-scoped — selected
 * when ``spaceId`` is set), subscribes to
 * ``poll.*`` WS frames so live vote tallies update as co-members
 * vote, and gives the author a "Close poll" affordance once the
 * poll is open.
 *
 * Visuals:
 *   * animated per-option percentage fill
 *   * your-vote checkmark badge
 *   * total-votes + closed-state pill in the footer
 *   * countdown to ``closes_at`` if set
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { confirmDialog } from '@/components/confirm'
import { isOne, t } from '@/i18n/i18n'

interface PollOption {
  id: string
  text: string
  vote_count: number
}

export interface PollData {
  post_id:        string
  question:       string
  options:        PollOption[]
  allow_multiple: boolean
  closed:         boolean
  closes_at:      string | null
  total_votes:    number
  user_vote:      string[]
  space_id:       string | null
}

interface Props {
  postId: string
  authorUserId?: string
  currentUserId: string
  spaceId?: string | null
}

function baseUrl(postId: string, spaceId?: string | null): string {
  return spaceId
    ? `/api/spaces/${spaceId}/posts/${postId}/poll`
    : `/api/posts/${postId}/poll`
}

export function PollUI({
  postId, authorUserId, currentUserId, spaceId,
}: Props) {
  const [data, setData] = useState<PollData | null>(null)
  // ``null`` while loading, ``'missing'`` when the poll endpoint
  // 404'd (post created with type=poll but no options attached —
  // happens with sloppy demo seeds and any time a federation
  // partial reaches us before the poll envelope), ``'error'`` on
  // any other failure.  Lets the renderer show a calming placeholder
  // instead of staying stuck on "Loading poll…" forever.
  const [loadState, setLoadState] = useState<'loading' | 'missing' | 'error' | 'ok'>('loading')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const d = await api.get(baseUrl(postId, spaceId)) as PollData
        if (stopped) return
        setData(d)
        setLoadState('ok')
      } catch (err: unknown) {
        if (stopped) return
        // ApiError carries .status; bare Error.message has the verb +
        // path "API 404: /api/posts/.../poll".  Either way, treat 404
        // as "no poll attached" rather than as a hard failure.
        const status = (err as { status?: number })?.status
        const msg = (err as Error)?.message ?? ''
        if (status === 404 || msg.includes('404')) {
          setLoadState('missing')
        } else {
          setLoadState('error')
        }
      }
    }
    void load()
    const off1 = ws.on('poll.voted',   (e) => {
      if ((e.data as { post_id: string }).post_id === postId) void load()
    })
    const off2 = ws.on('poll.closed',  (e) => {
      if ((e.data as { post_id: string }).post_id === postId) void load()
    })
    const off3 = ws.on('poll.created', (e) => {
      if ((e.data as { post_id: string }).post_id === postId) void load()
    })
    return () => { stopped = true; off1(); off2(); off3() }
  }, [postId, spaceId])

  if (loadState === 'loading') return (
    <div class="sh-poll">
      <p class="sh-muted">{t('poll.loading')}</p>
    </div>
  )
  if (loadState === 'missing') return (
    <div class="sh-poll sh-poll--missing">
      <p class="sh-muted">
        {t('poll.missing')}
      </p>
    </div>
  )
  if (loadState === 'error' || !data) return (
    <div class="sh-poll sh-poll--error">
      <p class="sh-muted">{t('poll.load_failed')}</p>
    </div>
  )
  if (data.options.length === 0) {
    // 200 OK but zero options — same UX as 404 from the SPA's POV.
    return (
      <div class="sh-poll sh-poll--missing">
        <p class="sh-muted">
          {t('poll.missing')}
        </p>
      </div>
    )
  }

  const vote = async (optionId: string) => {
    if (busy || data.closed) return
    setBusy(true)
    try {
      const next = await api.post(
        `${baseUrl(postId, spaceId)}/vote`,
        { option_id: optionId },
      ) as PollData
      setData(next)
    } catch (err: unknown) {
      showToast(
        t('poll.vote_failed', { error: String((err as Error)?.message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const closePoll = async () => {
    if (!await confirmDialog(t('poll.close_confirm'), { destructive: true })) return
    setBusy(true)
    try {
      const next = await api.post(
        `${baseUrl(postId, spaceId)}/close`, {},
      ) as PollData
      setData(next)
    } catch (err: unknown) {
      showToast(
        t('poll.close_failed', { error: String((err as Error)?.message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const total = data.total_votes
  const isAuthor = !!authorUserId && authorUserId === currentUserId
  const closesIn = _formatCountdown(data.closes_at)

  return (
    <div class="sh-poll" role="region" aria-label={t('poll.region')}>
      <h4 class="sh-poll-question">{data.question}</h4>
      {data.allow_multiple && (
        <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          {t('poll.pick_any')}
        </p>
      )}
      <div class="sh-poll-options">
        {data.options.map(opt => {
          const pct = total > 0 ? Math.round((opt.vote_count / total) * 100) : 0
          const voted = data.user_vote.includes(opt.id)
          const closedCls = data.closed ? 'sh-poll-option--closed' : ''
          return (
            <button
              key={opt.id}
              type="button"
              class={`sh-poll-option ${voted ? 'sh-poll-option--voted' : ''} ${closedCls}`}
              onClick={() => void vote(opt.id)}
              disabled={data.closed || busy}
              aria-pressed={voted}
              aria-label={`${opt.text} — ${t(isOne(opt.vote_count) ? 'poll.votes_one' : 'poll.votes', { n: String(opt.vote_count) })}`}
            >
              <span class="sh-poll-option-bar" style={{ width: `${pct}%` }} />
              <span class="sh-poll-option-row">
                <span class="sh-poll-option-text">
                  {voted && '✓ '}
                  {opt.text}
                </span>
                <span class="sh-poll-option-count">
                  {opt.vote_count}{total > 0 ? ` · ${pct}%` : ''}
                </span>
              </span>
            </button>
          )
        })}
      </div>
      <div class="sh-poll-footer">
        <span class="sh-muted">
          {t(isOne(total) ? 'poll.votes_one' : 'poll.votes', { n: String(total) })}
          {closesIn && ` · ${closesIn}`}
        </span>
        <div class="sh-row">
          {data.closed && <span class="sh-badge">{t('poll.closed')}</span>}
          {!data.closed && isAuthor && (
            <Button
              variant="secondary"
              loading={busy}
              onClick={() => void closePoll()}
            >
              {t('poll.close')}
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}


function _formatCountdown(iso: string | null): string | null {
  if (!iso) return null
  const end = Date.parse(iso)
  if (Number.isNaN(end)) return null
  const diff = end - Date.now()
  if (diff <= 0) return t('poll.voting_ended')
  const mins = Math.floor(diff / 60_000)
  if (mins < 60)   return t('poll.closes_in_min', { n: String(mins) })
  const hours = Math.floor(mins / 60)
  if (hours < 24)  return t('poll.closes_in_hours', { n: String(hours) })
  const days = Math.floor(hours / 24)
  return t('poll.closes_in_days', { n: String(days) })
}


/* ═══════════════════════════════════════════════════════════════════
 * PollBuilder — compose-time dialog used by the feed Composer.
 * ══════════════════════════════════════════════════════════════════*/

export interface PollDraft {
  question:       string
  options:        string[]
  allow_multiple: boolean
  closes_at:      string | null
}

interface BuilderProps {
  open: boolean
  onSubmit: (draft: PollDraft) => void
  onClose: () => void
}

export function PollBuilder({ open, onSubmit, onClose }: BuilderProps) {
  const [question, setQuestion] = useState('')
  const [options, setOptions] = useState<string[]>(['', ''])
  const [allowMulti, setAllowMulti] = useState(false)
  const [duration, setDuration] = useState<'' | '1h' | '24h' | '7d'>('')

  const addOption = () => {
    if (options.length < 10) setOptions([...options, ''])
  }
  const setOption = (i: number, text: string) => {
    const copy = [...options]
    copy[i] = text
    setOptions(copy)
  }
  const removeOption = (i: number) => {
    if (options.length > 2) setOptions(options.filter((_, j) => j !== i))
  }

  const cleaned = options.map(o => o.trim()).filter(Boolean)
  const canSubmit = question.trim().length > 0 && cleaned.length >= 2

  const submit = (e: Event) => {
    e.preventDefault()
    if (!canSubmit) return
    let closes_at: string | null = null
    if (duration) {
      const h = duration === '1h' ? 1 : duration === '24h' ? 24 : 24 * 7
      closes_at = new Date(Date.now() + h * 60 * 60 * 1000).toISOString()
    }
    onSubmit({
      question: question.trim(),
      options:  cleaned,
      allow_multiple: allowMulti,
      closes_at,
    })
  }

  return (
    <Modal open={open} onClose={onClose} title={t('poll.create_title')}>
      <form class="sh-form sh-poll-builder" onSubmit={submit}>
        <label>
          {t('poll.question')}
          <input class="sh-poll-question-input"
                 type="text" maxLength={200}
                 placeholder={t('poll.question_placeholder')}
                 value={question} autoFocus
                 onInput={(e) =>
                   setQuestion((e.target as HTMLInputElement).value)} />
        </label>

        <div>
          <strong style={{ fontSize: 'var(--sh-font-size-sm)' }}>{t('poll.options')}</strong>
          {options.map((opt, i) => (
            <div key={i} class="sh-poll-option-row">
              <input type="text" maxLength={80}
                     placeholder={t('poll.option_n', { n: String(i + 1) })}
                     value={opt}
                     onInput={(e) =>
                       setOption(i, (e.target as HTMLInputElement).value)} />
              <button type="button" class="sh-poll-remove"
                      aria-label={t('poll.remove_option', { n: String(i + 1) })}
                      disabled={options.length <= 2}
                      onClick={() => removeOption(i)}>✕</button>
            </div>
          ))}
          <button type="button" class="sh-link" onClick={addOption}
                  disabled={options.length >= 10}>
            {t('poll.add_option')}
          </button>
        </div>

        <label class="sh-toggle-row" style={{ borderBottom: 'none' }}>
          <span>{t('poll.allow_multiple')}</span>
          <input type="checkbox" checked={allowMulti}
                 onChange={() => setAllowMulti(v => !v)} />
        </label>

        <label>
          {t('poll.closes_after')}
          <select value={duration}
                  onChange={(e) =>
                    setDuration((e.target as HTMLSelectElement).value as typeof duration)}>
            <option value="">{t('poll.stay_open')}</option>
            <option value="1h">{t('poll.dur_1h')}</option>
            <option value="24h">{t('poll.dur_24h')}</option>
            <option value="7d">{t('poll.dur_7d')}</option>
          </select>
        </label>

        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!canSubmit}>{t('poll.create')}</Button>
        </div>
      </form>
    </Modal>
  )
}
