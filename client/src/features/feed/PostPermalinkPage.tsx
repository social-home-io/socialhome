/**
 * PostPermalinkPage — ``/post/{id}``: one household feed post with its
 * comments, the page a ``post_created`` / ``comment_added`` notification
 * opens. ``?comment={cid}`` picks that comment out (see
 * ``PostPermalink``). The way back is the household feed.
 */
import { useRoute } from 'preact-iso'
import { PostPermalink } from '@/components/PostPermalink'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'

export default function PostPermalinkPage() {
  useTitle(t('page_title.post'))
  const { params, query } = useRoute()
  return (
    <PostPermalink
      postId={params.id}
      spaceId={null}
      commentId={query?.comment ?? null}
      backPath="/feed"
    />
  )
}
