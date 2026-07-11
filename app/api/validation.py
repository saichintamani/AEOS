"""
Phase 9B.6 Priority 1 — Validation API Routes

Exposes the InvariantEngine, ProtocolValidator, and StateMachineValidator
through REST endpoints so operators can query live correctness status.

Routes:
  GET  /api/v1/validation/status         — overall health of invariant monitor
  GET  /api/v1/validation/invariants     — list all known invariants + catalog
  POST /api/v1/validation/evaluate       — trigger an on-demand evaluation
  GET  /api/v1/validation/violations     — recent violation history
  GET  /api/v1/validation/protocols      — protocol definitions
  POST /api/v1/validation/state-machine  — validate a state transition
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/validation", tags=["Validation"])


# ── Request / response models ─────────────────────────────────────────────────

class TransitionRequest(BaseModel):
    machine: str          # e.g. "SM-TASK"
    from_state: str       # e.g. "PENDING"
    to_state: str         # e.g. "RUNNING"
    event: str = ""
    context: dict = {}


class EvaluateRequest(BaseModel):
    invariant_ids: list[str] | None = None   # None = all registered
    raise_on_critical: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def validation_status(request: Request) -> JSONResponse:
    """Overall invariant engine health and statistics."""
    engine = getattr(request.app.state, "invariant_engine", None)
    if engine is None:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "reason": "InvariantEngine not initialised"},
        )
    stats = engine.stats
    recent_violations = engine.violation_history[-10:]  # last 10
    return JSONResponse(content={
        "status": "running" if engine._running else "stopped",
        "stats": stats,
        "recent_violations": [
            {
                "invariant_id": v.invariant_id,
                "severity": v.severity,
                "message": v.message,
                "context": v.context,
                "detected_at": v.detected_at,
            }
            for v in recent_violations
        ],
    })


@router.get("/invariants")
async def list_invariants() -> JSONResponse:
    """List all known invariants from the catalog."""
    from app.distributed.validation.invariants import InvariantCatalog
    catalog = InvariantCatalog.all()
    return JSONResponse(content={
        "total": len(catalog),
        "invariants": [
            {
                "id": m.invariant_id,
                "title": m.title,
                "severity": m.severity,
                "category": m.category,
                "doc_ref": m.doc_ref,
            }
            for m in catalog
        ],
    })


@router.post("/evaluate")
async def evaluate_invariants(request: Request, body: EvaluateRequest) -> JSONResponse:
    """Trigger an on-demand invariant evaluation."""
    engine = getattr(request.app.state, "invariant_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="InvariantEngine not initialised")
    result = await engine.evaluate(
        invariant_ids=body.invariant_ids,
        raise_on_critical=False,  # always safe from API
    )
    return JSONResponse(content={
        "ok": result.ok,
        "passed": result.passed,
        "violations": [
            {
                "invariant_id": v.invariant_id,
                "severity": v.severity,
                "message": v.message,
                "context": v.context,
            }
            for v in result.violations
        ],
        "evaluated_at": result.evaluated_at,
    })


@router.get("/violations")
async def violation_history(request: Request, limit: int = 50) -> JSONResponse:
    """Return recent invariant violation history."""
    engine = getattr(request.app.state, "invariant_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="InvariantEngine not initialised")
    history = engine.violation_history[-limit:]
    return JSONResponse(content={
        "count": len(history),
        "violations": [
            {
                "invariant_id": v.invariant_id,
                "severity": v.severity,
                "message": v.message,
                "context": v.context,
                "detected_at": v.detected_at,
            }
            for v in history
        ],
    })


@router.get("/protocols")
async def list_protocols() -> JSONResponse:
    """List all protocol definitions registered in the ProtocolValidator."""
    from app.distributed.validation.protocol import ProtocolValidator
    validator = ProtocolValidator()
    return JSONResponse(content={
        "protocols": list(validator._rules.keys()),
    })


@router.post("/state-machine")
async def validate_transition(body: TransitionRequest) -> JSONResponse:
    """
    Validate a single state machine transition.

    Returns {"valid": true} or {"valid": false, "reason": "..."}.
    """
    from app.distributed.validation.state_machine import StateMachineValidator, StateMachineViolation
    validator = StateMachineValidator()
    try:
        validator.transition(
            body.machine,
            body.from_state,
            body.to_state,
            event=body.event,
            context=body.context,
        )
        return JSONResponse(content={"valid": True, "machine": body.machine})
    except StateMachineViolation as exc:
        return JSONResponse(content={"valid": False, "reason": str(exc)})
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"State machine '{body.machine}' is not registered",
        )
