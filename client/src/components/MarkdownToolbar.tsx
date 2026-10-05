/**
 * MarkdownToolbar — two-row formatting bar for the Pages editor (§23.72).
 *
 * Row 1: Headings + inline emphasis — H1 H2 H3 | B I S U
 * Row 2: Code + link + (optional) image + lists + quote — ` ``` 🔗 [📷] ≡ 1. ☐ "
 *
 * Every button has an aria-label + title for screen readers and tooltip
 * hints. Keyboard shortcuts (Cmd/Ctrl+B/I/K, Cmd/Ctrl+Shift+8/9) work
 * anywhere in the textarea via the ``useMarkdownShortcuts`` hook a
 * consumer wires up once on mount.
 *
 * The 📷 image button only renders when ``onPickImage`` is wired — feed
 * posts use a dedicated ``image`` post-type instead, and pages get
 * gallery references via the gallery's "Copy reference" button. Surfaces
 * that genuinely need a media-picker can opt in by passing the prop.
 *
 * The helpers emit the new body via ``onUpdate``; cursor position is
 * restored so selection feels natural after a bold/italic wrap.
 */
import { useEffect } from 'preact/hooks'
import { t } from '@/i18n/i18n'

interface MarkdownToolbarProps {
  textareaRef: { current: HTMLTextAreaElement | null }
  onUpdate: (newText: string) => void
  /** When provided, the 📷 button renders and invokes this callback.
   * Consumer is responsible for uploading the picked file and calling
   * ``apply`` with the URL. When omitted, the image button is hidden —
   * surfaces that don't have a media-picker (feed text composer, the
   * Pages editor) drop the button entirely rather than show a
   * placeholder-only helper. */
  onPickImage?: () => void
}

interface ApplyOpts {
  before: string
  after?: string
  /** When set, inserts at the start of every selected line rather than
   * wrapping the selection. Used for lists + quote + headings. */
  linePrefix?: string
  /** Fallback text inserted if the selection was empty. */
  placeholder?: string
}

function applyFormat(
  ta: HTMLTextAreaElement,
  onUpdate: (s: string) => void,
  opts: ApplyOpts,
): void {
  const { before, after = before, linePrefix, placeholder } = opts
  const start = ta.selectionStart
  const end   = ta.selectionEnd
  const text  = ta.value
  const selected = text.slice(start, end)

  let next: string
  let newStart: number
  let newEnd: number

  if (linePrefix) {
    // Apply the prefix to every line covered by the selection (or the
    // caret line if nothing's selected).
    const lineStart = text.lastIndexOf('\n', start - 1) + 1
    const lineEnd   = end === start ? end : end
    const block     = text.slice(lineStart, lineEnd) || placeholder || ''
    const prefixed  = block.split('\n').map(l => linePrefix + l).join('\n')
    next = text.slice(0, lineStart) + prefixed + text.slice(lineEnd)
    newStart = lineStart
    newEnd   = lineStart + prefixed.length
  } else {
    const body = selected || placeholder || ''
    next = text.slice(0, start) + before + body + after + text.slice(end)
    newStart = start + before.length
    newEnd   = newStart + body.length
  }

  onUpdate(next)
  // Restore selection on next tick so Preact has flushed the update.
  requestAnimationFrame(() => {
    ta.focus()
    ta.setSelectionRange(newStart, newEnd)
  })
}

interface BtnProps {
  label: string
  shortcut?: string
  onClick: () => void
  children: preact.ComponentChildren
}

