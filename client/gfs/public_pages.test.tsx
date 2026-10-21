/**
 * A GFS's space page, invite landing and Momentum profile in the pinned
 * viewer: fetched from the GFS the link names (cross-origin, no
 * credentials, the invite token in a POST body), checked, and rendered as
 * text — in the visitor's language.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import type { PageBoot } from './page_link'
import {
  PublicPage,
  followUrl,
  onAccent,
  parseInvite,
  parsePerson,
  parseSpace,
} from './public_pages'
import { setLocale } from './viewer_i18n'

const G = 'https://gfs.example.org'
const PNG = 'data:image/png;base64,iVBORw0KGgo='

const SPACE = {
  server_name: 'Bern GFS',
  space: {
    space_id: 'sp-garden',
    name: 'Garden Club',
    description: 'Tomatoes and swaps',
    about_markdown: '**Fridays** <script>x</script>',
    cover: PNG,
    icon: null,
    primary_color: '#3a7d44',
    accent_color: '#3a7d44',
    member_count: 1,
    category: 'hobby_crafts',
  },
}
const INVITE = {
  server_name: 'Bern GFS',
  space: { name: 'Garden Club', accent_color: '#ffee00', icon: PNG },
  invite_code: 'socialhome://invite#eyJ0IjoxfQ',
  qr: PNG,
}
const PERSON = {
  server_name: 'Bern GFS',
  gfs_id: 'gfs-1',
  person: {
    user_id: 'u-noor',
    username: 'noor',
    display_name: 'Noor Haddad',
    instance_id: 'inst-noor',
    bio: 'Beekeeper.',
    picture: null,
    follower_count: 3,
  },
}

let calls: { url: string; init: RequestInit | undefined }[]
let answer: (url: string) => Response

beforeEach(() => {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    return answer(url)
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  setLocale('en')
  document.title = ''
})

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

function expectViewerRequest(init: RequestInit | undefined) {
  expect(init?.mode).toBe('cors')
  expect(init?.credentials).toBe('omit')
  expect(init?.referrerPolicy).toBe('no-referrer')
  expect(init?.redirect).toBe('error')
}

describe('space page', () => {
  const boot: PageBoot = { view: 'space', gfs: G, spaceId: 'sp-garden' }

  it('shows the space from the GFS JSON', async () => {
    answer = () => json(SPACE)
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.querySelector('h1')?.textContent).toBe('Garden Club'))
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(`${G}/gfs/pages/spaces/sp-garden`)
    expectViewerRequest(calls[0]!.init)
    expect(container.textContent).toContain('1 member · Hobby & crafts')
    expect(container.textContent).toContain('Tomatoes and swaps')
    expect(container.querySelector('.page-md strong')?.textContent).toBe('Fridays')
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('.page-cover')?.getAttribute('src')).toBe(PNG)
    const cta = container.querySelector('a.page-btn')!
    expect(cta.getAttribute('href')).toBe(`${G}/`)
    expect(cta.textContent).toBe('Connect your Social Home')
    expect((container.querySelector('.page') as HTMLElement).style.getPropertyValue('--accent')).toBe('#3a7d44')
    await waitFor(() => expect(document.title).toBe('Garden Club — Bern GFS'))
  })

  it('speaks German for a German browser', async () => {
    setLocale('de')
    answer = () => json({ ...SPACE, space: { ...SPACE.space, member_count: 12 } })
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.textContent).toContain('12 Mitglieder · Hobby & Basteln'))
    expect(container.textContent).toContain('Verbinde dein Social Home')
    expect(container.textContent).toContain('Zurück zu Bern GFS')
  })

  it('reads "not listed" for a 404, naming the server', async () => {
    answer = () => json({ server_name: 'Bern GFS' }, 404)
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.querySelector('h1')?.textContent).toBe('This space isn’t listed here'))
    expect(container.textContent).toContain('taken off Bern GFS')
    await waitFor(() => expect(document.title).toBe('Not found — Bern GFS'))
  })

  it('offers a retry when the GFS cannot be reached', async () => {
    let fail = true
    answer = () => {
      if (fail) throw new TypeError('Failed to fetch')
      return json(SPACE)
    }
    const { container, getByRole } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.textContent).toContain('couldn’t be loaded'))
    fail = false
    fireEvent.click(getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.querySelector('h1')?.textContent).toBe('Garden Club'))
  })

  it('treats a malformed answer as a load failure, not as content', async () => {
    answer = () => json({ server_name: 'x', space: { name: 5 } })
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.textContent).toContain('couldn’t be loaded'))
  })

  it('says when the GFS is busy', async () => {
    answer = () => json({ error: 'rate_limited' }, 429)
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.textContent).toContain('Too many viewers'))
  })
})

describe('invite page', () => {
  const boot: PageBoot = { view: 'join', gfs: G, token: 'tok_abc' }

  it('posts the token and shows the code, its QR and the steps', async () => {
    answer = () => json(INVITE)
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.querySelector('h1')?.textContent).toBe('You’re invited to Garden Club'))
    expect(calls[0]!.url).toBe(`${G}/gfs/pages/invite`)
    expect(calls[0]!.init?.method).toBe('POST')
    expect(JSON.parse(String(calls[0]!.init?.body))).toEqual({ token: 'tok_abc' })
    expect(calls[0]!.url).not.toContain('tok_abc')
    expectViewerRequest(calls[0]!.init)
    expect(container.querySelector('.page-code')?.textContent).toBe(INVITE.invite_code)
    expect(container.querySelector('.page-qr')?.getAttribute('alt')).toBe('QR code of the invite code')
    expect(container.querySelectorAll('ol li')).toHaveLength(3)
    // A light space colour gets dark text on its buttons.
    expect((container.querySelector('.page') as HTMLElement).style.getPropertyValue('--on-accent')).toBe('#1A1814')
    await waitFor(() => expect(document.title).toBe('Join Garden Club — Bern GFS'))
  })

  it('copies the code', async () => {
    const writeText = vi.fn(async () => undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    answer = () => json(INVITE)
    const { getByRole } = render(<PublicPage boot={boot} />)
    const button = await waitFor(() => getByRole('button', { name: 'Copy code' }))
    fireEvent.click(button)
    await waitFor(() => expect(button.textContent).toBe('Copied ✓'))
    expect(writeText).toHaveBeenCalledWith(INVITE.invite_code)
  })

  it('selects the code when the clipboard is refused', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(async () => { throw new Error('no') }) } })
    answer = () => json(INVITE)
    const { getByRole, container } = render(<PublicPage boot={boot} />)
    fireEvent.click(await waitFor(() => getByRole('button', { name: 'Copy code' })))
    await waitFor(() => expect(container.textContent).toContain('Select the code and copy it.'))
    expect(window.getSelection()?.toString()).toBe(INVITE.invite_code)
  })

  it('reads expired-or-revoked for a dead link', async () => {
    answer = () => json({ server_name: 'Bern GFS' }, 404)
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.querySelector('h1')?.textContent).toBe('This invite has expired or was revoked'))
    expect(container.textContent).toContain('Ask whoever shared it for a fresh link.')
    expect(container.textContent).not.toContain('tok_abc')
    await waitFor(() => expect(document.title).toBe('Invite unavailable — Bern GFS'))
  })

  it('names the GFS host when even the 404 is unreadable', async () => {
    answer = () => new Response('nope', { status: 404 })
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.textContent).toContain('on gfs.example.org'))
  })
})

describe('person page', () => {
  const boot: PageBoot = { view: 'person', gfs: G, userId: 'u-noor' }

  it('shows the profile and the Follow button', async () => {
    answer = () => json(PERSON)
    const { container } = render(<PublicPage boot={boot} />)
    await waitFor(() => expect(container.querySelector('h1')?.textContent).toBe('Noor Haddad'))
    expect(calls[0]!.url).toBe(`${G}/gfs/pages/people/u-noor`)
    expect(container.textContent).toContain('@noor · inst-noor')
    expect(container.textContent).toContain('3 followers')
    expect(container.querySelector('.page-initials')?.textContent).toBe('NH')
    const follow = container.querySelector('a.page-btn')!
    expect(follow.getAttribute('href')).toBe(followUrl('gfs-1', 'u-noor'))
    expect(container.querySelector('a.page-back')?.getAttribute('href')).toBe(`${G}/moments`)
    await waitFor(() => expect(document.title).toBe('Noor Haddad — Momentum'))
  })
})

describe('response checks', () => {
  it('drops images that are not raster data URIs and colours that are not hex', () => {
    const s = parseSpace({
      server_name: 'x',
      space: {
        ...SPACE.space,
        cover: 'https://tracker.example/a.png',
        icon: 'data:image/svg+xml;base64,PHN2Zz4=',
        primary_color: 'red;background:url(x)',
        accent_color: 'url(x)',
        category: 'evil',
      },
    })
    expect(s.cover).toBeNull()
    expect(s.icon).toBeNull()
    expect(s.accent).toBe('#D2542A')
    expect(s.category).toBe('general')
  })

  it('refuses an invite code of another scheme', () => {
    expect(() => parseInvite({ ...INVITE, invite_code: 'javascript:alert(1)' })).toThrow()
    expect(parseInvite({ ...INVITE, qr: 'data:image/gif;base64,R0lG' }).qr).toBeNull()
  })

  it('bounds the text it shows', () => {
    const p = parsePerson({ ...PERSON, person: { ...PERSON.person, bio: 'x'.repeat(10_000), follower_count: -4 } })
    expect(p.bio).toHaveLength(4000)
    expect(p.followers).toBe(0)
  })

  it('builds the follow link with its values encoded', () => {
    expect(followUrl('g&x', 'u 1')).toBe('https://social-home.io/momentum/follow?gfs=g%26x&user=u+1')
  })

  it('picks readable text for the accent', () => {
    expect(onAccent('#D2542A')).toBe('#fff')
    expect(onAccent('#1a1814')).toBe('#fff')
    expect(onAccent('#ffee00')).toBe('#1A1814')
    expect(onAccent('#fff')).toBe('#1A1814')
  })
})
