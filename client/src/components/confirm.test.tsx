import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { ConfirmDialogHost, confirmDialog } from './confirm'

describe('confirmDialog', () => {
  it('renders nothing when no prompt is pending', () => {
    const { container } = render(<ConfirmDialogHost />)
    expect(container.querySelector('.sh-modal')).toBeNull()
  })

  it('opens a modal and resolves true on confirm', async () => {
    const { findByText } = render(<ConfirmDialogHost />)
    const promise = confirmDialog('Delete this?', { destructive: true })
    const btn = await findByText('Confirm')
    fireEvent.click(btn)
    await expect(promise).resolves.toBe(true)
  })

  it('resolves false on cancel', async () => {
    const { findByText } = render(<ConfirmDialogHost />)
    const promise = confirmDialog('Drop it?')
    const btn = await findByText('Cancel')
    fireEvent.click(btn)
    await expect(promise).resolves.toBe(false)
  })

  it('uses caller-supplied confirmLabel and cancelLabel', async () => {
    const { findByText } = render(<ConfirmDialogHost />)
    const promise = confirmDialog('Sure?', {
      confirmLabel: 'Yes, dissolve',
      cancelLabel: 'Keep',
    })
    const btn = await findByText('Yes, dissolve')
    fireEvent.click(btn)
    await expect(promise).resolves.toBe(true)
  })

  it('drops a previous prompt when a second is queued', async () => {
    render(<ConfirmDialogHost />)
    const first = confirmDialog('First?')
    confirmDialog('Second?')
    // The first promise resolves false because the second supersedes it.
    await expect(first).resolves.toBe(false)
  })
})

describe('ConfirmDialogHost placement', () => {
  it('is the last overlay, so a confirm shows over the dialog that asked', () => {
    // Every overlay sits on the same modal z-index: DOM order decides.
    // Mounted before the highlight publish menu, "Remove all" opened its
    // confirm hidden behind the menu.
    const app = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8')
    const overlays = /overlays=\{<>([\s\S]*?)<\/>\}/.exec(app)![1]
    const tags = [...overlays.matchAll(/<([A-Z]\w*)\b/g)].map(m => m[1])
    expect(tags.at(-1)).toBe('ConfirmDialogHost')
  })
})
