import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiPost, apiPatch, apiDelete, toast, confirm } = vi.hoisted(() => ({
  apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(), toast: vi.fn(), confirm: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { post: apiPost, patch: apiPatch, delete: apiDelete } }))
vi.mock('./Toast', () => ({ showToast: toast }))
vi.mock('./confirm', () => ({ confirmDialog: confirm }))

import {
  BazaarFaqEditor, ListingFaq, faqQuestionFromMessage, faqQuestionText,
} from './BazaarListingFaq'
import type { BazaarFaqEntry } from '@/types'

const entry = (n: number, over: Partial<BazaarFaqEntry> = {}): BazaarFaqEntry => ({
  entry_id: `e${n}`, question: `Question ${n}?`, answer: `Answer ${n}.`, position: n, ...over,
})

beforeEach(() => {
  apiPost.mockReset(); apiPatch.mockReset(); apiDelete.mockReset()
  toast.mockReset(); confirm.mockReset()
})

describe('faq text helpers', () => {
  it('makes a question one trimmed line, cut to 200', () => {
    expect(faqQuestionText('  Does it\n fold?\t ')).toBe('Does it fold?')
    expect(faqQuestionFromMessage('x'.repeat(250))).toHaveLength(200)
  })
})

describe('ListingFaq', () => {
  it('shows nothing to a buyer when there are no questions', () => {
    const { container } = render(
      <ListingFaq postId="p1" entries={[]} isSeller={false} editable />,
    )
    expect(container.textContent).toBe('')
  })

  it('shows every entry as plain text — markup stays text', () => {
    const { container, getByText, queryByRole } = render(
      <ListingFaq postId="p1" isSeller={false} editable
                  entries={[entry(1, { answer: '<b>bold</b>\n**two**' })]} />,
    )
    expect(getByText('Questions and answers')).toBeTruthy()
    expect(getByText('Question 1?')).toBeTruthy()
    const dd = container.querySelector('.sh-bazaar-faq__a')!
    expect(dd.textContent).toBe('<b>bold</b>\n**two**')
    expect(dd.querySelector('b')).toBeNull()
    // A buyer gets no editing tools.
    expect(queryByRole('button', { name: /Edit the question/ })).toBeNull()
    expect(queryByRole('button', { name: /Add a question/ })).toBeNull()
  })

  it('folds on a feed card and opens on the detail', () => {
    const card = render(
      <ListingFaq postId="p1" isSeller={false} editable collapsible entries={[entry(1)]} />,
    )
    const details = card.container.querySelector('details')!
    expect(details.open).toBe(false)
    expect(card.container.querySelector('summary')!.textContent).toContain('1')
    card.unmount()
    const detail = render(
      <ListingFaq postId="p1" isSeller={false} editable entries={[entry(1)]} />,
    )
    expect(detail.container.querySelector('details')).toBeNull()
    expect(detail.getByRole('region', { name: 'Questions and answers' })).toBeTruthy()
  })

  it('invites the seller to add the first question', () => {
    const { getByText, getByRole } = render(
      <ListingFaq postId="p1" entries={[]} isSeller editable />,
    )
    expect(getByText(/Answer what buyers ask most/)).toBeTruthy()
    expect(getByRole('button', { name: /Add a question/ })).toBeTruthy()
  })

  it('the seller adds an entry through the editor', async () => {
    apiPost.mockResolvedValue(entry(1))
    const changed = vi.fn()
    const { getByRole, findByLabelText } = render(
      <ListingFaq postId="p1" entries={[]} isSeller editable onChanged={changed} />,
    )
    fireEvent.click(getByRole('button', { name: /Add a question/ }))
    fireEvent.input(await findByLabelText('Question'), { target: { value: ' Does it\nfold? ' } })
    fireEvent.input(await findByLabelText('Answer'), { target: { value: ' Yes. ' } })
    fireEvent.click(getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/bazaar/p1/faq', { question: 'Does it fold?', answer: 'Yes.' },
    ))
    await waitFor(() => expect(changed).toHaveBeenCalled())
  })

  it('the seller rewords and removes an entry', async () => {
    apiPatch.mockResolvedValue(entry(1, { question: 'New?' }))
    apiDelete.mockResolvedValue({})
    confirm.mockResolvedValue(true)
    const changed = vi.fn()
    const { getByRole, findByLabelText } = render(
      <ListingFaq postId="p1" entries={[entry(1)]} isSeller editable onChanged={changed} />,
    )
    fireEvent.click(getByRole('button', { name: 'Edit the question “Question 1?”' }))
    const q = await findByLabelText('Question') as HTMLTextAreaElement
    expect(q.value).toBe('Question 1?')
    fireEvent.input(q, { target: { value: 'New?' } })
    fireEvent.click(getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      '/api/bazaar/p1/faq/e1', { question: 'New?', answer: 'Answer 1.' },
    ))
    fireEvent.click(getByRole('button', { name: 'Remove the question “Question 1?”' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/bazaar/p1/faq/e1'))
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining('Remove this question'), { destructive: true },
    )
    expect(changed).toHaveBeenCalledTimes(2)
  })

  it('a cancelled removal removes nothing', async () => {
    confirm.mockResolvedValue(false)
    const { getByRole } = render(
      <ListingFaq postId="p1" entries={[entry(1)]} isSeller editable />,
    )
    fireEvent.click(getByRole('button', { name: /Remove the question/ }))
    await new Promise(res => setTimeout(res, 10))
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('says the list is full at 10 instead of offering Add', () => {
    const entries = Array.from({ length: 10 }, (_, n) => entry(n))
    const { getByText, queryByRole } = render(
      <ListingFaq postId="p1" entries={entries} isSeller editable />,
    )
    expect(getByText(/shows 10 questions/)).toBeTruthy()
    expect(queryByRole('button', { name: /Add a question/ })).toBeNull()
  })

  it('is read-only for the seller once the listing ended', () => {
    const { queryByRole, getByText } = render(
      <ListingFaq postId="p1" entries={[entry(1)]} isSeller editable={false} />,
    )
    expect(getByText('Question 1?')).toBeTruthy()
    expect(queryByRole('button', { name: /Edit the question/ })).toBeNull()
    expect(queryByRole('button', { name: /Add a question/ })).toBeNull()
  })
})

describe('BazaarFaqEditor', () => {
  it('keeps Publish off until both fields have text, and counts characters', async () => {
    const { getByRole, findByLabelText, container } = render(
      <BazaarFaqEditor open postId="p1" onClose={() => {}} />,
    )
    const publish = getByRole('button', { name: 'Publish' }) as HTMLButtonElement
    expect(publish.disabled).toBe(true)
    fireEvent.input(await findByLabelText('Question'), { target: { value: 'Q?' } })
    expect(publish.disabled).toBe(true)
    fireEvent.input(await findByLabelText('Answer'), { target: { value: 'A.' } })
    expect(publish.disabled).toBe(false)
    expect(container.ownerDocument.body.textContent).toContain('2 / 200')
    expect(container.ownerDocument.body.textContent).toContain('2 / 500')
  })

  it('shows the translated refusal and stays open', async () => {
    apiPost.mockRejectedValue(new Error('A listing can show at most 10 questions. Remove one first.'))
    const close = vi.fn()
    const { getByRole, findByRole } = render(
      <BazaarFaqEditor open postId="p1" initialQuestion="Q?" initialAnswer="A." onClose={close} />,
    )
    fireEvent.click(getByRole('button', { name: 'Publish' }))
    expect((await findByRole('alert')).textContent).toContain('at most 10 questions')
    expect(close).not.toHaveBeenCalled()
  })
})
