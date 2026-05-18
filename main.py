import asyncio
import json
import logging
import math
import os
import random
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase._async.client import AsyncClient, create_client as async_create_client

from routers.whatsapp_router import router as whatsapp_router

logger = logging.getLogger(__name__)

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

app = FastAPI(
    title="AI Technician Routing & Negotiation API",
    description="Match customers to technicians and negotiate pricing autonomously.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(whatsapp_router)

_supabase: Optional[AsyncClient] = None
_gemini_model: Optional[Any] = None

# WhatsApp automation is only available when ENABLE_WHATSAPP=true (local/VPS only)
ENABLE_WHATSAPP = os.getenv("ENABLE_WHATSAPP", "false").lower() == "true"


@app.on_event("startup")
async def startup():
    global _supabase
    if is_configured(SUPABASE_URL) and is_configured(SUPABASE_KEY):
        _supabase = await async_create_client(SUPABASE_URL, SUPABASE_KEY)
        app.state.supabase = _supabase
    if ENABLE_WHATSAPP:
        try:
            from services.whatsapp_negotiator import get_whatsapp
            asyncio.create_task(get_whatsapp().start())
        except ImportError:
            logger.warning("Playwright not installed — WhatsApp automation disabled")


@app.on_event("shutdown")
async def shutdown():
    if ENABLE_WHATSAPP:
        try:
            from services.whatsapp_negotiator import get_whatsapp
            await get_whatsapp().stop()
        except Exception:
            pass


NEGOTIATION_ROUNDS = 4
CONVERGENCE_THRESHOLD = 0.15
PLACEHOLDER_MARKERS = ("your-", "example", "changeme", "replace")


def is_configured(value: str) -> bool:
    if not value:
        return False
    return not any(m in value.lower() for m in PLACEHOLDER_MARKERS)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_supabase() -> AsyncClient:
    global _supabase
    if not is_configured(SUPABASE_URL) or not is_configured(SUPABASE_KEY):
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_URL and SUPABASE_KEY must be set in .env with real values.",
        )
    if _supabase is None:
        _supabase = await async_create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def get_gemini_model():
    global _gemini_model
    if _gemini_model is None and is_configured(GEMINI_API_KEY):
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    return _gemini_model


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def skill_matches(problem_type: str, skills: list) -> bool:
    target = problem_type.strip().lower()
    return any(str(s).strip().lower() == target for s in (skills or []))


def compute_technician_score(problem_type, customer_lat, customer_lng, tech):
    skill_score = 1.0 if skill_matches(problem_type, tech.get("skills") or []) else 0.0
    distance_km = haversine_km(customer_lat, customer_lng, float(tech["lat"]), float(tech["lng"]))
    proximity_score = max(0.0, 1.0 - distance_km / 50.0)
    rating = float(tech.get("rating") or 0.0)
    available_flag = 1.0 if tech.get("available") else 0.0
    score = (0.4 * skill_score) + (0.3 * proximity_score) + (0.2 * (rating / 5.0)) + (0.1 * available_flag)
    return round(score, 4), round(distance_km, 2)


def urgency_multiplier(urgency: int) -> float:
    return 1.0 + (urgency * 0.05)


def compute_negotiation_bounds(customer_budget: int, urgency: int, rate_min: int):
    mult = urgency_multiplier(urgency)
    tech_floor = int(round(rate_min * mult))
    customer_ceiling = min(int(round(customer_budget * mult)), customer_budget * 2)
    return tech_floor, customer_ceiling


def offers_within_threshold(a: int, b: int) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= CONVERGENCE_THRESHOLD * max(a, b)


def midpoint_price(a: int, b: int) -> int:
    return int(round((a + b) / 2))


