/**
 * CommentThread — threaded comment display (§23.46).
 *
 * Renders a nested comment tree with inline reply / edit / delete
 * affordances. Reply uses module-level signals (only one draft open at
 * a time). Edit uses local component state (multiple can coexist).
 */
import { signal } from '@preact/signals'
import { useState } from 'preact/hooks'
import { Avatar } from './Avatar'
import { Button } from './Button'
import { currentUser } from '@/store/auth'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import type { Comment } from '@/types'

interface CommentThreadProps {
  comments: Comment[]
  /** When set, the thread lives inside a space — drives avatar + display-name
   *  resolution through the per-space override cache. */
  spaceId?: string | null
  onReply: (parentId: string | null, content: string) => Promise<void>
  onDelete?: (commentId: string) => Promise<void> | void
  onEdit?: (commentId: string, content: string) => Promise<void>
}

const replyTo = signal<string | null>(null)
const replyContent = signal('')
const submitting = signal(false)

export function CommentThread(
  { comments, spaceId, onReply, onDelete, onEdit }: CommentThreadProps,
) {
  const topLevel = comments.filter(c => !c.parent_id)
  const replies = (parentId: string) =>
    comments.filter(c => c.parent_id === parentId)

  const handleSubmit = async (parentId: string | null) => {
    if (!replyContent.value.trim() || submitting.value) return
    submitting.value = true
    try {
      await onReply(parentId, replyContent.value)
      replyContent.value = ''
      replyTo.value = null
    } finally {
      submitting.value = false
    }
  }

  if (topLevel.length === 0) {
    return (
      <div class="sh-comments">
        <div class="sh-comment-empty sh-muted">
          No comments yet — be the first to reply.
        </div>
        <div class="sh-comment-new">
          <input placeholder="Add a comment…" value={replyContent.value}
            onInput={(e) => replyContent.value = (e.target as HTMLInputElement).value}
            onKeyDown={(e) => e.key === 'Enter' && handleSubmit(null)}
            aria-label="New comment" />
          <Button onClick={() => handleSubmit(null)} loading={submitting.value}
                  disabled={!replyContent.value.trim()}>
            Post
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div class="sh-comments">
      {topLevel.map(c => (
        <div key={c.id} class="sh-comment">
          <CommentItem comment={c} spaceId={spaceId}
            onDelete={onDelete} onEdit={onEdit}
            onReplyClick={() =>
              replyTo.value = replyTo.value === c.id ? null : c.id} />
          {replies(c.id).length > 0 && (
            <div class="sh-comment-replies">
              {replies(c.id).map(r => (
                <CommentItem key={r.id} comment={r} spaceId={spaceId}
                  onDelete={onDelete} onEdit={onEdit} indent />
              ))}
            </div>
          )}
          {replyTo.value === c.id && (
            <div class="sh-comment-reply-form">
              <input placeholder={`Reply to ${c.author}…`}
                value={replyContent.value} autoFocus
                onInput={(e) => replyContent.value = (e.target as HTMLInputElement).value}
                onKeyDown={(e) => e.key === 'Enter' && handleSubmit(c.id)} />
              <Button variant="secondary"
                      onClick={() => { replyTo.value = null; replyContent.value = '' }}>
                Cancel
              </Button>
              <Button onClick={() => handleSubmit(c.id)}
                      loading={submitting.value}
                      disabled={!replyContent.value.trim()}>
                Reply
              </Button>
            </div>
          )}
        </div>
      ))}
      <div class="sh-comment-new">
        <input placeholder="Add a comment…" value={replyContent.value}
          onInput={(e) => replyContent.value = (e.target as HTMLInputElement).value}
          onKeyDown={(e) => e.key === 'Enter' && handleSubmit(null)}
          aria-label="New comment" />
        <Button onClick={() => handleSubmit(null)} loading={submitting.value}
                disabled={!replyContent.value.trim()}>
          Post
        </Button>
      </div>
    </div>
  )
}

function CommentItem({ comment, spaceId, onDelete, onEdit, onReplyClick, indent }: {
  comment: Comment
  spaceId?: string | null
  onDelete?: (id: string) => Promise<void> | void
  onEdit?: (id: string, content: string) => Promise<void>
  onReplyClick?: () => void
  indent?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(comment.content ?? '')
  const [saving, setSaving] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const isMine = currentUser.value?.user_id === comment.author
  const canDelete = !!onDelete && (isMine || currentUser.value?.is_admin)
  const canEdit = !!onEdit && isMine && comment.type === 'text'

  const authorName = resolveDisplayName(spaceId, comment.author, comment.author)
  const avatarUrl = resolveAvatar(spaceId, comment.author, null)

  if (comment.content === null || comment.deleted) {
    return (
      <div class={`sh-comment-item ${indent ? 'sh-comment--indent' : ''}`}>
        <em class="sh-muted">(deleted)</em>
      </div>
    )
  }

  if (editing) {
    const save = async () => {
      if (!draft.trim() || !onEdit) return
      setSaving(true)
      try {
        await onEdit(comment.id, draft)
        setEditing(false)
      } finally {
        setSaving(false)
      }
    }
    return (
      <div class={`sh-comment-item ${indent ? 'sh-comment--indent' : ''}`}>
        <Avatar name={authorName} src={avatarUrl} size={28} />
        <div class="sh-comment-body">
          <div class="sh-comment-bubble sh-comment-bubble--editing">
            <span class="sh-comment-author">{authorName}</span>
            <input type="text" value={draft} maxLength={2000} autoFocus
              onInput={(e) => setDraft((e.target as HTMLInputElement).value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void save()
                if (e.key === 'Escape') setEditing(false)
              }} />
          </div>
          <div class="sh-comment-actions">
            <button type="button" class="sh-link"
                    disabled={saving}
                    onClick={() => void save()}>
              Save
            </button>
            <button type="button" class="sh-link sh-link--muted"
                    onClick={() => {
                      setEditing(false)
                      setDraft(comment.content ?? '')
                    }}>
              Cancel
            </button>
          </div>
        </div>
      </div>
    )
  }

  const closeMenu = () => setMenuOpen(false)
  const hasMenu = canEdit || canDelete

  return (
    <div class={`sh-comment-item ${indent ? 'sh-comment--indent' : ''}`}>
      <Avatar name={authorName} src={avatarUrl} size={28} />
      <div class="sh-comment-body">
        <div class="sh-comment-bubble">
          <span class="sh-comment-author">{authorName}</span>
          <span class="sh-comment-text">{comment.content}</span>
        </div>
        <div class="sh-comment-actions">
          {onReplyClick && (
            <button class="sh-link" type="button"
                    onClick={onReplyClick}>Reply</button>
          )}
          <time title={new Date(comment.created_at).toLocaleString()}>
            {formatRelative(comment.created_at)}
          </time>
          {comment.edited_at && (
            <span class="sh-comment-edited">edited</span>
          )}
          {hasMenu && (
            <div class="sh-comment-overflow-wrap">
              <button
                type="button"
                class="sh-comment-overflow"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                aria-label="Comment options"
                onClick={() => setMenuOpen((v) => !v)}
                onBlur={() => setTimeout(closeMenu, 100)}>
                ···
              </button>
              {menuOpen && (
                <div class="sh-post-menu" role="menu">
                  {canEdit && (
                    <button
                      role="menuitem"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        closeMenu()
                        setDraft(comment.content ?? '')
                        setEditing(true)
                      }}>
                      Edit
                    </button>
                  )}
                  {canDelete && (
                    <button
                      role="menuitem"
                      class="sh-post-menu-danger"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        closeMenu()
                        if (confirm('Delete this comment?')) {
                          void onDelete!(comment.id)
                        }
                      }}>
                      Delete
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
