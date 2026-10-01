import { describe, it, expect } from 'vitest'
import type { TaskItem } from '@/types'
import { applyPlan, columnTasks, planMove, planStep } from './moves'

function task(id: string, status: TaskItem['status'], position: number, created_at = '2026-01-01'): TaskItem {
  return { id, list_id: 'l1', title: id, description: null, status, position, due_date: null,
    assignees: [], created_by: 'u1', created_at }
}

const board = [
  task('a', 'todo', 0), task('b', 'todo', 1), task('c', 'todo', 2),
  task('d', 'in_progress', 0), task('e', 'in_progress', 1),
  task('f', 'done', 0),
]

describe('columnTasks', () => {
  it('sorts by position, then age', () => {
    const rows = [task('x', 'todo', 1), task('y', 'todo', 0, '2026-02-02'), task('z', 'todo', 0, '2026-01-01')]
    expect(columnTasks(rows, 'todo').map(x => x.id)).toEqual(['z', 'y', 'x'])
  })
})

describe('planMove', () => {
  it('reorders within a column', () => {
    expect(planMove(board, 'c', 'todo', 0)).toEqual({
      id: 'c', status: 'todo', order: ['c', 'a', 'b'], statusChanged: false, index: 0, total: 3,
    })
  })

  it('moves across columns at the drop index', () => {
    expect(planMove(board, 'a', 'in_progress', 1)).toMatchObject({
      order: ['d', 'a', 'e'], statusChanged: true, index: 1, total: 3,
    })
  })

  it('past the end appends; an empty column takes it', () => {
    expect(planMove(board, 'a', 'done', 99)?.order).toEqual(['f', 'a'])
    expect(planMove(board.filter(x => x.status !== 'done'), 'a', 'done', 0)?.order).toEqual(['a'])
  })

  it('dropping in place is a no-op', () => {
    expect(planMove(board, 'b', 'todo', 1)).toBeNull()
  })

  it('with a filter, hidden cards keep their places', () => {
    // Only a and c shown; drag a to just before c (index 0 among the
    // shown cards without a, [c]) — b, hidden, stays first.
    const visible = new Set(['a', 'c'])
    expect(planMove(board, 'a', 'todo', 0, visible)?.order).toEqual(['b', 'a', 'c'])
    expect(planMove(board, 'a', 'todo', 1, visible)?.order).toEqual(['b', 'c', 'a'])
    // Past the visible end → right after the last shown card.
    expect(planMove(board, 'd', 'todo', 5, new Set(['a']))?.order).toEqual(['a', 'd', 'b', 'c'])
  })

  it('never changes the relative order of the other cards', () => {
    const plan = planMove(board, 'a', 'todo', 2)!
    expect(plan.order.filter(x => x !== 'a')).toEqual(['b', 'c'])
  })
})

describe('planStep', () => {
  it('up / down by one, null at the ends', () => {
    expect(planStep(board, 'b', 'up')?.order).toEqual(['b', 'a', 'c'])
    expect(planStep(board, 'b', 'down')?.order).toEqual(['a', 'c', 'b'])
    expect(planStep(board, 'a', 'up')).toBeNull()
    expect(planStep(board, 'c', 'down')).toBeNull()
  })

  it('left / right go to the bottom of the next column, null past the edge', () => {
    expect(planStep(board, 'a', 'right')).toMatchObject({ status: 'in_progress', order: ['d', 'e', 'a'], index: 2, total: 3 })
    expect(planStep(board, 'd', 'left')?.status).toBe('todo')
    expect(planStep(board, 'a', 'left')).toBeNull()
    expect(planStep(board, 'f', 'right')).toBeNull()
  })

  it('steps over hidden cards', () => {
    expect(planStep(board, 'c', 'up', new Set(['a', 'c']))?.order).toEqual(['c', 'a', 'b'])
  })
})

describe('applyPlan', () => {
  it('gives the target column its new positions and the card its status', () => {
    const next = applyPlan(board, planMove(board, 'a', 'in_progress', 0)!)
    expect(columnTasks(next, 'in_progress').map(x => x.id)).toEqual(['a', 'd', 'e'])
    expect(columnTasks(next, 'todo').map(x => x.id)).toEqual(['b', 'c'])
  })
})
