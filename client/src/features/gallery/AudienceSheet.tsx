/**
 * Who sees a household album (gallery D7): "Everyone at home" or
 * "Private – only these people".
 *
 * ``AudienceChooser`` is the choice itself — used inline when an album is
 * made and inside ``AudienceSheet``, the album menu's "Who sees this
 * album?" dialog (its owner only; the server checks again). A protected
 * child's guardians always see the child's private albums: they are shown
 * as fixed, ticked rows nobody can untick. The sheet says honestly that
 * this is consideration within the household, not encryption — whoever
 * runs the home server can read the files.
 *
 * Privacy follows the upload, so the server refuses a change that would
 * hide photos already in the album (an album with photos can't turn
 * private) or show someone's private photos wider; its refusal is shown
 * as is (``apiErrors``: ``ALBUM_AUDIENCE``).
 */
import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { personLabel } from '@/utils/names'

export type Audience = 'everyone' | 'chosen'

export interface AlbumAudience {
  album_id: string
  audience: Audience
  user_ids: string[]
  owner_user_id: string | null
  guardian_ids: string[]
  can_edit: boolean
}

export interface AudienceChoice {
  audience: Audience
  userIds: string[]
}

/** The people of the household to choose from: everyone active but the
 *  owner, by name. */
function people(ownerId: string | null): { id: string; name: string }[] {
  return [...householdUsers.value.values()]
    .filter(u => u.user_id !== ownerId)
    .map(u => ({ id: u.user_id, name: personLabel(u.display_name, u.username) }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

export function AudienceChooser({
  value, onChange, ownerId, guardianIds = [], idPrefix = 'sh-aud',
}: {
  value: AudienceChoice
  onChange: (next: AudienceChoice) => void
  ownerId: string | null
  /** A protected owner's guardians: always in, shown fixed. */
  guardianIds?: string[]
  idPrefix?: string
}) {
  useEffect(() => { void loadHouseholdUsers() }, [])
  const everyone = people(ownerId)
  const guardians = new Set(guardianIds)
  const chosen = new Set(value.userIds)
  const me = currentUser.value
  const ownerName = ownerId === me?.user_id
    ? t('gallery.audience.you_owner')
    : t('gallery.audience.owner', {
      name: personLabel(householdUsers.value.get(ownerId ?? '')?.display_name,
        householdUsers.value.get(ownerId ?? '')?.username),
    })
  const toggle = (id: string, on: boolean) => {
    const next = new Set(chosen)
    if (on) next.add(id)
    else next.delete(id)
    onChange({ audience: 'chosen', userIds: [...next] })
  }
  const allNames = [ownerId === me?.user_id ? (me?.display_name || me?.username || '') : '', ...everyone.map(p => p.name)]
    .filter(Boolean)
  return (
    <fieldset class="sh-audience">
      <legend class="sr-only">{t('gallery.audience.legend')}</legend>
      <label class={`sh-audience__opt${value.audience === 'everyone' ? ' sh-audience__opt--on' : ''}`}>
        <input
          type="radio"
          name={`${idPrefix}-audience`}
          checked={value.audience === 'everyone'}
          onChange={() => onChange({ audience: 'everyone', userIds: value.userIds })}
        />
        <span>
          <strong>{t('gallery.audience.everyone')}</strong>
          <span class="sh-muted sh-audience__sub">{allNames.join(', ')}</span>
        </span>
      </label>
      <label class={`sh-audience__opt${value.audience === 'chosen' ? ' sh-audience__opt--on' : ''}`}>
        <input
          type="radio"
          name={`${idPrefix}-audience`}
          checked={value.audience === 'chosen'}
          onChange={() => onChange({ audience: 'chosen', userIds: value.userIds })}
        />
        <span>
          <strong>{t('gallery.audience.private')}</strong>
          <span class="sh-muted sh-audience__sub">{t('gallery.audience.private_hint')}</span>
        </span>
      </label>
      {value.audience === 'chosen' && (
        <div class="sh-audience__people" role="group" aria-label={t('gallery.audience.people')}>
          <label class="sh-audience__person">
            <input type="checkbox" checked disabled />
            <span>{ownerName}</span>
          </label>
          {everyone.map(p => {
            const fixed = guardians.has(p.id)
            return (
              <label key={p.id} class="sh-audience__person">
                <input
                  type="checkbox"
                  checked={fixed || chosen.has(p.id)}
                  disabled={fixed}
                  onChange={(e) => toggle(p.id, (e.target as HTMLInputElement).checked)}
                />
                <span>
                  {p.name}
                  {fixed && <span class="sh-muted"> · {t('gallery.audience.guardian')}</span>}
                </span>
              </label>
            )
          })}
          {guardians.size > 0 && (
            <p class="sh-muted sh-audience__guardian-note">{t('gallery.audience.guardian_note')}</p>
          )}
        </div>
      )}
      <p class="sh-audience__note" role="note">
        <span aria-hidden="true">ℹ︎ </span>{t('gallery.audience.not_encryption')}
      </p>
    </fieldset>
  )
}

/** The album menu's "Who sees this album?": loads the audience, the owner
 *  changes it. Anyone else who sees the album reads it. */
export function AudienceSheet({ albumId, albumName, onClose, onSaved }: {
  albumId: string
  albumName: string
  onClose: () => void
  onSaved: (audience: Audience) => void
}) {
  const [loaded, setLoaded] = useState<AlbumAudience | null>(null)
  const [choice, setChoice] = useState<AudienceChoice>({ audience: 'everyone', userIds: [] })
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    api.get<AlbumAudience>(`/api/gallery/albums/${encodeURIComponent(albumId)}/audience`)
      .then((a) => {
        setLoaded(a)
        setChoice({ audience: a.audience, userIds: a.user_ids })
      })
      .catch(() => setFailed(true))
  }, [albumId])

  const save = async (e: Event) => {
    e.preventDefault()
    if (!loaded || busy) return
    setBusy(true)
    try {
      const saved = await api.put<AlbumAudience>(
        `/api/gallery/albums/${encodeURIComponent(albumId)}/audience`,
        { audience: choice.audience, user_ids: choice.audience === 'chosen' ? choice.userIds : [] },
      )
      showToast(t('gallery.audience.saved'), 'success')
      onSaved(saved.audience)
      onClose()
    } catch (err) {
      showToast(String((err as Error)?.message ?? err), 'error')
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title={t('gallery.audience.title', { name: albumName })} class="sh-audience-dialog">
      <p class="sh-muted sh-audience__lead">{t('gallery.audience.lead')}</p>
      {failed ? (
        <p role="alert">{t('gallery.audience.load_failed')}</p>
      ) : !loaded ? (
        <p class="sh-muted" aria-busy="true">{t('common.loading')}</p>
      ) : (
        <form onSubmit={save}>
          {loaded.can_edit ? (
            <AudienceChooser
              value={choice}
              onChange={setChoice}
              ownerId={loaded.owner_user_id}
              guardianIds={loaded.guardian_ids}
            />
          ) : (
            <p>{loaded.audience === 'chosen'
              ? t('gallery.audience.read_private', {
                names: [loaded.owner_user_id, ...loaded.user_ids, ...loaded.guardian_ids]
                  .filter((v, i, a): v is string => !!v && a.indexOf(v) === i)
                  .map(id => personLabel(householdUsers.value.get(id)?.display_name,
                    householdUsers.value.get(id)?.username))
                  .join(', '),
              })
              : t('gallery.audience.read_everyone')}</p>
          )}
          <div class="sh-form-actions">
            <Button variant="secondary" type="button" onClick={onClose}>
              {loaded.can_edit ? t('common.cancel') : t('common.close')}
            </Button>
            {loaded.can_edit && <Button type="submit" loading={busy}>{t('common.save')}</Button>}
          </div>
        </form>
      )}
    </Modal>
  )
}
