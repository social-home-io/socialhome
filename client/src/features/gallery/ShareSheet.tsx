/**
 * ShareSheet — share a copy of household photos to the feed, a space (as
 * a post or into a space album) or a chat (gallery D8).
 *
 * Only targets the person can already post to are listed (the server's
 * ``GET /api/gallery/share/targets``: a child sees fewer, never an error
 * after picking). A target that can't take this many is shown with why.
 * The audience line — who sees the copy — sits pinned with the button,
 * together with who else added these photos (they are told) and, for a
 * video longer than a minute, the visible choice to keep its first
 * minute or leave it out. Nothing is cut or sent without the person
 * seeing it first. Phone: a bottom sheet (the Modal's); desktop: a dialog.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useMemo, useState } from 'preact/hooks'
import { t, isOne } from '@/i18n/i18n'
import { api, ApiError } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { spaceGlyph } from '@/features/spaces/spaceGlyph'
import { currentUser } from '@/store/auth'
import {
  audienceLine, clock, longVideos, resultMessage, shareBody, shareTitle, tooManyFor,
  uploadersLine,
  type ShareChoice, type ShareItem, type ShareResult, type ShareSpace, type ShareTargets,
  type SpaceMode,
} from './gallerySharing'

interface AlbumRow { id: string; name: string; is_system?: boolean }

const THUMBS = 6

export function ShareSheet({ items: picked, nameOf, onClose, onShared }: {
  /** The photos and videos picked (household items). */
  items: readonly ShareItem[]
  nameOf: (userId: string) => string
  onClose: () => void
  /** Shared (or partly): the toast text, built from the server's answer. */
  onShared: (message: string, result: ShareResult) => void
}) {
  // A video still being converted has nothing to copy yet.
  const items = useMemo(() => picked.filter(i => !i.processing), [picked])
  const waiting = picked.length - items.length
  const [targets, setTargets] = useState<ShareTargets | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [key, setKey] = useState<string>('')
  const [modes, setModes] = useState<Record<string, SpaceMode>>({})
  const [albums, setAlbums] = useState<Record<string, AlbumRow[] | 'failed'>>({})
  const [albumPick, setAlbumPick] = useState<Record<string, string>>({})
  const [newAlbum, setNewAlbum] = useState('')
  const [text, setText] = useState('')
  const [trim, setTrim] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setFailed(false)
    api.get<ShareTargets>('/api/gallery/share/targets')
      .then((got) => { if (live) setTargets(got) })
      .catch(() => { if (live) setFailed(true) })
    return () => { live = false }
  }, [attempt])

  const count = items.length
  const limits = targets?.limits

  const modeOf = (space: ShareSpace): SpaceMode => {
    const chosen = modes[space.id]
    if (chosen === 'post' && space.post && limits && !tooManyFor({ kind: 'space', mode: 'post' }, count, limits)) return 'post'
    if (chosen === 'album' && space.album) return 'album'
    // The album takes a big selection; a post takes up to five.
    if (space.post && limits && !tooManyFor({ kind: 'space', mode: 'post' }, count, limits)) return 'post'
    return space.album ? 'album' : 'post'
  }

  const choice: ShareChoice | null = useMemo(() => {
    if (!targets) return null
    if (key === 'feed' && targets.feed) return { kind: 'feed' }
    if (key.startsWith('space:')) {
      const space = targets.spaces.find(s => `space:${s.id}` === key)
      if (space) return { kind: 'space', space, mode: modeOf(space) }
    }
    if (key.startsWith('chat:')) {
      const chat = targets.chats.find(c => `chat:${c.id}` === key)
      if (chat) return { kind: 'chat', chat }
    }
    return null
  }, [targets, key, modes, count]) // eslint-disable-line react-hooks/exhaustive-deps -- modeOf reads these

  // A space album picked: its albums come in once.
  const albumSpace = choice?.kind === 'space' && choice.mode === 'album' ? choice.space.id : null
  useEffect(() => {
    if (!albumSpace || albums[albumSpace]) return
    let live = true
    api.get<AlbumRow[]>(`/api/spaces/${encodeURIComponent(albumSpace)}/gallery/albums?limit=200`)
      .then((rows) => {
        if (!live) return
        const list = rows.filter(a => !a.is_system)
        setAlbums(prev => ({ ...prev, [albumSpace]: list }))
        setAlbumPick(prev => ({ ...prev, [albumSpace]: prev[albumSpace] ?? list[0]?.id ?? 'new' }))
      })
      .catch(() => { if (live) setAlbums(prev => ({ ...prev, [albumSpace]: 'failed' })) })
    return () => { live = false }
  }, [albumSpace]) // eslint-disable-line react-hooks/exhaustive-deps -- once per space

  const long = limits ? longVideos(items, limits) : []
  const blocked = choice && limits
    ? tooManyFor(choice.kind === 'space' ? { kind: 'space', mode: choice.mode } : choice, count, limits)
    : null
  const albumId = albumSpace ? albumPick[albumSpace] : undefined
  const creatingAlbum = !!albumSpace && albumId === 'new'
  const albumReady = !albumSpace || (albumId && (albumId !== 'new' || newAlbum.trim().length > 0))
  const nothingLeft = long.length === count && !trim
  const canShare = !!choice && !blocked && !busy && count > 0 && !!albumReady && !nothingLeft
  const takesText = choice && !(choice.kind === 'space' && choice.mode === 'album')

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!canShare || !choice) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.post<ShareResult>('/api/gallery/share', shareBody(
        items.map(i => i.id), choice,
        {
          albumId: creatingAlbum ? undefined : albumId,
          newAlbumName: newAlbum,
          text,
          trim,
        },
      ))
      onShared(resultMessage(result, choice), result)
      onClose()
    } catch (err) {
      setBusy(false)
      setError(err instanceof ApiError && err.status === 403
        ? t('gallery.share.not_allowed')
        : t('gallery.share.failed'))
    }
  }

  const me = currentUser.value?.user_id
  const uploaders = uploadersLine(items, me, nameOf)

  const row = (
    rowKey: string,
    icon: ComponentChildren,
    name: string,
    sub: string,
    reason: string | null,
    extra?: ComponentChildren,
  ) => {
    const on = key === rowKey
    return (
      <div class={'sh-share-target' + (on ? ' sh-share-target--on' : '')} key={rowKey}>
        <label class="sh-share-row">
          <span class="sh-share-icon" aria-hidden="true">{icon}</span>
          <span class="sh-share-text">
            <span class="sh-share-name">{name}</span>
            <span class="sh-share-sub">{sub}</span>
            {reason && <span class="sh-share-why">{reason}</span>}
          </span>
          <input
            type="radio"
            name="sh-share-target"
            value={rowKey}
            checked={on}
            disabled={!!reason}
            onChange={() => { setKey(rowKey); setError(null) }}
          />
        </label>
        {on && extra}
      </div>
    )
  }

  const spaceExtra = (space: ShareSpace) => {
    const mode = modeOf(space)
    const postReason = limits ? tooManyFor({ kind: 'space', mode: 'post' }, count, limits) : null
    const list = albums[space.id]
    return (
      <div class="sh-share-sub-options">
        {space.post && space.album && (
          <fieldset class="sh-share-modes">
            <legend class="sr-only">{t('gallery.share.how_legend', { name: space.name })}</legend>
            <label class="sh-share-mode">
              <input
                type="radio"
                name={`sh-share-mode-${space.id}`}
                checked={mode === 'post'}
                disabled={!!postReason}
                onChange={() => setModes(m => ({ ...m, [space.id]: 'post' }))}
              />
              <span>{t('gallery.share.as_post')}</span>
            </label>
            <label class="sh-share-mode">
              <input
                type="radio"
                name={`sh-share-mode-${space.id}`}
                checked={mode === 'album'}
                onChange={() => setModes(m => ({ ...m, [space.id]: 'album' }))}
              />
              <span>{t('gallery.share.into_album')}</span>
            </label>
          </fieldset>
        )}
        {mode === 'album' && (
          list === 'failed' ? (
            <p class="sh-error" role="alert">{t('gallery.share.albums_failed')}</p>
          ) : !list ? (
            <p class="sh-muted" role="status">{t('common.loading')}</p>
          ) : (
            <div class="sh-share-album">
              <label class="sr-only" for={`sh-share-album-${space.id}`}>{t('gallery.share.album_label')}</label>
              <select
                id={`sh-share-album-${space.id}`}
                class="sh-share-album-select"
                value={albumPick[space.id] ?? 'new'}
                onChange={(e) => {
                  const v = (e.target as HTMLSelectElement).value
                  setAlbumPick(p => ({ ...p, [space.id]: v }))
                }}
              >
                {list.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
                <option value="new">{t('gallery.share.new_album')}</option>
              </select>
              {albumPick[space.id] === 'new' && (
                <input
                  type="text"
                  class="sh-share-album-new"
                  maxLength={80}
                  value={newAlbum}
                  aria-label={t('gallery.form.name')}
                  placeholder={t('gallery.form.name_placeholder')}
                  onInput={(e) => setNewAlbum((e.target as HTMLInputElement).value)}
                />
              )}
            </div>
          )
        )}
        {mode === 'post' && space.post_review && (
          <p class="sh-share-why">{t('gallery.share.post_review')}</p>
        )}
      </div>
    )
  }

  const hasTargets = !!targets && (targets.spaces.length > 0 || targets.chats.length > 0)

  return (
    <Modal open onClose={onClose} title={shareTitle(items.length ? items : picked)} class="sh-share-sheet">
      <form onSubmit={submit} class="sh-share-form">
        <ul class="sh-share-thumbs" aria-label={t('gallery.share.picked')}>
          {items.slice(0, THUMBS).map(i => (
            <li key={i.id} class="sh-share-thumb">
              <img src={i.thumbnail_url} alt="" loading="lazy" decoding="async" />
              {i.item_type === 'video' && typeof i.duration_s === 'number' && (
                <span class="sh-share-thumb-len">{clock(i.duration_s)}</span>
              )}
            </li>
          ))}
          {items.length > THUMBS && (
            <li class="sh-share-thumb sh-share-thumb--more">+{items.length - THUMBS}</li>
          )}
        </ul>
        {waiting > 0 && (
          <p class="sh-muted sh-share-note">
            {t(isOne(waiting) ? 'gallery.share.waiting_one' : 'gallery.share.waiting', { n: String(waiting) })}
          </p>
        )}

        {failed ? (
          <div class="sh-share-load-failed" role="alert">
            <p>{t('gallery.share.targets_failed')}</p>
            <Button variant="secondary" type="button" onClick={() => setAttempt(n => n + 1)}>
              {t('common.retry')}
            </Button>
          </div>
        ) : !targets ? (
          <p class="sh-muted" role="status">{t('common.loading')}</p>
        ) : (
          <>
            <fieldset class="sh-share-group">
              <legend class="sh-share-group-title">{t('gallery.share.group_home')}</legend>
              {targets.feed && row(
                'feed', '☰', t('gallery.share.feed'), t('gallery.share.feed_sub'),
                limits ? tooManyFor({ kind: 'feed' }, count, limits) : null,
              )}
            </fieldset>
            {targets.spaces.length > 0 && (
              <fieldset class="sh-share-group">
                <legend class="sh-share-group-title">{t('gallery.share.group_spaces')}</legend>
                {targets.spaces.map(space => {
                  const postOk = space.post && limits && !tooManyFor({ kind: 'space', mode: 'post' }, count, limits)
                  const albumOk = space.album && limits && !tooManyFor({ kind: 'space', mode: 'album' }, count, limits)
                  const reason = postOk || albumOk ? null
                    : limits ? tooManyFor({ kind: 'space', mode: space.album ? 'album' : 'post' }, count, limits) : null
                  const scope = t(`space.visibility.${space.space_type}`)
                  const sub = space.space_type === 'public' || space.space_type === 'global'
                    ? scope
                    : t(isOne(space.households) ? 'gallery.share.space_sub_one' : 'gallery.share.space_sub', {
                      scope, n: String(space.households),
                    })
                  return row(
                    `space:${space.id}`, spaceGlyph(space.emoji, space.name), space.name, sub, reason,
                    spaceExtra(space),
                  )
                })}
              </fieldset>
            )}
            {targets.chats.length > 0 && (
              <fieldset class="sh-share-group">
                <legend class="sh-share-group-title">{t('gallery.share.group_chats')}</legend>
                {targets.chats.map(chat => row(
                  `chat:${chat.id}`,
                  <span class="sh-share-initials">{initials(chat.name)}</span>,
                  chat.name,
                  chat.group ? t('gallery.share.chat_group') : t('gallery.share.chat_direct'),
                  limits ? tooManyFor({ kind: 'chat' }, count, limits) : null,
                ))}
              </fieldset>
            )}
            {!hasTargets && <p class="sh-muted sh-share-note">{t('gallery.share.only_feed')}</p>}
            {takesText && (
              <div class="sh-share-message">
                <label for="sh-share-text">{t('gallery.share.message_label')}</label>
                <textarea
                  id="sh-share-text"
                  rows={2}
                  maxLength={2000}
                  value={text}
                  placeholder={t('gallery.share.message_placeholder')}
                  onInput={(e) => setText((e.target as HTMLTextAreaElement).value)}
                />
              </div>
            )}
          </>
        )}

        <div class="sh-share-foot">
          {choice ? (
            <div class="sh-share-audience" aria-live="polite">
              <p>
                <span aria-hidden="true">🔒 </span>
                <strong>{audienceLine(choice)}</strong>{' '}
                {t('gallery.share.copy_note')}
              </p>
              {uploaders && <p class="sh-share-uploaders">{uploaders}</p>}
            </div>
          ) : targets && (
            <p class="sh-muted sh-share-pick">{t('gallery.share.pick_target')}</p>
          )}
          {long.length > 0 && limits && (
            <fieldset class="sh-share-video">
              <legend>
                <span aria-hidden="true">⏱ </span>
                {isOne(long.length) && long[0]
                  ? t(long[0].caption ? 'gallery.share.long_named' : 'gallery.share.long_one', {
                    name: long[0].caption ?? '', len: clock(long[0].duration_s ?? 0),
                  })
                  : t('gallery.share.long_many', { n: String(long.length) })}
              </legend>
              <div class="sh-share-video-choices">
                <label class="sh-share-mode">
                  <input type="radio" name="sh-share-trim" checked={trim} onChange={() => setTrim(true)} />
                  <span>{t('gallery.share.keep_minute')}</span>
                </label>
                <label class="sh-share-mode">
                  <input type="radio" name="sh-share-trim" checked={!trim} onChange={() => setTrim(false)} />
                  <span>{t(isOne(long.length) ? 'gallery.share.leave_out_one' : 'gallery.share.leave_out')}</span>
                </label>
              </div>
            </fieldset>
          )}
          {error && <p class="sh-error" role="alert">{error}</p>}
          <div class="sh-form-actions">
            <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" loading={busy} disabled={!canShare}>
              {t('gallery.share.confirm')}
            </Button>
          </div>
        </div>
      </form>
    </Modal>
  )
}

/** "OG" for "Oma Gisela" — a chat's avatar stand-in. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || '·'
}
