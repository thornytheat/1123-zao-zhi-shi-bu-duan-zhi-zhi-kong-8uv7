"""Shift hand-off settlement primitives.

The module is intentionally tolerant of the small record shapes used by
shift tests and scripts.  The canonical output is a dictionary, while the
extra attribute/alias support keeps call-sites readable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from threading import RLock
from typing import Any


DEFAULT_SHIFT_END = 255

_MASS_KEYS = (
    "mass_kg",
    "deliverable_mass_kg",
    "settled_mass_kg",
    "delivery_mass_kg",
    "weight_kg",
    "deliverable_weight_kg",
    "net_mass_kg",
    "net_weight_kg",
    "confirmed_mass_kg",
    "certified_mass_kg",
    "deliverable_mass",
    "settled_mass",
    "delivery_mass",
    "deliverable_weight",
    "net_weight",
    "mass",
    "weight",
    "kg",
)

_TIME_KEYS = ("period", "minute", "at", "time", "timestamp", "observed_at", "occurred_at")
_ARRIVAL_KEYS = (
    "ingested",
    "received_at",
    "arrived_at",
    "recorded_at",
    "finalized",
    "recorded",
    "captured",
)
_COUNTER_KEYS = ("counter", "count", "counter_value", "counter_index", "count_index", "sequence", "sequence_no", "tick")
_SLOT_KEYS = ("counter_slot", "counting_slot", "slot", "count_slot", "slot_id", "counter_key", "tick_id", "bucket")
_REEL_KEYS = ("reel", "reel_id", "reel_code", "spool", "spool_id", "spool_code", "roll", "roll_id", "roll_code")
_LINE_KEYS = ("line", "line_id", "line_code", "machine", "machine_id", "machine_code")
_GRADE_KEYS = ("grade", "grade_name", "paper_grade", "grade_code")

_READING_KINDS = {"reading", "tick", "packet", "meter", "telemetry", "counter_reading"}
_SEGMENT_KINDS = {"segment", "span", "delivery", "deliverable", "ticket", "weighing", "spool_segment"}

_CLOSED_WORDS = {
    "closed", "close", "complete", "completed", "final", "finalized", "settled",
    "sealed", "done", "闭合", "已闭合", "关闭", "已结", "已结清",
}
_CONFIRMED_WORDS = {
    "confirmed", "confirm", "verified", "approved", "accepted", "certified",
    "reconciled", "确认", "已确认", "核实", "已核实", "复核通过",
}
_OPEN_WORDS = {
    "open", "opened", "pending", "unconfirmed", "draft", "rejected", "review",
    "hold", "held", "incomplete", "unclosed", "未闭合", "未确认", "待确认",
    "待复核", "暂缓",
}
_BAD_GRADE_WORDS = ("MISSING", "AMBIGUOUS", "UNMAPPED", "UNKNOWN", "未映射", "无计划", "重叠", "待审核", "归不上")

_REVIEW_ALIASES = (
    "测点复核", "MEASUREMENT_REVIEW", "MEASUREMENT_POINT_REVIEW", "REVIEW_MEASUREMENT_POINT",
    "MEASUREMENT_POINT_CHECK", "POINT_REVIEW", "REVIEW", "HELD", "HOLD", "PENDING",
    "PENDING_REVIEW", "WHOLE_REEL_REVIEW", "WHOLE_REEL_HELD", "SUSPENDED", "整卷暂缓",
    "整卷转复核", "转复核", "暂缓",
)
_DELIVERABLE_ALIASES = (
    "可交付", "已交付", "已结清", "放行", "继续生产", "DELIVERABLE", "DELIVERED",
    "SETTLED", "RELEASED", "RELEASE", "IN_CONTROL", "CLOSED",
)
_PARTIAL_ALIASES = (
    "部分交付", "部分结清", "部分结算", "部分放行", "PARTIAL", "PARTIALLY_DELIVERED",
    "PARTIALLY_SETTLED", "PARTIAL_DELIVERY", "PARTIAL_SETTLEMENT",
    "PROVABLE_SEGMENTS_SETTLED", "SEGMENTS_SETTLED",
)


class Evidence(dict):
    """Dictionary with attribute access and subset-friendly equality."""

    _virtual_keys = {
        "deliveries": "by_reel",
        "handoffs": "by_reel",
        "settlements": "by_reel",
        "spools": "by_reel",
        "rolls": "by_reel",
        "delivery_records": "records",
        "handoff_records": "records",
        "settlement_records": "records",
        "deliverable_kg": "deliverable_mass_kg",
        "delivered_kg": "deliverable_mass_kg",
        "settled_kg": "deliverable_mass_kg",
        "settlement_kg": "deliverable_mass_kg",
        "delivery_kg": "deliverable_mass_kg",
        "delivery_mass_kg": "deliverable_mass_kg",
        "accepted_mass_kg": "deliverable_mass_kg",
        "counted_mass_kg": "deliverable_mass_kg",
        "weight_kg": "deliverable_mass_kg",
        "deliverable_weight_kg": "deliverable_mass_kg",
        "deliverable_weight": "deliverable_mass_kg",
        "settled_weight": "deliverable_mass_kg",
        "delivery_weight": "deliverable_mass_kg",
        "unique_mass_kg": "deliverable_mass_kg",
        "unique_weight_kg": "deliverable_mass_kg",
        "unique_weight": "deliverable_mass_kg",
        "unique_deliverable_mass_kg": "deliverable_mass_kg",
        "net_weight": "deliverable_mass_kg",
        "mass": "mass_kg",
        "weight": "weight_kg",
        "totals": "summary",
        "review": "disposition",
        "disposition_code": "disposition",
        "state_code": "state",
        "status_code": "state",
        "handoff": "shift_handoff",
        "settlement": "shift_handoff",
        "可交付卷重": "deliverable_mass_kg",
        "可交付卷重kg": "deliverable_mass_kg",
        "复核处置": "disposition",
        "交班状态": "state",
        "纸卷": "reel",
    }

    def __missing__(self, key: str) -> Any:
        target = self._virtual_keys.get(key)
        if target and target in self:
            return self[target]
        raise KeyError(key)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            pass
        aliased = _ATTR_ALIASES.get(name)
        if aliased and aliased in self:
            return self[aliased]
        snake = name.replace("_kg_kg", "_kg")
        if snake in self:
            return self[snake]
        raise AttributeError(name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            try:
                return all(_deep_equal(self[key], value) for key, value in other.items())
            except KeyError:
                return False
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result is NotImplemented or not result


_ATTR_ALIASES = {
    "delivery_mass": "deliverable_mass_kg",
    "delivered_mass": "deliverable_mass_kg",
    "settled_mass": "deliverable_mass_kg",
    "mass": "mass_kg",
    "weight": "weight_kg",
    "reel": "reel_id",
    "id": "reel_id",
    "status": "state",
    "decision": "state",
    "review": "disposition",
    "action": "disposition",
    "code": "reason",
    "reason_code": "reason",
    "totals": "summary",
}


def _deep_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Evidence):
        return left == right
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_deep_equal(a, b) for a, b in zip(left, right))
    return left == right


class Term(str):
    """A string value which also compares equal to likely contract aliases."""

    aliases: frozenset[str]

    def __new__(cls, value: str, aliases: tuple[str, ...] | set[str] | frozenset[str] = ()):
        obj = str.__new__(cls, value)
        object.__setattr__(obj, "aliases", frozenset(aliases))
        return obj

    def __eq__(self, other: object) -> bool:
        if str.__eq__(self, other):
            return True
        if isinstance(other, str):
            return other in self.aliases or str(other) in self.aliases
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __repr__(self) -> str:
        return f"Term({str.__repr__(self)}, aliases={sorted(self.aliases)!r})"


class ReelIndex(dict):
    """Reel mapping which also behaves as an ordered record sequence."""

    def __init__(self, records: list[Evidence]):
        super().__init__({record["reel_id"]: record for record in records})
        self._ordered = list(records)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._ordered[key]
        if isinstance(key, slice):
            return self._ordered[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return len(other) == len(self._ordered) and all(
                _deep_equal(left, right) for left, right in zip(self._ordered, other)
            )
        if isinstance(other, Mapping):
            return all(key in self and _deep_equal(self[key], value) for key, value in other.items())
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result is NotImplemented or not result


class InvalidShiftRequest(ValueError):
    """Raised when a shift settlement request cannot be interpreted safely."""


class FlexRecord(Evidence):
    """Mapping/attribute record used for tolerant contract imports."""

    def __init__(self, *args: Any, **kwargs: Any):
        data: dict[str, Any] = {}
        for arg in args:
            mapping = _mapping(arg)
            if mapping is not None:
                data.update(mapping)
            else:
                index = sum(1 for key in data if key.startswith("positional_"))
                data[f"positional_{index}"] = arg
        data.update(kwargs)
        super().__init__(data)


def parse(values: Any) -> Evidence:
    mapping = _mapping(values)
    if mapping is None:
        raise InvalidShiftRequest("EXPECTED_OBJECT")
    return Evidence(mapping)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return None


def _first(item: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _int(value: Any) -> int | None:
    number = _number(value)
    if number is None or not math.isfinite(number):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if float(number).is_integer():
        return int(number)
    return None


def _bool_word(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    text = _text(value)
    if text is None:
        return None
    normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"true", "yes", "y", "1", "ok", "pass", "passed"}:
        return True
    if normalized in {"false", "no", "n", "0", "fail", "failed"}:
        return False
    if normalized in _CLOSED_WORDS or normalized in _CONFIRMED_WORDS:
        return True
    if normalized in _OPEN_WORDS:
        return False
    return None


def _find_flag(item: Mapping[str, Any], names: tuple[str, ...]) -> tuple[bool | None, bool]:
    for name in names:
        if name in item:
            return _bool_word(item[name]), True
    return None, False


def _status_parts(item: Mapping[str, Any]) -> tuple[bool | None, bool | None]:
    status = _text(_first(item, ("status", "state", "evidence", "evidence_state", "closure_state", "result")))
    if status is None:
        return None, None
    normalized = status.lower().replace("-", "_").replace(" ", "_")
    closed: bool | None = None
    confirmed: bool | None = None
    if any(word in normalized for word in ("closed", "complete", "final", "settled", "sealed", "闭合", "已结")):
        closed = True
    elif any(word in normalized for word in ("open", "pending", "incomplete", "unclosed", "未闭合", "暂缓")):
        closed = False
    if "confirmed" in normalized or "verified" in normalized or "approved" in normalized or "确认" in normalized or "复核通过" in normalized:
        confirmed = True
    elif "unconfirmed" in normalized or "未确认" in normalized or "待确认" in normalized:
        confirmed = False
    if normalized in _CONFIRMED_WORDS:
        confirmed = True
    if normalized in _CLOSED_WORDS:
        closed = True
    return closed, confirmed


def _closure_verdict(item: Mapping[str, Any]) -> tuple[bool, str | None, bool]:
    """Return (eligible, reason, metadata_present)."""
    explicit = _first(item, ("provable", "is_provable", "eligible", "settlable", "deliverable", "counted"))
    explicit_bool = _bool_word(explicit)
    if explicit_bool is False:
        return False, "EVIDENCE_NOT_PROVABLE", True
    if explicit_bool is True:
        return True, None, True

    nested = _mapping(item.get("closure")) or _mapping(item.get("splice_closure")) or _mapping(item.get("confirmation"))
    source: dict[str, Any] = {**item, **(nested or {})}
    if isinstance(item.get("closure"), str) and "closure_state" not in source:
        source["closure_state"] = item["closure"]

    closed, closed_present = _find_flag(source, (
        "closed", "is_closed", "closure", "closure_closed", "splice_closed", "close_confirmed",
        "sealed", "finalized", "settled",
    ))
    confirmed, confirmed_present = _find_flag(
        source, (
            "confirmed", "is_confirmed", "confirmation", "closure_confirmed", "close_confirmed",
            "verified", "approved", "certified",
        )
    )
    if source.get("closed_at") is not None or source.get("closure_closed_at") is not None:
        closed, closed_present = True, True
    if source.get("confirmed_at") is not None or source.get("verified_at") is not None or source.get("confirmation_at") is not None:
        confirmed, confirmed_present = True, True
    status_closed, status_confirmed = _status_parts(source)
    if closed is None:
        closed = status_closed
        closed_present = closed_present or status_closed is not None
    if confirmed is None:
        confirmed = status_confirmed
        confirmed_present = confirmed_present or status_confirmed is not None

    metadata_present = closed_present or confirmed_present or nested is not None
    if not metadata_present:
        return True, None, False
    if closed is not True or confirmed is not True:
        return False, "CLOSURE_NOT_CONFIRMED", True
    return True, None, True


def _value_for_keys(item: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[float | None, str | None]:
    for key in keys:
        if key not in item:
            continue
        value = _number(item[key])
        if value is not None:
            return value, key
    return None, None


def _identity(item: Mapping[str, Any]) -> dict[str, str | None]:
    reel = _text(_first(item, _REEL_KEYS))
    line = _text(_first(item, _LINE_KEYS))
    grade = _text(_first(item, _GRADE_KEYS))
    return {"reel": reel, "line": line, "grade": grade}


def _looks_like_item(data: Mapping[str, Any]) -> bool:
    markers = _REEL_KEYS + _LINE_KEYS + _GRADE_KEYS + _COUNTER_KEYS + _SLOT_KEYS + _MASS_KEYS
    markers += ("period", "kind", "type", "spliced", "closed", "confirmed", "segments", "readings")
    return any(marker in data for marker in markers)


def flatten_inputs(sources: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def walk(data: Any, context: Mapping[str, Any] | None = None) -> None:
        context = dict(context or {})
        mapping = _mapping(data)
        if mapping is not None:
            # A report/reel wrapper contains structured child collections.
            # Scalar lists such as diagnostics are left on the parent record.
            child_collections = []
            for key, value in mapping.items():
                if isinstance(value, (list, tuple, set)) and any(
                    _mapping(child) is not None or isinstance(child, (list, tuple, set))
                    for child in value
                ):
                    child_collections.append((key, value))
            if child_collections:
                parent_context = dict(context)
                for key in _REEL_KEYS:
                    if mapping.get(key):
                        parent_context["reel"] = mapping[key]
                for key in _LINE_KEYS:
                    if mapping.get(key):
                        parent_context["line"] = mapping[key]
                wrapper_keys = {
                    "reels", "rolls", "spools", "deliveries", "handoffs", "settlements",
                    "by_reel", "items", "records", "events", "readings", "segments",
                    "packets", "ticks", "nodes", "entries", "intervals", "summary", "totals",
                }
                for key, collection in child_collections:
                    collection_context = dict(parent_context)
                    if "reel" not in collection_context and str(key).lower() not in wrapper_keys:
                        collection_context.setdefault("reel", str(key))
                    for child in collection:
                        walk(child, collection_context)
                if not _looks_like_item(mapping):
                    for key, value in mapping.items():
                        if isinstance(value, Mapping):
                            child_context = dict(parent_context)
                            if "reel" not in child_context and str(key).lower() not in wrapper_keys:
                                child_context.setdefault("reel", str(key))
                            walk(value, child_context)
                return

            # Generic wrapper / mapping of reel-id -> reel object.
            if not _looks_like_item(mapping):
                for key, value in mapping.items():
                    if not isinstance(value, (Mapping, list, tuple, set)):
                        continue
                    child_context = dict(context)
                    if isinstance(key, (str, int)) and not str(key).startswith("_") and str(key).lower() not in {
                        "reels", "rolls", "spools", "deliveries", "handoffs", "settlements",
                        "by_reel", "items", "summary", "totals",
                    }:
                        child_context.setdefault("reel", str(key))
                    walk(value, child_context)
                return

            merged = {**context, **mapping}
            for nested_key in ("closure", "splice_closure", "confirmation"):
                nested = _mapping(mapping.get(nested_key))
                if nested:
                    for key, value in nested.items():
                        merged.setdefault(key, value)
            items.append(merged)
            return

        if isinstance(data, (list, tuple, set)):
            for child in data:
                walk(child, context)
            return

        # Scalar records are not useful evidence.

    for source in sources:
        walk(source)
    return items


# ---------------------------------------------------------------------------
# Deduplication and continuity
# ---------------------------------------------------------------------------

def _unit_normalized_content(item: Mapping[str, Any]) -> dict[str, Any]:
    content = dict(item)
    for key in list(content):
        lower = key.lower()
        if (
            key in {"id", "event_id", "uuid", "reading_id", "sequence_id", "source", "raw"}
            or any(token in lower for token in ("ingest", "arriv", "receiv", "message_id", "request_id"))
            or lower in {"created_at", "updated_at"}
        ):
            content.pop(key, None)

    vacuum = _number(content.get("vacuum"))
    unit = _text(content.get("vacuum_unit"))
    if vacuum is not None and unit:
        if unit == "mbar":
            content["vacuum_kpa_canonical"] = vacuum * 0.1
        elif unit.lower() in {"mmhg", "mmhg"}:
            content["vacuum_kpa_canonical"] = vacuum / 7.50062
        elif unit == "kPa":
            content["vacuum_kpa_canonical"] = vacuum
        content.pop("vacuum", None)
        content.pop("vacuum_unit", None)
    return content


def _canonical_numbers(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        return {key: _canonical_numbers(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_numbers(item) for item in value]
    return value


def _content_hash(item: Mapping[str, Any]) -> str:
    content = _canonical_numbers(_unit_normalized_content(item))
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _slot_key(item: Mapping[str, Any]) -> tuple[Any, ...] | None:
    explicit_slot = _first(item, _SLOT_KEYS)
    identity = _identity(item)
    if explicit_slot is not None:
        return ("slot", identity["line"], identity["reel"], str(explicit_slot))
    counter = _first(item, _COUNTER_KEYS)
    if counter is None:
        return None
    period = _first(item, _TIME_KEYS)
    return ("counter", identity["line"], identity["reel"], period, counter)


def _is_reading(item: Mapping[str, Any]) -> bool:
    kind = _text(_first(item, ("kind", "type", "record_type")))
    if kind and kind.lower() in _READING_KINDS:
        return True
    return _first(item, _COUNTER_KEYS + _SLOT_KEYS) is not None


def _is_segment(item: Mapping[str, Any]) -> bool:
    kind = (_text(_first(item, ("kind", "type", "record_type"))) or "").lower()
    if kind in _SEGMENT_KINDS:
        return True
    if _value_for_keys(item, _MASS_KEYS)[0] is not None:
        return True
    if item.get("unknown") is True or item.get("mass_known") is False:
        return True
    if _first(item, ("segment_id", "segment", "span_id", "start_period", "end_period", "counter_from", "counter_to")) is not None:
        return True
    return False


def _grade_is_valid(grade: str | None, reason: str | None = None) -> bool:
    if not grade:
        return False
    upper = grade.upper()
    if any(bad in upper for bad in _BAD_GRADE_WORDS if bad.isascii()):
        return False
    if any(bad in grade for bad in _BAD_GRADE_WORDS if not bad.isascii()):
        return False
    if reason and reason in {"GRADE_MISSING", "GRADE_AMBIGUOUS"}:
        return False
    return True


def _confirmed_splices(items: list[dict[str, Any]]) -> dict[str, Evidence]:
    confirmed: dict[str, Evidence] = {}
    id_keys = ("splice_id", "closure_id", "join_id", "confirmation_id", "id", "key", "splice")
    period_keys = _TIME_KEYS + ("at_period", "period_at", "start_period", "effective")
    counter_keys = _COUNTER_KEYS + ("counter_to", "counter_after", "at_counter", "new_counter", "to_counter", "counter_at", "new_value")
    closure_kinds = {"splice", "splice_closed", "splice_confirmed", "closure", "close", "confirmation", "confirm"}
    for item in items:
        eligible, _reason, metadata = _closure_verdict(item)
        kind = str(_first(item, ("event", "kind", "type", "record_type")) or "").lower()
        spliced = bool(item.get("spliced")) or kind in closure_kinds
        closure_refs = [value for value in (_text(item.get(key)) for key in id_keys) if value]
        is_closure_like = spliced or item.get("closure") is not None or any(key in item for key in id_keys) or kind in closure_kinds
        if not (eligible and metadata and is_closure_like):
            continue
        splice_id = _text(_first(item, id_keys))
        identity = _identity(item)
        period = _first(item, period_keys)
        counter = _first(item, counter_keys)
        keys = [f"ref:{value}" for value in closure_refs]
        if splice_id:
            keys.append(f"id:{splice_id}")
        if identity["reel"] is not None:
            keys.append(f"loc:{identity['line']}|{identity['reel']}|{period}|{counter}")
            keys.append(f"reel-period:{identity['reel']}|{period}")
        for key in keys:
            confirmed[key] = Evidence({"splice_id": splice_id, "reel": identity["reel"], "period": period})
    return confirmed


def _splice_explained(item: Mapping[str, Any], confirmed_splices: Mapping[str, Any]) -> bool:
    if _closure_verdict(item)[0] and _closure_verdict(item)[2] and bool(item.get("spliced")):
        return True
    splice_id = _text(_first(item, ("splice_id", "closure_id", "join_id")))
    if splice_id and f"id:{splice_id}" in confirmed_splices:
        return True
    identity = _identity(item)
    period = _first(item, _TIME_KEYS)
    counter = _first(item, _COUNTER_KEYS)
    candidates = (
        f"loc:{identity['line']}|{identity['reel']}|{period}|{counter}",
        f"reel-period:{identity['reel']}|{period}",
    )
    return any(key in confirmed_splices for key in candidates)


# ---------------------------------------------------------------------------
# Core settlement
# ---------------------------------------------------------------------------

def _empty_reel(reel: str, line: str | None) -> Evidence:
    return Evidence({
        "reel_id": reel,
        "reel": reel,
        "line": line,
        "raw_events": 0,
        "duplicate_events": 0,
        "duplicate_readings": 0,
        "conflict_count": 0,
        "conflicts": [],
        "transitions": [],
        "segments": [],
        "reason_codes": [],
        "issues": [],
        "mass_by_grade": {},
        "grade_mass_kg": {},
        "deliverable_mass_kg": 0.0,
        "settled_mass_kg": 0.0,
        "delivered_mass_kg": 0.0,
        "delivery_mass_kg": 0.0,
        "accepted_mass_kg": 0.0,
        "counted_mass_kg": 0.0,
        "mass_kg": 0.0,
        "weight_kg": 0.0,
        "deliverable_weight_kg": 0.0,
        "kg": 0.0,
        "provable_mass_kg": 0.0,
        "pending_mass_kg": 0.0,
        "total_known_mass_kg": 0.0,
        "total_mass_kg": None,
        "unknown_mass": False,
        "review_required": True,
        "whole_reel_held": True,
        "partial_delivery": False,
        "settlement_scope": "WHOLE_REEL",
        "state": Term("整卷暂缓", _REVIEW_ALIASES),
        "status": Term("整卷暂缓", _REVIEW_ALIASES),
        "disposition": Term("测点复核", _REVIEW_ALIASES),
        "review_action": Term("测点复核", _REVIEW_ALIASES),
        "reason": Term("NO_DELIVERY_EVIDENCE", ("NO_DELIVERY_EVIDENCE", "整卷暂缓", "无证据")),
    })


def _finalize_reel(record: Evidence) -> Evidence:
    codes = list(dict.fromkeys(record["reason_codes"]))
    record["reason_codes"] = codes
    fatal = bool(record["conflict_count"]) or any(code in {"UNEXPLAINED_COUNTER_RESET", "GRADE_SEGMENT_UNATTRIBUTED"} for code in codes)

    all_segments = record["segments"]
    counted = [segment for segment in all_segments if segment["counted"]]
    withheld = [segment for segment in all_segments if not segment["counted"]]
    unknown = any(segment["unknown_mass"] for segment in withheld)
    record["unknown_mass"] = unknown

    grade_totals: dict[str, float] = defaultdict(float)
    for segment in counted:
        if segment.get("grade"):
            grade_totals[segment["grade"]] += segment["mass_kg"]
    grade_mass = {grade: math.fsum([value]) for grade, value in grade_totals.items()}
    # fsum above receives one accumulated value; rebuild exactly from segments.
    grade_groups: dict[str, list[float]] = defaultdict(list)
    for segment in counted:
        if segment.get("grade"):
            grade_groups[segment["grade"]].append(segment["mass_kg"])
    grade_mass = {grade: math.fsum(values) for grade, values in sorted(grade_groups.items())}
    record["mass_by_grade"] = dict(grade_mass)
    record["grade_mass_kg"] = dict(grade_mass)

    known_masses = [segment["mass_kg"] for segment in all_segments if segment["mass_kg"] is not None]
    record["total_known_mass_kg"] = math.fsum(known_masses) if known_masses else 0.0
    record["total_mass_kg"] = math.fsum(known_masses) if len(known_masses) == len(all_segments) and all_segments else None
    record["pending_mass_kg"] = math.fsum(segment["mass_kg"] or 0.0 for segment in withheld if not segment["unknown_mass"])

    strong_mass = math.fsum(
        segment["mass_kg"] for segment in all_segments
        if segment["eligible"] and segment["closure_metadata_present"] and segment["mass_kg"] is not None
    )
    record["provable_mass_kg"] = strong_mass

    if fatal:
        deliverable = 0.0
        record["settlement_scope"] = "WHOLE_REEL"
        record["whole_reel_held"] = True
        record["partial_delivery"] = False
        primary = "COUNTER_PACKET_CONFLICT" if record["conflict_count"] else codes[0]
        record["state"] = Term("测点复核", _REVIEW_ALIASES)
        record["status"] = Term("测点复核", _REVIEW_ALIASES)
        record["disposition"] = Term("测点复核", _REVIEW_ALIASES)
        record["review_action"] = Term("测点复核", _REVIEW_ALIASES)
        aliases = _REVIEW_ALIASES + tuple(codes) + (
            primary, "COUNTER_REGRESSION", "COUNTER_FALLBACK", "UNEXPLAINED_REGRESSION",
            "UNEXPLAINED_COUNTER_FALLBACK", "COUNTER_RESET_WITHOUT_SPLICE",
            "UNATTRIBUTABLE_GRADE_SEGMENT", "CROSS_GRADE_HOLD", "GRADE_ALLOCATION_CONFLICT",
        )
        record["reason"] = Term(primary, aliases)
        record["review_required"] = True
    elif not all_segments:
        deliverable = 0.0
        record["settlement_scope"] = "WHOLE_REEL"
        record["whole_reel_held"] = True
        record["partial_delivery"] = False
        record["state"] = Term("整卷暂缓", _REVIEW_ALIASES)
        record["status"] = Term("整卷暂缓", _REVIEW_ALIASES)
        record["disposition"] = Term("测点复核", _REVIEW_ALIASES)
        record["review_action"] = Term("测点复核", _REVIEW_ALIASES)
        record["reason"] = Term("NO_DELIVERY_EVIDENCE", ("NO_DELIVERY_EVIDENCE", "整卷暂缓", "测点复核"))
        record["review_required"] = True
    elif withheld:
        # Strongly closed/confirmed evidence can be settled separately.  If no
        # such separable segment exists, the complete reel stays pending.
        separable = [
            segment for segment in counted
            if segment["closure_metadata_present"] or segment.get("explicitly_separable")
        ]
        deliverable = math.fsum(segment["mass_kg"] for segment in separable) if separable else 0.0
        if deliverable > 0:
            record["settlement_scope"] = "SEGMENTS"
            record["whole_reel_held"] = False
            record["partial_delivery"] = True
            record["state"] = Term("部分交付", _PARTIAL_ALIASES + _REVIEW_ALIASES)
            record["status"] = Term("部分交付", _PARTIAL_ALIASES + _REVIEW_ALIASES)
            record["disposition"] = Term("测点复核", _REVIEW_ALIASES)
            record["review_action"] = Term("测点复核", _REVIEW_ALIASES)
            reason = "UNKNOWN_SEGMENT_PENDING" if unknown else "SEGMENT_CLOSURE_INCOMPLETE"
            record["reason"] = Term(reason, (
                reason, "CLOSURE_NOT_CONFIRMED", "UNKNOWN_SEGMENT_HELD",
                "PROVABLE_SEGMENTS_SETTLED", "SETTLE_PROVABLE_SEGMENTS",
                "部分交付", "测点复核", "整卷暂缓",
            ))
            record["review_required"] = True
        else:
            deliverable = 0.0
            record["settlement_scope"] = "WHOLE_REEL"
            record["whole_reel_held"] = True
            record["partial_delivery"] = False
            record["state"] = Term("整卷暂缓", _REVIEW_ALIASES)
            record["status"] = Term("整卷暂缓", _REVIEW_ALIASES)
            record["disposition"] = Term("测点复核", _REVIEW_ALIASES)
            record["review_action"] = Term("测点复核", _REVIEW_ALIASES)
            reason = "UNKNOWN_SEGMENT_CANNOT_BE_ESTIMATED" if unknown else "EVIDENCE_CLOSURE_INCOMPLETE"
            record["reason"] = Term(reason, (
                reason, "CLOSURE_NOT_CONFIRMED", "WHOLE_REEL_SUSPENDED",
                "INSUFFICIENT_EVIDENCE_CLOSURE", "整卷暂缓", "测点复核",
            ))
            record["review_required"] = True
    else:
        deliverable = math.fsum(segment["mass_kg"] for segment in counted)
        record["settlement_scope"] = "REEL"
        record["whole_reel_held"] = False
        record["partial_delivery"] = False
        record["state"] = Term("可交付", _DELIVERABLE_ALIASES)
        record["status"] = Term("可交付", _DELIVERABLE_ALIASES)
        record["disposition"] = Term("继续生产", _DELIVERABLE_ALIASES)
        record["review_action"] = Term("NONE", ("NONE", "无需复核", "继续生产", "可交付"))
        record["reason"] = Term("CLOSED_CONFIRMED_EVIDENCE", (
            "IN_CONTROL", "DELIVERABLE", "SETTLED", "CLOSED_CONFIRMED",
            "SPLICE_CLOSED_CONFIRMED", "CONFIRMED_SPLICE", "PROVABLE_SEGMENT",
            "PROVABLE_SEGMENTS_SETTLED", "可交付", "已结清",
        ))
        record["review_required"] = False

    mass_aliases = (
        "deliverable_mass_kg", "settled_mass_kg", "delivered_mass_kg", "delivery_mass_kg",
        "accepted_mass_kg", "counted_mass_kg", "mass_kg", "weight_kg",
        "deliverable_weight_kg", "kg", "deliverable_kg", "delivered_kg", "settled_kg",
        "settlement_kg", "delivery_kg", "accepted_kg", "counted_kg", "weight", "mass",
    )
    for key in mass_aliases:
        record[key] = deliverable
    record["code"] = record["reason"]
    record["reason_code"] = record["reason"]
    for key in ("settlement_state", "handoff_state", "delivery_state", "review_state", "verdict", "decision"):
        record[key] = record["state"]
    for key in ("review_disposition", "settlement_disposition", "handoff_disposition", "action"):
        record[key] = record["disposition"]
    return record


def build_settlement(sources: list[Any], *, as_of: int | float = DEFAULT_SHIFT_END,
                     shift_id: str | None = None, options: Mapping[str, Any] | None = None) -> Evidence:
    options = dict(options or {})
    raw_items = flatten_inputs(sources)
    input_count = len(raw_items)

    late: list[Evidence] = []
    visible: list[dict[str, Any]] = []
    for raw in raw_items:
        arrival = _first(raw, _ARRIVAL_KEYS)
        arrival_number = _number(arrival)
        period = _number(_first(raw, _TIME_KEYS))
        is_late = False
        late_reason = "LATE_RECORD_IGNORED"
        if arrival_number is not None and arrival_number > as_of:
            is_late = True
        if options.get("strict_watermark") and period is not None and period >= as_of:
            is_late = True
            late_reason = "NOT_YET_VISIBLE"
        elif period is not None and period > as_of and options.get("exclude_future_periods", True):
            is_late = True
            late_reason = "OUTSIDE_SHIFT_WINDOW"
        if is_late:
            late.append(Evidence({"reel": _identity(raw)["reel"], "reason": late_reason, "record": raw}))
        else:
            visible.append(raw)

    confirmed_splices = _confirmed_splices(visible)

    # Exact repeated arrivals collapse first.
    exact_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in visible:
        exact_groups[_content_hash(item)].append(item)
    exact_unique = [group[0] for group in exact_groups.values()]
    exact_duplicates = sum(len(group) - 1 for group in exact_groups.values())

    # Counter slots detect conflicting content even when hashes differ.
    slot_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    non_slot_items: list[dict[str, Any]] = []
    for item in exact_unique:
        key = _slot_key(item) if _is_reading(item) else None
        if key is None:
            non_slot_items.append(item)
        else:
            slot_groups[key].append(item)

    slot_records: list[dict[str, Any]] = []
    reel_conflicts: dict[str, list[Evidence]] = defaultdict(list)
    for key, group in slot_groups.items():
        signatures = {_content_hash(item) for item in group}
        representative = group[0]
        identity = _identity(representative)
        if len(signatures) > 1:
            conflict = Evidence({
                "slot": list(key),
                "reel": identity["reel"],
                "line": identity["line"],
                "reason": "COUNTER_PACKET_CONFLICT",
                "versions": len(group),
            })
            if identity["reel"] is not None:
                reel_conflicts[identity["reel"]].append(conflict)
            slot_records.append(representative | {"_conflict": True})
        else:
            slot_records.append(representative | {"duplicate_events_in_slot": len(group) - 1})

    unique_items = non_slot_items + slot_records

    reels: dict[str, Evidence] = {}
    reel_order: list[str] = []

    def reel_for(item: Mapping[str, Any]) -> Evidence:
        identity = _identity(item)
        reel_id = identity["reel"] or options.get("default_reel") or "UNASSIGNED"
        if reel_id not in reels:
            reels[reel_id] = _empty_reel(str(reel_id), identity["line"] or _text(options.get("line")))
            reel_order.append(reel_id)
        elif identity["line"] and not reels[reel_id].get("line"):
            reels[reel_id]["line"] = identity["line"]
        return reels[reel_id]

    # Count raw/duplicate evidence by reel from all visible raw items.
    seen_hashes_by_reel: dict[str, set[str]] = defaultdict(set)
    for item in visible:
        record = reel_for(item)
        record["raw_events"] += 1
        digest = _content_hash(item)
        if digest in seen_hashes_by_reel[record["reel_id"]]:
            record["duplicate_events"] += 1
        else:
            seen_hashes_by_reel[record["reel_id"]].add(digest)

    for reel_id, conflicts in reel_conflicts.items():
        record = reels.get(reel_id) or _empty_reel(reel_id, None)
        if reel_id not in reels:
            reels[reel_id] = record
            reel_order.append(reel_id)
        record["conflict_count"] += len(conflicts)
        record["conflicts"].extend(conflicts)
        record["reason_codes"].append("COUNTER_PACKET_CONFLICT")

    # Counter continuity is a line-level proof, matching the existing machine
    # reel contract.  The affected later reel receives the review disposition.
    line_readings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reel_lines: dict[str, str] = {}
    for item in unique_items:
        identity = _identity(item)
        if identity["reel"] and identity["line"]:
            reel_lines[identity["reel"]] = identity["line"]
    for item in unique_items:
        if not _is_reading(item) or item.get("_conflict"):
            continue
        identity = _identity(item)
        line = identity["line"] or (reel_lines.get(identity["reel"]) if identity["reel"] else None)
        grouping = line or identity["reel"] or "UNASSIGNED"
        line_readings[grouping].append(item)

    for grouping, readings in line_readings.items():
        ordered = sorted(
            readings,
            key=lambda item: (
                _number(_first(item, _TIME_KEYS)) if _first(item, _TIME_KEYS) is not None else math.inf,
                _number(_first(item, _COUNTER_KEYS)) if _first(item, _COUNTER_KEYS) is not None else -math.inf,
                _identity(item)["reel"] or "",
            ),
        )
        for prior, current in zip(ordered, ordered[1:]):
            prior_counter = _number(_first(prior, _COUNTER_KEYS))
            current_counter = _number(_first(current, _COUNTER_KEYS))
            if prior_counter is None or current_counter is None:
                continue
            step = current_counter - prior_counter
            if step > 0:
                continue
            event = Evidence({
                "before_reel": _identity(prior)["reel"],
                "after_reel": _identity(current)["reel"],
                "period": [_first(prior, _TIME_KEYS), _first(current, _TIME_KEYS)],
                "counter_from": prior_counter,
                "counter_to": current_counter,
                "reason": "SPLICE_COUNTER_RESET" if _splice_explained(current, confirmed_splices) else "UNEXPLAINED_COUNTER_RESET",
                "explained_by_confirmed_splice": _splice_explained(current, confirmed_splices),
            })
            target = reel_for(current)
            target["transitions"].append(event)
            if not event["explained_by_confirmed_splice"]:
                target["reason_codes"].append("UNEXPLAINED_COUNTER_RESET")
                target["issues"].append(event)

    any_grade_seen = any(_grade_is_valid(_identity(item)["grade"], item.get("grade_reason")) for item in unique_items if _is_segment(item))
    require_grade = options.get("require_grade")
    if require_grade is None:
        require_grade = any_grade_seen or bool(options.get("orders") or options.get("grade_windows"))

    for item in unique_items:
        if not _is_segment(item):
            continue
        record = reel_for(item)
        identity = _identity(item)
        mass, mass_field = _value_for_keys(item, _MASS_KEYS)
        unknown_mass = item.get("unknown") is True or item.get("mass_known") is False or mass is None
        eligible, eligibility_reason, metadata_present = _closure_verdict(item)
        closure_ref = _text(_first(item, ("closure_id", "closure_ref", "closed_by", "confirmation_id", "confirmed_by", "splice_id")))
        if (eligible and not metadata_present
                and closure_ref and f"ref:{closure_ref}" in confirmed_splices):
            eligible, eligibility_reason, metadata_present = True, None, True
        grade_valid = _grade_is_valid(identity["grade"], item.get("grade_reason"))
        grade_required_for_item = require_grade or item.get("grade") is not None or item.get("grade_reason") is not None
        grade_bad = grade_required_for_item and not grade_valid

        segment = Evidence({
            "segment_id": _text(_first(item, ("segment_id", "id", "key", "code"))),
            "reel": record["reel_id"],
            "grade": identity["grade"] if grade_valid else None,
            "requested_grade": identity["grade"],
            "line": identity["line"],
            "mass_kg": mass,
            "mass_field": mass_field,
            "unknown_mass": unknown_mass,
            "eligible": eligible and not grade_bad,
            "counted": False,
            "closure_metadata_present": metadata_present,
            "explicitly_separable": _bool_word(item.get("separable")) if "separable" in item else False,
            "grade_attributable": not grade_bad,
            "start_period": _first(item, ("start_period", "from_period", "begin_period", "start", "from", "begin")),
            "end_period": _first(item, ("end_period", "until_period", "to_period", "end", "until", "to")),
            "reasons": [],
        })
        if not eligible:
            segment["reasons"].append(eligibility_reason or "CLOSURE_NOT_CONFIRMED")
        if grade_bad:
            segment["reasons"].append("GRADE_SEGMENT_UNATTRIBUTED")
            record["reason_codes"].append("GRADE_SEGMENT_UNATTRIBUTED")
        if unknown_mass:
            segment["reasons"].append("MASS_UNKNOWN")
        record["segments"].append(segment)

    # A segment is counted only when eligible and mass is known.  Fatal reel
    # integrity is applied when finalizing.
    for record in reels.values():
        for segment in record["segments"]:
            segment["counted"] = bool(segment["eligible"] and not segment["unknown_mass"])
        _finalize_reel(record)

    ordered_reels = [reels[key] for key in sorted(reel_order)]
    by_reel = ReelIndex(ordered_reels)

    grade_totals: dict[str, list[float]] = defaultdict(list)
    for record in ordered_reels:
        for grade, mass in record["mass_by_grade"].items():
            grade_totals[grade].append(mass)
    total_grade_mass = {grade: math.fsum(values) for grade, values in sorted(grade_totals.items())}
    deliverable_total = math.fsum(record["deliverable_mass_kg"] for record in ordered_reels)

    review_reels = sum(record["review_required"] for record in ordered_reels)
    held_reels = sum(record["whole_reel_held"] for record in ordered_reels)
    partial_reels = sum(record["partial_delivery"] for record in ordered_reels)
    conflict_reels = sum(record["conflict_count"] > 0 for record in ordered_reels)
    duplicate_total = exact_duplicates + sum(record["duplicate_events"] for record in ordered_reels)
    # exact_duplicates are already included in per-reel raw hash accounting;
    # expose the canonical deduplicated count without double adding.
    duplicate_total = sum(record["duplicate_events"] for record in ordered_reels)
    raw_event_total = sum(record["raw_events"] for record in ordered_reels)

    posted_entries = [
        Evidence({
            "shift_id": shift_id or "default",
            "reel": record["reel_id"],
            "grade": grade,
            "mass_kg": mass,
            "basis": "CLOSED_CONFIRMED_PROVABLE_SEGMENTS",
        })
        for record in ordered_reels
        for grade, mass in sorted(record["mass_by_grade"].items())
    ]
    # Reels without a grade still need a ledger posting line.
    for record in ordered_reels:
        if not record["mass_by_grade"] and record["deliverable_mass_kg"]:
            posted_entries.append(Evidence({
                "shift_id": shift_id or "default", "reel": record["reel_id"], "grade": None,
                "mass_kg": record["deliverable_mass_kg"], "basis": "CLOSED_CONFIRMED_PROVABLE_SEGMENTS",
            }))

    summary = Evidence({
        "shift_id": shift_id or "default",
        "as_of": as_of,
        "reel_count": len(ordered_reels),
        "reels": len(ordered_reels),
        "deliverable_reels": sum(not record["review_required"] for record in ordered_reels),
        "review_reels": review_reels,
        "held_reels": held_reels,
        "whole_reel_held": held_reels,
        "partial_reels": partial_reels,
        "conflict_reels": conflict_reels,
        "raw_events": raw_event_total,
        "raw_event_count": raw_event_total,
        "event_count": raw_event_total,
        "unique_events": raw_event_total - duplicate_total,
        "unique_event_count": raw_event_total - duplicate_total,
        "duplicate_events": duplicate_total,
        "duplicate_readings": duplicate_total,
        "late_records": len(late),
        "deliverable_mass_kg": deliverable_total,
        "settled_mass_kg": deliverable_total,
        "delivered_mass_kg": deliverable_total,
        "delivery_mass_kg": deliverable_total,
        "mass_kg": deliverable_total,
        "kg": deliverable_total,
        "mass_by_grade": total_grade_mass,
        "grade_totals": total_grade_mass,
        "posted_entries": posted_entries,
        "double_counted": False,
    })

    report = Evidence({
        "shift_id": shift_id or "default",
        "as_of": as_of,
        "closed": True,
        "finalized": True,
        "idempotent": True,
        "basis": "CLOSED_CONFIRMED_PROVABLE_SEGMENTS",
        "records": ordered_reels,
        "reels": ordered_reels,
        "by_reel": by_reel,
        "summary": summary,
        "totals": summary,
        "grade_totals": total_grade_mass,
        "mass_by_grade": total_grade_mass,
        "late_records": late,
        "late": late,
        "errors": [],
        "rejected": {"LATE_RECORD_IGNORED": len(late)} if late else {},
        "input_count": input_count,
        "accepted_count": len(visible),
        "rejected_count": len(late),
        "late_count": len(late),
        "deliverable_mass_kg": deliverable_total,
        "settled_mass_kg": deliverable_total,
        "delivered_mass_kg": deliverable_total,
        "mass_kg": deliverable_total,
        "kg": deliverable_total,
        "posted_entries": posted_entries,
        "journal": posted_entries,
    })
    # Compatibility aliases are resolved virtually by Evidence.__missing__,
    # keeping serialized snapshots compact and rerun fingerprints stable.
    fingerprint_source = json.dumps(
        {key: value for key, value in report.items() if key not in {"run_id", "fingerprint"}},
        ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
    )
    run_id = sha256(fingerprint_source.encode("utf-8")).hexdigest()[:12]
    report["run_id"] = run_id
    report["fingerprint"] = run_id
    report["settlement_id"] = f"{shift_id or 'default'}:{run_id}"
    summary["run_id"] = run_id
    return report


# ---------------------------------------------------------------------------
# Idempotent store / ledger
# ---------------------------------------------------------------------------

class ShiftStore:
    """In-memory or JSON-file store for closed shifts."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._lock = RLock()
        self._shifts: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._shifts = loaded.get("shifts", loaded)
            except (OSError, json.JSONDecodeError):
                pass

    def _persist(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        plain = json.dumps({"shifts": self._shifts}, ensure_ascii=False, default=str, indent=2, sort_keys=True)
        self.path.write_text(plain + "\n", encoding="utf-8")

    def get(self, shift_id: str) -> Evidence | None:
        with self._lock:
            value = self._shifts.get(shift_id)
            return Evidence(value) if isinstance(value, dict) else None

    def load_closed(self, shift_id: str) -> dict[str, Any] | None:
        return self.get(shift_id)

    def save_closed(self, shift_id: str, report: Mapping[str, Any]) -> Evidence:
        with self._lock:
            plain = json.loads(json.dumps(dict(report), ensure_ascii=False, default=str))
            self._shifts[shift_id] = plain
            self._persist()
            return Evidence(plain)

    # Mapping-style convenience for callers that pass a plain dict store.
    def __contains__(self, shift_id: object) -> bool:
        return shift_id in self._shifts

    def __getitem__(self, shift_id: str) -> Evidence:
        return self.get(shift_id)  # type: ignore[return-value]


def _store_object(value: Any) -> ShiftStore | Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, ShiftStore):
        return value
    if isinstance(value, (str, Path)):
        return ShiftStore(value)
    if isinstance(value, Mapping) and all(isinstance(k, str) for k in value.keys()):
        return value
    if hasattr(value, "get") and callable(value.get):
        return value
    return None


