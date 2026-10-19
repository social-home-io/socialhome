import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
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

  // ``.sh-avatar`` is 40 px in CSS, and a stylesheet width beats the
  // <img> width/height attributes — so a photo has to carry the size
  // inline exactly like the initials tile, or a 24 px photo chip renders
  // at 40 px next to 24 px initials chips (PeoplePicker, calendar strip).
  it('a photo honours the size prop the same way the initials tile does', () => {
    const { container } = render(<Avatar name="A" src="/pic.jpg" size={24} />)
    const img = container.querySelector('img') as HTMLImageElement
    expect(img.style.width).toBe('24px')
    expect(img.style.height).toBe('24px')
  })

  it('a photo defaults to 40 px like the initials tile', () => {
    const { container } = render(<Avatar name="A" src="/pic.jpg" />)
    const img = container.querySelector('img') as HTMLImageElement
    expect(img.style.width).toBe('40px')
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

  it('falls back to initials when the image fails to load', () => {
    const { container } = render(<Avatar name="Carol Daniels" src="/expired.jpg" />)
    const img = container.querySelector('img') as HTMLImageElement
    expect(img).toBeTruthy()
    fireEvent.error(img)
    // After the load error the <img> is gone and the initials tile
    // takes over so the user doesn't see a broken-image icon.
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('CA')
  })
})
