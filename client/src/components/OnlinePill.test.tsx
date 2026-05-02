import { describe, it, expect, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'
import { OnlinePill } from './OnlinePill'
import { presence } from '@/store/presence'

describe('OnlinePill', () => {
  beforeEach(() => { presence.value = {} })

  it('renders nothing for unknown users', () => {
    const { container } = render(<OnlinePill user_id="u-unknown" />)
    expect(container.querySelector('.sh-online-pill')).toBeNull()
  })

  it('renders nothing for offline users', () => {
    presence.value = {
      anna: { username: 'anna', user_id: 'u-anna', state: 'home', is_online: false },
    }
    const { container } = render(<OnlinePill user_id="u-anna" />)
    expect(container.querySelector('.sh-online-pill')).toBeNull()
  })

  it('renders an online pill with label and zone', () => {
    presence.value = {
      anna: {
        username: 'anna', user_id: 'u-anna', state: 'home',
        is_online: true, is_idle: false, zone_name: 'Home',
      },
    }
    const { container } = render(<OnlinePill user_id="u-anna" showZone />)
    const pill = container.querySelector('.sh-online-pill')
    expect(pill).not.toBeNull()
    expect(pill!.classList.contains('sh-online-pill--online')).toBe(true)
    expect(pill!.textContent).toContain('Online')
    expect(pill!.textContent).toContain('@ Home')
  })

  it('renders idle styling when is_idle is true', () => {
    presence.value = {
      anna: {
        username: 'anna', user_id: 'u-anna', state: 'home',
        is_online: true, is_idle: true,
      },
    }
    const { container } = render(<OnlinePill user_id="u-anna" />)
    expect(container.querySelector('.sh-online-pill--idle')).not.toBeNull()
  })

  it('hides zone when showZone is false', () => {
    presence.value = {
      anna: {
        username: 'anna', user_id: 'u-anna', state: 'home',
        is_online: true, is_idle: false, zone_name: 'Office',
      },
    }
    const { container } = render(<OnlinePill user_id="u-anna" showZone={false} />)
    expect(container.textContent).not.toContain('@ Office')
  })

  it('compact form renders only the dot', () => {
    presence.value = {
      anna: {
        username: 'anna', user_id: 'u-anna', state: 'home',
        is_online: true,
      },
    }
    const { container } = render(<OnlinePill user_id="u-anna" compact />)
    const pill = container.querySelector('.sh-online-pill--compact')
    expect(pill).not.toBeNull()
    expect(pill!.textContent).toBe('')
  })
})
