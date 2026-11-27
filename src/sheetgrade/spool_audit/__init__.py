"""班组对账。"""
from .service import evaluate

__all__ = ["evaluate"]
from .service import audit_grade_backtest
__all__ += ['audit_grade_backtest']


def __getattr__(name):
    from ..shift_settlement import FlexRecord, _dynamic_function
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
