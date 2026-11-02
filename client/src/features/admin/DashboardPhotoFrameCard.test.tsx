import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/preact'
import type { DashboardState, FrameAlbumChoice, SaveFn } from './dashboardTypes'
import { DashboardPhotoFrameCard } from './DashboardPhotoFrameCard'

const SUMMER: FrameAlbumChoice = {
  id: 'al-summer', name: 'Summer by the lake', photo_count: 42, is_system: false, default_name: false,
}
const POSTS: FrameAlbumChoice = {
  id: 'sys', name: 'Posts', photo_count: 1, is_system: true, default_name: false,
}
const DEFAULT: FrameAlbumChoice = {
  id: 'al-default', name: 'Fotos', photo_count: 3, is_system: false, default_name: true,
}

function state(over: Partial<DashboardState> = {}): DashboardState {
  return {
    enabled: true, running: true, running_port: 8100, port: 8100, port_fixed: false,
    password_set: true, password_set_at: null, pin_set: false, address: null,
    url: 'http://homeassistant.local:8100', url_host_allowed: true, locale: 'en',
    idle_return_minutes: 3, night_start: '22:00', night_end: '06:00',
    keep_screen_on: true, show_status_text: false, show_household_messages: false,
    mention_sound: true, panel_unlock_from_ha: false,
    photo_frame: false, photo_frame_minutes: 5, photo_frame_albums: [],
    photo_frame_album_choices: [POSTS, DEFAULT, SUMMER],
    household_chat_enabled: true, last_seen_at: null, people: [], displays: [],
    ...over,
  }
}

let save: ReturnType<typeof vi.fn<SaveFn>>

beforeEach(() => {
  save = vi.fn<SaveFn>().mockResolvedValue(null)
})

describe('DashboardPhotoFrameCard', () => {
  it('is off by default with its controls greyed', () => {
    render(<DashboardPhotoFrameCard state={state()} save={save} />)
    expect(screen.getByRole('heading', { name: 'Photo screensaver' })).toBeTruthy()
    const toggle = screen.getByRole('switch', { name: 'Show photos when idle' }) as HTMLInputElement
    expect(toggle.getAttribute('aria-checked') ?? String(toggle.checked)).toMatch(/false/)
    expect((screen.getByLabelText('Start after') as HTMLSelectElement).disabled).toBe(true)
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes.every((b) => b.disabled)).toBe(true)
    // No warning while it is off.
    expect(screen.queryByText(/Pick at least one album/)).toBeNull()
  })

  it('turns on and picks when to start', () => {
    const { rerender } = render(<DashboardPhotoFrameCard state={state()} save={save} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Show photos when idle' }))
    expect(save).toHaveBeenCalledWith({ photo_frame: true })
    rerender(<DashboardPhotoFrameCard state={state({ photo_frame: true })} save={save} />)
    const after = screen.getByLabelText('Start after') as HTMLSelectElement
    expect([...after.options].map((o) => o.value)).toEqual(['1', '2', '5', '10', '15', '30'])
    expect(after.value).toBe('5')
    fireEvent.change(after, { target: { value: '10' } })
    expect(save).toHaveBeenLastCalledWith({ photo_frame_minutes: 10 })
  })

  it('names the albums like the gallery and counts their photos', () => {
    render(<DashboardPhotoFrameCard state={state({ photo_frame: true })} save={save} />)
    expect(screen.getByText('Posts')).toBeTruthy()
    // The default album nobody renamed: the reader's word for "Photos".
    expect(screen.getByText('Photos')).toBeTruthy()
    expect(screen.queryByText('Fotos')).toBeNull()
    expect(screen.getByText('42 photos')).toBeTruthy()
    expect(screen.getByText('1 photo')).toBeTruthy()
    expect(screen.getByText(/Private albums and space albums never show/)).toBeTruthy()
  })

  it('asks for an album while none is picked', () => {
    render(<DashboardPhotoFrameCard state={state({ photo_frame: true })} save={save} />)
    expect(screen.getByRole('status').textContent).toMatch(/Pick at least one album/)
  })

  it('adds and removes albums, dropping ones no longer offered', () => {
    const s = state({ photo_frame: true, photo_frame_albums: ['al-gone-private', 'sys'] })
    render(<DashboardPhotoFrameCard state={s} save={save} />)
    expect(screen.queryByRole('status')).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: /Summer by the lake/ }))
    expect(save).toHaveBeenLastCalledWith({ photo_frame_albums: ['sys', 'al-summer'] })
    fireEvent.click(screen.getByRole('checkbox', { name: /Posts/ }))
    expect(save).toHaveBeenLastCalledWith({ photo_frame_albums: [] })
  })

  it('says how to get an album when there is none', () => {
    render(<DashboardPhotoFrameCard state={state({ photo_frame_album_choices: [] })} save={save} />)
    expect(screen.getByText(/No albums yet that everyone at home can see/)).toBeTruthy()
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  })
})
