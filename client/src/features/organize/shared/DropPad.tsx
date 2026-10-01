/**
 * DropPad — the dashed "Drop here to move into Aldi" target an empty
 * bucket shows while something is being dragged, so an empty store
 * section (or board column) is still a place to drop onto. Purely
 * visual: the drop handlers live on the bucket (``useDragBuckets``),
 * and the keyboard path is the row's own picker, so it's aria-hidden.
 */
interface DropPadProps {
  label: string
  /** The pointer is over this bucket right now. */
  active?: boolean
}

export function DropPad({ label, active }: DropPadProps) {
  return (
    <div class={active ? 'sh-drop-pad sh-drop-pad--active' : 'sh-drop-pad'} aria-hidden="true">
      {label}
    </div>
  )
}
