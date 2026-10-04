import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { setLocale } from '@/i18n/i18n'
import { ConfirmDialog } from './ConfirmDialog'

describe('ConfirmDialog', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <ConfirmDialog open={false} title="T" message="M"
        onConfirm={() => {}} onCancel={() => {}} />
    )
    expect(container.textContent).toBe('')
  })

  it('shows title and message when open', () => {
    const { getByText } = render(
      <ConfirmDialog open={true} title="Delete?" message="Are you sure?"
        onConfirm={() => {}} onCancel={() => {}} />
    )
    expect(getByText('Delete?')).toBeTruthy()
    expect(getByText('Are you sure?')).toBeTruthy()
  })

  it('calls onConfirm when confirm clicked', () => {
    const fn = vi.fn()
    const { getByText } = render(
      <ConfirmDialog open={true} title="T" message="M"
        onConfirm={fn} onCancel={() => {}} />
    )
    fireEvent.click(getByText('Confirm'))
    expect(fn).toHaveBeenCalledOnce()
  })

  it('calls onCancel when cancel clicked', () => {
    const fn = vi.fn()
    const { getByText } = render(
      <ConfirmDialog open={true} title="T" message="M"
        onConfirm={() => {}} onCancel={fn} />
    )
    fireEvent.click(getByText('Cancel'))
    expect(fn).toHaveBeenCalledOnce()
  })

  it('labels the default buttons in the UI language', async () => {
    await setLocale('de')
    try {
      const { getByText } = render(
        <ConfirmDialog open title="T" message="M"
          onConfirm={() => {}} onCancel={() => {}} />,
      )
      expect(getByText('Abbrechen')).toBeTruthy()
      expect(getByText('Bestätigen')).toBeTruthy()
    } finally {
      await setLocale('en')
    }
  })
})
