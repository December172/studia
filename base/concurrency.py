"""Shared bounds for user-configurable worker counts."""

DEFAULT_WORKERS = 1
MAX_WORKERS = 8
INTERACTIVE_WORKERS = 8


def validate_workers(value):
    if type(value) is not int or not DEFAULT_WORKERS <= value <= MAX_WORKERS:
        raise ValueError(f"并发数必须为 {DEFAULT_WORKERS}–{MAX_WORKERS} 的整数")
    return value
