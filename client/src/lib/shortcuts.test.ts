import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import {
  handleShortcutKey,
  installKeyboardShortcuts,
  shortcutsHelpOpen,
  shouldIgnoreShortcut,
} from './shortcuts'

function key(k: string, init: KeyboardEventInit = {}, target?: HTMLElement): KeyboardEvent {
  const e = new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...init })
  ;(target ?? document.body).dispatchEvent(e)
  return e
}

function mount(html: string) {
  document.body.innerHTML = html
}

describe('keyboard shortcuts', () => {
  let teardown: () => void

  beforeEach(() => {
    shortcutsHelpOpen.value = false
    mount(`
      <input data-shortcut="search" id="search" />
      <main id="main"><textarea data-shortcut="composer" id="composer"></textarea></main>
    `)
    teardown = installKeyboardShortcuts()
  })

  afterEach(() => {
    teardown()
    document.body.innerHTML = ''
  })

  it('"/" focuses the search field and swallows the key', () => {
    const e = key('/')
    expect(document.activeElement?.id).toBe('search')
    expect(e.defaultPrevented).toBe(true)
  })

  it('"n" focuses the page composer', () => {
    key('n')
    expect(document.activeElement?.id).toBe('composer')
  })

  it('"n" prefers the composer inside #main over one elsewhere', () => {
    mount(`
      <aside><textarea data-shortcut="composer" id="side"></textarea></aside>
      <main id="main"><textarea data-shortcut="composer" id="page"></textarea></main>
    `)
    key('n')
    expect(document.activeElement?.id).toBe('page')
  })

  it('"n" on a page without a composer does nothing and keeps the browser default', () => {
    mount('<main id="main"></main>')
    const e = key('n')
    expect(e.defaultPrevented).toBe(false)
  })

  it('"?" opens the help dialog', () => {
    key('?', { shiftKey: true })
    expect(shortcutsHelpOpen.value).toBe(true)
  })

  it('teardown removes the listener', () => {
    teardown()
    key('?', { shiftKey: true })
    expect(shortcutsHelpOpen.value).toBe(false)
    teardown = () => {}
  })

  describe('ignore rules', () => {
    it('ignores keys typed into an input', () => {
      const input = document.getElementById('search') as HTMLInputElement
      input.focus()
      key('?', {}, input)
      expect(shortcutsHelpOpen.value).toBe(false)
    })

    it('ignores keys typed into a textarea', () => {
      const ta = document.getElementById('composer') as HTMLTextAreaElement
      ta.focus()
      const e = key('/', {}, ta)
      expect(e.defaultPrevented).toBe(false)
      expect(document.activeElement?.id).toBe('composer')
    })

    it('ignores keys typed into a select', () => {
      mount('<select id="s"><option>a</option></select>')
      const s = document.getElementById('s') as HTMLSelectElement
      s.focus()
      key('?', {}, s)
      expect(shortcutsHelpOpen.value).toBe(false)
    })

    it('ignores keys inside a contenteditable region', () => {
      mount('<div contenteditable="true"><span id="inner">x</span></div>')
      key('?', {}, document.getElementById('inner')!)
      expect(shortcutsHelpOpen.value).toBe(false)
    })

    it.each([
      ['ctrlKey'], ['metaKey'], ['altKey'],
    ])('ignores keys with %s held', (mod) => {
      key('/', { [mod]: true })
      expect(document.activeElement).toBe(document.body)
    })

    it('ignores auto-repeat', () => {
      key('?', { repeat: true })
      expect(shortcutsHelpOpen.value).toBe(false)
    })

    it('ignores keys while an aria-modal dialog is open', () => {
      mount('<div role="dialog" aria-modal="true"><button>x</button></div><input data-shortcut="search" id="search" />')
      key('/')
      expect(document.activeElement).toBe(document.body)
    })

    it('does not treat a closed drawer (aria-modal="false") as a modal', () => {
      mount('<div role="dialog" aria-modal="false"></div><input data-shortcut="search" id="search" />')
      key('/')
      expect(document.activeElement?.id).toBe('search')
    })

    it('ignores keys while the QuickSwitcher is open', () => {
      mount('<div class="sh-switcher-overlay"></div><input data-shortcut="search" id="search" />')
      key('/')
      expect(document.activeElement).toBe(document.body)
    })

    it('ignores keys another handler already claimed', () => {
      const e = new KeyboardEvent('keydown', { key: '?', cancelable: true })
      e.preventDefault()
      expect(shouldIgnoreShortcut(e)).toBe(true)
      expect(handleShortcutKey(e)).toBe(false)
    })

    it('ignores unrelated keys', () => {
      const e = key('x')
      expect(e.defaultPrevented).toBe(false)
      expect(shortcutsHelpOpen.value).toBe(false)
    })
  })
})