def extract_json_from_text(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        cleaned = cleaned[start:end + 1]
    data = json.loads(cleaned)
    return {"offer": int(data["offer"]), "message": str(data.get("message", "")).strip() or "No message."}


def call_gemini_agent(prompt: str) -> dict:
    model = get_gemini_model()
    if model is None:
        raise RuntimeError("Gemini not configured")
    response = model.generate_content(prompt, generation_config={"temperature": 0.7, "max_output_tokens": 300})
    text = (response.text or "").strip()
    if not text:
        raise ValueError("Empty response from Gemini")
    return extract_json_from_text(text)


def fallback_customer_offer(round_num, ceiling, min_offer, tech_last_offer):
    anchor = tech_last_offer if tech_last_offer is not None else ceiling
    gap = max(anchor - min_offer, 1)
    offer = max(min_offer, min(int(round(ceiling - gap * 0.55 * round_num / NEGOTIATION_ROUNDS)), ceiling))
    return {"offer": offer, "message": f"Round {round_num}: Our offer is INR {offer}."}


def fallback_tech_offer(round_num, floor, ceiling, customer_last_offer):
    anchor = customer_last_offer if customer_last_offer is not None else floor
    gap = max(ceiling - anchor, 1)
    offer = max(floor, min(int(round(floor + gap * 0.45 * round_num / NEGOTIATION_ROUNDS)), ceiling))
    return {"offer": offer, "message": f"Round {round_num}: I can do INR {offer}."}


def run_customer_agent(problem_type, ceiling, round_num, tech_last_offer):
    prompt = (
        f"You are negotiating for a customer for a {problem_type} job. "
        f"Max budget INR {ceiling}. Round {round_num}. Tech last offer: INR {tech_last_offer or 'none'}. "
        f'Respond JSON: {{"offer": integer, "message": string}}'
    )
    try:
        return call_gemini_agent(prompt)
    except Exception:
        return fallback_customer_offer(round_num, ceiling, max(1, int(ceiling * 0.4)), tech_last_offer)


def run_tech_agent(problem_type, floor, ceiling, round_num, customer_last_offer):
    prompt = (
        f"You are a technician negotiating for a {problem_type} job. "
        f"Min rate INR {floor}. Round {round_num}. Customer last offer: INR {customer_last_offer or 'none'}. "
        f'Respond JSON: {{"offer": integer, "message": string}}'
    )
    try:
        return call_gemini_agent(prompt)
    except Exception:
        return fallback_tech_offer(round_num, floor, ceiling, customer_last_offer)


class JobCreate(BaseModel):
    problem_type: str
    urgency: int = Field(ge=1, le=5)
    customer_budget: int = Field(gt=0)
    description: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    visit_date: Optional[str] = None   # ISO date string "YYYY-MM-DD"
    visit_time: Optional[str] = None   # "HH:MM"
    # lat/lng kept for future geocoding; default to Hyderabad city centre
    customer_lat: float = 17.385
    customer_lng: float = 78.4867


class TechnicianCreate(BaseModel):
    name: str
    skills: list[str]
    lat: float
    lng: float
    rate_min: int = Field(gt=0)
    rating: float = Field(ge=1.0, le=5.0)
    available: bool = True


@app.get("/health")
async def health():
    tables_ok = False
    if is_configured(SUPABASE_URL) and is_configured(SUPABASE_KEY):
        try:
            client = await get_supabase()
            await client.table("jobs").select("id").limit(1).execute()
            tables_ok = True
        except Exception:
            tables_ok = False
    return {
        "status": "ok",
        "timestamp": utc_now_iso(),
        "supabase_configured": is_configured(SUPABASE_URL) and is_configured(SUPABASE_KEY),
        "gemini_configured": is_configured(GEMINI_API_KEY),
        "database_tables_ready": tables_ok,
    }


@app.post("/init-db")
async def init_database():
    if not is_configured(DATABASE_URL):
        raise HTTPException(status_code=400, detail="DATABASE_URL is not set in .env.")
    try:
        import psycopg2
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Run: pip install psycopg2-binary") from exc
    try:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            sql = f.read()
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
        conn.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Schema init failed: {exc}") from exc
    return {"message": "Database tables created successfully", "tables": ["technicians", "jobs", "negotiation_logs"]}


@app.post("/jobs", status_code=201)
async def create_job(body: JobCreate):
    client = await get_supabase()
    record = {
        "id": str(uuid.uuid4()),
        "problem_type": body.problem_type.strip(),
        "customer_lat": body.customer_lat,
        "customer_lng": body.customer_lng,
        "urgency": body.urgency,
        "customer_budget": body.customer_budget,
        "description": (body.description or "").strip() or None,
        "customer_name": (body.customer_name or "").strip() or None,
        "customer_phone": (body.customer_phone or "").strip() or None,
        "customer_address": (body.customer_address or "").strip() or None,
        "visit_date": body.visit_date or None,
        "visit_time": body.visit_time or None,
        "status": "pending",
        "assigned_tech_id": None,
        "agreed_price": None,
        "created_at": utc_now_iso(),
    }
    result = await client.table("jobs").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create job")
    return result.data[0]


@app.post("/jobs/{job_id}/match")
async def match_technicians(job_id: str):
    client = await get_supabase()
    job_result = await client.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_result.data[0]

    tech_result = await client.table("technicians").select("*").execute()
    ranked = []
    for tech in tech_result.data or []:
        score, distance_km = compute_technician_score(
            job["problem_type"], float(job["customer_lat"]), float(job["customer_lng"]), tech
        )
        ranked.append({
            "id": tech["id"], "name": tech["name"], "skills": tech.get("skills") or [],
            "lat": tech["lat"], "lng": tech["lng"], "rate_min": tech["rate_min"],
            "rating": tech["rating"], "available": tech["available"],
            "score": score, "distance_km": distance_km,
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)
    await client.table("jobs").update({"status": "matched"}).eq("id", job_id).execute()
    return {"job_id": job_id, "problem_type": job["problem_type"], "technicians": ranked[:3]}


@app.post("/jobs/{job_id}/negotiate/{tech_id}")
async def negotiate_job(job_id: str, tech_id: str):
    client = await get_supabase()
    job_result = await client.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    tech_result = await client.table("technicians").select("*").eq("id", tech_id).limit(1).execute()
    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    if not tech_result.data:
        raise HTTPException(status_code=404, detail="Technician not found")

    job = job_result.data[0]
    tech = tech_result.data[0]
    floor, ceiling = compute_negotiation_bounds(int(job["customer_budget"]), int(job["urgency"]), int(tech["rate_min"]))
    await client.table("jobs").update({"status": "negotiating"}).eq("id", job_id).execute()

    rounds_output = []
    agreed_price = None
    tech_last_offer = None
    customer_last_offer = None

    for round_num in range(1, NEGOTIATION_ROUNDS + 1):
        customer_turn = await asyncio.to_thread(run_customer_agent, job["problem_type"], ceiling, round_num, tech_last_offer)
        customer_offer = max(floor, min(int(customer_turn["offer"]), ceiling))
        customer_last_offer = customer_offer

        tech_turn = await asyncio.to_thread(run_tech_agent, job["problem_type"], floor, ceiling, round_num, customer_last_offer)
        tech_offer = max(floor, min(int(tech_turn["offer"]), ceiling))
        tech_last_offer = tech_offer

        created_at = utc_now_iso()
        log_record = {
            "id": str(uuid.uuid4()), "job_id": job_id, "round": round_num,
            "customer_offer": customer_offer, "tech_offer": tech_offer,
            "customer_message": customer_turn["message"], "tech_message": tech_turn["message"],
            "created_at": created_at,
        }
        await client.table("negotiation_logs").insert(log_record).execute()
        rounds_output.append({**log_record})

        if offers_within_threshold(customer_offer, tech_offer):
            agreed_price = midpoint_price(customer_offer, tech_offer)
            break

    if agreed_price is None:
        agreed_price = int(round((floor + ceiling) / 2))

    await client.table("jobs").update({
        "status": "assigned", "agreed_price": agreed_price, "assigned_tech_id": tech_id,
    }).eq("id", job_id).execute()

    return {"rounds": rounds_output, "agreed_price": agreed_price, "status": "assigned", "technician_name": tech["name"]}


@app.get("/jobs")
async def list_jobs():
    client = await get_supabase()
    result = await client.table("jobs").select("*").order("created_at", desc=True).execute()
    jobs = result.data or []
    tech_ids = list({j["assigned_tech_id"] for j in jobs if j.get("assigned_tech_id")})
    name_map = {}
    if tech_ids:
        names = await client.table("technicians").select("id, name").in_("id", tech_ids).execute()
        name_map = {r["id"]: r["name"] for r in (names.data or [])}
    for job in jobs:
        job["technician_name"] = name_map.get(job.get("assigned_tech_id"))
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    client = await get_supabase()
    job_result = await client.table("jobs").select("*").eq("id", job_id).limit(1).execute()
    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(job_result.data[0])
    tech_id = job.get("assigned_tech_id")
    if tech_id:
        names = await client.table("technicians").select("id, name").eq("id", tech_id).execute()
        name_map = {r["id"]: r["name"] for r in (names.data or [])}
        job["technician_name"] = name_map.get(tech_id)
    else:
        job["technician_name"] = None
    return job


@app.get("/jobs/{job_id}/messages")
async def get_job_messages(job_id: str):
    """Live WhatsApp negotiation messages for a job — poll this every 3s for live chat."""
    client = await get_supabase()
    result = await client.table("whatsapp_messages").select("*").eq(
        "job_id", job_id
    ).order("sent_at").execute()
    return {"messages": result.data or [], "count": len(result.data or [])}


@app.get("/jobs/{job_id}/messages")
async def get_job_messages(job_id: str):
    """Live WhatsApp negotiation messages for a job — poll this every 3s for live chat."""
    client = await get_supabase()
    result = await client.table("whatsapp_messages").select("*").eq(
        "job_id", job_id
    ).order("sent_at").execute()
    return {"messages": result.data or [], "count": len(result.data or [])}


@app.get("/technicians")
async def list_technicians():
    client = await get_supabase()
    result = await client.table("technicians").select("*").order("name").execute()
    data = result.data or []
    return {"technicians": data, "count": len(data)}


@app.post("/technicians", status_code=201)
async def create_technician(body: TechnicianCreate):
    client = await get_supabase()
    record = {
        "id": str(uuid.uuid4()),
        "name": body.name.strip(),
        "skills": [s.strip() for s in body.skills if s.strip()],
        "lat": body.lat, "lng": body.lng,
        "rate_min": body.rate_min,
        "rating": round(body.rating, 1),
        "available": body.available,
    }
    result = await client.table("technicians").insert(record).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create technician")
    return result.data[0]


@app.get("/stats")
async def get_stats():
    client = await get_supabase()
    result = await client.table("jobs").select("*").execute()
    jobs = result.data or []
    total_jobs = len(jobs)
    completed_jobs = sum(1 for j in jobs if j.get("status") == "completed")
    agreed_prices = [int(j["agreed_price"]) for j in jobs if j.get("agreed_price") is not None]
    avg_agreed_price = round(sum(agreed_prices) / len(agreed_prices), 2) if agreed_prices else 0.0
    total_savings = sum(
        int(j["customer_budget"]) - int(j["agreed_price"])
        for j in jobs
        if j.get("agreed_price") is not None and int(j["agreed_price"]) < int(j["customer_budget"])
    )
    return {"total_jobs": total_jobs, "completed_jobs": completed_jobs, "avg_agreed_price": avg_agreed_price, "total_savings": total_savings}


SEED_FIRST = ["Ravi", "Suresh", "Kiran", "Anil", "Prasad", "Venkat", "Harish", "Naveen", "Mahesh", "Rajesh", "Srinivas", "Gopal", "Arjun", "Vikram", "Deepak", "Sanjay", "Ramesh", "Ajay", "Karthik", "Manoj"]
SEED_LAST = ["Reddy", "Rao", "Kumar", "Sharma", "Naidu", "Gupta", "Singh", "Patel", "Varma", "Iyer", "Chowdary", "Babu", "Prasad", "Murthy", "Goud", "Yadav", "Mohan", "Das", "Pillai", "Acharya"]
SKILLS = ["AC", "plumbing", "electrical", "appliance", "carpentry", "painting"]


@app.post("/seed")
async def seed_technicians():
    random.seed(42)
    client = await get_supabase()
    records = [
        {
            "id": str(uuid.uuid4()),
            "name": f"{SEED_FIRST[i]} {SEED_LAST[i]}",
            "skills": random.sample(SKILLS, random.randint(1, 3)),
            "lat": round(random.uniform(17.3, 17.5), 6),
            "lng": round(random.uniform(78.3, 78.6), 6),
            "rate_min": random.randint(200, 800),
            "rating": round(random.uniform(3.5, 5.0), 1),
            "available": random.choice([True, True, True, False]),
        }
        for i in range(20)
    ]
    result = await client.table("technicians").insert(records).execute()
    data = result.data or records
    return {"message": "Seeded 20 technicians for Hyderabad region", "count": len(data), "technicians": data}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
