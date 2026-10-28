import os
import re
import sys
import uuid
import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

ROOT_DIR = Path(__file__).parent.parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from supabase_client import (
    upload_file_to_supabase,
    save_job_to_supabase,
    get_job_from_supabase,
    list_jobs_from_supabase,
    delete_job_from_supabase,
    get_host_heartbeat,
)

STATIC_DIR = ROOT_DIR / "static"

app = FastAPI(title="Antigravity News Summarizer Hub (Cloud Vercel Edition)")


def extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    pattern = r"https?://[^\s<>\"')\]+]+"
    found = re.findall(pattern, text)
    seen = set()
    out = []
    for u in found:
        u = u.rstrip(".,;!)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def normalize_url(u: str) -> str:
    u = u.strip()
    u = re.sub(r"[?&](rdid|share_url|utm_[^=&]+|_rdr)=[^&]*", "", u)
    return u.rstrip("?&/")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")
    return "<h1>Antigravity News Summarizer Hub</h1>"


@app.get("/api/system-info")
async def system_info():
    hb = get_host_heartbeat()
    jobs = list_jobs_from_supabase(limit=100)
    counts = {"queued": 0, "processing": 0, "completed": 0, "failed": 0}
    for j in jobs:
        st = j.get("status", "queued")
        counts[st] = counts.get(st, 0) + 1

    return {
        "lan_url": hb.get("lan_url") or "http://localhost:8080",
        "local_url": "http://localhost:8080",
        "tunnel_url": hb.get("vercel_url") or hb.get("tunnel_url") or "",
        "host_online": hb.get("online", True),
        "host_last_seen": hb.get("last_seen_str", ""),
        "stats": counts,
    }


@app.get("/api/check-duplicate")
async def check_duplicate(url: str):
    if not url.strip():
        return {"duplicate": False}
    norm = normalize_url(url)
    jobs = list_jobs_from_supabase(limit=80)
    for j in jobs:
        for existing_u in j.get("urls", []):
            if normalize_url(existing_u) == norm:
                res = j.get("result") or {}
                return {
                    "duplicate": True,
                    "job_id": j.get("id"),
                    "employee_name": j.get("employee_name", "พนักงาน"),
                    "created_at": j.get("created_at", ""),
                    "status": j.get("status", ""),
                    "headline": res.get("headline", "งานที่ส่งไว้แล้ว"),
                }
    return {"duplicate": False}


@app.post("/api/submit")
async def submit_job(
    employee_name: str = Form("พนักงาน"),
    urls_input: str = Form(""),
    raw_text: str = Form(""),
    focus_instruction: str = Form(""),
    output_mode: str = Form("standard_4lock"),
    images: List[UploadFile] = File(default=[]),
):
    urls = extract_urls_from_text(urls_input)
    if not urls and raw_text:
        urls = extract_urls_from_text(raw_text)

    valid_images = [img for img in images if img and img.filename]
    if not urls and not raw_text.strip() and not valid_images:
        raise HTTPException(status_code=400, detail="กรุณาใส่ลิงก์คลิป/ข่าว, ข้อความ หรืออัปโหลดรูปภาพอย่างน้อย 1 อย่าง")

    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]

    uploaded_image_urls: List[str] = []
    for idx, img in enumerate(valid_images):
        ext = os.path.splitext(img.filename)[1].lower() or ".png"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".png"
        fname = f"uploaded_{idx + 1}{ext}"
        content = await img.read()
        if content:
            ctype = "image/png" if ext == ".png" else "image/jpeg"
            pub_url = upload_file_to_supabase(f"{job_id}/{fname}", content, ctype)
            uploaded_image_urls.append(pub_url)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    job_doc = {
        "id": job_id,
        "employee_name": employee_name.strip() or "พนักงาน",
        "urls": urls,
        "raw_text": raw_text.strip(),
        "focus_instruction": focus_instruction.strip(),
        "output_mode": output_mode,
        "status": "queued",
        "progress_text": "ส่งเข้าคิว Cloud (Supabase) แล้ว รอเครื่องแม่ดึงไปประมวลผล...",
        "progress_pct": 8,
        "result": None,
        "uploaded_image_urls": uploaded_image_urls,
        "created_at": now_str,
        "completed_at": None,
    }
    save_job_to_supabase(job_doc)
    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs")
async def list_jobs(employee: Optional[str] = None, limit: int = 50):
    jobs = list_jobs_from_supabase(limit=limit)
    if employee and employee.strip() and employee != "ALL":
        jobs = [j for j in jobs if j.get("employee_name") == employee.strip()]
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    j = get_job_from_supabase(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
    return j


class UpdateJobBody(BaseModel):
    raw_news: str
    headline: Optional[str] = None


@app.put("/api/jobs/{job_id}")
async def update_job_text(job_id: str, body: UpdateJobBody):
    j = get_job_from_supabase(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
    res = j.get("result") or {}
    res["raw_news"] = body.raw_news
    if body.headline:
        res["headline"] = body.headline
    j["result"] = res
    save_job_to_supabase(j)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    delete_job_from_supabase(job_id)
    return {"ok": True}
