/** Split a search snippet into text runs, flagging the ones the server
 *  wrapped in ``<mark>…</mark>`` (FTS5 ``snippet()`` for ``/api/search``,
 *  the gallery search's own for ``/api/gallery/search``).
 *
 *  A snippet copies what someone typed verbatim — only the ``<mark>``
 *  delimiters are server-generated, the text between them is a post / DM /
 *  page / caption author's (possibly on another household), markup
 *  included. So a snippet is rendered as Preact text nodes, never via
 *  ``innerHTML``. A literal ``<mark>`` typed by an author is
 *  indistinguishable from a delimiter and gets highlighted — harmless, it
 *  carries no attributes. */
export function snippetParts(snippet: string): { text: string, mark: boolean }[] {
  const out: { text: string, mark: boolean }[] = []
  const re = /<mark>([\s\S]*?)<\/mark>/g
  let last = 0
  for (let m = re.exec(snippet); m !== null; m = re.exec(snippet)) {
    if (m.index > last) out.push({ text: snippet.slice(last, m.index), mark: false })
    out.push({ text: m[1], mark: true })
    last = m.index + m[0].length
  }
  if (last < snippet.length) out.push({ text: snippet.slice(last), mark: false })
  return out
}

/** The snippet's plain text, for a label (``aria-label``, ``title``). */
export function snippetText(snippet: string): string {
  return snippetParts(snippet).map(p => p.text).join('')
}
