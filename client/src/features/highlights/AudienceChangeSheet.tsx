/**
 * "Change for all of today…" — owner decision 1.
 *
 * Today's highlight has one audience, set with its first frame. This
 * sheet is the one way to change it, and it changes it for every frame
 * of the day. Before anything is sent it says exactly what changes for
 * everyone — who gets all of today's frames, who loses the highlight —
 * and that watching can't be undone: people who already watched keep
 * what they saw. "Keep" leaves everything as it was.
 *
 * ``PATCH /api/highlights/{id}/audience``; a failure keeps the sheet open
 * with the error and the choice, so the author can try again.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { t, isOne } from '@/i18n/i18n'
import type { OwnHighlight } from '@/types'
import { AudiencePicker } from './AudiencePicker'
import {
  audienceChange,
  audienceOfChoice,
  choiceFromAudience,
  choiceIsComplete,
  nameList,
  type AudienceChoice,
  type AudienceDirectory,
  type ConnectedPerson,
  type RemoteHousehold,
} from './todayAudience'

interface Props {
  open: boolean
  highlight: OwnHighlight
  /** Frames already shared today — what a newly added person gets. */
  frames: number
  households: RemoteHousehold[]
  people: ConnectedPerson[]
  directory: AudienceDirectory
  ownCount: number
  loadError: string | null
  onRetry: () => void
  onClose: () => void
  onChanged: (highlight: OwnHighlight) => void
}

export function AudienceChangeSheet(props: Props) {
  const { highlight } = props
  const current = choiceFromAudience(highlight.audience_kind, highlight.audience)
  const [choice, setChoice] = useState<AudienceChoice>(current)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Every opening starts from today's audience, never a stale draft.
  useEffect(() => {
    if (!props.open) return
    setChoice(choiceFromAudience(highlight.audience_kind, highlight.audience))
    setError(null)
    setBusy(false)
  }, [props.open, highlight])

  const next = audienceOfChoice(choice)
  const { gains, losses } = audienceChange(
    highlight.audience_kind, highlight.audience, next.kind, next.ids, props.directory,
  )
  const same = next.kind === highlight.audience_kind
    && next.ids.length === highlight.audience.length
    && next.ids.every(id => highlight.audience.includes(id))
  const complete = choiceIsComplete(choice)

  const confirm = async () => {
    if (same || !complete || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.patch(`/api/highlights/${highlight.id}/audience`, {
        audience_kind: next.kind,
        audience: next.ids,
      }) as { highlight: OwnHighlight }
      props.onChanged({ ...res.highlight, audience: res.highlight.audience ?? next.ids })
    } catch (err: unknown) {
      setError((err as Error)?.message ?? String(err))
      setBusy(false)
    }
  }

  const n = props.frames
  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title={t('highlight.change.title')}
      class="sh-hlc-change"
    >
      <AudiencePicker
        name="sh-hlc-change-audience"
        value={choice}
        onChange={setChoice}
        households={props.households}
        people={props.people}
        ownName={props.directory.ownName}
        ownCount={props.ownCount}
        loadError={props.loadError}
        onRetry={props.onRetry}
        sub={mode => (mode === current.mode && mode !== 'choose' ? t('highlight.change.now') : undefined)}
      />
      <div class="sh-hlc-change__summary" aria-live="polite">
        {same ? (
          <p>{t('highlight.change.no_change')}</p>
        ) : (
          <>
            {gains.length > 0 && (
              <p>
                <strong>{t(isOne(n) ? 'highlight.change.gains_one' : 'highlight.change.gains', { n: String(n) })}</strong>
                {' '}<span dir="auto">{nameList(gains)}</span>
              </p>
            )}
            {losses.length > 0 && (
              <p>
                <strong>{t('highlight.change.losses')}</strong>
                {' '}<span dir="auto">{nameList(losses)}</span>
              </p>
            )}
          </>
        )}
        <p class="sh-muted">{t('highlight.change.watched_note')}</p>
      </div>
      {error && (
        <p role="alert" class="sh-hlc-change__error">
          {t('highlight.change.failed', { error })}
        </p>
      )}
      <div class="sh-form-actions">
        <Button type="button" variant="secondary" onClick={props.onClose}>
          {t('highlight.change.keep')}
        </Button>
        <Button
          type="button"
          loading={busy}
          disabled={same || !complete || busy}
          onClick={() => void confirm()}
        >
          {t('highlight.change.confirm')}
        </Button>
      </div>
    </Modal>
  )
}
