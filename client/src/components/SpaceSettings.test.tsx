import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({
  t: (key: string) => key,
  locale: { value: 'en' },
  setLocale: vi.fn(),
}))
vi.mock('@/api', () => {
  const m = { get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SpaceSettings } from './SpaceSettings'
import { api } from '@/api'
import { showToast } from './Toast'

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>
  patch: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
}

function makeSpace(overrides: Partial<{
  retention_days: number | null
  features: object
  archived: boolean
  archived_reason: 'dissolved' | 'removed' | null
  has_remote_households: boolean
}> = {}) {
  return {
    id: 's-1',
    name: 'Trip group',
    description: '',
    emoji: null,
    space_type: 'private' as const,
    join_mode: 'invite_only' as const,
    features: overrides.features ?? {
      calendar: true, todo: true, location: false,
      stickies: false, pages: true, gallery: true,
      posts_access: 'open', pages_access: 'open',
      stickies_access: 'open', calendar_access: 'open',
      tasks_access: 'open',
      allowed_post_types: ['text'],
    },
    retention_days: overrides.retention_days ?? null,
    archived: overrides.archived ?? false,
    archived_reason: overrides.archived_reason ?? null,
    has_remote_households: overrides.has_remote_households ?? false,
  } as never
}

describe('SpaceSettings', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
  })

  it('module exports exist', async () => {
    const mod = await import('./SpaceSettings')
    expect(mod).toBeTruthy()
    expect(typeof mod.SpaceSettings).toBe('function')
  })

  it('renders the retention input prefilled with the space value', () => {
    const space = makeSpace({ retention_days: 30 })
    const { container } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const input = container.querySelector(
      'input[type="number"]',
    ) as HTMLInputElement | null
    expect(input).toBeTruthy()
    expect(input!.value).toBe('30')
  })

  it('sends retention_days in the PATCH on save', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({ retention_days: null })
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const input = container.querySelector(
      'input[type="number"]',
    ) as HTMLInputElement
    fireEvent.input(input, { target: { value: '90' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledOnce()
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.retention_days).toBe(90)
  })

  it('sends 0 for retention_days when the field is cleared (= forever)', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({ retention_days: 90 })
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const input = container.querySelector(
      'input[type="number"]',
    ) as HTMLInputElement
    fireEvent.input(input, { target: { value: '' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.retention_days).toBe(0)
  })

  it('offers the followers switch, OFF by default, gating the two engagement boxes', () => {
    // ``allow_subscribers`` is the readability opt-in — it is what makes a
    // public / global space publicly readable, independently of join_mode.
    // Defaults OFF, and while it is off "let followers react / comment" are
    // meaningless, so they are disabled.
    const space = makeSpace()
    const { getByLabelText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const follow = getByLabelText(/Let anyone follow this space/) as HTMLInputElement
    expect(follow.checked).toBe(false)
    expect(
      (getByLabelText(/Let followers leave reactions/) as HTMLInputElement).disabled,
    ).toBe(true)
    expect(
      (getByLabelText(/Let followers comment on posts/) as HTMLInputElement).disabled,
    ).toBe(true)
  })

  it('sends allow_subscribers in the PATCH features block when turned on', async () => {
    apiMock.patch.mockResolvedValue({})
    const space = makeSpace()
    const { getByLabelText, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const follow = getByLabelText(/Let anyone follow this space/) as HTMLInputElement
    fireEvent.click(follow)
    // The engagement boxes come alive once followers may exist.
    expect(
      (getByLabelText(/Let followers leave reactions/) as HTMLInputElement).disabled,
    ).toBe(false)
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalled())
    const [, body] = apiMock.patch.mock.calls[0]
    // …and the join mode is untouched (not even re-sent): the two dials are
    // independent.
    expect(body).toEqual({ features: { allow_subscribers: true } })
  })

  it('reflects an already-on allow_subscribers from the space payload', () => {
    const space = makeSpace({
      features: {
        calendar: true, todo: true, location: false,
        stickies: false, pages: true, gallery: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open',
        tasks_access: 'open',
        allow_subscribers: true,
      },
    })
    const { getByLabelText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    expect(
      (getByLabelText(/Let anyone follow this space/) as HTMLInputElement).checked,
    ).toBe(true)
    expect(
      (getByLabelText(/Let followers comment on posts/) as HTMLInputElement).disabled,
    ).toBe(false)
  })

  it('renders the Features fieldset with seven toggle checkboxes', () => {
    const space = makeSpace()
    const { getByTestId } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-features')
    const checkboxes = fieldset.querySelectorAll('input[type="checkbox"]')
    // Pages, Calendar, Timetable, Tasks, Stickies, Gallery, Bazaar.
    expect(checkboxes.length).toBe(7)
  })

  it('mirrors the space features in the Features fieldset', () => {
    const space = makeSpace({
      features: {
        calendar: false, todo: true, location: false,
        stickies: true, pages: false, gallery: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open',
        tasks_access: 'open',
      },
    })
    const { getByTestId } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-features')
    const checkboxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    // Order matches the JSX: pages, calendar, timetable, tasks, stickies,
    // gallery, bazaar. ``bazaar`` is omitted from the payload → defaults
    // on; ``timetable`` is an opt-in → defaults off.
    expect(checkboxes.map((c) => c.checked)).toEqual([
      false, false, false, true, true, true, true,
    ])
  })

  it('defaults Features toggles ON for a space whose features payload omits the keys', () => {
    const space = makeSpace({
      features: {
        // Empty-ish payload — simulates an upstream that doesn't
        // surface the per-space feature flags. The SpaceFeatures
        // dataclass default on the backend is all-on (except
        // location, which is an opt-in privacy contract); the SPA
        // mirrors that.
        location: false,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open',
        tasks_access: 'open',
      },
    })
    const { getByTestId } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-features')
    const checkboxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    // pages, calendar, tasks, stickies, gallery, bazaar — all on; the
    // timetable (3rd) is an opt-in and stays off.
    expect(checkboxes.map((c) => c.checked)).toEqual([
      true, true, false, true, true, true, true,
    ])
  })

  it('sends only the feature toggles that changed, as a partial block', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace()
    const { getByTestId, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-features')
    const checkboxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    // Flip pages OFF (was true) and gallery OFF (was true).
    fireEvent.change(checkboxes[0], { target: { checked: false } })
    fireEvent.change(checkboxes[5], { target: { checked: false } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledOnce()
    const [, body] = apiMock.patch.mock.calls[0]
    // The backend merges a partial block onto the current features, so the
    // untouched toggles stay out.
    expect(body).toEqual({ features: { pages: false, gallery: false } })
  })

  it('defaults gallery=true for a pre-migration space whose features lack the key', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({
      features: {
        // No ``gallery`` key — simulates a row that pre-dates the
        // 0008 migration. The dataclass default on the backend is
        // True; the SPA mirrors that so existing spaces still expose
        // the gallery toggle as on.
        calendar: false, todo: true, location: false,
        stickies: false, pages: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open',
        tasks_access: 'open',
      },
    })
    const { getByTestId, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-features')
    const checkboxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    // Gallery is the 6th checkbox — should default to checked.
    expect(checkboxes[5].checked).toBe(true)
    // Flip calendar (was off) so there is something to save: the defaulted
    // gallery=true counts as unchanged, so it isn't re-sent.
    fireEvent.change(checkboxes[1], { target: { checked: true } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body).toEqual({ features: { calendar: true } })
  })

  it('offers the Timetable feature (off by default) and sends it in the diff', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const { getByLabelText, getByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
    )
    const box = getByLabelText(/space\.feature\.timetable/) as HTMLInputElement
    expect(box.checked).toBe(false)
    expect(getByText('space.feature.timetable_sub')).toBeTruthy()
    fireEvent.change(box, { target: { checked: true } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body).toEqual({ features: { timetable: true } })
  })

  it('mirrors an enabled Timetable feature and turns it off', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({
      features: {
        calendar: true, todo: true, location: false,
        stickies: false, pages: true, gallery: true, timetable: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open',
        tasks_access: 'open',
        allowed_post_types: ['text'],
      },
    })
    const { getByLabelText, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const box = getByLabelText(/space\.feature\.timetable/) as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.change(box, { target: { checked: false } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body).toEqual({ features: { timetable: false } })
  })

  it('renders the Post types fieldset reflecting allowed_post_types', () => {
    const space = makeSpace({
      features: {
        calendar: true, todo: true, location: false,
        stickies: true, pages: true, gallery: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open', tasks_access: 'open',
        allowed_post_types: ['text', 'image'],
      },
    })
    const { getByTestId } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-post-types')
    const boxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    // Order: text, image, video, file, poll, schedule, location,
    // highlight_share. (Bazaar is a tab feature now, not a post type.)
    expect(boxes).toHaveLength(8)
    expect(boxes[0].checked).toBe(true)  // text
    expect(boxes[1].checked).toBe(true)  // image
    expect(boxes[2].checked).toBe(false) // video (not in the list)
    expect(boxes[7].checked).toBe(false) // highlight_share
  })

  it('sends allowed_post_types on save, preserving non-composer types', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({
      features: {
        calendar: true, todo: true, location: false,
        stickies: true, pages: true, gallery: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open', tasks_access: 'open',
        // transcript + event aren't in the settings UI — they must
        // survive a save untouched rather than being silently dropped.
        allowed_post_types: ['text', 'transcript', 'event'],
      },
    })
    const { getByTestId, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-post-types')
    const boxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    // Turn poll (index 4) ON in addition to the already-on text.
    fireEvent.change(boxes[4], { target: { checked: true } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    const allowed: string[] = body.features.allowed_post_types
    expect(allowed).toContain('text')
    expect(allowed).toContain('poll')
    // Preserved, even though they have no checkbox.
    expect(allowed).toContain('transcript')
    expect(allowed).toContain('event')
    // Never-enabled composer type stays out.
    expect(allowed).not.toContain('video')
  })

  it('refuses to save when every post type is disabled', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace({
      features: {
        calendar: true, todo: true, location: false,
        stickies: true, pages: true, gallery: true,
        posts_access: 'open', pages_access: 'open',
        stickies_access: 'open', calendar_access: 'open', tasks_access: 'open',
        allowed_post_types: ['text'],
      },
    })
    const { getByTestId, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-post-types')
    const boxes = Array.from(
      fieldset.querySelectorAll('input[type="checkbox"]'),
    ) as HTMLInputElement[]
    boxes.forEach((b) => fireEvent.change(b, { target: { checked: false } }))
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).not.toHaveBeenCalled()
  })

  it('preserves typed name across re-renders triggered by sibling state', async () => {
    // Regression for the signal-in-render footgun: previously the
    // form rebuilt fresh ``signal()`` instances on every render, so
    // typing into ``name`` and then triggering a render via a sibling
    // change (e.g. toggling location-sharing) silently dropped the
    // typed value back to the prop default. ``useSignal`` keeps the
    // instance stable; this test guards that invariant.
    apiMock.patch.mockResolvedValueOnce({})
    const space = makeSpace()
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    // Name input is the first ``<input>`` in the form (no ``type``
    // attribute — defaults to ``text``).
    const nameInput = container.querySelector(
      '.sh-form input',
    ) as HTMLInputElement
    expect(nameInput).toBeTruthy()
    fireEvent.input(nameInput, { target: { value: 'New name' } })
    // Trigger a re-render by toggling the location checkbox.
    const checkbox = container.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement
    fireEvent.change(checkbox, { target: { checked: true } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.name).toBe('New name')
  })

  it('renders a pending publication with a muted "Pending review" label, no public link', async () => {
    // /api/gfs/connections then /api/spaces/{id}/publications.
    apiMock.get.mockReset()
    apiMock.get.mockImplementation((url: string) => {
      if (url.includes('/connections')) {
        return Promise.resolve([
          { id: 'gfs-1', gfs_instance_id: 'i1', display_name: 'Town GFS',
            inbox_url: 'https://gfs.example.com', status: 'active',
            paired_at: '', published_space_count: 0 },
        ])
      }
      return Promise.resolve([
        { space_id: 's-1', gfs_connection_id: 'gfs-1',
          published_at: '2026-06-06T00:00:00+00:00', status: 'pending' },
      ])
    })
    const space = makeSpace()
    const { container, queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    await new Promise(r => setTimeout(r, 0))
    // Pending label shown, not the green "published" label.
    expect(queryByText('space.publish_pending')).toBeTruthy()
    expect(queryByText('space.published')).toBeNull()
    // No live public link in pending state; the pending hint shows instead.
    expect(container.querySelector('.sh-federation-public-url')).toBeNull()
    expect(queryByText('space.publish_pending_hint')).toBeTruthy()
  })

  it('renders the live public link only for an active publication', async () => {
    apiMock.get.mockReset()
    apiMock.get.mockImplementation((url: string) => {
      if (url.includes('/connections')) {
        return Promise.resolve([
          { id: 'gfs-1', gfs_instance_id: 'i1', display_name: 'Town GFS',
            inbox_url: 'https://gfs.example.com', status: 'active',
            paired_at: '', published_space_count: 1 },
        ])
      }
      return Promise.resolve([
        { space_id: 's-1', gfs_connection_id: 'gfs-1',
          published_at: '2026-06-06T00:00:00+00:00', status: 'active' },
      ])
    })
    const space = makeSpace()
    const { container, queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    await new Promise(r => setTimeout(r, 0))
    expect(queryByText('space.published')).toBeTruthy()
    const link = container.querySelector(
      '.sh-federation-public-url__link',
    ) as HTMLAnchorElement | null
    expect(link).toBeTruthy()
    expect(link!.getAttribute('href')).toBe(
      'https://gfs.example.com/spaces/s-1',
    )
  })

  it('does NOT render a Publish button for a non-active (pending) GFS', async () => {
    apiMock.get.mockReset()
    apiMock.get.mockImplementation((url: string) => {
      if (url.includes('/connections')) {
        return Promise.resolve([
          { id: 'gfs-active', gfs_instance_id: 'i1', display_name: 'Active GFS',
            inbox_url: 'https://active.example.com', status: 'active',
            paired_at: '', published_space_count: 0 },
          { id: 'gfs-pending', gfs_instance_id: 'i2', display_name: 'Pending GFS',
            inbox_url: 'https://pending.example.com', status: 'pending',
            paired_at: '', published_space_count: 0 },
        ])
      }
      return Promise.resolve([]) // nothing published
    })
    const space = makeSpace()
    const { container, queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    await new Promise(r => setTimeout(r, 0))
    // Active GFS row renders with a Publish button.
    expect(container.querySelector('[data-testid="gfs-row-gfs-active"]')).toBeTruthy()
    // Pending GFS gets NO publish row at all.
    expect(container.querySelector('[data-testid="gfs-row-gfs-pending"]')).toBeNull()
    // Exactly one Publish button (the active one).
    const publishBtns = Array.from(container.querySelectorAll('button'))
      .filter(b => b.textContent === 'gfs.publish')
    expect(publishBtns).toHaveLength(1)
    // A muted note explains the held connection.
    expect(queryByText('space.gfs_pending_note')).toBeTruthy()
  })

  it('confirms before publishing, then POSTs and shows the returned status', async () => {
    apiMock.get.mockReset()
    apiMock.get.mockImplementation((url: string) => {
      if (url.includes('/connections')) {
        return Promise.resolve([
          { id: 'gfs-1', gfs_instance_id: 'i1', display_name: 'Town GFS',
            inbox_url: 'https://gfs.example.com', status: 'active',
            paired_at: '', published_space_count: 0 },
        ])
      }
      return Promise.resolve([]) // not published yet
    })
    apiMock.post = vi.fn().mockResolvedValue({
      space_id: 's-1', gfs_connection_id: 'gfs-1',
      published_at: '2026-06-06T00:00:00+00:00', status: 'pending',
    })
    const space = makeSpace()
    const { container, getByText, queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    await new Promise(r => setTimeout(r, 0))
    // Clicking Publish opens the confirm dialog — it does NOT post yet.
    fireEvent.click(getByText('gfs.publish'))
    expect(apiMock.post).not.toHaveBeenCalled()
    const dialog = container.querySelector('[role="dialog"]') as HTMLElement | null
    expect(dialog).toBeTruthy()
    expect(dialog!.textContent).toContain('Publish this space?')
    // Confirm → POST fires, and the row reflects the pending status.
    const confirmBtn = dialog!.querySelector('.sh-btn--primary') as HTMLButtonElement
    fireEvent.click(confirmBtn)
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.post).toHaveBeenCalledWith('/api/spaces/s-1/publish/gfs-1')
    expect(queryByText('space.publish_pending')).toBeTruthy()
  })

  it('shows the Unarchive button for a plain admin-archived space', () => {
    const space = makeSpace({ archived: true, archived_reason: null })
    const { getByText, queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    expect(getByText('Unarchive space')).toBeTruthy()
    expect(queryByText(/dissolved by its owner/i)).toBeNull()
    expect(queryByText(/removed from this space/i)).toBeNull()
  })

  it('hides Unarchive and explains a dissolved space cannot be reactivated', () => {
    const space = makeSpace({ archived: true, archived_reason: 'dissolved' })
    const { queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    expect(queryByText('Unarchive space')).toBeNull()
    expect(queryByText(/dissolved by its owner/i)).toBeTruthy()
    expect(queryByText(/can't be reactivated/i)).toBeTruthy()
  })

  it('hides Unarchive and explains a removed space cannot be reactivated', () => {
    const space = makeSpace({ archived: true, archived_reason: 'removed' })
    const { queryByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    expect(queryByText('Unarchive space')).toBeNull()
    expect(queryByText(/removed from this space/i)).toBeTruthy()
    expect(queryByText(/can't be reactivated/i)).toBeTruthy()
  })

  it('proposes a publication-tier change via POST /proposals', async () => {
    apiMock.post = vi.fn().mockResolvedValue({ proposal: { status: 'pending' } })
    const space = makeSpace()
    const { getByText, container } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    // The tier <select> defaults to the space's current tier; the button is
    // disabled until it changes.
    const selects = container.querySelectorAll('select')
    const tierSelect = Array.from(selects).find((s) =>
      Array.from(s.options).some((o) => o.value === 'global'),
    ) as HTMLSelectElement
    expect(tierSelect).toBeTruthy()
    fireEvent.change(tierSelect, { target: { value: 'public' } })
    fireEvent.click(getByText('Propose tier change'))
    await Promise.resolve()
    expect(apiMock.post).toHaveBeenCalledWith('/api/spaces/s-1/proposals', {
      action: 'set_public_tier',
      space_type: 'public',
    })
  })

  describe('dissolve', () => {
    async function proposeDissolve(result: Promise<unknown>) {
      const { isLocalDissolve } = await import('@/store/spaces')
      let markedDuringRequest = false
      apiMock.post.mockImplementationOnce(() => {
        // The server's ``dissolved`` frame lands while this request is
        // still in flight — the tab must already have claimed it.
        markedDuringRequest = isLocalDissolve('s-1')
        return result
      })
      const { getByText, getByRole } = render(
        <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
      )
      fireEvent.click(getByText('Dissolve space'))
      fireEvent.click(getByRole('button', { name: 'Propose dissolve' }))
      await vi.waitFor(() => expect(apiMock.post).toHaveBeenCalled())
      await new Promise(r => setTimeout(r, 0))
      return { markedDuringRequest, isLocalDissolve }
    }

    it('claims an executed dissolve and hard-navigates inside the ingress base', async () => {
      const spy = vi.spyOn(window, 'location', 'get')
      const loc = { href: '' } as Location
      spy.mockReturnValue(loc)
      try {
        const { markedDuringRequest } = await proposeDissolve(
          Promise.resolve({ proposal: { status: 'executed' } }),
        )
        expect(markedDuringRequest).toBe(true)
        expect(apiMock.post).toHaveBeenCalledWith(
          '/api/spaces/s-1/proposals', { action: 'dissolve' },
        )
        expect(loc.href.endsWith('/spaces')).toBe(true)
      } finally {
        spy.mockRestore()
      }
    })

    it('releases the claim when the dissolve is only proposed', async () => {
      const { markedDuringRequest, isLocalDissolve } = await proposeDissolve(
        Promise.resolve({ proposal: { status: 'pending', needed: 2 } }),
      )
      expect(markedDuringRequest).toBe(true)
      expect(isLocalDissolve('s-1')).toBe(false)
    })

    it('releases the claim when the request fails', async () => {
      const { isLocalDissolve } = await proposeDissolve(Promise.reject(new Error('nope')))
      expect(isLocalDissolve('s-1')).toBe(false)
    })
  })
})

describe('SpaceSettings — @here toggle', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
  })

  it('is OFF by default and sends allow_here_mention when turned on', async () => {
    apiMock.patch.mockResolvedValue({})
    const { getByLabelText, getByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
    )
    const box = getByLabelText(/notify everyone with @here/) as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalled())
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.allow_here_mention).toBe(true)
  })

  it('reflects an already-on toggle and can turn it off', async () => {
    apiMock.patch.mockResolvedValue({})
    const space = { ...(makeSpace() as object), allow_here_mention: true } as never
    const { getByLabelText, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const box = getByLabelText(/notify everyone with @here/) as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(box)
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalled())
    expect(apiMock.patch.mock.calls[0][1].allow_here_mention).toBe(false)
  })
})

describe('SpaceSettings — retention exempt types', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
    vi.mocked(showToast).mockReset()
  })

  const withExempt = (days: number | null, exempt: string[]) =>
    ({
      ...(makeSpace({ retention_days: days }) as object),
      retention_exempt_types: exempt,
    }) as never

  it('hides the keep-list while retention is off (forever)', () => {
    const { queryByTestId } = render(
      <SpaceSettings space={withExempt(null, [])} onUpdate={() => {}} />,
    )
    expect(queryByTestId('retention-exempt-types')).toBeNull()
  })

  it('shows the keep-list once a day count is typed', () => {
    const { container, queryByTestId } = render(
      <SpaceSettings space={withExempt(null, [])} onUpdate={() => {}} />,
    )
    const days = container.querySelector('input[type="number"]') as HTMLInputElement
    fireEvent.input(days, { target: { value: '30' } })
    const box = queryByTestId('retention-exempt-types')
    expect(box).not.toBeNull()
    const values = Array.from(
      box!.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'),
    ).map((i) => i.value)
    // Real PostType values only; text / transcript / highlight_share are
    // deliberately not offered.
    expect(values).toEqual([
      'image', 'video', 'file', 'poll', 'schedule', 'event', 'bazaar', 'location',
    ])
    expect(box!.textContent).toContain('space.retention_keep_legend')
    expect(box!.textContent).toContain('space.retention_keep_poll')
  })

  it('prefills from the space and sends the exact sorted list on save', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const { getByTestId, getByText } = render(
      <SpaceSettings space={withExempt(30, ['poll'])} onUpdate={() => {}} />,
    )
    const box = getByTestId('retention-exempt-types')
    const poll = box.querySelector('input[value="poll"]') as HTMLInputElement
    const event = box.querySelector('input[value="event"]') as HTMLInputElement
    expect(poll.checked).toBe(true)
    expect(event.checked).toBe(false)
    fireEvent.click(event)
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalledOnce())
    const [url, body] = apiMock.patch.mock.calls[0]
    expect(url).toBe('/api/spaces/s-1')
    // The unchanged day count isn't re-sent — only the edited list.
    expect(body).toEqual({ retention_exempt_types: ['event', 'poll'] })
    expect(showToast).toHaveBeenCalledWith('Space updated', 'success')
  })

  it('unchecking removes the type and keeps values the UI does not offer', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const { getByTestId, getByText } = render(
      <SpaceSettings space={withExempt(7, ['poll', 'text'])} onUpdate={() => {}} />,
    )
    fireEvent.click(
      getByTestId('retention-exempt-types').querySelector('input[value="poll"]')!,
    )
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalledOnce())
    expect(apiMock.patch.mock.calls[0][1].retention_exempt_types).toEqual(['text'])
  })

  it('leaves the stored list alone when retention is turned off', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const { container, getByText } = render(
      <SpaceSettings space={withExempt(30, ['poll'])} onUpdate={() => {}} />,
    )
    const days = container.querySelector('input[type="number"]') as HTMLInputElement
    fireEvent.input(days, { target: { value: '' } })
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalledOnce())
    expect(apiMock.patch.mock.calls[0][1]).toEqual({ retention_days: 0 })
  })

  it('shows the server error when the save is rejected', async () => {
    apiMock.patch.mockRejectedValueOnce(new Error('unknown retention exempt type'))
    const { getByTestId, getByText } = render(
      <SpaceSettings space={withExempt(30, [])} onUpdate={() => {}} />,
    )
    fireEvent.click(
      getByTestId('retention-exempt-types').querySelector('input[value="poll"]')!,
    )
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('unknown retention exempt type', 'error'),
    )
  })
})

describe('SpaceSettings — sends only what the admin changed', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
    vi.mocked(showToast).mockReset()
  })

  const nameInput = (container: Element) =>
    container.querySelector('.sh-form input') as HTMLInputElement

  it('a co-admin on another household renaming the space sends just the name', async () => {
    // Regression: the remote admin's copy never had the host's retention, so
    // the form showed "Forever" and a rename PATCHed ``retention_days: 0``
    // (plus every other displayed default) — the host then switched its
    // cleanup off. The body must carry only the edited field.
    apiMock.patch.mockResolvedValueOnce({})
    const space = {
      ...(makeSpace({ retention_days: null }) as object),
      retention_exempt_types: [],
    } as never
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} isRemoteSpace />,
    )
    fireEvent.input(nameInput(container), { target: { value: 'Summer trip' } })
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalledOnce())
    expect(apiMock.patch.mock.calls[0]).toEqual([
      '/api/spaces/s-1',
      { name: 'Summer trip' },
    ])
  })

  it('keeps loaded retention settings out of an unrelated save', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = {
      ...(makeSpace({ retention_days: 30 }) as object),
      retention_exempt_types: ['poll'],
    } as never
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    fireEvent.input(nameInput(container), { target: { value: 'Renamed' } })
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalledOnce())
    expect(apiMock.patch.mock.calls[0][1]).toEqual({ name: 'Renamed' })
  })

  it('sends an emptied description so it can actually be cleared', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const space = {
      ...(makeSpace() as object),
      description: 'Old blurb',
    } as never
    const { container, getByText } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const textarea = container.querySelector('.sh-form textarea') as HTMLTextAreaElement
    fireEvent.input(textarea, { target: { value: '' } })
    fireEvent.click(getByText('Save changes'))
    await vi.waitFor(() => expect(apiMock.patch).toHaveBeenCalledOnce())
    expect(apiMock.patch.mock.calls[0][1]).toEqual({ description: '' })
  })

  it('does not PATCH when nothing changed and says so', async () => {
    const { getByText } = render(
      <SpaceSettings space={makeSpace({ retention_days: 30 })} onUpdate={() => {}} />,
    )
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).not.toHaveBeenCalled()
    expect(showToast).toHaveBeenCalledWith('No changes to save', 'info')
  })

  it('an edit reverted before saving counts as no change', async () => {
    const { container, getByText } = render(
      <SpaceSettings space={makeSpace({ retention_days: 30 })} onUpdate={() => {}} />,
    )
    const days = container.querySelector('input[type="number"]') as HTMLInputElement
    fireEvent.input(days, { target: { value: '7' } })
    fireEvent.input(days, { target: { value: '30' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).not.toHaveBeenCalled()
  })
})

