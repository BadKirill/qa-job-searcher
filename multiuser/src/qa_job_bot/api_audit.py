from contextvars import ContextVar

CALL_AUDIT = ContextVar("api_call_audit", default=None)


def audit_call(event, **data):
    observer = CALL_AUDIT.get()
    if observer is not None:
        observer(event, data)
