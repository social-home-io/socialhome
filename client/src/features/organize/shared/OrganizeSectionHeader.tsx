/**
 * OrganizeSectionHeader — the title row every Organize tab opens with:
 * a display-font title, a mono count line ("4 to buy · 3 done") and a
 * slot for the tab's own controls (view switch, Stores…). Shared so
 * Shopping, Tasks and Stickies read as one family.
 */
import { Fragment, type ComponentChildren } from 'preact'

export interface HeaderCount {
  label: string
  /** ``open`` = accent, ``done`` = success, default = muted. */
  tone?: 'open' | 'done'
}

interface OrganizeSectionHeaderProps {
  /** Section heading (h2). Optional — omit when the page has none. */
  title?: string
  /** Keep the h2 for assistive tech but don't show it — for tabs whose
   *  title the app top bar already displays. */
  hideTitle?: boolean
  counts?: HeaderCount[]
  children?: ComponentChildren
}

export function OrganizeSectionHeader({
  title, hideTitle = false, counts = [], children,
}: OrganizeSectionHeaderProps) {
  return (
    <div class="sh-organize-header">
      <div class="sh-organize-header__text">
        {title && (
          <h2 class={hideTitle ? 'sr-only' : 'sh-organize-header__title'}>{title}</h2>
        )}
        {counts.length > 0 && (
          <span class="sh-organize-header__counts">
            {counts.map((c, i) => (
              <Fragment key={c.label}>
                {i > 0 && <span class="sh-organize-header__sep" aria-hidden="true">·</span>}
                <span class={c.tone ? `sh-organize-header__count--${c.tone}` : undefined}>
                  {c.label}
                </span>
              </Fragment>
            ))}
          </span>
        )}
      </div>
      {children && <div class="sh-organize-header__actions">{children}</div>}
    </div>
  )
}