describe('SpaceSettings — who can contribute (§4.3)', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
  })

  function selects(container: Element) {
    const fs = container.querySelector('[data-testid="space-access"]')!
    return Object.fromEntries(
      Array.from(fs.querySelectorAll('select')).map(s => [
        (s as HTMLSelectElement).dataset.feature,
        s as HTMLSelectElement,
      ]),
    )
  }

  const values = (sel: HTMLSelectElement) => Array.from(sel.options).map(o => o.value)
  const reviewed = (sel: HTMLSelectElement) =>
    Array.from(sel.options).find(o => o.value === 'moderated')!

  it('offers one select per enabled feature, Reviewed for every feature', () => {
    // stickies is off in the default fixture → no stickies select.
    const { container, queryByTestId } = render(<SpaceSettings space={makeSpace()} onUpdate={() => {}} />)
    const s = selects(container)
    expect(Object.keys(s)).toEqual(['posts', 'pages', 'tasks', 'calendar'])
    for (const f of ['posts', 'pages', 'tasks', 'calendar']) {
      expect(values(s[f])).toEqual(['open', 'moderated', 'admin_only'])
      expect(reviewed(s[f]).disabled).toBe(false)
    }
    expect(s.posts.value).toBe('open')
    expect(queryByTestId('space-access-review-hint')).toBeNull()
  })

  it('with members from other households, Reviewed is posts-only (disabled + hint)', () => {
    const { container, getByTestId } = render(
      <SpaceSettings space={makeSpace({ has_remote_households: true })} onUpdate={() => {}} />,
    )
    const s = selects(container)
    expect(reviewed(s.posts).disabled).toBe(false)
    for (const f of ['pages', 'tasks', 'calendar']) {
      expect(reviewed(s[f]).disabled).toBe(true)
      expect(reviewed(s[f]).title).toBe('space.access.moderated_unavailable')
    }
    expect(getByTestId('space-access-review-hint').textContent)
      .toBe('space.access.moderated_unavailable')
  })

  it('on a remote-hosted space, Reviewed is posts-only too', () => {
    const { container } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} isRemoteSpace />,
    )
    const s = selects(container)
    expect(reviewed(s.posts).disabled).toBe(false)
    expect(reviewed(s.tasks).disabled).toBe(true)
  })

  it('keeps a Reviewed level already in force selectable', () => {
    const space = makeSpace({
      has_remote_households: true,
      features: {
        calendar: true, todo: true, location: false, stickies: false, pages: true,
        gallery: true, posts_access: 'open', pages_access: 'moderated',
        tasks_access: 'open', stickies_access: 'open', calendar_access: 'open',
        allowed_post_types: ['text'],
      },
    })
    const { container } = render(<SpaceSettings space={space} onUpdate={() => {}} />)
    const s = selects(container)
    expect(s.pages.value).toBe('moderated')
    expect(reviewed(s.pages).disabled).toBe(false)
  })

  it('warns on a Reviewed level the space can no longer hold, until another is picked', () => {
    const space = makeSpace({
      has_remote_households: true,
      features: {
        calendar: true, todo: true, location: false, stickies: false, pages: true,
        gallery: true, posts_access: 'moderated', pages_access: 'moderated',
        tasks_access: 'open', stickies_access: 'open', calendar_access: 'open',
        allowed_post_types: ['text'],
      },
    })
    const { container, queryByTestId, getByTestId } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const warning = getByTestId('space-access-stale-pages')
    expect(warning.textContent).toContain('space.access.moderated_stale')
    expect(warning.getAttribute('role')).toBe('status')
    // Posts keep Reviewed across households; an open feature has nothing to warn.
    expect(queryByTestId('space-access-stale-posts')).toBeNull()
    expect(queryByTestId('space-access-stale-tasks')).toBeNull()
    const s = selects(container)
    fireEvent.change(s.pages, { target: { value: 'open' } })
    expect(queryByTestId('space-access-stale-pages')).toBeNull()
  })

  it('no stale warning for a Reviewed feature on a host-only space', () => {
    const space = makeSpace({
      has_remote_households: false,
      features: {
        calendar: true, todo: true, location: false, stickies: false, pages: true,
        gallery: true, posts_access: 'open', pages_access: 'moderated',
        tasks_access: 'open', stickies_access: 'open', calendar_access: 'open',
        allowed_post_types: ['text'],
      },
    })
    const { queryByTestId } = render(<SpaceSettings space={space} onUpdate={() => {}} />)
    expect(queryByTestId('space-access-stale-pages')).toBeNull()
  })

  it('a 422 MODERATION_NOT_FEDERATED shows the translated toast', async () => {
    const refused = Object.assign(new Error('not federated'), {
      status: 422, code: 'MODERATION_NOT_FEDERATED', extra: {},
    })
    apiMock.patch.mockRejectedValueOnce(refused)
    const { container, getByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
    )
    fireEvent.change(selects(container).tasks, { target: { value: 'moderated' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch.mock.calls[0][1]).toEqual({ features: { tasks_access: 'moderated' } })
    expect(showToast).toHaveBeenCalledWith('space.access.moderated_not_federated', 'error')
  })

  it('mirrors the stored levels', () => {
    const space = makeSpace({
      features: {
        calendar: true, todo: true, location: false, stickies: true, pages: true,
        gallery: true, posts_access: 'moderated', pages_access: 'admin_only',
        tasks_access: 'open', stickies_access: 'admin_only', calendar_access: 'open',
        allowed_post_types: ['text'],
      },
    })
    const { container } = render(<SpaceSettings space={space} onUpdate={() => {}} />)
    const s = selects(container)
    expect(s.posts.value).toBe('moderated')
    expect(s.pages.value).toBe('admin_only')
    expect(s.stickies.value).toBe('admin_only')
  })

  it('sends only the changed level in the features block', async () => {
    apiMock.patch.mockResolvedValueOnce({})
    const { container, getByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
    )
    fireEvent.change(selects(container).tasks, { target: { value: 'admin_only' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledOnce()
    expect(apiMock.patch.mock.calls[0][1]).toEqual({
      features: { tasks_access: 'admin_only' },
    })
  })

  it('asks before applying a level older households cannot enforce, then forces', async () => {
    const tooOld = Object.assign(new Error('needs update'), {
      status: 409,
      code: 'PEERS_TOO_OLD',
      extra: {
        households: [
          { instance_id: 'i-1', display_name: "Granny's house", proto_version: 41 },
        ],
      },
    })
    apiMock.patch.mockRejectedValueOnce(tooOld).mockResolvedValueOnce({})
    const onUpdate = vi.fn()
    const { container, getByText, findByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={onUpdate} />,
    )
    fireEvent.change(selects(container).pages, { target: { value: 'admin_only' } })
    fireEvent.click(getByText('Save changes'))
    // The dialog names the household and offers to apply anyway.
    expect(await findByText('space.access.peers_too_old.title')).toBeTruthy()
    expect(container.ownerDocument.body.textContent).toContain('space.access.peers_too_old.body')
    fireEvent.click(getByText('space.access.peers_too_old.apply'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledTimes(2)
    expect(apiMock.patch.mock.calls[1][1]).toEqual({
      features: { pages_access: 'admin_only' },
      force: true,
    })
    expect(onUpdate).toHaveBeenCalled()
  })

  it('cancelling the older-households prompt changes nothing', async () => {
    const tooOld = Object.assign(new Error('needs update'), {
      status: 409,
      code: 'PEERS_TOO_OLD',
      extra: { households: [{ instance_id: 'i-1', display_name: 'G', proto_version: 41 }] },
    })
    apiMock.patch.mockRejectedValueOnce(tooOld)
    const { container, getByText, findByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
    )
    fireEvent.change(selects(container).pages, { target: { value: 'admin_only' } })
    fireEvent.click(getByText('Save changes'))
    await findByText('space.access.peers_too_old.title')
    fireEvent.click(getByText('Cancel'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledOnce()
  })
})

describe('SpaceSettings — an edit forwarded to the host', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue([])
    apiMock.patch.mockReset()
    vi.mocked(showToast).mockReset()
  })

  it('never shows a forwarded access change as saved', async () => {
    apiMock.patch.mockResolvedValueOnce({ id: 's-1', forwarded: true })
    const onUpdate = vi.fn()
    const { container, getByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={onUpdate} />,
    )
    const sel = container.querySelector('select[data-feature="tasks"]') as HTMLSelectElement
    fireEvent.change(sel, { target: { value: 'admin_only' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(showToast).toHaveBeenCalledWith('space.access.forwarded', 'info')
    expect(showToast).not.toHaveBeenCalledWith('Space updated', 'success')
    // The select shows the level in force (this household's copy), not the ask.
    expect((container.querySelector('select[data-feature="tasks"]') as HTMLSelectElement).value)
      .toBe('open')
    expect(onUpdate).toHaveBeenCalled()
  })

  it('a forwarded edit without access changes says the host applies it', async () => {
    apiMock.patch.mockResolvedValueOnce({ id: 's-1', forwarded: true })
    const { container, getByText } = render(
      <SpaceSettings space={makeSpace()} onUpdate={() => {}} />,
    )
    const name = container.querySelector('input[type="text"], input:not([type])') as HTMLInputElement
    fireEvent.input(name, { target: { value: 'New name' } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(showToast).toHaveBeenCalledWith('space.settings.forwarded', 'info')
  })
})
