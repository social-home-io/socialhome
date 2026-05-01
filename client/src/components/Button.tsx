import type { JSX } from 'preact'

// Preact's generic ``HTMLAttributes<T>`` omits tag-specific attrs
// (``type``, ``disabled`` for <button>). Use the specialised
// ``ButtonHTMLAttributes`` so consumers can forward those props.
interface ButtonProps extends JSX.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual variant matching the website's button language:
   *   - ``primary``   = terracotta hearth (default call-to-action)
   *   - ``secondary`` = cream pill outline (paired tertiary)
   *   - ``danger``    = warning red (destructive)
   *   - ``ghost``     = transparent inset border, inverts on hover
   *   - ``moss``      = household / private-space scope (success)
   *   - ``honey``     = public / global-space scope (warning)
   */
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'moss' | 'honey'
  loading?: boolean
}

export function Button({ variant = 'primary', loading, children, disabled, ...props }: ButtonProps) {
  return (
    <button
      class={`sh-btn sh-btn--${variant}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <span class="sh-spinner-sm" /> : children}
    </button>
  )
}
