/**
 * CommentThread — threaded comment display (§23.46).
 *
 * Renders a nested comment tree with inline reply / edit / delete
 * affordances. The two distinct draft surfaces — the inline "Reply
 * to {author}…" form and the always-visible "Add a comment…" input
 * at the bottom — keep their own module-level signals so typing in
 * one doesn't echo into the other when both are on screen at the
 * same time. Edit uses local component state (multiple can coexist).
 */
import { canModerate } from '@/features/spaces/spaceRoles'
import { signal, type Signal } from '@preact/signals'
import { useRef, useState } from 'preact/hooks'
import { Avatar } from './Avatar'
import { Button } from './Button'
import {
  EmojiAutocomplete,
  checkForEmojiTrigger,
  closeEmojiAutocomplete,
  handleEmojiAutocompleteKey,
} from './EmojiAutocomplete'
import { EmojiPickButton } from './EmojiPickButton'
import {
  MentionAutocomplete,
  checkForMentionTrigger,
  closeMentionAutocomplete,
  handleMentionAutocompleteKey,
  mentionInputAria,
} from './MentionAutocomplete'
import { TypingIndicator, sendTyping } from './TypingIndicator'
import { currentUser } from '@/store/auth'
import { spaceMentionRender, viewerSpaceRole } from '@/store/spaceMembers'
import { splitMentions } from '@/utils/mentions'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import type { Comment } from '@/types'
import { confirmDialog } from '@/components/confirm'
import { openReport } from './ReportDialog'
import { t } from '@/i18n/i18n'

interface CommentThreadProps {
  comments: Comment[]
  /** When set, the thread lives inside a space — drives avatar + display-name
   *  resolution through the per-space override cache AND scopes the
   *  ``comment.user_typing`` fan-out to space members. */
  spaceId?: string | null
  /** Post the thread belongs to. Required for typing indicators; left
   *  optional only so existing test fixtures keep working. */
  postId?: string
  onReply: (parentId: string | null, content: string) => Promise<void>
  onDelete?: (commentId: string) => Promise<void> | void
  onEdit?: (commentId: string, content: string) => Promise<void>
}

const replyTo = signal<string | null>(null)
// Inline "Reply to …" form — bound only to the open reply, of which
// there is at most one at a time.
const replyContent = signal('')
// Bottom "Add a comment…" input — separate signal so it can stay
// drafted while the user opens a reply form on a sibling comment.
const newCommentContent = signal('')
const submitting = signal(false)

/** Splice an emoji into a Signal-bound text input at ``[start, end]``.
 *  Used by the ``:foo`` autocomplete to replace the typed token with
 *  the picked glyph. */
function spliceIntoSignal(
  target: Signal<string>,
  emoji: string,
  range: [number, number],
): void {
  const [start, end] = range
  const before = target.value.slice(0, start)
  const after = target.value.slice(end)
  target.value = before + emoji + after
}

/** Generic ``onInput`` handler that syncs the input value into a
 *  signal AND fires the ``:foo`` autocomplete check. The autocomplete
 *  needs a splice callback so a single ``<EmojiAutocomplete>`` mount
 *  can route picks back to whichever input started the trigger. */
function bindEmojiAwareInput(target: Signal<string>, spaceId?: string | null) {
  const splice = (emoji: string, range: [number, number]) =>
    spliceIntoSignal(target, emoji, range)
  return (e: Event) => {
    const t = e.target as HTMLInputElement
    target.value = t.value
    checkForEmojiTrigger(t.value, t.selectionStart ?? 0, t, splice)
    // Space threads only (``spaceId`` null → never opens): ``@`` picks a
    // member of the space.
    checkForMentionTrigger(t.value, t.selectionStart ?? 0, t, spaceId, splice)
  }
}

/** Autocomplete keys first (mention, then emoji); ``true`` = consumed. */
function autocompleteKey(e: KeyboardEvent): boolean {
  if (handleMentionAutocompleteKey(e) || handleEmojiAutocompleteKey(e)) {
    e.preventDefault()
    return true
  }
  return false
}

function closeAutocompletes(): void {
  closeEmojiAutocomplete()
  closeMentionAutocomplete()
}

/** Spread onto a comment input: live combobox ARIA while the mention
 *  picker is anchored to that input (looked up by its id, so each input
 *  only claims the picker it opened). */
function mentionAria(id: string, spaceId?: string | null) {
  if (!spaceId) return {}
  return mentionInputAria(document.getElementById(id))
}

