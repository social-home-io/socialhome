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
 * It never overpromises: the audience is only one way in. Opening, it
 * reads ``GET /api/highlights/{id}/audience`` — whether the highlight has a
 * public link and where it was shared — and a narrowing says plainly who
 * can still open it (the household feed post, each space's post), and
 * offers to remove the public link too (pre-ticked; removed right after
 * the change, ``DELETE …/publish``).
 *
 * ``PATCH /api/highlights/{id}/audience``; a failure keeps the sheet open
 * with the error and the choice, so the author can try again.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
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

/** ``GET /api/highlights/{id}/audience`` — where else it opens. */
interface Reach {
  published: boolean
  shared: { household: boolean, spaces: Array<{ id: string, name: string }> }
}

const NO_REACH: Reach = { published: false, shared: { household: false, spaces: [] } }

interface Props {
  open: boolean
  highlight: OwnHighlight
  /** Whether it has a public link, as the composer knows it — the sheet
   *  re-reads it on opening. */
  published: boolean
  /** Frames already shared today — what a newly added person gets. */
  frames: number
  households: RemoteHousehold[]
  people: ConnectedPerson[]
  directory: AudienceDirectory
  ownCount: number
  loadError: string | null
  onRetry: () => void
  onClose: () => void
  onChanged: (highlight: OwnHighlight, published: boolean) => void
}

export function AudienceChangeSheet(props: Props) {
  const { highlight } = props
  const current = choiceFromAudience(highlight.audience_kind, highlight.audience)
  const [choice, setChoice] = useState<AudienceChoice>(current)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reach, setReach] = useState<Reach>({ ...NO_REACH, published: props.published })
  const [removeLink, setRemoveLink] = useState(true)

  // Every opening starts from today's audience, never a stale draft, and
  // re-reads where else it opens (a share or a link made elsewhere since).
  useEffect(() => {
    if (!props.open) return
    setChoice(choiceFromAudience(highlight.audience_kind, highlight.audience))
    setError(null)
    setBusy(false)
    setRemoveLink(true)
    setReach({ ...NO_REACH, published: props.published })
    let live = true
    api.get(`/api/highlights/${highlight.id}/audience`).then((res: Partial<Reach> | undefined) => {
      if (!live || !res) return
      setReach({
        published: res.published === true,
        shared: {
          household: res.shared?.household === true,
          spaces: Array.isArray(res.shared?.spaces) ? res.shared.spaces : [],
        },
      })
    }).catch(() => {
      // Unreadable: keep what the composer knew.
    })
    return () => { live = false }
  }, [props.open, props.published, highlight])

  const next = audienceOfChoice(choice)
  const { gains, losses } = audienceChange(
    highlight.audience_kind, highlight.audience, next.kind, next.ids, props.directory,
  )
  const same = next.kind === highlight.audience_kind
    && next.ids.length === highlight.audience.length
    && next.ids.every(id => highlight.audience.includes(id))
  const complete = choiceIsComplete(choice)
  // Only a narrowing takes the highlight from someone — that is when the
  // other ways in matter.
  const narrowing = !same && losses.length > 0
  const offerUnlink = narrowing && reach.published
  const spaceNames = reach.shared.spaces.map(s => s.name)
  const stillOpens = narrowing
    && ((reach.published && !removeLink) || reach.shared.household || spaceNames.length > 0)

  const confirm = async () => {
    if (same || !complete || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.patch(`/api/highlights/${highlight.id}/audience`, {
        audience_kind: next.kind,
        audience: next.ids,
      }) as { highlight: OwnHighlight }
      const changed = { ...res.highlight, audience: res.highlight.audience ?? next.ids }
      let published = reach.published
      if (offerUnlink && removeLink) {
        try {
          await api.delete(`/api/highlights/${highlight.id}/publish`)
          published = false
        } catch (err: unknown) {
          // The audience did change; say the link is still up.
          showToast(
            t('highlight.change.link_failed', { error: (err as Error)?.message ?? String(err) }),
            'error',
          )
        }
      }
      props.onChanged(changed, published)
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
                <strong>
                  {t(stillOpens ? 'highlight.change.losses_elsewhere' : 'highlight.change.losses')}
                </strong>
                {' '}<span dir="auto">{nameList(losses)}</span>
              </p>
            )}
            {offerUnlink && (
              <label class="sh-hlc-pick__check sh-hlc-change__unlink">
                <input
                  type="checkbox"
                  checked={removeLink}
                  onChange={e => setRemoveLink((e.target as HTMLInputElement).checked)}
                />
                <span>{t('highlight.change.remove_link')}</span>
              </label>
            )}
            {narrowing && reach.published && !removeLink && (
              <p>{t('highlight.change.link_stays')}</p>
            )}
            {narrowing && reach.shared.household && (
              <p>{t('highlight.change.household_post')}</p>
            )}
            {narrowing && spaceNames.length > 0 && (
              <p dir="auto">{t('highlight.change.space_posts', { spaces: nameList(spaceNames) })}</p>
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
