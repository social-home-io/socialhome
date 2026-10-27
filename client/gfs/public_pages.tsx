/* A GFS's public pages, in the pinned viewer ("one viewer for the outside
 * world"): a global space's page, an invite landing and a person's Momentum
 * profile with its Follow button. The GFS redirects its own
 * ``/spaces/{id}``, ``/join/{token}`` and ``/moments/{user}`` here
 * (``page_link.ts``) and serves the content as JSON (``/gfs/pages/…``).
 *
 * Everything shown is text the GFS sent, so it is rendered as text: the
 * About blurb goes through ``markdown_lite.tsx`` (nodes, never HTML), a
 * colour must be plain hex, an image a raster ``data:`` URI (the CSP
 * admits nothing else anyway), and every string is length-bounded. A
 * response of any other shape reads as "couldn't be loaded".
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { ComponentChildren } from 'preact'
import { renderMarkdown } from './markdown_lite'
import type { PageBoot } from './page_link'
import { pinnedEndpoint, pinnedRequestInit } from './pinned_link'
import { t, tn, type MessageKey } from './viewer_i18n'
import { RetryHost, ViewerError, type RetryControl } from './viewer_retry'

/** Where the Follow button and "learn more" lead (the project website). */
export const WEBSITE = 'https://social-home.io'

const HEX = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i
const DATA_IMAGE = /^data:image\/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$/
const CATEGORIES = new Set([
  'general', 'local', 'community', 'non_profit', 'family_parenting',
  'learning', 'books_writing', 'hobby_crafts', 'music_arts', 'food_drink',
  'sports_outdoors', 'health_wellbeing', 'nature_garden', 'pets_animals',
  'travel', 'gaming', 'tech', 'faith_spirituality', 'politics_activism',
  'work_career',
])

/** Longest strings shown (the GFS caps them lower; this is the backstop). */
const MAX_TEXT = 4000
const MAX_NAME = 200
/** A picture or cover is a few hundred KB of base64 at most. */
const MAX_IMAGE = 3 * 1024 * 1024

export class PageNotFound extends Error {
  constructor(readonly serverName: string) {
    super('not found')
  }
}

class PageBusy extends Error {}

// ─── Response checks ──────────────────────────────────────────────────────

function str(v: unknown, max: number): string {
  if (typeof v !== 'string') throw new TypeError('bad field')
  return v.length > max ? v.slice(0, max) : v
}

function image(v: unknown): string | null {
  return typeof v === 'string' && v.length <= MAX_IMAGE && DATA_IMAGE.test(v) ? v : null
}

function color(v: unknown, fallback: string): string {
  return typeof v === 'string' && HEX.test(v) ? v : fallback
}

function count(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0
}

function obj(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw new TypeError('bad shape')
  return v as Record<string, unknown>
}

export interface SpacePageData {
  serverName: string
  name: string
  description: string
  about: string
  cover: string | null
  icon: string | null
  accent: string
  members: number
  category: string
}

export interface InvitePageData {
  serverName: string
  name: string
  accent: string
  icon: string | null
  code: string
  qr: string | null
}

export interface PersonPageData {
  serverName: string
  gfsId: string
  userId: string
  username: string
  displayName: string
  instanceId: string
  bio: string
  picture: string | null
  followers: number
}

export function parseSpace(raw: unknown): SpacePageData {
  const body = obj(raw)
  const s = obj(body.space)
  const category = typeof s.category === 'string' && CATEGORIES.has(s.category)
    ? s.category
    : 'general'
  return {
    serverName: str(body.server_name, MAX_NAME),
    name: str(s.name, MAX_NAME),
    description: str(s.description ?? '', MAX_TEXT),
    about: str(s.about_markdown ?? '', MAX_TEXT),
    cover: image(s.cover),
    icon: image(s.icon),
    accent: color(s.primary_color, color(s.accent_color, '#D2542A')),
    members: count(s.member_count),
    category,
  }
}

