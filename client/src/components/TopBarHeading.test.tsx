import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'

const openPersonSpy = vi.fn()
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

const { TopBarHeading } = await import('./TopBarHeading')
const { pageTitle, pageTitleAvatar } = await import('@/store/pageTitle')

describe('TopBarHeading', () => {
  beforeEach(() => { openPersonSpy.mockReset() })
  afterEach(() => {
    cleanup()
    pageTitle.value = ''
    pageTitleAvatar.value = null
  })

  it('renders nothing without a title', () => {
    const { container } = render(<TopBarHeading />)
    expect(container.innerHTML).toBe('')
  })

  it('a plain page title is a plain h1', () => {
    pageTitle.value = 'Calendar'
    const { container } = render(<TopBarHeading />)
    expect(container.querySelector('h1')?.textContent).toBe('Calendar')
    expect(container.querySelector('button')).toBeNull()
  })

  it("a 1:1 DM's peer: the title opens their sheet, the face is its twin", () => {
    pageTitle.value = 'Bob'
    pageTitleAvatar.value = {
      src: null, name: 'Bob', personId: 'u-bob',
      personHint: { display_name: 'Bob', username: 'bob', is_local: true },
    }
    const { container, getByTestId } = render(<TopBarHeading />)
    const title = getByTestId('topbar-person')
    expect(title.tagName).toBe('BUTTON')
    expect(title.closest('h1')).not.toBeNull()
    fireEvent.click(title)
    expect(openPersonSpy).toHaveBeenCalledWith('u-bob', {
      person: { display_name: 'Bob', username: 'bob', is_local: true },
    })
    const face = container.querySelector('.sh-person-btn--avatar') as HTMLElement
    expect(face.getAttribute('tabindex')).toBe('-1')
    fireEvent.click(face)
    expect(openPersonSpy).toHaveBeenCalledTimes(2)
  })

  it('an avatar without a person stays a picture', () => {
    pageTitle.value = 'Queue'
    pageTitleAvatar.value = { src: null, name: 'Queue' }
    const { container } = render(<TopBarHeading />)
    expect(container.querySelector('.sh-avatar')).not.toBeNull()
    expect(container.querySelector('button')).toBeNull()
  })
})
