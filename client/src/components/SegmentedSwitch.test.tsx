import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { SegmentedSwitch } from './SegmentedSwitch'

afterEach(() => cleanup())

type V = 'posts' | 'chat'
const labels = { posts: 'Feed', chat: 'Chat' }

describe('SegmentedSwitch', () => {
  it('a labelled group of toggle buttons, the current one pressed', () => {
    const { getByRole } = render(
      <SegmentedSwitch<V> options={['posts', 'chat']} value="posts" labels={labels}
                          ariaLabel="Feed or chat" class="extra" onChange={() => {}} />)
    const group = getByRole('group', { name: 'Feed or chat' })
    expect(group.className).toBe('sh-timetable-seg extra')
    const feed = getByRole('button', { name: 'Feed' })
    expect(feed.getAttribute('aria-pressed')).toBe('true')
    expect(feed.className).toContain('is-on')
    expect(getByRole('button', { name: 'Chat' }).getAttribute('aria-pressed')).toBe('false')
  })

  it('reports a new choice, not the current one', () => {
    const onChange = vi.fn()
    const { getByRole } = render(
      <SegmentedSwitch<V> options={['posts', 'chat']} value="posts" labels={labels}
                          ariaLabel="x" onChange={onChange} />)
    fireEvent.click(getByRole('button', { name: 'Feed' }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.click(getByRole('button', { name: 'Chat' }))
    expect(onChange).toHaveBeenCalledWith('chat')
  })

  it('an unread pill (capped at 99+) announced as unread; none at zero', () => {
    const { getByRole, rerender, container } = render(
      <SegmentedSwitch<V> options={['posts', 'chat']} value="posts" labels={labels}
                          ariaLabel="x" badges={{ chat: 3 }} onChange={() => {}} />)
    expect(getByRole('button', { name: /Chat.*3 unread/ })).toBeTruthy()
    expect(container.querySelector('.sh-tab-unread')!.textContent).toBe('3')
    rerender(
      <SegmentedSwitch<V> options={['posts', 'chat']} value="posts" labels={labels}
                          ariaLabel="x" badges={{ chat: 150 }} onChange={() => {}} />)
    expect(container.querySelector('.sh-tab-unread')!.textContent).toBe('99+')
    rerender(
      <SegmentedSwitch<V> options={['posts', 'chat']} value="posts" labels={labels}
                          ariaLabel="x" badges={{ chat: 0 }} onChange={() => {}} />)
    expect(container.querySelector('.sh-tab-unread')).toBeNull()
  })
})