export function parseInvite(raw: unknown): InvitePageData {
  const body = obj(raw)
  const s = obj(body.space)
  const code = str(body.invite_code, 8192)
  if (!code.startsWith('socialhome://invite#')) throw new TypeError('bad code')
  const qr = typeof body.qr === 'string' && body.qr.startsWith('data:image/png;base64,')
    ? image(body.qr)
    : null
  return {
    serverName: str(body.server_name, MAX_NAME),
    name: str(s.name, MAX_NAME),
    accent: color(s.accent_color, '#D2542A'),
    icon: image(s.icon),
    code,
    qr,
  }
}

export function parsePerson(raw: unknown): PersonPageData {
  const body = obj(raw)
  const p = obj(body.person)
  return {
    serverName: str(body.server_name, MAX_NAME),
    gfsId: str(body.gfs_id, MAX_NAME),
    userId: str(p.user_id, MAX_NAME),
    username: str(p.username, MAX_NAME),
    displayName: str(p.display_name, MAX_NAME),
    instanceId: str(p.instance_id, MAX_NAME),
    bio: str(p.bio ?? '', MAX_TEXT),
    picture: image(p.picture),
    followers: count(p.follower_count),
  }
}

// ─── Fetching ─────────────────────────────────────────────────────────────

/** One page request to the GFS: absolute, cross-origin, no credentials,
 *  no referrer, no redirects (``pinned_link.ts``). A 404 is the page's own
 *  "not here" state and still names the server. */
export async function fetchPage<T>(
  gfs: string,
  path: string,
  parse: (raw: unknown) => T,
  init?: RequestInit,
): Promise<T> {
  const r = await fetch(pinnedEndpoint(gfs, path), pinnedRequestInit(init))
  if (r.status === 404) {
    let name = ''
    try {
      const body = obj(await r.json())
      name = typeof body.server_name === 'string' ? body.server_name.slice(0, MAX_NAME) : ''
    } catch {
      name = ''
    }
    throw new PageNotFound(name)
  }
  if (r.status === 429) throw new PageBusy('busy')
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return parse(await r.json())
}

export function loadPage(boot: PageBoot): Promise<SpacePageData | InvitePageData | PersonPageData> {
  switch (boot.view) {
    case 'space':
      return fetchPage(boot.gfs, `/gfs/pages/spaces/${encodeURIComponent(boot.spaceId)}`, parseSpace)
    case 'join':
      return fetchPage(boot.gfs, '/gfs/pages/invite', parseInvite, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: boot.token }),
      })
    case 'person':
      return fetchPage(boot.gfs, `/gfs/pages/people/${encodeURIComponent(boot.userId)}`, parsePerson)
  }
}

// ─── Pieces ───────────────────────────────────────────────────────────────

/** Readable text on ``hex``: dark ink on a light colour, white otherwise. */
export function onAccent(hex: string): string {
  let h = hex.slice(1)
  if (h.length <= 4) h = [...h.slice(0, 3)].map((c) => c + c).join('')
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
  const lin = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  const lum = 0.2126 * lin(r!) + 0.7152 * lin(g!) + 0.0722 * lin(b!)
  // Contrast against white vs. against #1A1814 (luminance ~0.01). White
  // wins near a tie (the brand orange is ~4.2:1 either way), as on the GFS
  // page; a light colour (yellow, pastel) gets the dark ink.
  const white = 1.05 / (lum + 0.05)
  const dark = (lum + 0.05) / 0.06
  return white >= 4 || white >= dark ? '#fff' : '#1A1814'
}

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => [...w][0]!.toUpperCase()).join('')
}

function Shell({
  accent,
  serverName,
  cover,
  children,
}: {
  accent?: string
  serverName: string
  cover?: string | null
  children: ComponentChildren
}) {
  const style = accent ? { '--accent': accent, '--on-accent': onAccent(accent) } : undefined
  return (
    <div class="page" style={style}>
      {cover && <img class="page-cover" src={cover} alt="" />}
      <main class="page-main">{children}</main>
      <footer class="page-footer">
        {t('page.footer', { server: serverName })}{' '}
        <a href={WEBSITE} rel="noopener noreferrer">Social Home</a>
      </footer>
    </div>
  )
}

