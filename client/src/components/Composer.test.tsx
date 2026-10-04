import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

function commonMocks() {
  vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
  vi.doMock('@/store/auth', () => ({
    currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
  }))
  vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
}

describe('Composer', () => {
  it('module exports exist', async () => {
    commonMocks()
    const mod = await import('./Composer')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('hides the bazaar option when not in a space', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(<Composer onSubmit={vi.fn()} />)
    expect(queryByLabelText('Text')).toBeTruthy()
    expect(queryByLabelText('Poll')).toBeTruthy()
    expect(queryByLabelText('Bazaar listing')).toBeNull()
  })

  it('shows a Bazaar shortcut in a space when the feature is on', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1" bazaarEnabled={true} />,
    )
    // Bazaar is a tab feature now, not a feed post type — the composer
    // surfaces it as a shortcut to the full new-listing dialog.
    expect(queryByLabelText('List something in the Bazaar')).toBeTruthy()
    expect(queryByLabelText('Bazaar listing')).toBeNull()
  })

  it('hides the Bazaar shortcut when the bazaar feature is off', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1" bazaarEnabled={false} />,
    )
    expect(queryByLabelText('List something in the Bazaar')).toBeNull()
  })

  it('filters the type picker to the space allowed_post_types', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1"
        allowedTypes={['text', 'image']} />,
    )
    expect(queryByLabelText('Text')).toBeTruthy()
    expect(queryByLabelText('Photo')).toBeTruthy()
    // Disabled types disappear from the picker entirely.
    expect(queryByLabelText('Poll')).toBeNull()
    expect(queryByLabelText('Bazaar listing')).toBeNull()
  })

  it('falls back to the first allowed type when text is disabled', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1"
        allowedTypes={['image', 'video']} />,
    )
    // ``text`` (the module default) isn't offered, so the picker auto-
    // selects the first type that is, keeping the active button + submit
    // in sync instead of leaving a phantom ``text`` selection.
    expect(queryByLabelText('Text')).toBeNull()
    expect(queryByLabelText('Photo')?.getAttribute('aria-pressed')).toBe('true')
  })

  it('offers every type when allowedTypes is omitted', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByLabelText } = render(
      <Composer onSubmit={vi.fn()} spaceId="space-1" />,
    )
    expect(queryByLabelText('Poll')).toBeTruthy()
    expect(queryByLabelText('Photo')).toBeTruthy()
  })

  it('hides the textarea when poll/schedule is picked (builder modes)', async () => {
    commonMocks()
    const { Composer } = await import('./Composer')
    const { queryByPlaceholderText, getByLabelText } = render(
      <Composer onSubmit={vi.fn()} />,
    )
    expect(queryByPlaceholderText(/What's on your mind/)).toBeTruthy()
    fireEvent.click(getByLabelText('Poll'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeNull()
    fireEvent.click(getByLabelText('Scheduling poll'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeNull()
    fireEvent.click(getByLabelText('Text'))
    expect(queryByPlaceholderText(/What's on your mind/)).toBeTruthy()
  })

  it('enables the Post button once an image upload lands in the images slot', async () => {
    // Regression for the bug Pascal saw as "I can select a photo but
    // nothing happens afterwards" — the Post-button disabled gate
    // used to look only at the single-file ``mediaUrl`` slot used
    // by video / file posts. Image posts populate the multi-file
    // ``images`` array instead, so the gate stayed disabled even
    // after a successful upload. The fix accepts either source as
    // "post has media".
    commonMocks()
    vi.doMock('./UploadProgress', () => ({
      uploadWithProgress: vi.fn(async (file: File) => ({
        url: `/api/media/${file.name}`,
        signed_url: `/api/media/${file.name}?sig=stub`,
        filename: file.name,
      })),
      UploadProgressBar: () => null,
      // Composer reads ``uploadProgress.value`` directly to decide
      // whether to hide the dropzone — stub it as a signal-shaped
      // object so the gate evaluates to "no upload in flight".
      uploadProgress: { value: null },
    }))
    const { Composer } = await import('./Composer')
    const { getByLabelText, container } = render(
      <Composer onSubmit={vi.fn()} />,
    )
    fireEvent.click(getByLabelText('Photo'))
    // Pre-fix the Post button is disabled because ``images`` is
    // empty + ``mediaUrl`` is null; we'll re-check it post-upload.
    const postButton = (): HTMLButtonElement | null => {
      return Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === 'Post',
      ) as HTMLButtonElement | null
    }
    expect(postButton()?.disabled).toBe(true)

    // Synthesize a file and fire it at the hidden input the dropzone
    // mounts. The composer's ``acceptFiles`` handler awaits the
    // upload promise (stubbed above) and then drops a row into the
    // images list — which is the state we want the Post button to
    // react to.
    const fileInput = container.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement
    const file = new File([new Uint8Array(8)], 'photo.png', {
      type: 'image/png',
    })
    Object.defineProperty(fileInput, 'files', {
      configurable: true,
      value: [file],
    })
    fireEvent.change(fileInput)
    // Wait for the upload promise + setState to settle.
    await new Promise((r) => setTimeout(r, 30))
    expect(postButton()?.disabled).toBe(false)
  })
})

describe('Composer link preview', () => {
  const CARD = {
    url: 'https://example.com/story',
    title: 'Story title',
    description: 'About it',
    site_name: 'Example',
    thumbnail_url: null,
  }

  async function typeLink(post: ReturnType<typeof vi.fn>) {
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
    }))
    vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
    const { Composer } = await import('./Composer')
    const onSubmit = vi.fn(async () => 'new-id')
    const view = render(<Composer onSubmit={onSubmit} />)
    const ta = view.container.querySelector('textarea') as HTMLTextAreaElement
    fireEvent.input(ta, { target: { value: 'read https://example.com/story' } })
    await new Promise((r) => setTimeout(r, 750))
    return { ...view, onSubmit }
  }

  it('shows the server-built card and posts without an opt-out', async () => {
    const post = vi.fn(async () => ({ preview: CARD }))
    const { findByText, container, onSubmit } = await typeLink(post)
    expect(await findByText('Story title')).toBeTruthy()
    expect(post).toHaveBeenCalledWith('/api/link-preview', {
      url: 'https://example.com/story',
    })
    fireEvent.submit(container.querySelector('form')!)
    await new Promise((r) => setTimeout(r, 20))
    expect(onSubmit).toHaveBeenCalledOnce()
    const extras = (onSubmit.mock.calls[0] as unknown[])[3] as
      | { noLinkPreview?: boolean }
      | undefined
    expect(extras?.noLinkPreview).toBeUndefined()
  })

  it('remove (×) opts the post out, and can be undone', async () => {
    const post = vi.fn(async () => ({ preview: CARD }))
    const { findByLabelText, findByText, getByText, queryByText, container, onSubmit } =
      await typeLink(post)
    fireEvent.click(await findByLabelText('Remove link preview'))
    expect(queryByText('Story title')).toBeNull()
    // One click back.
    fireEvent.click(getByText('Show link preview'))
    expect(await findByText('Story title')).toBeTruthy()
    fireEvent.click(await findByLabelText('Remove link preview'))
    fireEvent.submit(container.querySelector('form')!)
    await new Promise((r) => setTimeout(r, 20))
    const extras = (onSubmit.mock.calls[0] as unknown[])[3] as { noLinkPreview?: boolean }
    expect(extras.noLinkPreview).toBe(true)
  })
})

describe('Composer — poll attachments', () => {
  async function composePoll(spaceId?: string) {
    const post = vi.fn().mockResolvedValue({})
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { username: 'pascal', display_name: 'Pascal' } },
    }))
    vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
    const { Composer } = await import('./Composer')
    const onSubmit = vi.fn().mockResolvedValue('p1')
    const view = render(<Composer onSubmit={onSubmit} spaceId={spaceId} context={spaceId ?? 'home'} />)
    fireEvent.click(view.getByLabelText('Poll'))
    // First submit opens the builder.
    fireEvent.submit(view.container.querySelector('form.sh-composer')!)
    fireEvent.input(await view.findByPlaceholderText('e.g. Pizza or tacos tonight?'),
      { target: { value: 'Dinner?' } })
    fireEvent.input(view.getByPlaceholderText('Option 1'), { target: { value: 'Pizza' } })
    fireEvent.input(view.getByPlaceholderText('Option 2'), { target: { value: 'Tacos' } })
    fireEvent.submit(document.querySelector('form.sh-poll-builder')!)
    fireEvent.submit(view.container.querySelector('form.sh-composer')!)
    await new Promise(r => setTimeout(r, 0))
    return { onSubmit, post }
  }

  it('a space post sends its poll in the same request (no follow-up call)', async () => {
    const { onSubmit, post } = await composePoll('space-1')
    expect(onSubmit).toHaveBeenCalledOnce()
    const extras = onSubmit.mock.calls[0][3]
    expect(extras.poll).toMatchObject({ question: 'Dinner?', options: ['Pizza', 'Tacos'] })
    expect(post).not.toHaveBeenCalled()
  })

  it('the household feed still attaches the poll after the post exists', async () => {
    const { onSubmit, post } = await composePoll()
    expect(onSubmit.mock.calls[0][3]?.poll).toBeUndefined()
    expect(post).toHaveBeenCalledWith('/api/posts/p1/poll', expect.objectContaining({
      question: 'Dinner?', options: ['Pizza', 'Tacos'],
    }))
  })
})
