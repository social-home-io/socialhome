import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({ t: (k: string) => k }))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import { PrivateChannelPicker, channelChoices, sameGfsSet } from './PrivateChannelPicker'
import { api } from '@/api'
import type { SpaceGfsServersView } from '@/types'

const apiMock = api as unknown as Record<'get', ReturnType<typeof vi.fn>>

const TOWN = { gfs_id: 'g-town', display_name: 'Town GFS', inbox_url: 'https://town.example' }
const CLUB = { gfs_id: 'g-club', display_name: 'Club GFS', inbox_url: 'https://club.example' }

function view(over: Partial<SpaceGfsServersView> = {}): SpaceGfsServersView {
  return {
    space_type: 'private',
    mode: 'channel',
    gfs_ids: [],
    max: 8,
    unlisted: false,
    servers: null,
    listings: [],
    available: [
      { ...TOWN, listed: false, listing_status: null, channel_capable: true },
      { ...CLUB, listed: false, listing_status: null, channel_capable: false },
    ],
    ...over,
  }
}

beforeEach(() => {
  cleanup()
  apiMock.get.mockReset()
})

describe('channelChoices', () => {
  it('offers only GFSs that can carry a channel, pre-ticking the single one', () => {
    const c = channelChoices(view())
    expect(c.choices.map(x => x.id)).toEqual(['g-town'])
    expect(c.saved).toEqual([])
    expect(c.initial).toEqual(['g-town'])
    expect(c.max).toBe(8)
  })

  it('starts from the saved channel GFSs when there are some', () => {
    const c = channelChoices(view({ gfs_ids: ['g-town'] }))
    expect(c.saved).toEqual(['g-town'])
    expect(c.initial).toEqual(['g-town'])
  })

  it('a listing view (option not saved yet) saves nothing and offers unflagged GFSs', () => {
    const c = channelChoices(view({
      mode: 'listing',
      gfs_ids: ['g-club'],
      available: [
        { ...TOWN, listed: false, listing_status: null },
        { ...CLUB, listed: false, listing_status: null },
      ],
    }))
    expect(c.saved).toEqual([])
    expect(c.choices).toHaveLength(2)
    // Two to choose from: nothing pre-ticked.
    expect(c.initial).toEqual([])
  })

  it('copes with no view at all', () => {
    expect(channelChoices(null)).toEqual({ choices: [], saved: [], initial: [], max: 8 })
  })
})

describe('sameGfsSet', () => {
  it('ignores order', () => {
    expect(sameGfsSet(['a', 'b'], ['b', 'a'])).toBe(true)
    expect(sameGfsSet(['a'], ['a', 'b'])).toBe(false)
    expect(sameGfsSet(['a'], ['b'])).toBe(false)
  })
})

describe('PrivateChannelPicker', () => {
  it('loads the choices, reports them and renders the channel checklist', async () => {
    apiMock.get.mockResolvedValue(view())
    const onLoaded = vi.fn()
    const onChange = vi.fn()
    const { findByTestId, getByText } = render(
      <PrivateChannelPicker spaceId="s-1" selected={null} onChange={onChange} onLoaded={onLoaded} />,
    )
    const list = await findByTestId('private-channel-checklist')
    expect(apiMock.get).toHaveBeenCalledWith('/api/spaces/s-1/gfs-servers')
    expect(onLoaded).toHaveBeenCalledWith(expect.objectContaining({ initial: ['g-town'] }))
    expect(getByText('space.private_gfs.channels_legend')).toBeTruthy()
    expect(getByText('space.private_gfs.channels_intro')).toBeTruthy()
    const box = list.querySelector('input[value="g-town"]') as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(box)
    expect(onChange).toHaveBeenCalledWith([])
  })

  it('shows the error under the list', async () => {
    apiMock.get.mockResolvedValue(view())
    const { findByRole } = render(
      <PrivateChannelPicker
        spaceId="s-1" selected={[]} onChange={() => {}} onLoaded={() => {}}
        error="space.private_gfs.channels_required"
      />,
    )
    expect((await findByRole('alert')).textContent).toBe('space.private_gfs.channels_required')
  })

  it('offers a retry when the choices fail to load', async () => {
    apiMock.get.mockRejectedValueOnce(new Error('down'))
    apiMock.get.mockResolvedValueOnce(view())
    const { findByTestId, getByText } = render(
      <PrivateChannelPicker spaceId="s-1" selected={null} onChange={() => {}} onLoaded={() => {}} />,
    )
    await findByTestId('private-channel-load-failed')
    fireEvent.click(getByText('common.retry'))
    await findByTestId('private-channel-checklist')
  })

  it('says so when no GFS can carry a channel', async () => {
    apiMock.get.mockResolvedValue(view({ available: [] }))
    const { findByTestId } = render(
      <PrivateChannelPicker spaceId="s-1" selected={null} onChange={() => {}} onLoaded={() => {}} />,
    )
    expect((await findByTestId('private-channel-none')).textContent)
      .toBe('space.private_gfs.channels_none')
  })
})

describe('channelChoices — chosen servers that cannot be used right now', () => {
  it('keeps every chosen server as a row with a note, so it can be unticked', () => {
    const c = channelChoices(view({
      gfs_ids: ['g-club', 'g-old'],
      chosen: [
        { ...CLUB, connection_status: 'active', channel_capable: false },
        {
          gfs_id: 'g-old', display_name: 'Old GFS', inbox_url: 'https://old.example',
          connection_status: 'suspended', channel_capable: false,
        },
      ],
    }))
    // A stable order by name: a suspended row never jumps to the top.
    expect(c.choices.map(x => [x.id, x.note])).toEqual([
      ['g-club', 'space.private_gfs.channel_unsupported'],
      ['g-old', 'space.private_gfs.channel_suspended'],
      ['g-town', undefined],
    ])
    const later = channelChoices(view({
      gfs_ids: ['g-town'],
      available: [
        { ...TOWN, listed: true, listing_status: null, channel_capable: true },
        { ...CLUB, listed: false, listing_status: null, channel_capable: true },
      ],
      chosen: [{ ...TOWN, connection_status: 'suspended', channel_capable: false }],
    }))
    expect(later.choices.map(x => x.id)).toEqual(['g-club', 'g-town'])
    expect(c.initial).toEqual(['g-club', 'g-old'])
  })

  it('renders the note, and unticking drops the id from the next save', async () => {
    apiMock.get.mockResolvedValue(view({
      gfs_ids: ['g-club'],
      chosen: [{ ...CLUB, connection_status: 'active', channel_capable: false }],
    }))
    const onChange = vi.fn()
    const { findByTestId } = render(
      <PrivateChannelPicker spaceId="s-1" selected={null} onChange={onChange} onLoaded={() => {}} />,
    )
    const list = await findByTestId('private-channel-checklist')
    expect((await findByTestId('private-channel-note-g-club')).textContent)
      .toBe('space.private_gfs.channel_unsupported')
    fireEvent.click(list.querySelector('input[value="g-club"]') as HTMLInputElement)
    expect(onChange).toHaveBeenCalledWith([])
  })
})
