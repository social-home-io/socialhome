import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

function commonMocks() {
  vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
  vi.doMock('@/store/auth', () => ({
    currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
  }))
  vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
}

describe('Composer', () => {
  it('module exports exist', async () => {
    commonMocks()
    const mod = await import('./Composer')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('hides the bazaar option when not in a space', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByTitle } = render(<Composer onSubmit={vi.fn()} />)
    expect(queryByTitle('text')).toBeTruthy()
    expect(queryByTitle('poll')).toBeTruthy()
    expect(queryByTitle('bazaar')).toBeNull()
  })

  it('exposes the bazaar option inside a space', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByTitle } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1" />,
    )
    expect(queryByTitle('bazaar')).toBeTruthy()
  })

  it('hides the textarea when poll/schedule is picked (builder modes)', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByPlaceholderText, getByTitle } = render(
      <Composer onSubmit={vi.fn()} />,
    )
    expect(queryByPlaceholderText(/What's on your mind/)).toBeTruthy()
    fireEvent.click(getByTitle('poll'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeNull()
    fireEvent.click(getByTitle('schedule'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeNull()
    fireEvent.click(getByTitle('text'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeTruthy()
  })
})
