import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { AliasDialog, openAliasDialog } from './AliasDialog'

const mockApi = {
  put: vi.fn(),
  delete: vi.fn(),
}
vi.mock('@/api', () => ({
  get api() {
    return mockApi
  },
}))

vi.mock('./Toast', () => ({
  showToast: vi.fn(),
}))

describe('AliasDialog', () => {
  beforeEach(() => {
    mockApi.put.mockReset()
    mockApi.delete.mockReset()
    mockApi.put.mockResolvedValue({})
    mockApi.delete.mockResolvedValue(undefined)
  })

  it('module exports exist', () => {
    expect(typeof openAliasDialog).toBe('function')
    expect(typeof AliasDialog).toBe('function')
  })

  it('renders nothing when closed', () => {
    const { container } = render(<AliasDialog />)
    expect(container.querySelector('input')).toBeNull()
  })

  it('opens prefilled when openAliasDialog is called', async () => {
    const { container, findByRole } = render(<AliasDialog />)
    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: 'Mr B',
    })
    const input = await findByRole('textbox')
    expect((input as HTMLInputElement).value).toBe('Mr B')
    // Dialog explains who's being renamed.
    expect(container.textContent).toContain('Bob')
  })

  it('save button disabled when input matches current alias', async () => {
    const { findByRole, getByText } = render(<AliasDialog />)
    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: 'Mr B',
    })
    await findByRole('textbox')
    const save = getByText('Save nickname') as HTMLButtonElement
    expect(save.disabled).toBe(true)
  })

  it('save calls PUT with trimmed alias', async () => {
    const onSave = vi.fn()
    const { findByRole, getByText } = render(<AliasDialog />)
    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: '',
      onSave,
    })
    const input = (await findByRole('textbox')) as HTMLInputElement
    fireEvent.input(input, { target: { value: '  Mom  ' } })
    fireEvent.click(getByText('Save nickname'))
    await new Promise(r => setTimeout(r, 0))
    expect(mockApi.put).toHaveBeenCalledWith(
      '/api/aliases/users/uid-bob',
      { alias: 'Mom' },
    )
    expect(onSave).toHaveBeenCalledWith('Mom')
  })

  it('reset button only shows when alias is set', async () => {
    const { findByRole, queryByText, rerender } = render(<AliasDialog />)
    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: '',
    })
    await findByRole('textbox')
    expect(queryByText('Reset to default')).toBeNull()

    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: 'Mr B',
    })
    rerender(<AliasDialog />)
    expect(queryByText('Reset to default')).toBeTruthy()
  })

  it('reset calls DELETE and reports null to onSave', async () => {
    const onSave = vi.fn()
    const { findByText } = render(<AliasDialog />)
    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: 'Mr B',
      onSave,
    })
    const reset = await findByText('Reset to default')
    fireEvent.click(reset)
    await new Promise(r => setTimeout(r, 0))
    expect(mockApi.delete).toHaveBeenCalledWith(
      '/api/aliases/users/uid-bob',
    )
    expect(onSave).toHaveBeenCalledWith(null)
  })

  it('rejects oversized alias', async () => {
    const { findByRole, getByText } = render(<AliasDialog />)
    openAliasDialog({
      targetUserId: 'uid-bob',
      globalDisplayName: 'Bob',
      currentAlias: '',
    })
    const input = (await findByRole('textbox')) as HTMLInputElement
    fireEvent.input(input, { target: { value: 'x'.repeat(81) } })
    const save = getByText('Save nickname') as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(mockApi.put).not.toHaveBeenCalled()
  })
})
