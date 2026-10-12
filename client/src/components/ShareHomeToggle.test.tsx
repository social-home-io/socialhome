import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/preact'
import { ShareHomeToggle } from './ShareHomeToggle'

const mockPatch = vi.fn()

vi.mock('@/api', () => ({
  api: {
    patch: (...a: unknown[]) => mockPatch(...a),
  },
}))

const mockShowToast = vi.fn()

vi.mock('./Toast', () => ({
  showToast: (...a: unknown[]) => mockShowToast(...a),
}))

beforeEach(() => {
  mockPatch.mockReset().mockResolvedValue({})
  mockShowToast.mockReset()
})

describe('ShareHomeToggle — card variant (Manage sheet)', () => {
  it('is a labelled switch with the map wording, and PATCHes the same field', async () => {
    render(<ShareHomeToggle variant="card" instanceId="abc123" peerName="The Smiths" initialValue={true} />)
    const sw = screen.getByRole('switch', { name: 'Share our home location' }) as HTMLInputElement
    expect(sw.checked).toBe(true)
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)!.textContent)
      .toBe('Your “You” pin appears on The Smiths\'s map.')
    fireEvent.click(sw)
    expect(mockPatch).toHaveBeenCalledWith('/api/pairing/connections/abc123', { share_home: false })
    expect(await screen.findByText('Your home stays hidden from The Smiths\'s map.')).toBeTruthy()
  })
})

describe('ShareHomeToggle — URL', () => {
  it('URL-encodes the instance id', () => {
    render(<ShareHomeToggle instanceId="a/b c" peerName="P" initialValue={true} />)
    fireEvent.click(screen.getByRole('checkbox'))
    expect(mockPatch).toHaveBeenCalledWith('/api/pairing/connections/a%2Fb%20c', { share_home: false })
  })
})

describe('ShareHomeToggle', () => {
  it('renders the label and helper text (enabled)', () => {
    const { container } = render(
      <ShareHomeToggle
        instanceId="abc123"
        peerName="The Smiths"
        initialValue={true}
      />,
    )
    expect(container.textContent).toContain('Share our home location with The Smiths')
    expect(container.textContent).toContain(
      'On — your "You" pin appears on The Smiths\'s Connections → Map.',
    )
    const checkbox = screen.getByRole('checkbox') as HTMLInputElement
    expect(checkbox.checked).toBe(true)
  })

  it('renders the helper text when disabled', () => {
    const { container } = render(
      <ShareHomeToggle
        instanceId="abc123"
        peerName="The Smiths"
        initialValue={false}
      />,
    )
    expect(container.textContent).toContain(
      "Off — your home stays hidden from The Smiths's map.",
    )
    const checkbox = screen.getByRole('checkbox') as HTMLInputElement
    expect(checkbox.checked).toBe(false)
  })

  it('clicking the toggle flips state and calls api.patch with share_home', async () => {
    const onChange = vi.fn()
    render(
      <ShareHomeToggle
        instanceId="abc123"
        peerName="The Smiths"
        initialValue={true}
        onChange={onChange}
      />,
    )
    const checkbox = screen.getByRole('checkbox') as HTMLInputElement
    fireEvent.change(checkbox, { target: { checked: false } })
    // Optimistic flip is immediate
    expect(checkbox.checked).toBe(false)
    // Wait for the async PATCH to settle
    await new Promise(r => setTimeout(r, 0))
    expect(mockPatch).toHaveBeenCalledWith(
      '/api/pairing/connections/abc123',
      { share_home: false },
    )
    expect(onChange).toHaveBeenCalledWith(false)
    expect(mockShowToast).not.toHaveBeenCalled()
  })

  it('reverts state and shows a toast when api.patch fails', async () => {
    mockPatch.mockRejectedValueOnce(new Error('Server error'))
    render(
      <ShareHomeToggle
        instanceId="abc123"
        peerName="The Smiths"
        initialValue={false}
      />,
    )
    const checkbox = screen.getByRole('checkbox') as HTMLInputElement
    fireEvent.change(checkbox, { target: { checked: true } })
    // Optimistic flip: toggled to true immediately
    expect(checkbox.checked).toBe(true)
    // Wait for async rejection to propagate
    await new Promise(r => setTimeout(r, 0))
    // Reverted back to false
    expect(checkbox.checked).toBe(false)
    expect(mockShowToast).toHaveBeenCalledWith('Server error', 'error')
  })
})