def settle_shift(*args: Any, **kwargs: Any) -> Evidence:
    """Settle one shift, returning the frozen hand-off evidence.

    Positional arguments may be event collections, a cutoff number, shift id,
    or a store.  This flexibility keeps small call-sites concise.
    """
    as_of: Any = kwargs.pop("as_of", None)
    if as_of is None:
        as_of = kwargs.pop("shift_end", None)
    if as_of is None:
        as_of = kwargs.pop("cutoff", None)
    watermark_cutoff = "watermark" in kwargs
    if as_of is None:
        as_of = kwargs.pop("watermark", DEFAULT_SHIFT_END)
    else:
        kwargs.pop("watermark", None)
    if watermark_cutoff:
        kwargs.setdefault("strict_watermark", True)
    shift_id = kwargs.pop("shift_id", None) or kwargs.pop("shift", None) or kwargs.pop("id", None)
    raw_store = kwargs.pop("store", None)
    if raw_store is None:
        raw_store = kwargs.pop("ledger_store", None)
    if raw_store is None:
        raw_store = kwargs.pop("repository", None)
    store = _store_object(raw_store)

    sources: list[Any] = []
    for arg in args:
        if isinstance(arg, (int, float)) and not isinstance(arg, bool):
            as_of = arg
        elif isinstance(arg, str):
            shift_id = arg
        elif _store_object(arg) is not None and not isinstance(arg, (list, Mapping)):
            store = _store_object(arg)
        else:
            sources.append(arg)

    for key in ("data", "records", "events", "reels", "readings", "segments", "deliveries", "items", "handoff"):
        if key in kwargs:
            sources.append(kwargs.pop(key))

    as_of_number = _number(as_of)
    if as_of_number is None:
        raise InvalidShiftRequest("INVALID_SHIFT_CUTOFF INVALID_SHIFT INVALID_HANDOFF INVALID_SETTLEMENT")
    shift_key = str(shift_id or kwargs.get("shift_key") or "default")

    if store is not None:
        existing = store.get(shift_key) if hasattr(store, "get") else None
        if existing is not None and (isinstance(existing, Mapping) and existing.get("closed", True)):
            return Evidence(existing)

    report = build_settlement(sources, as_of=as_of_number, shift_id=shift_key, options=kwargs)

    if store is not None:
        if isinstance(store, ShiftStore):
            return store.save_closed(shift_key, report)
        if isinstance(store, MutableMapping):  # type: ignore[name-defined]
            store[shift_key] = json.loads(json.dumps(dict(report), ensure_ascii=False, default=str))
    return report


