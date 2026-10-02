/**
 * PostCard inline edit: "Edit" in the post menu (only with ``onEdit``,
 * only for editable post types) swaps the text for a textarea; Save hands
 * the new text to ``onEdit`` and closes on ``true``, stays open on
 * ``false``; Cancel / Escape restore the text untouched.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { PostCard, isTextEditable } from './PostCard'
import type { FeedPost } from '@/types'

const post: FeedPost = {
  id: 'p1',
  author: 'anna',
  type: 'text',
  content: 'Hello world!',
  media_url: null,
  image_urls: [],
  file_meta: null,
  reactions: {},
  comment_count: 0,
  pinned: false,
  created_at: new Date().toISOString(),
  edited_at: null,
}

function openEditor(r: ReturnType<typeof render>) {
  fireEvent.click(r.getByRole('button', { name: 'Post actions' }))
  fireEvent.click(r.getByRole('menuitem', { name: 'Edit' }))
  return r.getByRole('textbox', { name: 'Edit post text' }) as HTMLTextAreaElement
}

describe('PostCard inline edit', () => {
  it('has no Edit item without onEdit', () => {
    const r = render(<PostCard post={post} />)
    fireEvent.click(r.getByRole('button', { name: 'Post actions' }))
    expect(r.queryByRole('menuitem', { name: 'Edit' })).toBeNull()
  })

  it('saves the new text and closes on success', async () => {
    const onEdit = vi.fn().mockResolvedValue(true)
    const r = render(<PostCard post={post} onEdit={onEdit} />)
    const box = openEditor(r)
    expect(box.value).toBe('Hello world!')
    const save = r.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true) // unchanged
    fireEvent.input(box, { target: { value: 'Hello there' } })
    fireEvent.click(r.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onEdit).toHaveBeenCalledWith('Hello there'))
    await waitFor(() => expect(r.queryByRole('textbox', { name: 'Edit post text' })).toBeNull())
  })

  it('stays open when the save fails', async () => {
    const onEdit = vi.fn().mockResolvedValue(false)
    const r = render(<PostCard post={post} onEdit={onEdit} />)
    const box = openEditor(r)
    fireEvent.input(box, { target: { value: 'Nope' } })
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true })
    await waitFor(() => expect(onEdit).toHaveBeenCalledWith('Nope'))
    expect(r.getByRole('textbox', { name: 'Edit post text' })).toBeTruthy()
  })

  it('Escape cancels without saving; an emptied text post cannot be saved', () => {
    const onEdit = vi.fn()
    const r = render(<PostCard post={post} onEdit={onEdit} />)
    const box = openEditor(r)
    fireEvent.input(box, { target: { value: '   ' } })
    expect((r.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(r.queryByRole('textbox')).toBeNull()
    expect(r.getByText('Hello world!')).toBeTruthy()
    expect(onEdit).not.toHaveBeenCalled()
  })

  it('closing the editor puts focus back on the post menu button', async () => {
    const onEdit = vi.fn().mockResolvedValue(true)
    const r = render(<PostCard post={post} onEdit={onEdit} />)
    const box = openEditor(r)
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(document.activeElement).toBe(r.getByRole('button', { name: 'Post actions' })))
    const box2 = openEditor(r)
    fireEvent.input(box2, { target: { value: 'changed' } })
    fireEvent.click(r.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(r.queryByRole('textbox')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(r.getByRole('button', { name: 'Post actions' })))
  })

  it('Escape does nothing while the save is on the wire', async () => {
    let done: (v: boolean) => void = () => {}
    const onEdit = vi.fn(() => new Promise<boolean>(res => { done = res }))
    const r = render(<PostCard post={post} onEdit={onEdit} />)
    const box = openEditor(r)
    fireEvent.input(box, { target: { value: 'changed' } })
    fireEvent.click(r.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onEdit).toHaveBeenCalled())
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(r.getByRole('textbox', { name: 'Edit post text' })).toBeTruthy()
    done(false)
    await waitFor(() => expect((r.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false))
  })

  it('event and bazaar posts, bot posts and deleted posts are not editable', () => {
    expect(isTextEditable(post)).toBe(true)
    expect(isTextEditable({ ...post, type: 'image' })).toBe(true)
    expect(isTextEditable({ ...post, type: 'event' })).toBe(false)
    expect(isTextEditable({ ...post, type: 'bazaar' })).toBe(false)
    expect(isTextEditable({ ...post, author: 'system-integration' })).toBe(false)
    expect(isTextEditable({ ...post, content: null })).toBe(false)
  })
})