export function CommentThread(
  { comments, spaceId, postId, onReply, onDelete, onEdit }: CommentThreadProps,
) {
  // Same 2-second cooldown the DM thread uses (DmThreadPage). The
  // server already throttles to 1 emit / sec internally; the client
  // throttle just trims keystroke chatter on the WS pipe.
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const fireTyping = () => {
    if (!postId) return
    if (typingTimer.current) return
    sendTyping({ postId, spaceId: spaceId ?? null })
    typingTimer.current = setTimeout(() => {
      typingTimer.current = null
    }, 2000)
  }
  const topLevel = comments.filter(c => !c.parent_id)
  const replies = (parentId: string) =>
    comments.filter(c => c.parent_id === parentId)

  const handleSubmit = async (parentId: string | null) => {
    const draft = parentId ? replyContent : newCommentContent
    if (!draft.value.trim() || submitting.value) return
    submitting.value = true
    try {
      await onReply(parentId, draft.value)
      draft.value = ''
      if (parentId) replyTo.value = null
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
        {postId && (
          <TypingIndicator scope={`post:${postId}`} />
        )}
        <div class="sh-comment-new">
          <input placeholder="Add a comment…" value={newCommentContent.value}
            id="sh-comment-new-input"
            {...mentionAria('sh-comment-new-input', spaceId)}
            onInput={(e) => {
              bindEmojiAwareInput(newCommentContent, spaceId)(e)
              fireTyping()
            }}
            onKeyDown={(e) => {
              if (autocompleteKey(e)) return
              if (e.key === 'Enter') handleSubmit(null)
            }}
            onBlur={closeAutocompletes}
            aria-label="New comment" />
          <EmojiPickButton target={newCommentContent} openKey="comment-new" />
          <Button onClick={() => handleSubmit(null)} loading={submitting.value}
                  disabled={!newCommentContent.value.trim()}>
            Post
          </Button>
        </div>
        <EmojiAutocomplete />
        {spaceId && <MentionAutocomplete />}
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
                id="sh-comment-reply-input"
                {...mentionAria('sh-comment-reply-input', spaceId)}
                value={replyContent.value} autoFocus
                aria-label={`Reply to ${c.author}`}
                onInput={(e) => {
                  bindEmojiAwareInput(replyContent, spaceId)(e)
                  fireTyping()
                }}
                onKeyDown={(e) => {
                  if (autocompleteKey(e)) return
                  if (e.key === 'Enter') handleSubmit(c.id)
                }}
                onBlur={closeAutocompletes} />
              <EmojiPickButton target={replyContent} openKey={`reply-${c.id}`} />
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
      {postId && (
        <TypingIndicator scope={`post:${postId}`} />
      )}
      <div class="sh-comment-new">
        <input placeholder="Add a comment…" value={newCommentContent.value}
          id="sh-comment-new-input"
          {...mentionAria('sh-comment-new-input', spaceId)}
          onInput={(e) => {
            bindEmojiAwareInput(newCommentContent, spaceId)(e)
            fireTyping()
          }}
          onKeyDown={(e) => {
            if (autocompleteKey(e)) return
            if (e.key === 'Enter') handleSubmit(null)
          }}
          onBlur={closeAutocompletes}
          aria-label="New comment" />
        <EmojiPickButton target={newCommentContent} openKey="comment-new" />
        <Button onClick={() => handleSubmit(null)} loading={submitting.value}
                disabled={!newCommentContent.value.trim()}>
          Post
        </Button>
      </div>
      {/* Mounted once for the whole thread; module-level state keeps it
          singleton across all three input surfaces. Each input
          registers its own splice target via :func:`checkForEmojiTrigger`
          / :func:`checkForMentionTrigger`. */}
      <EmojiAutocomplete />
      {spaceId && <MentionAutocomplete />}
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
  // In a space, acting on somebody else's comment is content authority
  // (owner / admin / moderator, from the viewer's SPACE role); on the
  // household feed it stays the household admin's delete.
  const moderates = spaceId
    ? canModerate(viewerSpaceRole(spaceId))
    : !!currentUser.value?.is_admin
  const canDelete = !!onDelete && (isMine || moderates)
  const canEdit = !!onEdit && comment.type === 'text' && (isMine || (!!spaceId && moderates))

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
  // Anyone may report someone else's comment — in a space it goes to the
  // space's moderators (``ReportDialog``).
  const canReport = !isMine
  const hasMenu = canEdit || canDelete || canReport

  return (
    <div class={`sh-comment-item ${indent ? 'sh-comment--indent' : ''}`}>
      <Avatar name={authorName} src={avatarUrl} size={28} />
      <div class="sh-comment-body">
        <div class="sh-comment-bubble">
          <span class="sh-comment-author">{authorName}</span>
          <span class="sh-comment-text">
            <MentionText text={comment.content ?? ''} spaceId={spaceId}
              authorId={comment.author} />
          </span>
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
                      onClick={async () => {
                        closeMenu()
                        if (await confirmDialog('Delete this comment?', { destructive: true })) {
                          void onDelete!(comment.id)
                        }
                      }}>
                      Delete
                    </button>
                  )}
                  {canReport && (
                    <button
                      role="menuitem"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        closeMenu()
                        openReport('comment', comment.id, spaceId)
                      }}>
                      {t('report.action')}
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

/** Plain comment text with the space's known @-mentions highlighted.
 *  JSX all the way down — user text is never parsed as HTML. */
function MentionText({ text, spaceId, authorId }: {
  text: string
  spaceId?: string | null
  authorId?: string | null
}) {
  const { mentions, selfMention } = spaceMentionRender(spaceId, authorId)
  const self = selfMention?.toLocaleLowerCase() ?? null
  return (
    <>
      {splitMentions(text, mentions).map((p, i) => (
        typeof p === 'string'
          ? p
          : (
            <span key={i}
              class={p.token === 'here'
                ? 'sh-mention sh-mention--here'
                : p.token === self ? 'sh-mention sh-mention--self' : 'sh-mention'}>
              {p.raw}
            </span>
          )
      ))}
    </>
  )
}