function BackLink({ href, label }: { href: string; label: string }) {
  return <a class="page-back" href={href}>← {label}</a>
}

function NotFound({ boot, serverName }: { boot: PageBoot; serverName: string }) {
  const keys: Record<PageBoot['view'], [MessageKey, MessageKey]> = {
    space: ['space.not_found', 'space.not_found_body'],
    join: ['join.dead', 'join.dead_body'],
    person: ['person.not_found', 'person.not_found_body'],
  }
  const [title, body] = keys[boot.view]
  const server = serverName || new URL(boot.gfs).host
  return (
    <Shell serverName={server}>
      <div class="page-accent" />
      <h1>{t(title)}</h1>
      {boot.view === 'join' && <p class="page-muted">{t('join.on', { server })}</p>}
      <p>{t(body, { server })}</p>
      {boot.view === 'join' && <p class="page-muted">{t('join.dead_ask')}</p>}
      <p><BackLink href={`${boot.gfs}/`} label={t('page.back', { server })} /></p>
    </Shell>
  )
}

export function SpacePage({ gfs, data }: { gfs: string; data: SpacePageData }) {
  const about = renderMarkdown(data.about)
  return (
    <Shell accent={data.accent} serverName={data.serverName} cover={data.cover}>
      {data.icon && <img class="page-avatar" src={data.icon} alt="" />}
      <div class="page-accent" />
      <BackLink href={`${gfs}/`} label={t('page.back', { server: data.serverName })} />
      <h1>{data.name || '—'}</h1>
      <p class="page-muted">
        {tn('space.members', data.members)} · {t(`cat.${data.category}` as MessageKey)}
      </p>
      <section class="page-cta">
        <a class="page-btn" href={`${gfs}/`}>{t('space.cta')}</a>
        <p class="page-muted page-small">{t('space.cta_hint')}</p>
      </section>
      {(data.description || about.length > 0) && (
        <section class="page-card" aria-labelledby="about-h">
          <h2 id="about-h">{t('space.about')}</h2>
          {data.description && <p>{data.description}</p>}
          {about.length > 0 && <div class="page-md">{about}</div>}
        </section>
      )}
    </Shell>
  )
}

export function InvitePage({ gfs, data }: { gfs: string; data: InvitePageData }) {
  const [copy, setCopy] = useState<'idle' | 'copied' | 'failed'>('idle')
  const codeEl = useRef<HTMLElement | null>(null)

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(data.code)
      setCopy('copied')
    } catch {
      // No clipboard (an older browser, a denied permission): select the
      // code so one more keystroke copies it.
      const el = codeEl.current
      const sel = window.getSelection()
      if (el && sel) {
        const range = document.createRange()
        range.selectNodeContents(el)
        sel.removeAllRanges()
        sel.addRange(range)
      }
      setCopy('failed')
    }
  }

  return (
    <Shell accent={data.accent} serverName={data.serverName}>
      <div class="page-accent" />
      {data.icon && <img class="page-avatar" src={data.icon} alt="" />}
      <h1>{t('join.title', { space: data.name })}</h1>
      <p class="page-muted">{t('join.on', { server: data.serverName })}</p>
      <div class="page-handoff">
        {data.qr && <img class="page-qr" src={data.qr} alt={t('join.qr_alt')} />}
        <div class="page-steps">
          <ol>
            <li>{t('join.step1')}</li>
            <li>{t('join.step2')}</li>
            <li>{t('join.step3')}</li>
          </ol>
          <span class="page-label" id="invite-code-label">{t('join.code_label')}</span>
          <code class="page-code" ref={codeEl} aria-labelledby="invite-code-label">{data.code}</code>
          <div class="page-copy">
            <button type="button" class="page-btn" onClick={() => void onCopy()}>
              {copy === 'copied' ? t('join.copied') : t('join.copy')}
            </button>
            <p class="page-muted page-small" role="status">
              {copy === 'failed' ? t('join.copy_failed') : ''}
            </p>
          </div>
        </div>
      </div>
      <p class="page-muted">{t('join.hint')}</p>
      <p class="page-muted page-small">
        {t('join.new')}{' '}
        <a href={WEBSITE} rel="noopener noreferrer">{t('join.learn')}</a>
      </p>
      <p><BackLink href={`${gfs}/`} label={t('page.back', { server: data.serverName })} /></p>
    </Shell>
  )
}

