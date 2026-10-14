"""Mobile trade notifications (Telegram).

Push every trade lifecycle event — entry, modify, exit — to the operator's phone,
for paper trades as well as live. AshwaX runs unattended and the React dashboard is
localhost-only, so without this there is no way to learn that a trade happened
without sitting in front of the machine.

The public surface is deliberately tiny: hook sites import exactly one sync
function, `notify()`, which can neither block the event loop nor raise. Everything
else (HTTP, retries, throttling, persistence) happens in a background worker task.
"""

from src.notifications.events import EventKind, NotificationEvent, Priority
from src.notifications.notifier import Notifier, get_notifier, init_notifier, notify

__all__ = [
    "EventKind",
    "NotificationEvent",
    "Notifier",
    "Priority",
    "get_notifier",
    "init_notifier",
    "notify",
]
