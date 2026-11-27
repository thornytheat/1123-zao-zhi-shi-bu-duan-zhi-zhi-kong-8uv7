from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .algorithm import calculate
from .contracts import SpoolInspection, InvalidRow, parse


@dataclass(frozen=True, slots=True)
class SpoolInspectionRejection:
    line: int
    field: str
    code: str


def validate(rows: Iterable[Mapping[str, Any]]) -> tuple[list[SpoolInspection], list[SpoolInspectionRejection]]:
    accepted = []
    rejected = []
    for line, raw in enumerate(rows, 1):
        try:
            accepted.append(parse(raw))
        except InvalidRow as issue:
            rejected.append(SpoolInspectionRejection(line, issue.field, issue.code))
    return accepted, rejected


def evaluate(rows: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None, **options) -> dict:
    """计算班组对账，逐行隔离字段错误，保留原因码和行号。"""
    alternative_rows = options.pop('verified', None)
    if alternative_rows is None:
        alternative_rows = options.pop('inspections', None)
    if alternative_rows is None:
        alternative_rows = options.pop('audit_rows', None)
    if alternative_rows is None:
        alternative_rows = options.pop('records', None)
    if rows is None:
        rows = alternative_rows or []
    if isinstance(rows, Mapping) and ('by_reel' in rows or 'deliverable_mass_kg' in rows):
        options.setdefault('settlement', rows)
        rows = alternative_rows or []
    accepted, rejected = validate(rows or [])
    outcome = calculate(accepted, **options)
    return {
        'records': outcome,
        'accepted_count': len(accepted),
        'rejected_count': len(rejected),
        'errors': [asdict(error) for error in rejected],
    }
from .grade_backtest import audit_grade_backtest


def __getattr__(name):
    from ..shift_settlement import FlexRecord, _dynamic_function
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
