/* Render-shape tests for the GFS admin panels. The previous
   inline-HTML admin_ui/index.html had no tests at all; these guard
   against regressions in the Preact port. */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'
import { OverviewPanel } from './Overview'
import { ClientsPanel } from './Clients'
import { SpacesPanel } from './Spaces'
import { ReportsPanel } from './Reports'
import { AppealsPanel } from './Appeals'
import { PolicyPanel } from './Policy'
import { BrandingPanel } from './Branding'
import { AuditPanel } from './Audit'

function stubFetch(impl: (url: string, opts?: RequestInit) => Promise<unknown>) {
  global.fetch = vi.fn(async (input, init) => {
    const url = typeof input === 'string' ? input : (input as Request).url
    const body = await impl(url, init)
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }) as typeof fetch
}

beforeEach(() => {
  vi.restoreAllMocks()
})


describe('OverviewPanel', () => {
  it('renders the three stat cards from the API response', async () => {
    stubFetch(async () => ({
      clients: { active: 3, pending: 1, banned: 0 },
      spaces: { active: 2, pending: 5, banned: 1 },
      open_reports: 7,
    }))
    const { container } = render(<OverviewPanel />)
    await waitFor(() => expect(container.textContent).toMatch(/Overview/))
    expect(container.textContent).toContain('3')
    expect(container.textContent).toContain('1 pending')
    expect(container.textContent).toContain('Open reports')
    expect(container.textContent).toContain('7')
  })
})


describe('ClientsPanel', () => {
  it('renders a row per client with the right action buttons', async () => {
    stubFetch(async () => [
      { instance_id: 'i-1', display_name: 'Alice', inbox_url: 'https://a/wh', status: 'pending' },
      { instance_id: 'i-2', display_name: 'Bob',   inbox_url: 'https://b/wh', status: 'active' },
    ])
    const { container, findByText } = render(<ClientsPanel />)
    await findByText('Alice')
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2)
    expect(container.textContent).toContain('Accept')
    expect(container.textContent).toContain('Ban')
  })

  it('shows empty state when no clients', async () => {
    stubFetch(async () => [])
    const { findByText } = render(<ClientsPanel />)
    expect(await findByText(/No clients\./)).toBeTruthy()
  })
})


describe('SpacesPanel', () => {
  it('renders subscriber counts + status pills', async () => {
    stubFetch(async () => [
      {
        space_id: 's-1', name: 'Bouldering', owning_instance: 'i-1',
        subscriber_count: 4, status: 'active',
      },
    ])
    const { container, findByText } = render(<SpacesPanel />)
    await findByText('Bouldering')
    expect(container.textContent).toContain('4')
    expect(container.querySelector('.pill.active')).toBeTruthy()
  })
})


describe('ReportsPanel', () => {
  it('shows extra "Ban instance" only for space targets', async () => {
    stubFetch(async () => [
      {
        id: 'r-1', target_type: 'space', target_id: 's-x', category: 'spam',
        notes: '', reporter_instance_id: 'i-1', created_at: 1_000,
      },
      {
        id: 'r-2', target_type: 'comment', target_id: 'c-x', category: 'abuse',
        notes: '', reporter_instance_id: 'i-2', created_at: 1_000,
      },
    ])
    const { container, findByText } = render(<ReportsPanel />)
    await findByText('s-x')
    const buttons = container.querySelectorAll('button.danger')
    // r-1 → ban_target + ban_instance, r-2 → ban_target only.
    expect(buttons.length).toBe(3)
  })
})


describe('AppealsPanel', () => {
  it('renders Lift and Dismiss buttons per appeal', async () => {
    stubFetch(async () => [
      { id: 'a-1', target_type: 'instance', target_id: 'i-1', message: 'Sorry', created_at: 1_000 },
    ])
    const { container, findByText } = render(<AppealsPanel />)
    await findByText('Sorry')
    expect(container.textContent).toContain('Lift ban')
    expect(container.textContent).toContain('Dismiss')
  })
})


describe('PolicyPanel', () => {
  it('reflects current policy values', async () => {
    stubFetch(async () => ({
      auto_accept_clients: true,
      auto_accept_spaces: false,
      fraud_threshold: 5,
    }))
    const { container } = render(<PolicyPanel />)
    await waitFor(() => expect(container.querySelector('input[type=checkbox]')).toBeTruthy())
    const cbs = container.querySelectorAll('input[type=checkbox]') as NodeListOf<HTMLInputElement>
    expect(cbs[0].checked).toBe(true)
    expect(cbs[1].checked).toBe(false)
    const num = container.querySelector('input[type=number]') as HTMLInputElement
    expect(num.value).toBe('5')
  })
})


describe('BrandingPanel', () => {
  it('hydrates server name + landing markdown into form fields', async () => {
    stubFetch(async () => ({
      server_name: 'My GFS',
      landing_markdown: 'Welcome.',
      header_image_file: 'banner.png',
    }))
    const { container } = render(<BrandingPanel />)
    await waitFor(() => {
      const inputs = container.querySelectorAll('input[type=text], input:not([type])')
      expect((inputs[0] as HTMLInputElement).value).toBe('My GFS')
    })
  })
})


describe('AuditPanel', () => {
  it('renders one row per audit entry', async () => {
    stubFetch(async () => [
      { action: 'login_ok', target_type: null, target_id: null, admin_ip: '1.1.1.1', created_at: 1_000 },
      { action: 'ban_client', target_type: 'instance', target_id: 'i-1', admin_ip: null, created_at: 2_000 },
    ])
    const { container, findByText } = render(<AuditPanel />)
    await findByText('login_ok')
    expect(container.querySelectorAll('.audit-row').length).toBe(2)
  })

  it('shows empty state when no audit entries', async () => {
    stubFetch(async () => [])
    const { findByText } = render(<AuditPanel />)
    expect(await findByText(/No audit entries yet\./)).toBeTruthy()
  })
})
