/**
 * Card-shaped loading placeholders: a skeleton hero and skeleton rows in
 * the same shell as ConnCard, so nothing jumps when the data lands. The
 * blocks are hidden from assistive tech; one visually hidden status line
 * announces the load instead.
 */
import { t } from '@/i18n/i18n'

function Block({ shape, w, h, extra = '' }: { shape: 'line' | 'rect' | 'circle'; w: string; h: number; extra?: string }) {
  const style = shape === 'circle' ? { width: `${h}px`, height: `${h}px` } : { width: w, height: `${h}px` }
  return <span class={`sh-skeleton sh-skeleton--${shape} ${extra}`} style={style} aria-hidden="true" />
}

export function SkeletonCards({ widths }: { widths: [string, string][] }) {
  return (
    <ul class="sh-conn-list" aria-hidden="true">
      {widths.map(([a, b], i) => (
        <li key={i}>
          <div class="sh-conn-card sh-conn-card--static sh-conn-card--skeleton">
            <Block shape="circle" w="" h={44} />
            <span class="sh-conn-card__body">
              <Block shape="line" w={a} h={14} />
              <Block shape="line" w={b} h={12} />
            </span>
          </div>
        </li>
      ))}
    </ul>
  )
}

export function HeroSkeleton() {
  return (
    <>
      <div class="sh-conn-sr" role="status">{t('connections.loading')}</div>
      <section class="sh-conn-hero sh-conn-hero--skeleton" aria-hidden="true">
        <div class="sh-conn-hero__top">
          <Block shape="line" w="96px" h={10} />
          <Block shape="line" w="70%" h={28} />
        </div>
        <div class="sh-conn-hero__art">
          <span class="sh-skeleton sh-skeleton--circle sh-conn-skel-orbit" />
        </div>
        <div class="sh-conn-hero__rest">
          <Block shape="line" w="55%" h={12} />
          <span class="sh-conn-chips">
            <Block shape="rect" w="112px" h={32} extra="sh-conn-skel-pill" />
            <Block shape="rect" w="96px" h={32} extra="sh-conn-skel-pill" />
          </span>
        </div>
      </section>
    </>
  )
}
