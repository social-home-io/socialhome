import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { BotAvatar } from './BotAvatar'
import type { SpaceBotSummary } from '@/types'

const bot = (overrides: Partial<SpaceBotSummary> = {}): SpaceBotSummary => ({
  bot_id: 'b1',
  scope: 'space',
  name: 'Doorbell',
  icon: '🔔',
  created_by_display_name: 'Alice',
  ...overrides,
})

describe('BotAvatar', () => {
  it('renders emoji icons inline', () => {
    const { container } = render(<BotAvatar bot={bot()} size={40} />)
    expect(container.textContent).toContain('🔔')
  })

  it('renders a house fallback for HA entity_id icons', () => {
    const { container } = render(
      <BotAvatar bot={bot({ icon: 'binary_sensor.front_door' })} />,
    )
    // entity_id format → we show a generic home glyph, not the entity_id string.
    expect(container.textContent).toContain('🏠')
    expect(container.textContent).not.toContain('binary_sensor')
  })

  it('renders a home fallback when bot is null (deleted)', () => {
    const { container } = render(<BotAvatar bot={null} />)
    expect(container.textContent).toContain('🏠')
    const el = container.firstElementChild as HTMLElement
    expect(el.className).toContain('sh-bot-avatar--fallback')
  })

  it('applies a distinct class for member-scope bots', () => {
    const { container } = render(
      <BotAvatar bot={bot({ scope: 'member' })} />,
    )
    const el = container.firstElementChild as HTMLElement
    expect(el.className).toContain('sh-bot-avatar--member')
  })
})
