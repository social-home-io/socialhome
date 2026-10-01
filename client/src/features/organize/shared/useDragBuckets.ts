/**
 * useDragBuckets — HTML5 drag-and-drop of rows between buckets
 * (shopping items between store sections; store headers to reorder;
 * later task cards between columns).
 *
 * Each instance owns one drag *kind*, told apart by its ``mime`` on
 * ``dataTransfer`` — so two instances can share the same bucket
 * element (Shopping's section takes both item drops and store-header
 * drops) without one swallowing the other's drag. Combine their
 * bucket handlers with ``composeDragHandlers``.
 *
 * Drag is a pointer nicety only: every caller must also offer a
 * keyboard path (Shopping: the row's store picker and ▲▼ buttons).
 */
import { useState } from 'preact/hooks'

export interface DragItemProps {
  draggable: boolean
  onDragStart: (e: DragEvent) => void
  onDragEnd: (e: DragEvent) => void
}

export interface DragBucketHandlers {
  onDragOver: (e: DragEvent) => void
  onDragLeave: (e: DragEvent) => void
  onDrop: (e: DragEvent) => void
}

export interface DragBuckets<B extends string> {
  /** Id of the row being dragged, or ``null``. */
  draggingId: string | null
  /** Bucket the drag is hovering, or ``null``. */
  overBucket: B | null
  itemProps: (id: string) => DragItemProps
  bucketProps: (bucket: B) => DragBucketHandlers
}

interface Options<B extends string> {
  /** Opaque type stamped on ``dataTransfer``. */
  mime: string
  onDrop: (id: string, bucket: B) => void
  /** Return ``false`` for buckets that refuse this kind of drag. */
  canDrop?: (bucket: B) => boolean
}

function carries(e: DragEvent, mime: string): boolean {
  return !!e.dataTransfer?.types?.includes(mime)
}

export function useDragBuckets<B extends string>({
  mime, onDrop, canDrop,
}: Options<B>): DragBuckets<B> {
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [overBucket, setOverBucket] = useState<B | null>(null)

  const end = () => {
    setDraggingId(null)
    setOverBucket(null)
  }

  const itemProps = (id: string): DragItemProps => ({
    draggable: true,
    onDragStart: (e) => {
      // A drag inside a nested draggable (header inside a section)
      // must not also start the outer one.
      e.stopPropagation()
      e.dataTransfer?.setData(mime, id)
      // "move", not Chrome's default "copy" — the row moves.
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
      setDraggingId(id)
    },
    onDragEnd: end,
  })

  const bucketProps = (bucket: B): DragBucketHandlers => ({
    onDragOver: (e) => {
      if (!carries(e, mime)) return
      if (canDrop && !canDrop(bucket)) return
      e.preventDefault()
      if (overBucket !== bucket) setOverBucket(bucket)
    },
    onDragLeave: (e) => {
      if (!carries(e, mime)) return
      // ``dragleave`` also fires when moving onto a child — only clear
      // once the pointer really left the bucket.
      const host = e.currentTarget as Node | null
      const next = e.relatedTarget as Node | null | undefined
      if (next ? host?.contains(next) : e.target !== host) return
      if (overBucket === bucket) setOverBucket(null)
    },
    onDrop: (e) => {
      if (!carries(e, mime)) return
      if (canDrop && !canDrop(bucket)) return
      e.preventDefault()
      const id = e.dataTransfer?.getData(mime) || draggingId
      end()
      if (id) onDrop(id, bucket)
    },
  })

  return { draggingId, overBucket, itemProps, bucketProps }
}

/** Run several bucket handler sets on one element, in order. */
export function composeDragHandlers(...sets: DragBucketHandlers[]): DragBucketHandlers {
  return {
    onDragOver: (e) => sets.forEach(s => s.onDragOver(e)),
    onDragLeave: (e) => sets.forEach(s => s.onDragLeave(e)),
    onDrop: (e) => sets.forEach(s => s.onDrop(e)),
  }
}