/** The Follow button's target: the website's follow hand-off, as the
 *  GFS-rendered page links it. */
export function followUrl(gfsId: string, userId: string): string {
  return `${WEBSITE}/momentum/follow?${new URLSearchParams({ gfs: gfsId, user: userId })}`
}

export function PersonPage({ gfs, data }: { gfs: string; data: PersonPageData }) {
  return (
    <Shell serverName={data.serverName}>
      <BackLink href={`${gfs}/moments`} label={t('person.directory')} />
      <article class="page-card page-person">
        {data.picture
          ? <img class="page-avatar" src={data.picture} alt="" />
          : <span class="page-avatar page-initials" aria-hidden="true">{initials(data.displayName)}</span>}
        <h1>{data.displayName}</h1>
        <p class="page-muted">@{data.username} · {data.instanceId}</p>
        {data.bio && <p>{data.bio}</p>}
        <p class="page-muted">{tn('person.followers', data.followers)}</p>
        <div class="page-cta">
          <a class="page-btn" href={followUrl(data.gfsId, data.userId)} rel="noopener noreferrer">
            {t('person.follow')}
          </a>
        </div>
      </article>
    </Shell>
  )
}

// ─── The page ─────────────────────────────────────────────────────────────

type State =
  | { status: 'loading' }
  | { status: 'ready'; data: SpacePageData | InvitePageData | PersonPageData }
  | { status: 'not_found'; serverName: string }
  | { status: 'error'; message: string }

function title(boot: PageBoot, state: State): string {
  const server = state.status === 'ready'
    ? state.data.serverName
    : state.status === 'not_found' && state.serverName
      ? state.serverName
      : new URL(boot.gfs).host
  if (state.status === 'ready') {
    const d = state.data
    if (boot.view === 'space') return t('title.space', { space: (d as SpacePageData).name, server })
    if (boot.view === 'join') return t('title.join', { space: (d as InvitePageData).name, server })
    return t('title.person', { name: (d as PersonPageData).displayName })
  }
  if (state.status === 'not_found') {
    return boot.view === 'join' ? t('title.join_dead', { server }) : t('title.not_found', { server })
  }
  return server
}

function PageView({ boot, retry }: { boot: PageBoot; retry: RetryControl }) {
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    loadPage(boot)
      .then((data) => { if (!cancelled) setState({ status: 'ready', data }) })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof PageNotFound) setState({ status: 'not_found', serverName: err.serverName })
        else setState({ status: 'error', message: t(err instanceof PageBusy ? 'error.busy' : 'error.page') })
      })
    return () => { cancelled = true }
  }, [boot])

  useEffect(() => {
    if (state.status !== 'loading') document.title = title(boot, state)
  }, [boot, state])

  if (state.status === 'loading') {
    return <div class="page"><p class="page-status" role="status">{t('loading')}</p></div>
  }
  if (state.status === 'not_found') return <NotFound boot={boot} serverName={state.serverName} />
  if (state.status === 'error') {
    return (
      <div class="page">
        <main class="page-main">
          <ViewerError class="highlight-error" message={state.message} retry={retry} />
        </main>
      </div>
    )
  }
  const { data } = state
  if (boot.view === 'space') return <SpacePage gfs={boot.gfs} data={data as SpacePageData} />
  if (boot.view === 'join') return <InvitePage gfs={boot.gfs} data={data as InvitePageData} />
  return <PersonPage gfs={boot.gfs} data={data as PersonPageData} />
}

/** The page for ``boot``, with "Try again" on a network failure. */
export function PublicPage({ boot }: { boot: PageBoot }) {
  return (
    <RetryHost render={(retry, attempt) => <PageView key={attempt} boot={boot} retry={retry} />} />
  )
}