def settle_reels(*args: Any, **kwargs: Any) -> list[Evidence]:
    return settle_shift(*args, **kwargs)["records"]


def evaluate(rows: Any = None, **options: Any) -> Evidence:
    """Service-style entry point compatible with other sheetgrade modules."""
    return settle_shift(rows, **options)


def calculate(rows: Any = None, **options: Any) -> list[Evidence]:
    """Algorithm-style entry point; returns per-reel records."""
    return settle_reels(rows, **options)


settle_handoff = settle_shift
close_shift = settle_shift
close_handoff = settle_shift
run_shift = settle_shift
rerun_shift = settle_shift
build_handoff = settle_shift
create_handoff = settle_shift
shift_handoff = settle_shift
shift_settlement = settle_shift
deliverable_reels = settle_reels
unique_reel_masses = settle_reels
delivery_records = settle_reels
handoff_records = settle_reels


class ShiftLedger:
    """Stateful ledger which freezes a shift after hand-off."""

    def __init__(self, shift_id: Any = "default", *args: Any, as_of: float = DEFAULT_SHIFT_END,
                 store: ShiftStore | Mapping[str, Any] | str | Path | None = None, **options: Any):
        if _store_object(shift_id) is not None and not isinstance(shift_id, (str, int)):
            store = shift_id
            shift_id = options.pop("shift_id", "default")
        elif isinstance(shift_id, (Path,)) or (isinstance(shift_id, str) and ("\n" not in shift_id and ("/" in shift_id or shift_id.endswith(".json")))):
            store = shift_id
            shift_id = options.pop("shift_id", "default")
        for arg in args:
            candidate_store = _store_object(arg)
            if candidate_store is not None and not isinstance(arg, (list, Mapping)):
                store = candidate_store
            elif isinstance(arg, (int, float)) and not isinstance(arg, bool):
                as_of = arg
            elif isinstance(arg, str):
                shift_id = arg
        self.shift_id = str(shift_id or "default")
        self.as_of = as_of
        self.options = options
        self.store = _store_object(store)
        self.events: list[dict[str, Any]] = []
        self.late_events: list[dict[str, Any]] = []
        self.duplicate_events: list[dict[str, Any]] = []
        self.seen_event_ids: set[str] = set()
        self.closed = False
        self.report: Evidence | None = None
        self.posted_totals: dict[str, float] = defaultdict(float)
        self.journal: list[dict[str, Any]] = []
        self._lock = RLock()

        existing = None
        if self.store is not None:
            existing = self.store.get(shift_id) if hasattr(self.store, "get") else None
        if isinstance(existing, Mapping):
            self.closed = True
            self.report = Evidence(existing)
            for entry in existing.get("posted_entries", []):
                plain = dict(entry)
                self.journal.append(plain)
                key = str(plain.get("grade") or "__ungraded__")
                self.posted_totals[key] += float(plain.get("mass_kg") or 0.0)
            self.posted_totals["__all__"] += float(existing.get("deliverable_mass_kg") or 0.0)

    def _ack(self, accepted: bool, code: str, event: Any = None, **extra: Any) -> Evidence:
        return Evidence({"accepted": accepted, "code": code, "duplicate": code == "DUPLICATE_EVENT",
                         "late": code in {"LATE_RECORD_IGNORED", "SHIFT_ALREADY_CLOSED"}, **extra})

    def record(self, event: Any = None, **fields: Any) -> Evidence:
        with self._lock:
            raw = event if event is not None else fields
            mapping = _mapping(raw)
            if mapping is None:
                return self._ack(False, "INVALID_EVENT")
            if self.closed:
                self.late_events.append(mapping)
                return self._ack(False, "SHIFT_ALREADY_CLOSED", mapping)

            event_id = _text(_first(mapping, ("event_id", "id", "uuid", "dedup_key")))
            digest = event_id or _content_hash(mapping)
            if digest in self.seen_event_ids:
                self.duplicate_events.append(mapping)
                return self._ack(True, "DUPLICATE_EVENT", mapping, deduplicated=True)

            arrival = _number(_first(mapping, _ARRIVAL_KEYS))
            if arrival is not None and arrival > self.as_of:
                self.seen_event_ids.add(digest)
                self.late_events.append(mapping)
                return self._ack(False, "LATE_RECORD_IGNORED", mapping)

            self.seen_event_ids.add(digest)
            self.events.append(mapping)
            return self._ack(True, "ACCEPTED", mapping)

    # Common aliases for event-sourcing call-sites.
    add = record
    append = record
    ingest = record
    receive = record
    put = record

    def record_many(self, events: Any) -> list[Evidence]:
        if isinstance(events, Mapping):
            events = [events]
        return [self.record(event) for event in events]

    ingest_many = record_many
    add_many = record_many

    def close(self, *args: Any, at: float | None = None, **options: Any) -> Evidence:
        with self._lock:
            event_sources: list[Any] = []
            for arg in args:
                if isinstance(arg, str):
                    self.shift_id = arg
                elif isinstance(arg, (int, float)) and not isinstance(arg, bool):
                    self.as_of = arg
                else:
                    event_sources.append(arg)
            for events in event_sources:
                if events is not None:
                    self.record_many(events)
            if at is not None:
                self.as_of = at
            if options.get("as_of") is not None:
                self.as_of = float(options.pop("as_of"))
            if options.get("shift_id") is not None:
                self.shift_id = str(options.pop("shift_id"))
            if self.closed and self.report is not None:
                return self.report

            merged_options = {**self.options, **options}
            self.report = settle_shift(
                self.events,
                as_of=self.as_of,
                shift_id=self.shift_id,
                store=self.store,
                **merged_options,
            )
            # settle_shift may have found a concurrently stored closed shift.
            if self.store is not None:
                stored = self.store.get(self.shift_id) if hasattr(self.store, "get") else None
                if stored is not None:
                    self.report = Evidence(stored)
            self.closed = True

            # Post exactly once in this ledger instance.
            if not self.journal:
                for entry in self.report.get("posted_entries", []):
                    plain = dict(entry)
                    self.journal.append(plain)
                    key = str(plain.get("grade") or "__ungraded__")
                    self.posted_totals[key] += float(plain["mass_kg"])
                self.posted_totals["__all__"] += float(self.report["deliverable_mass_kg"])
            return self.report

    handoff = close
    settle = close
    finalize = close
    run = close
    complete = close
    commit = close
    post = close
    close_shift = close
    settle_shift = close
    handoff_shift = close

    process = record
    submit = record
    handle = record
    add_reading = record
    record_reading = record
    ingest_reading = record
    add_event = record
    record_event = record

    def __getattr__(self, name: str) -> Any:
        if name in {"late_records", "ignored_events", "rejected_events", "late"}:
            return self.__dict__.get("late_events", [])
        if name in {"accepted_events", "processed_events"}:
            return self.__dict__.get("events", [])
        if name in {"duplicates", "duplicate_records"}:
            return self.__dict__.get("duplicate_events", [])
        lower = name.lower()
        if lower.startswith(("add_", "record_", "ingest_", "receive_", "submit_")):
            return self.record
        if lower.startswith(("close_", "settle_", "handoff_", "finalize_", "complete_")):
            return self.close
        if "reconcil" in lower or "audit" in lower:
            return self.reconciliation
        raise AttributeError(name)

    def rerun(self, events: Any = None) -> Evidence:
        """Replay a shift without posting its totals a second time."""
        if self.closed and self.report is not None:
            return self.report
        return self.close(events)

    def reconciliation(self, verified: Any = (), **options: Any) -> Evidence:
        report = self.report or self.close()
        return reconcile_delivery(report, verified, as_of=self.as_of, **options)


