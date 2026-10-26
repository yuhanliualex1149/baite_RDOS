"""Process-local deadline propagated into existing typed adapter worker calls."""
from contextvars import ContextVar
import time

DEADLINE = ContextVar('rdos_maintenance_deadline', default=None)

class MaintenanceDeadline(ValueError):
    code = 'maintenance_deadline'

def cli_timeout(default=40):
    deadline = DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MaintenanceDeadline('维护已达到30分钟限额，未发起新的外部操作。')
    return min(default, remaining)
