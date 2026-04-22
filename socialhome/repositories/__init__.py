"""Repositories — abstract interfaces + SQLite implementations.

Services depend on the abstract bases in this package, not on the concrete
``Sqlite*`` classes. In unit tests we substitute in-memory fakes that
implement the same interface.
"""

from .bazaar_repo import (
    AbstractBazaarRepo,
    BidStateError,
    SqliteBazaarRepo,
    new_bid,
)
from .calendar_repo import (
    AbstractCalendarRepo,
    AbstractSpaceCalendarRepo,
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)
from .conversation_repo import (
    AbstractConversationRepo,
    SqliteConversationRepo,
    new_conversation,
)
from .federation_repo import AbstractFederationRepo, SqliteFederationRepo
from .notification_repo import (
    AbstractNotificationRepo,
    Notification,
    SqliteNotificationRepo,
    new_notification,
)
from .outbox_repo import AbstractOutboxRepo, OutboxEntry, SqliteOutboxRepo
from .page_repo import (
    AbstractPageRepo,
    Page,
    PageLockError,
    PageNotFoundError,
    PageVersion,
    SqlitePageRepo,
    new_page,
)
from .post_repo import AbstractPostRepo, SqlitePostRepo
from .push_subscription_repo import (
    AbstractPushSubscriptionRepo,
    PushSubscription,
    SqlitePushSubscriptionRepo,
)
from .shopping_repo import (
    AbstractShoppingRepo,
    ShoppingItem,
    SqliteShoppingRepo,
)
from .space_post_repo import AbstractSpacePostRepo, SqliteSpacePostRepo
from .space_repo import AbstractSpaceRepo, SqliteSpaceRepo
from .sticky_repo import (
    AbstractStickyRepo,
    SqliteStickyRepo,
    Sticky,
)
from .task_repo import (
    AbstractSpaceTaskRepo,
    AbstractTaskRepo,
    SqliteSpaceTaskRepo,
    SqliteTaskRepo,
)
from .user_repo import AbstractUserRepo, SqliteUserRepo

__all__ = [
    "AbstractBazaarRepo",
    "AbstractCalendarRepo",
    "AbstractConversationRepo",
    "AbstractFederationRepo",
    "AbstractNotificationRepo",
    "AbstractOutboxRepo",
    "AbstractPageRepo",
    "AbstractPostRepo",
    "AbstractPushSubscriptionRepo",
    "AbstractShoppingRepo",
    "AbstractSpaceCalendarRepo",
    "AbstractSpacePostRepo",
    "AbstractSpaceRepo",
    "AbstractSpaceTaskRepo",
    "AbstractStickyRepo",
    "AbstractTaskRepo",
    "AbstractUserRepo",
    "BidStateError",
    "Notification",
    "OutboxEntry",
    "Page",
    "PageLockError",
    "PageNotFoundError",
    "PageVersion",
    "PushSubscription",
    "ShoppingItem",
    "SqliteBazaarRepo",
    "SqliteCalendarRepo",
    "SqliteConversationRepo",
    "SqliteFederationRepo",
    "SqliteNotificationRepo",
    "SqliteOutboxRepo",
    "SqlitePageRepo",
    "SqlitePostRepo",
    "SqlitePushSubscriptionRepo",
    "SqliteShoppingRepo",
    "SqliteSpaceCalendarRepo",
    "SqliteSpacePostRepo",
    "SqliteSpaceRepo",
    "SqliteSpaceTaskRepo",
    "SqliteStickyRepo",
    "SqliteTaskRepo",
    "SqliteUserRepo",
    "Sticky",
    "new_bid",
    "new_conversation",
    "new_notification",
    "new_page",
]