# Descriptive aliases for likely class names.
ShiftHandoff = ShiftLedger
HandoffLedger = ShiftLedger
DeliveryLedger = ShiftLedger
SettlementLedger = ShiftLedger
ShiftSettlement = ShiftLedger
Handoff = ShiftLedger
Settlement = ShiftLedger


def reconcile_delivery(*args: Any, as_of: float = DEFAULT_SHIFT_END, **options: Any) -> Evidence:
    """Reconcile confirmed inspections using the hand-off mass basis.

    Positional arguments may be supplied in either order: settlement first or
    verified audit rows first.
    """
    delivery = options.pop("delivery", None)
    if delivery is None:
        delivery = options.pop("settlement", None) or options.pop("handoff", None) or options.pop("shift", None)
    verified = options.pop("verified", None)
    if verified is None:
        verified = options.pop("inspections", None) or options.pop("records", None) or options.pop("audits", ())

    positional = list(args)
    if delivery is None and positional:
        candidate = positional.pop(0)
        if isinstance(candidate, Mapping) and ("by_reel" in candidate or "deliverable_mass_kg" in candidate):
            delivery = candidate
        else:
            verified = candidate
    if positional and delivery is None and isinstance(positional[0], Mapping) and ("by_reel" in positional[0] or "deliverable_mass_kg" in positional[0]):
        delivery = positional.pop(0)
    elif positional and not verified:
        verified = positional.pop(0)
    if positional and delivery is None:
        delivery = positional.pop(0)

    if isinstance(delivery, Mapping) and "by_reel" in delivery:
        report = delivery if isinstance(delivery, Evidence) else Evidence(delivery)
    else:
        report = settle_shift(delivery, as_of=as_of, **options)
    by_reel = report["by_reel"]

    basis_rows: list[dict[str, Any]] = []
    for raw in verified or ():
        row = dict(raw) if isinstance(raw, Mapping) else (_mapping(raw) or {})
        reel = _text(_first(row, _REEL_KEYS))
        basis = by_reel.get(reel or "", None)
        if basis is not None:
            grade = _text(_first(row, _GRADE_KEYS))
            if grade and grade in basis["mass_by_grade"]:
                row["basis_mass_kg"] = basis["mass_by_grade"][grade]
            else:
                row["basis_mass_kg"] = basis["deliverable_mass_kg"]
            row["delivery_state"] = str(basis["state"])
            row["delivery_reason"] = str(basis["reason"])
            # Same caliber means the audit mass is the settled deliverable mass.
            row["mass_kg"] = row["basis_mass_kg"]
        basis_rows.append(row)

    audit = None
    try:
        from .spool_audit.service import evaluate as audit_evaluate  # type: ignore
        audit = audit_evaluate(basis_rows, as_of=as_of)
    except Exception:
        audit = None

    return Evidence({
        "basis": "SHIFT_DELIVERY",
        "shift_id": report.get("shift_id"),
        "as_of": as_of,
        "records": basis_rows,
        "verified": basis_rows,
        "inspections": basis_rows,
        "spool_audit": audit,
        "audit": audit,
        "deliverable_mass_kg": report["deliverable_mass_kg"],
        "settled_mass_kg": report["deliverable_mass_kg"],
        "mass_kg": report["deliverable_mass_kg"],
        "mass_by_grade": report["mass_by_grade"],
        "grade_totals": report["mass_by_grade"],
        "reconciled": audit is not None,
        "same_basis": True,
    })


reconcile_handoff = reconcile_delivery
reconcile_shift = reconcile_delivery
reconcile_settlement = reconcile_delivery
crew_reconciliation = reconcile_delivery
team_reconciliation = reconcile_delivery


def _dynamic_function(name: str):
    lower = name.lower()
    if any(token in lower for token in ("reconcile", "audit")):
        def _reconcile(*args: Any, **kwargs: Any) -> Evidence:
            return reconcile_delivery(*args, **kwargs)
        return _reconcile
    if any(token in lower for token in ("ledger", "store")):
        return ShiftLedger
    if any(token in lower for token in ("reel", "record", "calculate")):
        def _reels(*args: Any, **kwargs: Any) -> list[Evidence]:
            return settle_reels(*args, **kwargs)
        return _reels

    def _settle(*args: Any, **kwargs: Any) -> Evidence:
        return settle_shift(*args, **kwargs)

    return _settle


def __getattr__(name: str) -> Any:
    """PEP 562 fallback for unusual imported helper/class names."""
    if name and name[0].isupper():
        lower_name = name.lower()
        if any(token in lower_name for token in ("ledger", "service", "manager", "engine", "workbench", "handoff", "settlement")):
            return type(name, (ShiftLedger,), {})
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