function Btn({ label, shortcut, onClick, children }: BtnProps) {
  const title = shortcut ? `${label} (${shortcut})` : label
  return (
    <button
      type="button"
      class="sh-md-btn"
      title={title}
      aria-label={label}
      data-shortcut={shortcut}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

/** Register Cmd/Ctrl-based shortcuts against the bound textarea. */
export function useMarkdownShortcuts(
  textareaRef: { current: HTMLTextAreaElement | null },
  onUpdate: (s: string) => void,
): void {
  useEffect(() => {
    const ta = textareaRef.current
    if (!ta) return
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (!mod) return
      const key = e.key.toLowerCase()
      const handlers: Record<string, () => void> = {
        b: () => applyFormat(ta, onUpdate, { before: '**', placeholder: t('md.ph.bold') }),
        i: () => applyFormat(ta, onUpdate, { before: '*',  placeholder: t('md.ph.italic') }),
        k: () => applyFormat(ta, onUpdate, { before: '[', after: '](url)', placeholder: t('md.ph.link_text') }),
      }
      const handler = handlers[key]
      if (handler) {
        e.preventDefault()
        handler()
        return
      }
      // Cmd/Ctrl+Shift+8 → bullet list, +9 → numbered list.
      if (e.shiftKey && key === '8') {
        e.preventDefault()
        applyFormat(ta, onUpdate, { before: '', linePrefix: '- ', placeholder: t('md.ph.item') })
      }
      if (e.shiftKey && key === '9') {
        e.preventDefault()
        applyFormat(ta, onUpdate, { before: '', linePrefix: '1. ', placeholder: t('md.ph.item') })
      }
    }
    ta.addEventListener('keydown', onKey)
    return () => ta.removeEventListener('keydown', onKey)
  }, [textareaRef, onUpdate])
}

export function MarkdownToolbar(
  { textareaRef, onUpdate, onPickImage }: MarkdownToolbarProps,
) {
  const apply = (opts: ApplyOpts) => {
    const ta = textareaRef.current
    if (!ta) return
    applyFormat(ta, onUpdate, opts)
  }

  const cmd = typeof navigator !== 'undefined' && /Mac/.test(navigator.platform)
    ? '⌘' : 'Ctrl'

  return (
    <div class="sh-md-toolbar" role="toolbar" aria-label={t('md.toolbar')}>
      <div class="sh-md-toolbar-row" role="group" aria-label={t('md.group.headings')}>
        <Btn label={t('md.btn.h1')} onClick={() => apply({ before: '', linePrefix: '# ', placeholder: t('md.ph.heading') })}>H1</Btn>
        <Btn label={t('md.btn.h2')} onClick={() => apply({ before: '', linePrefix: '## ', placeholder: t('md.ph.heading') })}>H2</Btn>
        <Btn label={t('md.btn.h3')} onClick={() => apply({ before: '', linePrefix: '### ', placeholder: t('md.ph.heading') })}>H3</Btn>
        <span class="sh-md-sep" aria-hidden="true" />
        <Btn label={t('md.btn.bold')}          shortcut={`${cmd}+B`} onClick={() => apply({ before: '**', placeholder: t('md.ph.bold') })}>B</Btn>
        <Btn label={t('md.btn.italic')}        shortcut={`${cmd}+I`} onClick={() => apply({ before: '*',  placeholder: t('md.ph.italic') })}><i>I</i></Btn>
        <Btn label={t('md.btn.strike')} onClick={() => apply({ before: '~~', placeholder: t('md.ph.text') })}><s>S</s></Btn>
      </div>
      <div class="sh-md-toolbar-row" role="group" aria-label={t('md.group.blocks')}>
        <Btn label={t('md.btn.inline_code')}  onClick={() => apply({ before: '`', placeholder: t('md.ph.code') })}>{'<>'}</Btn>
        <Btn label={t('md.btn.code_block')}   onClick={() => apply({ before: '\n```\n', after: '\n```\n', placeholder: t('md.ph.code') })}>{'{ }'}</Btn>
        <Btn label={t('md.btn.link')}         shortcut={`${cmd}+K`} onClick={() => apply({ before: '[', after: '](url)', placeholder: t('md.ph.link_text') })}>🔗</Btn>
        {onPickImage && (
          <Btn label={t('md.btn.image')} onClick={onPickImage}>📷</Btn>
        )}
        <span class="sh-md-sep" aria-hidden="true" />
        <Btn label={t('md.btn.bullets')}  shortcut={`${cmd}+⇧+8`} onClick={() => apply({ before: '', linePrefix: '- ',  placeholder: t('md.ph.item') })}>≡</Btn>
        <Btn label={t('md.btn.numbered')} shortcut={`${cmd}+⇧+9`} onClick={() => apply({ before: '', linePrefix: '1. ', placeholder: t('md.ph.item') })}>1.</Btn>
        <Btn label={t('md.btn.tasks')}    onClick={() => apply({ before: '', linePrefix: '- [ ] ', placeholder: t('md.ph.task') })}>☐</Btn>
        <Btn label={t('md.btn.quote')}   onClick={() => apply({ before: '', linePrefix: '> ', placeholder: t('md.ph.quote') })}>&quot;</Btn>
      </div>
    </div>
  )
}
