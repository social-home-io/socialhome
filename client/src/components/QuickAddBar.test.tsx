import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { QuickAddBar } from './QuickAddBar'

function Host({ onSubmit }: { onSubmit: (v: string) => void }) {
  const [v, setV] = useState('')
  return (
    <QuickAddBar
      value={v}
      onValueChange={setV}
      onSubmit={() => onSubmit(v)}
      placeholder="Add an item"
      inputLabel="New item"
      submitLabel="Add"
      class="extra"
    />
  )
}

describe('QuickAddBar', () => {
  it('renders a labelled input and a submit button disabled while empty', () => {
    const { getByRole, container } = render(<Host onSubmit={() => {}} />)
    const input = getByRole('textbox', { name: 'New item' }) as HTMLInputElement
    expect(input.placeholder).toBe('Add an item')
    expect(input.getAttribute('autocomplete')).toBe('off')
    expect((getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(true)
    expect(container.querySelector('form')!.className).toBe('sh-quick-add extra')
  })

  it('submits on Enter / button once there is text, without a page reload', () => {
    const onSubmit = vi.fn()
    const { getByRole, container } = render(<Host onSubmit={onSubmit} />)
    const input = getByRole('textbox') as HTMLInputElement
    fireEvent.input(input, { target: { value: 'Milk' } })
    expect((getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(false)
    const ev = new Event('submit', { bubbles: true, cancelable: true })
    container.querySelector('form')!.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith('Milk')
  })

  it('does not submit whitespace', () => {
    const onSubmit = vi.fn()
    const { getByRole, container } = render(<Host onSubmit={onSubmit} />)
    fireEvent.input(getByRole('textbox'), { target: { value: '   ' } })
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('forwards extra input props and the ref', () => {
    const onFocus = vi.fn()
    let el: HTMLInputElement | null = null
    render(
      <QuickAddBar value="" onValueChange={() => {}} onSubmit={() => {}}
                   inputLabel="New" submitLabel="Add"
                   inputRef={(r) => { el = r }}
                   inputProps={{ onFocus, name: 'text' }} />,
    )
    expect(el).not.toBeNull()
    expect(el!.name).toBe('text')
    fireEvent.focus(el!)
    expect(onFocus).toHaveBeenCalled()
  })
})
