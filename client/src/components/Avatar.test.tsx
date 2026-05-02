import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { Avatar } from './Avatar'

describe('Avatar', () => {
  it('renders initials when no src', () => {
    const { container } = render(<Avatar name="Anna Bee" />)
    expect(container.textContent).toContain('AN')
  })

  it('renders img when src provided', () => {
    const { container } = render(<Avatar name="Anna" src="/pic.jpg" />)
    const img = container.querySelector('img')
    expect(img).toBeTruthy()
    expect(img?.getAttribute('src')).toBe('/pic.jpg')
  })

  it('respects size prop', () => {
    const { container } = render(<Avatar name="A" size={64} />)
    const el = container.firstElementChild as HTMLElement
    expect(el.style.width).toContain('64')
  })

  it('renders no online dot when online prop is omitted', () => {
    const { container } = render(<Avatar name="A" />)
    expect(container.querySelector('.sh-avatar-status-dot')).toBeNull()
  })

  it('renders a green dot when online', () => {
    const { container } = render(<Avatar name="A" online="online" />)
    const dot = container.querySelector('.sh-avatar-status-dot')
    expect(dot).not.toBeNull()
    expect(dot!.classList.contains('sh-avatar-status-dot--online')).toBe(true)
  })

  it('renders an amber dot when idle and labels for screen readers', () => {
    const { container, getByRole } = render(<Avatar name="Anna" online="idle" />)
    const dot = container.querySelector('.sh-avatar-status-dot--idle')
    expect(dot).not.toBeNull()
    expect(getByRole('img').getAttribute('aria-label')).toContain('Anna')
    expect(getByRole('img').getAttribute('aria-label')).toContain('idle')
  })
})
