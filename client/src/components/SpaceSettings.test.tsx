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
    expect(body.features.allow_subscribers).toBe(true)
    // …and the join mode is untouched: the two dials are independent.
    expect(body.join_mode).toBe('invite_only')
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

  it('renders the Features fieldset with six toggle checkboxes', () => {
    const space = makeSpace()
    const { getByTestId } = render(
      <SpaceSettings space={space} onUpdate={() => {}} />,
    )
    const fieldset = getByTestId('space-features')
    const checkboxes = fieldset.querySelectorAll('input[type="checkbox"]')
    // Pages, Calendar, Tasks, Stickies, Gallery, Bazaar.
    expect(checkboxes.length).toBe(6)
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
    // Order matches the JSX: pages, calendar, tasks, stickies, gallery,
    // bazaar. ``bazaar`` is omitted from the payload → defaults on.
    expect(checkboxes.map((c) => c.checked)).toEqual([
      false, false, true, true, true, true,
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
    // pages, calendar, tasks, stickies, gallery, bazaar — all on.
    expect(checkboxes.map((c) => c.checked)).toEqual([
      true, true, true, true, true, true,
    ])
  })

  it('sends all five feature toggles in the PATCH body on save', async () => {
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
    fireEvent.change(checkboxes[4], { target: { checked: false } })
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    expect(apiMock.patch).toHaveBeenCalledOnce()
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.features.pages).toBe(false)
    expect(body.features.calendar).toBe(true)
    expect(body.features.todo).toBe(true)
    expect(body.features.stickies).toBe(false)
    expect(body.features.gallery).toBe(false)
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
    // Gallery is the 5th checkbox — should default to checked.
    expect(checkboxes[4].checked).toBe(true)
    fireEvent.click(getByText('Save changes'))
    await new Promise(r => setTimeout(r, 0))
    const [, body] = apiMock.patch.mock.calls[0]
    expect(body.features.gallery).toBe(true)
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
})
