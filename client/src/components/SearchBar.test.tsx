import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, act, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

const openPersonSpy = vi.fn()
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

const { SearchBar, openSearchResult } = await import('./SearchBar')

const OMA = {
  user_id: 'u-oma', username: 'oma', display_name: 'Oma Vizeli', picture_url: 'api/u/oma',
}

async function typeAndWait(input: HTMLInputElement, value: string) {
  fireEvent.input(input, { target: { value } })
  await act(async () => { vi.advanceTimersByTime(350) })
  await act(async () => { await Promise.resolve() })
}

describe('SearchBar', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    apiGet.mockReset()
    apiGet.mockResolvedValue([OMA, { user_id: 'u-bob', username: 'bob', display_name: 'Bob' }])
    openPersonSpy.mockReset()
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('renders search input', () => {
    const { container } = render(<SearchBar />)
    const input = container.querySelector('.sh-search-input')
    expect(input).toBeTruthy()
  })

  it('has placeholder text', () => {
    const { container } = render(<SearchBar />)
    const input = container.querySelector('input') as HTMLInputElement
    expect(input.placeholder).toContain('Search')
  })

  it('picking a person hands type, id and row to onSelect and clears the bar', async () => {
    const onSelect = vi.fn()
    const { container, getByRole } = render(<SearchBar onSelect={onSelect} />)
    const input = container.querySelector('input') as HTMLInputElement
    await typeAndWait(input, 'oma')
    const hit = getByRole('button', { name: /Oma Vizeli/ })
    expect(hit.getAttribute('type')).toBe('button')
    fireEvent.click(hit)
    expect(onSelect).toHaveBeenCalledWith('user', 'u-oma', OMA)
    expect(container.querySelector('.sh-search-results')).toBeNull()
    expect(input.value).toBe('')
  })

  it('the results stay open while focus Tabs from the input into them', async () => {
    const { container } = render(<SearchBar onSelect={vi.fn()} />)
    const input = container.querySelector('input') as HTMLInputElement
    await typeAndWait(input, 'oma')
    const hit = container.querySelector('.sh-search-result') as HTMLButtonElement
    fireEvent.blur(input, { relatedTarget: hit })
    await act(async () => { vi.advanceTimersByTime(300) })
    expect(container.querySelector('.sh-search-result')).not.toBeNull()
    // Leaving the bar altogether closes it.
    fireEvent.blur(hit, { relatedTarget: document.body })
    await act(async () => { vi.advanceTimersByTime(300) })
    expect(container.querySelector('.sh-search-result')).toBeNull()
  })

  it('Escape closes the results', async () => {
    const { container } = render(<SearchBar onSelect={vi.fn()} />)
    const input = container.querySelector('input') as HTMLInputElement
    await typeAndWait(input, 'oma')
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(container.querySelector('.sh-search-results')).toBeNull()
  })
})

describe('openSearchResult (the app shell onSelect)', () => {
  beforeEach(() => { openPersonSpy.mockReset() })

  it('a person opens their sheet as a household member', () => {
    openSearchResult('user', 'u-oma', OMA)
    expect(openPersonSpy).toHaveBeenCalledWith('u-oma', {
      person: {
        display_name: 'Oma Vizeli', picture_url: 'api/u/oma', username: 'oma', is_local: true,
      },
    })
  })

  it('falls back to the username for a nameless row; no row → bare id', () => {
    openSearchResult('user', 'u-x', { user_id: 'u-x', username: 'x' })
    expect(openPersonSpy.mock.calls[0][1].person.display_name).toBe('x')
    openSearchResult('user', 'u-y')
    expect(openPersonSpy).toHaveBeenLastCalledWith('u-y', {})
  })

  it('other result types are left alone', () => {
    openSearchResult('space', 's-1')
    expect(openPersonSpy).not.toHaveBeenCalled()
  })
})
