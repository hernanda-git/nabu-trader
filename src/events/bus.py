"""In-process event bus — simple pub/sub for loose coupling."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

from src.domain.models import Event

log = logging.getLogger("events.bus")

EventHandler = Callable[[Event], None]


class EventBus:
    """Simple synchronous in-process event bus.

    Consumers subscribe via ``subscribe(event_type, handler)``.
    Producers emit via ``emit(event)``.

    Example::

        bus = EventBus()
        bus.subscribe("SignalReceived", my_handler)
        bus.emit(Event("SignalReceived", {"message_id": 123}))
    """

    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        self._handlers[event_type].append(handler)
        log.debug("Subscribed handler for %s (total: %d)", event_type, len(self._handlers[event_type]))

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler from an event type."""
        if handler in self._handlers.get(event_type, []):
            self._handlers[event_type].remove(handler)
            log.debug("Unsubscribed handler for %s", event_type)

    def emit(self, event: Event) -> None:
        """Emit an event to all subscribed handlers."""
        log.debug("Emitting %s", event.event_type)
        for handler in self._handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                log.exception("Handler %s failed for event %s", handler.__name__, event.event_type)

    def clear(self) -> None:
        """Remove all handlers."""
        self._handlers.clear()


# Module-level singleton (optional, for simple apps)
_default_bus: EventBus | None = None


def get_bus() -> EventBus:
    """Get or create the default singleton event bus."""
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus


def reset_bus() -> None:
    """Reset the singleton bus (useful in tests)."""
    global _default_bus
    _default_bus = None
