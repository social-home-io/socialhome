/* A space's "About" text, rendered as Preact nodes — never as HTML.
 *
 * The same small subset as the GFS's own page
 * (``socialhome/global_server/markdown_lite.py``): ``#`` headings, ``-`` /
 * ``*`` lists, ``>`` quotes, ``**bold**`` / ``__bold__``, ``*italic*`` /
 * ``_italic_``, `` `code` ``, ``[text](url)`` links (``http``, ``https``,
 * ``mailto`` only, ``rel``-guarded, new tab), blank-line paragraphs with a
 * single newline as a line break.
 *
 * The text is owner-supplied and comes from whichever GFS the link names,
 * so it is never parsed as HTML: every piece becomes a text node or one of
 * the elements above, and a link's URL is scheme-checked. There is nothing
 * to sanitize because nothing is ever interpreted.
 */
import type { ComponentChildren, VNode } from 'preact'

/** The GFS truncates to this already; bound it here too. */
export const MAX_SOURCE_CHARS = 4000

const ALLOWED_SCHEMES = /^(https?:\/\/|mailto:)/i
const HEADING = /^(#{1,6})\s+(.*)$/
const LIST_ITEM = /^[-*]\s+/

type Rule = { re: RegExp; make: (m: RegExpExecArray, key: string) => ComponentChildren }

/* Inline rules, in the Python module's order of precedence: code first
 * (its content is literal), then links, bold, italic. */
const RULES: Rule[] = [
  { re: /`([^`]+)`/, make: (m, key) => <code key={key}>{m[1]}</code> },
  {
    re: /\[([^\]]+)\]\(([^)\s]+)\)/,
    make: (m, key) =>
      ALLOWED_SCHEMES.test(m[2]!) ? (
        <a key={key} href={m[2]} rel="noopener noreferrer nofollow ugc" target="_blank">
          {inline(m[1]!, key)}
        </a>
      ) : (
        m[0]
      ),
  },
  { re: /(\*\*|__)(.+?)\1/, make: (m, key) => <strong key={key}>{inline(m[2]!, key)}</strong> },
  {
    re: /(?<![*_\w])([*_])(?=\S)(.+?)(?<=\S)\1(?![*_\w])/,
    make: (m, key) => <em key={key}>{inline(m[2]!, key)}</em>,
  },
]

/** Inline formatting of one line: the earliest match of any rule wins
 *  (ties go to the earlier rule), recursing into its text. */
export function inline(text: string, keyPrefix = 'i', rules: Rule[] = RULES): ComponentChildren[] {
  const out: ComponentChildren[] = []
  let rest = text
  let n = 0
  while (rest) {
    let best: { m: RegExpExecArray; rule: Rule } | null = null
    for (const rule of rules) {
      const m = rule.re.exec(rest)
      if (m && (best === null || m.index < best.m.index)) best = { m, rule }
    }
    if (!best) {
      out.push(rest)
      break
    }
    if (best.m.index > 0) out.push(rest.slice(0, best.m.index))
    out.push(best.rule.make(best.m, `${keyPrefix}.${n++}`))
    rest = rest.slice(best.m.index + best.m[0].length)
  }
  return out
}

/** ``src`` as blocks of Preact nodes; empty for blank input. */
export function renderMarkdown(src: string | null | undefined): VNode[] {
  if (!src || !src.trim()) return []
  let text = src.length > MAX_SOURCE_CHARS ? src.slice(0, MAX_SOURCE_CHARS) + '…' : src
  text = text.replace(/\r\n?/g, '\n').trim()
  const blocks: VNode[] = []
  text.split(/\n[ \t]*\n/).forEach((block, b) => {
    const lines = block.split('\n')
    const nonblank = lines.filter((ln) => ln.trim())
    if (!nonblank.length) return
    const key = `b${b}`
    const heading = lines.length === 1 ? HEADING.exec(lines[0]!) : null
    if (heading) {
      const Tag = `h${Math.min(heading[1]!.length + 2, 6)}` as 'h3'
      blocks.push(<Tag key={key}>{inline(heading[2]!, key)}</Tag>)
    } else if (nonblank.every((ln) => LIST_ITEM.test(ln))) {
      blocks.push(
        <ul key={key}>
          {nonblank.map((ln, i) => (
            <li key={i}>{inline(ln.replace(LIST_ITEM, ''), `${key}.${i}`)}</li>
          ))}
        </ul>,
      )
    } else if (nonblank.every((ln) => ln.startsWith('>'))) {
      const inner = nonblank.map((ln) => ln.replace(/^>[ \t]?/, '')).join(' ')
      blocks.push(<blockquote key={key}>{inline(inner, key)}</blockquote>)
    } else {
      blocks.push(
        <p key={key}>
          {lines.map((ln, i) => [i > 0 && <br key={`br${i}`} />, ...inline(ln, `${key}.${i}`)])}
        </p>,
      )
    }
  })
  return blocks
}
