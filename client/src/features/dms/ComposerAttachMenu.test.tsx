import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { ComposerAttachMenu } from './ComposerAttachMenu'

function setup(fileDisabled = false) {
  const onPickFile = vi.fn()
  const onPickLocation = vi.fn()
  const utils = render(
    <ComposerAttachMenu
      fileDisabled={fileDisabled}
      onPickFile={onPickFile}
      onPickLocation={onPickLocation}
    />,
  )
  const trigger = utils.getByRole('button', { name: 'Attach' })
  return { ...utils, trigger, onPickFile, onPickLocation }
}

describe('ComposerAttachMenu', () => {
  it('is closed until the paperclip is pressed', () => {
    const { trigger, queryByRole } = setup()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(queryByRole('menu')).toBeNull()
    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(queryByRole('menu')).toBeTruthy()
  })

  it('opens the file picker or the location picker and closes', () => {
    const { trigger, getByRole, queryByRole, onPickFile, onPickLocation } = setup()
    fireEvent.click(trigger)
    fireEvent.click(getByRole('menuitem', { name: /Location/ }))
    expect(onPickLocation).toHaveBeenCalledTimes(1)
    expect(queryByRole('menu')).toBeNull()
    fireEvent.click(trigger)
    fireEvent.click(getByRole('menuitem', { name: /Photo, video or file/ }))
    expect(onPickFile).toHaveBeenCalledTimes(1)
  })

  it('keeps location available while a file is staged', () => {
    const { trigger, getByRole } = setup(true)
    fireEvent.click(trigger)
    expect((getByRole('menuitem', { name: /Photo/ }) as HTMLButtonElement).disabled)
      .toBe(true)
    expect((getByRole('menuitem', { name: /Location/ }) as HTMLButtonElement).disabled)
      .toBe(false)
  })

  it('focuses the first item, moves with arrows, and Escape returns focus', () => {
    const { trigger, getByRole, queryByRole } = setup()
    fireEvent.click(trigger)
    const file = getByRole('menuitem', { name: /Photo/ })
    const loc = getByRole('menuitem', { name: /Location/ })
    expect(document.activeElement).toBe(file)
    fireEvent.keyDown(file, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(loc)
    fireEvent.keyDown(loc, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(file)
    fireEvent.keyDown(file, { key: 'Escape' })
    expect(queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('closes on a tap outside', () => {
    const { trigger, queryByRole } = setup()
    fireEvent.click(trigger)
    fireEvent.pointerDown(document.body)
    expect(queryByRole('menu')).toBeNull()
  })
})
