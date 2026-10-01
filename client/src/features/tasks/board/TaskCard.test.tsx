import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import type { TaskItem } from '@/types'

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1' } },
}))

import { TaskCard } from './TaskCard'

const people = [
  { user_id: 'u1', name: 'Admin' }, { user_id: 'u2', name: 'Bo' },
  { user_id: 'u3', name: 'Cy' }, { user_id: 'u4', name: 'Di' },
]
const nameOf = (uid: string) => people.find(p => p.user_id === uid)?.name ?? uid

function card(extra: Partial<TaskItem> = {}, props: Record<string, unknown> = {}) {
  const task: TaskItem = {
    id: 't1', list_id: 'l1', title: 'Water the roses', description: null, status: 'todo',
    position: 0, due_date: null, assignees: [], created_by: 'u1', priority: null, labels: [], ...extra,
  }
  return render(
    <TaskCard task={task} editable readOnlyReason="Nope" nameOf={nameOf} people={people}
              onOpen={vi.fn()} {...props} />,
  )
}

function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

describe('TaskCard', () => {
  it('a plain card is just its title button — no priority, chips or meta', () => {
    const r = card()
    expect(r.getByRole('button', { name: 'Water the roses' })).toBeTruthy()
    expect(r.container.querySelector('.sh-task-priority')).toBeNull()
    expect(r.container.querySelector('.sh-board-card__meta')).toBeNull()
  })

  it('priority icon with spoken text', () => {
    const r = card({ priority: 'high' })
    expect(r.container.querySelector('.sh-task-priority--high')!.textContent).toBe('Priority: High')
  })

  it('up to three label chips, then +N, coloured by name', () => {
    const r = card({ labels: ['Garden', 'School', 'Urgent', 'Car', 'Bills'] })
    const chips = Array.from(r.container.querySelectorAll('.sh-task-label'))
    expect(chips.map(c => c.textContent)).toEqual(['Garden', 'School', 'Urgent', '+2'])
    expect(chips[0].className).toMatch(/sh-timetable-c--\w+/)
    expect(chips[3].getAttribute('title')).toBe('Car, Bills')
    expect(r.container.textContent).toMatch(/Labels: Garden, School, Urgent, Car,? (and|&) Bills/)
  })

  it('one due chip: overdue in danger, today in amber; none once done', () => {
    const past = new Date(); past.setDate(past.getDate() - 3)
    let r = card({ due_date: ymd(past) })
    expect(r.container.querySelector('.sh-task-due--overdue')!.textContent).toMatch(/^Overdue · /)
    r.unmount()
    r = card({ due_date: ymd(new Date()) })
    expect(r.container.querySelector('.sh-task-due--today')!.textContent).toBe('Today')
    r.unmount()
    r = card({ due_date: ymd(past), status: 'done' })
    expect(r.container.querySelector('.sh-task-due')).toBeNull()
  })

  it('up to three avatars, then +N, with the names spoken', () => {
    const r = card({ assignees: ['u1', 'u2', 'u3', 'u4'] })
    expect(r.container.querySelectorAll('.sh-board-card__avatar').length).toBe(4)
    expect(r.container.querySelector('.sh-board-card__avatar--more')!.textContent).toBe('+1')
    expect(r.container.textContent).toMatch(/Assigned to Admin, Bo, Cy,? (and|&) Di/)
  })

  it('avatars without a due chip still share the footer row, after an empty start slot', () => {
    const r = card({ assignees: ['u2'] })
    const foot = r.container.querySelector('.sh-board-card__foot')!
    expect(foot.children[0].className).toBe('sh-board-card__foot-start')
    expect(foot.children[0].children.length).toBe(0)
    expect(foot.children[1].className).toBe('sh-board-card__people')
  })

  it('≡ when there are notes', () => {
    const r = card({ description: 'Back yard' })
    expect(r.container.querySelector('.sh-board-card__notes')).toBeTruthy()
  })

  it('a locked card: 🔒 with the reason, no ⋯ menu, no keyboard move', () => {
    const onStep = vi.fn()
    const r = card({}, { editable: false, onStep })
    expect(r.container.querySelector('.sh-board-card__lock')!.getAttribute('title')).toBe('Nope')
    expect(r.queryByRole('button', { name: /Card actions/ })).toBeNull()
    fireEvent.keyDown(r.getByRole('button', { name: 'Water the roses' }), { key: 'ArrowUp', altKey: true })
    expect(onStep).not.toHaveBeenCalled()
  })

  it('on a board that is read-only as a whole, no 🔒 per card', () => {
    const r = card({}, { editable: false, showLock: false })
    expect(r.container.querySelector('.sh-board-card__lock')).toBeNull()
    expect(r.container.querySelector('.sh-board-card__meta')).toBeNull()
  })

  it('Alt+arrows ask for a step; plain arrows do nothing', () => {
    const onStep = vi.fn()
    const r = card({}, { onStep })
    const title = r.getByRole('button', { name: 'Water the roses' })
    fireEvent.keyDown(title, { key: 'ArrowLeft', altKey: true })
    fireEvent.keyDown(title, { key: 'ArrowDown', altKey: true })
    fireEvent.keyDown(title, { key: 'ArrowDown' })
    expect(onStep.mock.calls).toEqual([['left'], ['down']])
  })

  it('the title opens the card', () => {
    const onOpen = vi.fn()
    const r = card({}, { onOpen })
    fireEvent.click(r.getByRole('button', { name: 'Water the roses' }))
    expect(onOpen).toHaveBeenCalled()
  })
})
