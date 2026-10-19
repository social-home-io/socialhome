import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { get, patch, toast } = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', () => ({ api: { get, patch } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import type { Space } from '@/types'
import { ModerationSection, parseRetention } from './ModerationSection'

function space(over: Partial<Space> = {}): Space {
  return { id: 's1', name: 'Garden', space_type: 'private', join_mode: 'invite_only', features: {}, ...over } as Space
}

beforeEach(() => {
  get.mockReset(); get.mockResolvedValue({ min_age: 0 })
  patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset()
})

describe('parseRetention', () => {
  it('empty or not positive is forever; junk is left out', () => {
    expect(parseRetention('')).toBeNull()
    expect(parseRetention('0')).toBeNull()
    expect(parseRetention('30')).toBe(30)
    expect(parseRetention('abc')).toBeUndefined()
  })
})

describe('ModerationSection', () => {
  it('saves the day count when the field is left, and 0 for forever', async () => {
    const view = render(<ModerationSection space={space({ retention_days: 90 } as Partial<Space>)} isRemoteSpace={false} onUpdate={vi.fn()} />)
    const input = view.getByRole('spinbutton') as HTMLInputElement
    expect(input.value).toBe('90')
    fireEvent.input(input, { target: { value: '30' } })
    expect(patch).not.toHaveBeenCalled()
    fireEvent.change(input)
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { retention_days: 30 }))
    fireEvent.input(input, { target: { value: '' } })
    fireEvent.change(input)
    await waitFor(() => expect(patch).toHaveBeenLastCalledWith('/api/spaces/s1', { retention_days: 0 }))
  })

  it('an unchanged count sends nothing', () => {
    const view = render(<ModerationSection space={space()} isRemoteSpace={false} onUpdate={vi.fn()} />)
    fireEvent.change(view.getByRole('spinbutton'))
    expect(patch).not.toHaveBeenCalled()
  })

  it('the keep-list shows only while retention is on and saves the sorted list', async () => {
    const off = render(<ModerationSection space={space()} isRemoteSpace={false} onUpdate={vi.fn()} />)
    expect(off.queryByTestId('retention-exempt-types')).toBeNull()
    off.unmount()
    const view = render(<ModerationSection space={space({ retention_days: 30, retention_exempt_types: ['poll', 'text'] } as Partial<Space>)} isRemoteSpace={false} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('checkbox', { name: 'Photos' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', {
      retention_exempt_types: ['image', 'poll', 'text'],
    }))
  })

  it('links the moderation queue; the age gate only where the space is hosted', async () => {
    const view = render(<ModerationSection space={space()} isRemoteSpace={false} onUpdate={vi.fn()} />)
    expect(view.getByRole('link', { name: /Moderation/ }).getAttribute('href')).toBe('/spaces/s1/moderation')
    expect(await view.findByLabelText('Minimum age')).toBeTruthy()
    view.unmount()
    const remote = render(<ModerationSection space={space()} isRemoteSpace onUpdate={vi.fn()} />)
    expect(remote.queryByText('Age & safety')).toBeNull()
  })
})
