import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { ArchiveDivider } from './ArchiveDivider'

describe('ArchiveDivider', () => {
  it('shows the label and runs the action', () => {
    const onAction = vi.fn()
    const { getByText, getByRole, container } = render(
      <ArchiveDivider label="Already bought (2)" actionLabel="Clear all"
                      actionAriaLabel="Clear all bought items" onAction={onAction}
                      class="extra" />,
    )
    expect(getByText('Already bought (2)')).toBeTruthy()
    expect(container.firstElementChild!.className).toBe('sh-archive-divider extra')
    fireEvent.click(getByRole('button', { name: 'Clear all bought items' }))
    expect(onAction).toHaveBeenCalledTimes(1)
  })

  it('omits the button without an action', () => {
    const { queryByRole } = render(<ArchiveDivider label="Done (1)" />)
    expect(queryByRole('button')).toBeNull()
  })
})
