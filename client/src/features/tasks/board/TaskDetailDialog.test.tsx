import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import type { TaskItem } from '@/types'

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1' } },
}))

import { TaskDetailDialog } from './TaskDetailDialog'

const people = [{ user_id: 'u1', name: 'Admin' }, { user_id: 'u2', name: 'Bo' }]
const nameOf = (uid: string) => (uid === 'u1' ? 'You' : people.find(p => p.user_id === uid)?.name ?? uid)

const BASE: TaskItem = {
  id: 't1', list_id: 'l1', title: 'Homework', description: 'Maths p. 12', status: 'todo',
  position: 0, due_date: '2099-01-02', assignees: ['u1'], created_by: 'u1',
  priority: 'high', labels: ['School'],
}

function open(extra: Partial<TaskItem> = {}, props: Record<string, unknown> = {}) {
  const onSave = vi.fn().mockResolvedValue(undefined)
  const onClose = vi.fn()
  const r = render(
    <TaskDetailDialog task={{ ...BASE, ...extra }} editable readOnlyReason="Nope" people={people}
                      nameOf={nameOf} labelSuggestions={['Garden', 'School', 'Urgent']}
                      onClose={onClose} onSave={onSave} {...props} />,
  )
  const save = () => fireEvent.click(r.getByRole('button', { name: 'Save' }))
  return { ...r, onSave, onClose, save }
}

describe('TaskDetailDialog', () => {
  it('no change → closes without saving', async () => {
    const t = open()
    t.save()
    await waitFor(() => expect(t.onClose).toHaveBeenCalled())
    expect(t.onSave).not.toHaveBeenCalled()
  })

  it('status, due date cleared and priority "None" are sent as such', async () => {
    const t = open()
    fireEvent.click(t.getByRole('radio', { name: 'In progress' }))
    fireEvent.click(t.getByRole('button', { name: 'Clear the due date' }))
    fireEvent.click(t.getByRole('radio', { name: 'None' }))
    t.save()
    await waitFor(() => expect(t.onSave).toHaveBeenCalledWith({
      status: 'in_progress', due_date: null, priority: null,
    }))
  })

  it('trailing whitespace in the description is not a change', async () => {
    const t = open({ description: 'Maths p. 12  ' })
    fireEvent.input(t.getByLabelText('Description'), { target: { value: 'Maths p. 12' } })
    t.save()
    await waitFor(() => expect(t.onClose).toHaveBeenCalled())
    expect(t.onSave).not.toHaveBeenCalled()
  })

  it('picks a priority from None · Low · Medium · High · Urgent', async () => {
    const t = open({ priority: null })
    const group = t.getByRole('radiogroup', { name: 'Priority' })
    expect(Array.from(group.querySelectorAll('[role="radio"]')).map(b => b.textContent))
      .toEqual(['None', 'Low', 'Medium', 'High', 'Urgent'])
    fireEvent.click(t.getByRole('radio', { name: 'Urgent' }))
    t.save()
    await waitFor(() => expect(t.onSave).toHaveBeenCalledWith({ priority: 'urgent' }))
  })

  it('labels: add by Enter or a suggestion, remove, refuse a duplicate', async () => {
    const t = open()
    const input = t.getByLabelText('Labels') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Garden work' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.click(t.getByRole('button', { name: 'Add label Urgent' }))
    // School is already on it: not offered, and typing it is refused.
    expect(t.queryByRole('button', { name: 'Add label School' })).toBeNull()
    fireEvent.input(input, { target: { value: 'school' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(t.getByRole('alert').textContent).toBe('That label is already on this task.')
    fireEvent.click(t.getByRole('button', { name: 'Remove label School' }))
    fireEvent.input(input, { target: { value: '' } })
    t.save()
    await waitFor(() => expect(t.onSave).toHaveBeenCalledWith({ labels: ['Garden work', 'Urgent'] }))
  })

  it('labels: at most 10, each at most 32 characters', () => {
    const ten = Array.from({ length: 10 }, (_, i) => `L${i}`)
    const t = open({ labels: ten })
    const input = t.getByLabelText('Labels') as HTMLInputElement
    expect(input.disabled).toBe(true)
    expect(input.placeholder).toBe('Up to 10 labels')
    expect(t.queryByRole('group', { name: 'Labels used in this list' })).toBeNull()
    t.unmount()
    const u = open({ labels: [] })
    expect((u.getByLabelText('Labels') as HTMLInputElement).maxLength).toBe(32)
  })

  it('Backspace in the empty label field removes the last label', async () => {
    const t = open({ labels: ['A', 'B'] })
    fireEvent.keyDown(t.getByLabelText('Labels'), { key: 'Backspace' })
    t.save()
    await waitFor(() => expect(t.onSave).toHaveBeenCalledWith({ labels: ['A'] }))
  })

  it('assignees through the people picker', async () => {
    const t = open()
    fireEvent.click(t.getByRole('button', { name: /Bo/ }))
    fireEvent.click(t.getByRole('button', { name: /Admin/ }))
    t.save()
    await waitFor(() => expect(t.onSave).toHaveBeenCalledWith({ assignees: ['u2'] }))
  })

  it('an assignee who left the roster can still be unassigned', async () => {
    const t = open({ assignees: ['gone'] })
    fireEvent.click(t.getByRole('button', { name: /gone/ }))
    t.save()
    await waitFor(() => expect(t.onSave).toHaveBeenCalledWith({ assignees: [] }))
  })

  it('read-only: every field as text with the reason', () => {
    const t = open({ assignees: ['u2'] }, { editable: false })
    expect(t.getByRole('dialog', { name: 'Task details' })).toBeTruthy()
    expect(t.getByText('Nope')).toBeTruthy()
    expect(t.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(t.container.querySelector('input, textarea, select')).toBeNull()
    const dl = t.container.querySelector('.sh-task-view')!
    expect(dl.textContent).toContain('High')
    expect(dl.textContent).toContain('School')
    expect(dl.textContent).toContain('Bo')
  })
})
