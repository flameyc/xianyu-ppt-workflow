"""网页工程草案经本地审查集成；只给制作建议，不改报价或收款。"""
from decimal import Decimal, InvalidOperation, localcontext

ROUTES = {"auto", "economy", "quality"}
COMPLEXITIES = {"standard", "complex"}
CHECKS = [
    "content_coverage",
    "native_text_editable",
    "final_full_page_visual_check",
    "revision_source_binding",
]


def _decimal(value, name):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be bool")
    if not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{name} must be numeric or decimal string")
    try:
        value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} is invalid") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    if len(value.as_tuple().digits) > 18 or abs(value.as_tuple().exponent) > 12:
        raise ValueError('价格或页数超出支持的精度范围')
    return value


def _pages(value):
    if value is None:
        return None
    value = _decimal(value, "billable_pages")
    if value <= 0 or value != value.to_integral_value():
        raise ValueError("billable_pages must be a positive integer")
    return int(value)


def _fmt(value):
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


def choose_route(payload, policy=None):
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    policy = {} if policy is None else policy
    if not isinstance(policy, dict):
        raise ValueError("policy must be a dict")
    unknown_policy = set(policy) - {"quality_threshold"}
    if unknown_policy:
        raise ValueError(f"unknown policy keys: {sorted(unknown_policy)}")

    threshold = _decimal(policy.get("quality_threshold", "10"),
                         "quality_threshold")
    if threshold is None or threshold <= 0:
        raise ValueError('质量路线阈值必须大于零')

    complexity = payload.get("complexity", "standard")
    requested = payload.get("requested_route", "auto")
    reason = payload.get("override_reason")

    if not isinstance(complexity, str) or complexity not in COMPLEXITIES:
        raise ValueError("complexity must be standard or complex")
    if not isinstance(requested, str) or requested not in ROUTES:
        raise ValueError("requested_route must be auto/economy/quality")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("override_reason must be a string or null")
    reason = reason.strip() if isinstance(reason, str) else None
    if requested != "auto" and not reason:
        raise ValueError("override_reason is required for manual route")

    unit = _decimal(payload.get("unit_price"), "unit_price")
    total = _decimal(payload.get("agreed_total"), "agreed_total")
    pages = _pages(payload.get("billable_pages"))

    with localcontext() as ctx:
        ctx.prec = 50
        derived = total / Decimal(pages) if total is not None and pages else None
    warnings = []

    if unit is not None:
        effective = unit
        source = "unit_price"
        if derived is not None and unit != derived:
            warnings.append("price_mismatch")
    elif derived is not None:
        effective = derived
        source = "agreed_total/billable_pages"
    else:
        effective = None
        source = "unknown"
        warnings.append("price_confirmation_required")

    evidence = payload.get('price_evidence')
    if evidence is not None and not isinstance(evidence, str):
        raise ValueError('价格依据必须为文字或空值')
    provisional = effective is None or not (evidence and evidence.strip()) or 'price_mismatch' in warnings
    if effective is not None and not (evidence and evidence.strip()):
        warnings.append('price_evidence_missing')
    recommended = (
        "economy"
        if effective is None or effective < threshold
        else "quality"
    )
    route = recommended if requested == "auto" else requested

    if requested != "auto" and route != recommended:
        warnings.append("manual_route_differs_from_price_rule")

    requires_scope_decision = False
    if effective is None and (complexity == 'complex' or route == 'quality'):
        warnings.append('scope_budget_unknown')
        requires_scope_decision = True
    if effective is not None and effective < threshold:
        if complexity == "complex":
            warnings.append("scope_conflict")
            requires_scope_decision = True
        if route == "quality":
            warnings.append("high_cost_low_price_conflict")
            requires_scope_decision = True

    return {
        "route": route,
        "basis": {
            "price_source": source,
            "quality_threshold": _fmt(threshold),
            "recommended_route": recommended,
            "price_evidence": payload.get("price_evidence"),
            "override_reason": reason if requested != "auto" else None,
        },
        "effective_unit_price": _fmt(effective),
        "provisional": provisional,
        "warnings": warnings,
        "requires_scope_decision": requires_scope_decision,
        "checks": {
            "economy": list(CHECKS),
            "quality": list(CHECKS),
        },
    }
