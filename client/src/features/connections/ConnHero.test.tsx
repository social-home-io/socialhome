import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import type { Connection } from '@/store/connections'
import { ConnHero, heroSubText } from './ConnHero'
import { AdminContactNote } from './AdminContactNote'
import type { Household } from './model'

function hh(id: string, name: string, status: Household['status'], over: Partial<Connection> = {}, behind = 0): Household {
  return {
    conn: { instance_id: id, display_name: name, reachable: status !== 'down', status: 'confirmed', ...over },
    status,
    behind,
  }
}

const NOW = Date.parse('2026-10-14T12:00:00Z')

describe('heroSubText', () => {
  it('names the one unreachable household and since when', () => {
    expect(heroSubText([hh('e', 'Epsilon House', 'down', { unreachable_since: '2026-10-12T09:00:00Z' })], NOW))
      .toBe("Epsilon House hasn't answered since Monday.")
  })

  it('counts several unreachable households, and prefers them over updates', () => {
    expect(heroSubText([hh('a', 'A', 'down'), hh('b', 'B', 'down'), hh('c', 'C', 'warn', {}, 1)], NOW))
      .toBe("2 households aren't answering.")
  })

  it('falls back to version wording when nothing is down', () => {
    expect(heroSubText([hh('b', 'Beta House', 'warn', {}, 2)], NOW)).toBe('Beta House is running an older version of Social Home.')
    expect(heroSubText([hh('a', 'A', 'ok')], NOW)).toBeNull()
  })
})

describe('ConnHero', () => {
  const problem = [
    hh('e', 'Epsilon House', 'down'),
    hh('b', 'Beta House', 'warn', {}, 2),
    hh('a', 'Alpha House', 'ok'),
  ]
  const base = {
    households: problem, gfs: true, ours: 57, onPair: () => {}, onJump: () => {},
  }

  it('problem (admin): headline, chips that jump to the card, version chip, both CTAs', () => {
    const onJump = vi.fn()
    const onPairVia = vi.fn()
    const { container, getByText } = render(
      <ConnHero {...base} variant="problem" isAdmin onJump={onJump} onPairVia={onPairVia} />,
    )
    expect(container.querySelector('h2')!.textContent).toBe('2 of 3 households need a look')
    expect(container.querySelector('h2 em')!.textContent).toBe('need a look')
    fireEvent.click(getByText('1 unreachable'))
    expect(onJump).toHaveBeenCalledWith('e')
    fireEvent.click(getByText('1 needs an update'))
    expect(onJump).toHaveBeenCalledWith('b')
    expect(container.textContent).toContain('You’re on v57'.replace('’', "'"))
    fireEvent.click(container.querySelector('[aria-label="Through a household you know"]')!)
    expect(onPairVia).toHaveBeenCalled()
  })

  it('the constellation is decorative and draws problems with their status', () => {
    const { container } = render(<ConnHero {...base} variant="problem" isAdmin />)
    const svg = container.querySelector('svg.sh-conn-constellation')!
    expect(svg.getAttribute('aria-hidden')).toBe('true')
    expect([...svg.querySelectorAll('[data-status]')].map((g) => g.getAttribute('data-status'))).toEqual(['down', 'warn', 'ok'])
    expect(svg.textContent).toContain('GFS')
  })

  it('healthy (member): no version, no CTAs, the member note instead', () => {
    const { container } = render(
      <ConnHero {...base} households={[hh('a', 'Alpha House', 'ok')]} variant="healthy" isAdmin={false}
        memberNote={<AdminContactNote admins={['Dave', 'Alice']} />} />,
    )
    expect(container.querySelector('h2')!.textContent).toBe("Everyone's connected")
    expect(container.querySelector('.sh-conn-hero__ok')!.textContent).toBe('1 household')
    expect(container.querySelector('.sh-btn')).toBeNull()
    expect(container.querySelector('.sh-conn-hero__note')!.textContent)
      .toBe("Only admins can pair households. Ask Dave or Alice if you'd like to connect with someone.")
  })

  it('empty: first-run headline, dashed slots, no GFS tile, no via CTA', () => {
    const { container, queryByText } = render(
      <ConnHero {...base} households={[]} variant="empty" isAdmin />,
    )
    expect(container.querySelector('h2')!.textContent).toBe('Your household, ready to meet the neighbors')
    expect(container.querySelectorAll('.node--empty')).toHaveLength(3)
    expect(container.querySelector('svg')!.textContent).not.toContain('GFS')
    expect(queryByText('Through a household you know')).toBeNull()
  })
})

describe('AdminContactNote', () => {
  it.each([
    [['Dave'], 'Ask Dave'],
    [['Dave', 'Alice'], 'Ask Dave or Alice'],
    [['Dave', 'Alice', 'Bob'], 'Ask an admin'],
    [null, 'Ask an admin'],
    [[], 'Ask an admin'],
  ])('%o → %s', (admins, ask) => {
    const { container } = render(<AdminContactNote admins={admins} />)
    expect(container.textContent).toContain(`${ask} if you'd like`)
  })
})
