import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { SpaceCard } from './SpaceCard'
import type { DirectoryEntry } from '@/types'

const baseEntry: DirectoryEntry = {
  space_id: 's1', host_instance_id: 'h1',
  host_display_name: 'Nabu Casa', host_is_paired: true,
  name: 'Chess Club', description: 'Weekly chess', emoji: '♟',
  member_count: 7, scope: 'global', join_mode: 'request',
  // The host's readability opt-in, independent of join_mode. Concrete
  // `false` here: a directory row always carries one (the mapper fails
  // closed), and only the peer "From friends" tab leaves it undefined.
  allow_subscribers: false,
  min_age: 0,
}

describe('SpaceCard', () => {
  it('renders the global scope chip', () => {
    const { getByText } = render(
      <SpaceCard entry={baseEntry} onAction={() => {}} />,
    )
    expect(getByText(/Global/).textContent).toContain('Global')
  })

  it('renders a "Connect first" CTA when host is unpaired', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_is_paired: false }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Connect with/i)).toBeTruthy()
  })

  // ── Host label (GFS-discovered spaces carry a raw instance id) ──────

  // A space found through a Global Federation Server is usually hosted by
  // an unpaired household, so the backend falls back to the raw 52-char
  // base32 instance id for host_display_name. It has no break
  // opportunities, so rendering it in full blew the CTA button out of the
  // card (desktop) / off-screen (mobile).
  const RAW_IID = 'ywee64sjb5g2ebsprbm6t7wy37jy7qdeywee64sjb5g2ebsp'

  it('shortens a host label that is just the raw instance id', () => {
    const { getByText, queryByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry,
          host_is_paired: false,
          host_instance_id: RAW_IID,
          host_display_name: RAW_IID,
        }}
        onAction={() => {}}
      />,
    )
    // CTA carries the shortened id, never the full 52 chars.
    const cta = getByText(/Connect with/i)
    expect(cta.textContent).toBe('Connect with ywee64sj… first')
    expect(cta.textContent).not.toContain(RAW_IID)
    // "Hosted by" line too.
    expect(getByText('ywee64sj…')).toBeTruthy()
    expect(queryByText(RAW_IID)).toBeNull()
  })

  it('renders a real host display name in full', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry,
          host_is_paired: false,
          host_instance_id: RAW_IID,
          host_display_name: 'Nabu Casa',
        }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Connect with/i).textContent)
      .toBe('Connect with Nabu Casa first')
    expect(getByText('Nabu Casa')).toBeTruthy()
  })

  it('renders "Request pending" disabled when pending', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, request_pending: true }}
        onAction={() => {}}
      />,
    )
    const btn = getByText('Request pending') as HTMLButtonElement
    expect(btn).toBeTruthy()
    expect(btn.closest('button')?.disabled).toBe(true)
  })

  it('renders "Open space" when already a member', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, already_member: true, scope: 'household' }}
        onAction={() => {}}
      />,
    )
    expect(getByText('Open space')).toBeTruthy()
  })

  it('shows age chip when min_age > 0', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, min_age: 13 }}
        onAction={() => {}}
      />,
    )
    expect(getByText('13+')).toBeTruthy()
  })

  it('shows a category chip when category is set and not general', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, category: 'gaming' }}
        onAction={() => {}}
      />,
    )
    expect(getByText('Gaming')).toBeTruthy()
  })

  it('hides the category chip for the general category', () => {
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, category: 'general' }}
        onAction={() => {}}
      />,
    )
    expect(queryByText('General')).toBeNull()
  })

  it('hides the category chip when category is unset', () => {
    const { queryByText } = render(
      <SpaceCard entry={baseEntry} onAction={() => {}} />,
    )
    expect(queryByText('General')).toBeNull()
  })

  it('renders no category chip for an unknown / legacy category value', () => {
    // 'gaming2' isn't a recognized SPACE_CATEGORIES value: categoryLabel
    // would fall back to 'General', so the gate must suppress the chip
    // entirely rather than render a misleading "General" label.
    const { container, queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, category: 'gaming2', min_age: 13 }}
        onAction={() => {}}
      />,
    )
    expect(queryByText('General')).toBeNull()
    // Only the min_age chip should remain — no extra category chip.
    expect(container.querySelectorAll('.sh-age-chip')).toHaveLength(1)
    expect(queryByText('13+')).toBeTruthy()
  })

  // ── Subscribe / unsubscribe ─────────────────────────────────────────

  // Subscribe is only meaningful where content actually reaches a
  // non-member — i.e. where the owner opted into followers.
  it('renders a Subscribe button for LOCAL public / global non-members', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry, host_instance_id: 'local', allow_subscribers: true,
        }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Subscribe/)).toBeTruthy()
  })

  it('hides Subscribe when the space takes no followers', () => {
    // With allow_subscribers off nothing is relayed and no content key is
    // handed out, so a subscription would seat someone who never receives
    // anything (both the backend and the GFS refuse such a subscribe).
    const { queryByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry, host_instance_id: 'local', allow_subscribers: false,
        }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('says the content is private when the space takes no followers', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, allow_subscribers: false }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/content is private/i)).toBeTruthy()
  })

  it('offers Subscribe on an INVITE-ONLY space that allows followers', () => {
    // The two dials are independent: invite-only + followers-on is a
    // broadcast space — invited people post, anyone may read along.
    const { getByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry,
          host_instance_id: 'local',
          join_mode:         'invite_only',
          allow_subscribers: true,
        }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Subscribe/)).toBeTruthy()
    // …and it does NOT claim the content is private, because it isn't.
    expect(getByText(/Invite-only/)).toBeTruthy()
  })

  it('hides Subscribe on an OPEN-to-join space that takes no followers', () => {
    // The mirror image: anyone may join, but nobody may merely read.
    const { queryByText, getByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry,
          host_instance_id: 'local',
          join_mode:         'open',
          allow_subscribers: false,
        }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
    expect(getByText(/Open to join/)).toBeTruthy()
    expect(getByText(/content is private/i)).toBeTruthy()
  })

  it('says nothing about readability on a household-scope card', () => {
    // A private household space is private BY DEFINITION — it is never
    // published, never relayed, never subscribable. The "Your household" tab
    // maps allow_subscribers off every local space's features, so without a
    // scope check every private space on the tab wore a 🔒 "Content is
    // private" chip, which is noise: it announces the default.
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, scope: 'household', allow_subscribers: false }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/content is private/i)).toBeNull()
  })

  it('still flags a PUBLIC household-hosted space that takes no followers', () => {
    // Scope, not host: a public space of ours is discoverable, so whether
    // strangers may read it is real information.
    const { getByText } = render(
      <SpaceCard
        entry={{
          ...baseEntry,
          scope:             'public',
          host_instance_id:  'local',
          allow_subscribers: false,
        }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/content is private/i)).toBeTruthy()
  })

  it('says nothing about readability when the flag is unknown', () => {
    // The peer "From friends" directory does not carry the flag yet —
    // claiming "content is private" for an open friend space would be a lie.
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, allow_subscribers: undefined }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/content is private/i)).toBeNull()
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('shows both chips on a private approval-required space, CTA still live', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, join_mode: 'request' }}
        onAction={() => {}}
      />,
    )
    // The private-content chip is independent of the join mode…
    expect(getByText(/content is private/i)).toBeTruthy()
    // …and approval-required stays self-service, so the CTA is live, not
    // the disabled "Invite required".
    const cta = getByText('Request to join') as HTMLButtonElement
    expect(cta.disabled).toBe(false)
    expect(getByText(/Approval required/)).toBeTruthy()
  })

  it('hides Subscribe for a remotely-hosted (friends / global) space', () => {
    // No remote-subscribe federation path — the button would just 404, so
    // it must not show; remote spaces are joined via the request flow.
    const { queryByText } = render(
      <SpaceCard entry={baseEntry} onAction={() => {}} />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('does not render a Subscribe button for household-scope entries', () => {
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, scope: 'household' }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('flips to Unsubscribe when already subscribed', () => {
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, already_subscribed: true }}
        onAction={() => {}}
      />,
    )
    expect(getByText(/Unsubscribe/)).toBeTruthy()
    // Subscribed pill also appears in the header.
    expect(getByText(/🔔 Subscribed/)).toBeTruthy()
  })

  it('does not offer Subscribe once the user is a full member', () => {
    const { queryByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, scope: 'public', already_member: true }}
        onAction={() => {}}
      />,
    )
    expect(queryByText(/Subscribe/)).toBeNull()
  })

  it('calls onAction with kind=subscribe on click', () => {
    let captured: { kind: string } | null = null
    const { getByText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_instance_id: 'local', allow_subscribers: true }}
        onAction={(_e, a) => { captured = a }}
      />,
    )
    ;(getByText(/Subscribe/) as HTMLButtonElement).click()
    expect(captured).toEqual({ kind: 'subscribe' })
  })

  it('disables Subscribe while the parent reports it busy', () => {
    const { getByLabelText } = render(
      <SpaceCard
        entry={{ ...baseEntry, host_instance_id: 'local', allow_subscribers: true }}
        onAction={() => {}}
        subscribeBusy={true}
      />,
    )
    const btn = getByLabelText(/Subscribe to Chess Club/) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })
})
