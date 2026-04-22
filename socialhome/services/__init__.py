"""Service layer — business logic that orchestrates repositories + events.

Services are stateless async classes. They:

* receive their dependencies (repositories, event bus, config) via
  constructor injection;
* translate route-level inputs into domain objects;
* enforce domain invariants (permission checks, validation);
* persist state via repositories;
* publish :class:`~socialhome.domain.events.DomainEvent` values on the
  injected :class:`~socialhome.infrastructure.event_bus.EventBus` so that
  subscribers (notification / websocket / federation broadcast) can react.

No service imports aiohttp or any DB driver directly — that keeps them
unit-testable against in-memory repo fakes.
"""

from .dm_service import DmService
from .feed_service import FeedService
from .notification_service import NotificationService
from .space_service import SpaceService
from .user_service import UserService

__all__ = [
    "DmService",
    "FeedService",
    "NotificationService",
    "SpaceService",
    "UserService",
]
