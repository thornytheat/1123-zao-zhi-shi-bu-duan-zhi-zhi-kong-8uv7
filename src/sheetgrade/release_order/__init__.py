"""质检处置。"""
from .service import evaluate

__all__ = ["evaluate"]

from .service import stress_laboratory
__all__ += ["stress_laboratory"]


def __getattr__(name):
    from ..shift_settlement import FlexRecord, _dynamic_function
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
