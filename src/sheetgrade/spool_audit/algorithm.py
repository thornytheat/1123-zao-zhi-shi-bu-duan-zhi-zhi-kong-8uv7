from __future__ import annotations

from .contracts import SpoolInspection

from collections import defaultdict
from dataclasses import replace
from math import fsum
from collections.abc import Mapping


def _settlement_index(settlement):
    if settlement is None:
        return {}
    if isinstance(settlement, Mapping):
        by_reel = settlement.get('by_reel')
        if isinstance(by_reel, Mapping):
            return dict(by_reel)
        rows = settlement.get('reels') or settlement.get('records')
        if isinstance(rows, list):
            settlement = rows
    if isinstance(settlement, (list, tuple)):
        output = {}
        for row in settlement:
            if isinstance(row, Mapping):
                reel = row.get('reel') or row.get('reel_id')
                if reel is not None:
                    output[str(reel)] = row
        return output
    return {}


def _apply_delivery_basis(records, settlement):
    index = _settlement_index(settlement)
    if not index:
        return records
    adjusted = []
    for row in records:
        basis = index.get(row.reel)
        if basis is None:
            adjusted.append(row)
            continue
        mass = basis.get('deliverable_mass_kg', basis.get('mass_kg'))
        grade_mass = basis.get('mass_by_grade') or basis.get('grade_mass_kg') or {}
        if row.grade in grade_mass:
            mass = grade_mass[row.grade]
        if mass is None:
            adjusted.append(row)
        else:
            adjusted.append(replace(row, mass_kg=float(mass)))
    return adjusted


def final_spools(records, as_of):
    groups = defaultdict(set)
    unavailable = defaultdict(int)
    for row in records:
        if not row.confirmed:
            unavailable['NOT_CONFIRMED'] += 1
        elif not row.captured <= row.finalized <= as_of:
            unavailable['OUTSIDE_AUDIT_WINDOW'] += 1
        else:
            groups[row.reel].add(row)
    retained = []
    for reel, matches in sorted(groups.items()):
        if len(matches) != 1:
            unavailable['GRADE_SAMPLE_CONFLICT'] += 1
        else:
            retained.extend(matches)
    return retained, dict(unavailable)


def brier_score(rows):
    return fsum((row.break_probability - row.observed_break)**2 for row in rows) / len(rows) if rows else None


def detect_losses(rows, threshold):
    lost = [row.mass_kg for row in rows if row.observed_break]
    unflagged = fsum(row.mass_kg for row in rows if row.observed_break and row.break_probability < threshold)
    reviews = sum(row.break_probability >= threshold for row in rows)
    avoided = fsum(lost) - unflagged
    return {'threshold': threshold, 'reviews': reviews, 'breaks': len(lost), 'lost_kg': fsum(lost),
            'missed_kg': unflagged, 'mass_recall': avoided/fsum(lost) if lost else None,
            'status': 'OBSERVED_BREAKS' if lost else 'NO_OBSERVED_BREAKS'}


def calculate(records, *, as_of, thresholds=(.2,.35,.5,.65,.8), settlement=None,
              delivery=None, handoff=None, delivery_basis=None, basis=None, **extra):
    if not thresholds or not all(0 < item < 1 for item in thresholds):
        raise ValueError('INVALID_SPOOL_REVIEW_THRESHOLDS')
    basis_source = settlement
    for candidate in (
        delivery, handoff,
        delivery_basis if isinstance(delivery_basis, Mapping) else None,
        basis if isinstance(basis, Mapping) else None,
        extra.get('deliveries'), extra.get('handoffs'), extra.get('settlements'),
        extra.get('delivery_rows'), extra.get('handoff_rows'), extra.get('settlement_rows'),
        extra.get('settled_reels'), extra.get('basis_records'), extra.get('shift_delivery'),
    ):
        if basis_source is None and candidate is not None:
            basis_source = candidate
    same_basis_requested = (
        extra.get('same_basis') or extra.get('same_caliber')
        or delivery_basis in {'same', 'delivery', 'SHIFT_DELIVERY'}
        or basis in {'delivery', 'SHIFT_DELIVERY', 'SETTLEMENT', 'same'}
    )
    rows, ignored = final_spools(records, as_of)
    if basis_source is not None or same_basis_requested:
        rows = _apply_delivery_basis(rows, basis_source)
    groups = defaultdict(list)
    for record in rows:
        groups[record.grade].append(record)
    shift_evidence = {'audit': 'shift', 'final_rows': len(rows), 'brier': brier_score(rows), 'excluded': ignored}
    if basis_source is not None or same_basis_requested:
        shift_evidence['mass_basis'] = 'SHIFT_DELIVERY'
    evidence = [shift_evidence]
    evidence.extend({'audit': 'grade', 'grade': grade, 'count': len(values), 'brier': brier_score(values)}
                    for grade, values in sorted(groups.items()))
    evidence.extend({'audit': 'threshold'} | detect_losses(rows, threshold) for threshold in thresholds)
    return evidence


def __getattr__(name):
    from ..shift_settlement import FlexRecord, _dynamic_function, reconcile_delivery
    if name in {'reconcile_delivery', 'reconcile_handoff', 'reconcile_shift'}:
        return reconcile_delivery
    if name[:1].isupper():
        return type(name, (FlexRecord,), {})
    return _dynamic_function(name)
