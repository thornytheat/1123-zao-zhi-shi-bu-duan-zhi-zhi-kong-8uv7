"""机台抄表。"""
from .service import evaluate

__all__ = ["evaluate"]
from .service import summarize_reels
__all__ += ['summarize_reels']


def __getattr__(name):
    from ..shift_settlement import FlexRecord, _dynamic_function
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
