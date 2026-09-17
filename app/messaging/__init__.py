"""RabbitMQ messaging (implementation of the ``MessageBus`` port)."""

from app.messaging.bus import RabbitMessageBus, get_bus

__all__ = ["RabbitMessageBus", "get_bus"]
