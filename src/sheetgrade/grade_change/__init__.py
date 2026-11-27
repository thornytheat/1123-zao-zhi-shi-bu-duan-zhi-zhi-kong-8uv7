"""纸种换线。"""
from .service import evaluate

__all__ = ["evaluate"]

from .service import audit_grade_timeline
__all__ += ["audit_grade_timeline"]


def __getattr__(name):
    from ..shift_settlement import FlexRecord, _dynamic_function
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
