"""
WhatsApp Negotiation API Router
"""
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/negotiate", tags=["whatsapp-negotiation"])


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _haversine(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _score(problem_type, clat, clng, tech) -> float:
    skill = 1.0 if any(
        str(s).strip().lower() == problem_type.strip().lower()
        for s in (tech.get("skills") or [])
    ) else 0.0
    dist = _haversine(clat, clng, float(tech["lat"]), float(tech["lng"]))
    prox = max(0.0, 1.0 - dist / 50.0)
    rating = float(tech.get("rating") or 0.0)
    avail = 1.0 if tech.get("available") else 0.0
    return 0.4 * skill + 0.3 * prox + 0.2 * (rating / 5.0) + 0.1 * avail


async def _pick_best_technician(db, job: dict) -> Optional[dict]:
    """Select the highest-scoring available technician with a phone number."""
    result = await db.table("technicians").select("*").eq("available", True).execute()
    techs = [t for t in (result.data or []) if t.get("phone_number")]
    if not techs:
        return None
    scored = sorted(
        techs,
        key=lambda t: _score(
            job["problem_type"],
            float(job["customer_lat"]),
            float(job["customer_lng"]),
            t,
        ),
        reverse=True,
    )
    return scored[0]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{job_id}")
async def negotiate_via_whatsapp(
    job_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    tech_id: Optional[str] = None,
):
    import os
    if os.getenv("ENABLE_WHATSAPP", "false").lower() != "true":
        raise HTTPException(
            status_code=503,
            detail="WhatsApp live automation available in local demo mode."
        )

    db = request.app.state.supabase

    job_res = await db.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    if not job_res.data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_res.data[0]

    if job["status"] in ("negotiating", "assigned"):
        raise HTTPException(status_code=400, detail=f"Job is already in status '{job['status']}'")

    if tech_id:
        tech_res = await db.table("technicians").select("*").eq("id", tech_id).limit(1).execute()
        if not tech_res.data:
            raise HTTPException(status_code=404, detail="Technician not found")
        technician = tech_res.data[0]
        if not technician.get("phone_number"):
            raise HTTPException(status_code=400, detail="Technician has no phone_number")
    else:
        technician = await _pick_best_technician(db, job)
        if not technician:
            raise HTTPException(status_code=400, detail="No available technicians with phone numbers found")

    from services.whatsapp_negotiator import get_whatsapp
    wa = get_whatsapp()
    if not wa._ready:
        raise HTTPException(status_code=503, detail="WhatsApp is not ready. Check /api/negotiate/whatsapp-status")

    from services.negotiation_manager import NegotiationManager

    async def _run():
        try:
            result = await NegotiationManager(db).run(job, technician)
            logger.info("Negotiation complete: %s", result)
        except Exception as exc:
            logger.error("Background negotiation failed: %s", exc)

    background_tasks.add_task(_run)

    return {
        "message": "WhatsApp negotiation started",
        "job_id": job_id,
        "technician_id": technician["id"],
        "technician_name": technician["name"],
        "phone_number": technician["phone_number"],
        "status": "initiated",
    }


@router.post("/{job_id}/sync")
async def negotiate_via_whatsapp_sync(
    job_id: str,
    request: Request,
    tech_id: Optional[str] = None,
):
    db = request.app.state.supabase

    job_res = await db.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    if not job_res.data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_res.data[0]

    if tech_id:
        tech_res = await db.table("technicians").select("*").eq("id", tech_id).limit(1).execute()
        if not tech_res.data:
            raise HTTPException(status_code=404, detail="Technician not found")
        technician = tech_res.data[0]
    else:
        technician = await _pick_best_technician(db, job)
        if not technician:
            raise HTTPException(status_code=400, detail="No available technicians with phone numbers")

    from services.whatsapp_negotiator import get_whatsapp
    wa = get_whatsapp()
    if not wa._ready:
        raise HTTPException(status_code=503, detail="WhatsApp not ready — scan QR first")

    from services.negotiation_manager import NegotiationManager
    return await NegotiationManager(db).run(job, technician)


@router.get("/whatsapp-status")
async def whatsapp_status(request: Request):
    from services.whatsapp_negotiator import get_whatsapp
    wa = get_whatsapp()
    status = await wa.get_session_status()
    return {
        **status,
        "message": "WhatsApp Web is ready" if status["ready"] else "Not ready — scan the QR code",
    }


@router.get("/sessions")
async def list_sessions(request: Request, job_id: Optional[str] = None):
    """List all WhatsApp negotiation sessions."""
    db = request.app.state.supabase
    try:
        query = db.table("whatsapp_negotiations").select("*").order("created_at", desc=True)
        if job_id:
            query = query.eq("job_id", job_id)
        result = await query.execute()
        return {"sessions": result.data or [], "count": len(result.data or [])}
    except Exception:
        return {"sessions": [], "count": 0}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, request: Request):
    """Get all messages for a negotiation session."""
    db = request.app.state.supabase
    try:
        result = await db.table("whatsapp_messages").select("*").eq(
            "negotiation_id", session_id
        ).order("sent_at").execute()
        return {"messages": result.data or [], "count": len(result.data or [])}
    except Exception:
        return {"messages": [], "count": 0}
