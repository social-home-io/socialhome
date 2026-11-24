/**
 * LinkifiedText — user text with its web links clickable.
 *
 * JSX all the way down (user text is never parsed as HTML). The link
 * grammar and its safety rules are ``splitLinks`` in ``./markdown`` — the
 * same one the post renderer uses — so a link reads the same in a post,
 * a comment, a DM, a moment, the calendar or the bazaar: only
 * ``http(s)`` hrefs, a new tab with ``noopener noreferrer nofollow ugc``,
 * a decorative ``↗`` with a screen-reader "(opens in a new tab)", and a
 * long link shortened with the full URL in ``title``.
 *
 * ``renderText`` lets a caller decorate the plain parts (mentions,
 * hashtags) without ever touching the text inside a link.
 */
import { Fragment, type ComponentChildren } from 'preact'
import { t } from '@/i18n/i18n'
import {
  EXTERNAL_LINK_ICON, EXTERNAL_LINK_REL, linkLabel, splitLabelTail, splitLinks, type TextLink,
} from './markdown'

/** One link from :func:`splitLinks`. A click never reaches the
 *  surrounding row / bubble (those have their own tap handlers). */
export function ExternalLink({ link }: { link: TextLink }) {
  const label = linkLabel(link.text)
  const [head, tail] = splitLabelTail(label)
  return (
    <a
      class="sh-ext-link"
      href={link.href}
      title={label === link.text ? undefined : link.text}
      target="_blank"
      rel={EXTERNAL_LINK_REL}
      onClick={(e) => e.stopPropagation()}
    >
      {head}
      <span class="sh-ext-link__tail">
        {tail}
        <span class="sh-ext-link__icon" aria-hidden="true">{EXTERNAL_LINK_ICON}</span>
      </span>
      <span class="sr-only">{t('link.new_tab')}</span>
    </a>
  )
}

interface Props {
  text: string
  /** Renders a plain (non-link) part; default: the text itself. */
  renderText?: (text: string, key: number) => ComponentChildren
}

export function LinkifiedText({ text, renderText }: Props) {
  return (
    <>
      {splitLinks(text).map((p, i) => (
        typeof p === 'string'
          ? (renderText ? <Fragment key={i}>{renderText(p, i)}</Fragment> : p)
          : <ExternalLink key={i} link={p} />
      ))}
    </>
  )
}
