"""Pure domain dataclasses — no I/O, no imports from services or repos.

All domain types are ``@dataclass(slots=True, frozen=True)`` so that
a domain value is trivially hashable, safe to share across coroutines, and
cannot be mutated by a service by accident.
"""
