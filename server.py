import os
import re
import uuid
import json
import time
import socket
import sqlite3
import asyncio
import datetime
import requests
from pathlib import Path
from typing import List, Optional, Set

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from processor import process_job, extract_urls_from_text
from supabase_client import (
    upload_file_to_supabase,
    save_job_to_supabase,
    get_job_from_supabase,
    list_jobs_from_supabase,
    delete_job_from_supabase,
    update_host_heartbeat,
)

BASE_DIR = Path(__file__).parent.resolve()
JOBS_DIR = BASE_DIR / "jobs_data"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "jobs.db"
TUNNEL_FILE = BASE_DIR / "tunnel_url.txt"

JOBS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Antigravity News Summarizer Hub (สำหรับทีมข่าว 10+ คน)")
app.mount("/jobs", StaticFiles(directory=str(JOBS_DIR)), name="jobs")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Limit concurrent heavy AI + video jobs to 2 at a time so Host PC remains smooth
WORKER_SEMAPHORE = asyncio.Semaphore(2)
ACTIVE_LOCAL_JOBS: Set[str] = set()


def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            employee_name TEXT,
            urls_json TEXT,
            raw_text TEXT,
            focus_instruction TEXT,
            output_mode TEXT,
            status TEXT,
            progress_text TEXT,
            progress_pct INTEGER,
            result_json TEXT,
            created_at TEXT,
            completed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def normalize_url(u: str) -> str:
    u = u.strip()
    u = re.sub(r"[?&](rdid|share_url|utm_[^=&]+|_rdr)=[^&]*", "", u)
    return u.rstrip("?&/")


def sync_row_to_supabase(job_id: str):
    """Read job from local SQLite and push to Supabase."""
    try:
        conn = get_db()
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        if not r:
            return
        item = dict(r)
        job_doc = {
            "id": item["id"],
            "employee_name": item["employee_name"],
            "urls": json.loads(item["urls_json"] or "[]"),
            "raw_text": item["raw_text"],
            "focus_instruction": item["focus_instruction"],
            "output_mode": item["output_mode"],
            "status": item["status"],
            "progress_text": item["progress_text"],
            "progress_pct": item["progress_pct"],
            "result": json.loads(item["result_json"]) if item["result_json"] else None,
            "created_at": item["created_at"],
            "completed_at": item["completed_at"],
        }
        save_job_to_supabase(job_doc)
    except Exception:
        pass


def update_job_progress(job_id: str, status: str, text: str, pct: int, result_dict: Optional[dict] = None):
    conn = get_db()
    if result_dict is not None:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE jobs SET status=?, progress_text=?, progress_pct=?, result_json=?, completed_at=? WHERE id=?",
            (status, text, pct, json.dumps(result_dict, ensure_ascii=False), now_str, job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET status=?, progress_text=?, progress_pct=? WHERE id=?",
            (status, text, pct, job_id),
        )
    conn.commit()
    conn.close()
    sync_row_to_supabase(job_id)


def upload_job_media_to_cloud(job_id: str, job_dir: Path, result_dict: dict) -> dict:
    """Upload contact sheets and uploaded images to Supabase Storage so Vercel can display them."""
    try:
        new_sheets = []
        for s_url in result_dict.get("contact_sheets", []):
            if s_url.startswith("http"):
                new_sheets.append(s_url)
                continue
            fname = s_url.split("/")[-1]
            local_f = job_dir / fname
            if local_f.exists():
                ctype = "image/png" if fname.endswith(".png") else "image/jpeg"
                pub_url = upload_file_to_supabase(f"{job_id}/{fname}", local_f.read_bytes(), ctype)
                new_sheets.append(pub_url)
            else:
                new_sheets.append(s_url)
        result_dict["contact_sheets"] = new_sheets

        new_imgs = []
        for img_url in result_dict.get("uploaded_images", []):
            if img_url.startswith("http"):
                new_imgs.append(img_url)
                continue
            fname = img_url.split("/")[-1]
            local_f = job_dir / fname
            if local_f.exists():
                ctype = "image/png" if fname.endswith(".png") else "image/jpeg"
                pub_url = upload_file_to_supabase(f"{job_id}/{fname}", local_f.read_bytes(), ctype)
                new_imgs.append(pub_url)
            else:
                new_imgs.append(img_url)
        result_dict["uploaded_images"] = new_imgs
    except Exception:
        pass
    return result_dict


async def background_worker(
    job_id: str,
    job_dir: Path,
    urls: List[str],
    raw_text: str,
    focus_instruction: str,
    output_mode: str,
    image_files: List[str],
):
    ACTIVE_LOCAL_JOBS.add(job_id)
    async with WORKER_SEMAPHORE:
        try:
            await asyncio.to_thread(update_job_progress, job_id, "processing", "เครื่องแม่กำลังเริ่มประมวลผล...", 12)

            def cb(msg: str, pct: int):
                update_job_progress(job_id, "processing", msg, pct)

            result = await process_job(
                job_id=job_id,
                job_dir=job_dir,
                urls=urls,
                raw_text=raw_text,
                focus_instruction=focus_instruction,
                output_mode=output_mode,
                image_files=image_files,
                progress_cb=cb,
            )
            result = await asyncio.to_thread(upload_job_media_to_cloud, job_id, job_dir, result)
            await asyncio.to_thread(update_job_progress, job_id, "completed", "สรุปเนื้อข่าวเสร็จสมบูรณ์!", 100, result)
        except Exception as e:
            err_res = {
                "headline": "เกิดข้อผิดพลาดในการประมวลผล",
                "raw_news": f"ระบบพบข้อผิดพลาด: {str(e)}",
                "visual_audio_breakdown": "-",
                "context_connected": "-",
                "audit_tag": "⚠️ กรุณาตรวจสอบลิงก์หรือลองใหม่อีกครั้ง",
                "contact_sheets": [],
                "uploaded_images": [f"/jobs/{job_id}/{img}" for img in image_files],
            }
            await asyncio.to_thread(update_job_progress, job_id, "failed", f"ผิดพลาด: {str(e)}", 100, err_res)
        finally:
            ACTIVE_LOCAL_JOBS.discard(job_id)


async def supabase_cloud_poller():
    """Background loop on Host PC that picks up queued jobs submitted via Vercel/Supabase."""
    while True:
        try:
            lan_ip = get_lan_ip()
            tunnel_url = ""
            if TUNNEL_FILE.exists():
                try:
                    tunnel_url = TUNNEL_FILE.read_text(encoding="utf-8").strip()
                except Exception:
                    pass

            await asyncio.to_thread(update_host_heartbeat, f"http://{lan_ip}:8080", tunnel_url)
            cloud_jobs = await asyncio.to_thread(list_jobs_from_supabase, 30)

            for cj in cloud_jobs:
                jid = cj.get("id")
                if not jid:
                    continue
                if cj.get("status") == "queued" and jid not in ACTIVE_LOCAL_JOBS:
                    ACTIVE_LOCAL_JOBS.add(jid)
                    job_dir = JOBS_DIR / jid
                    job_dir.mkdir(parents=True, exist_ok=True)

                    # Download any uploaded images from Supabase Storage into job_dir
                    saved_images: List[str] = []
                    for idx, img_url in enumerate(cj.get("uploaded_image_urls") or []):
                        try:
                            ext = ".png" if img_url.lower().endswith(".png") else ".jpg"
                            fname = f"uploaded_{idx + 1}{ext}"
                            fpath = job_dir / fname
                            resp = await asyncio.to_thread(requests.get, img_url, timeout=20)
                            if resp.status_code == 200 and resp.content:
                                fpath.write_bytes(resp.content)
                                saved_images.append(fname)
                        except Exception:
                            pass

                    # Insert into local SQLite if not present
                    conn = get_db()
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO jobs (id, employee_name, urls_json, raw_text, focus_instruction, output_mode, status, progress_text, progress_pct, result_json, created_at, completed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            jid,
                            cj.get("employee_name", "พนักงาน"),
                            json.dumps(cj.get("urls", []), ensure_ascii=False),
                            cj.get("raw_text", ""),
                            cj.get("focus_instruction", ""),
                            cj.get("output_mode", "standard_4lock"),
                            "processing",
                            "เครื่องแม่รับงานจาก Cloud แล้ว กำลังเริ่มประมวลผล...",
                            10,
                            None,
                            cj.get("created_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            None,
                        ),
                    )
                    conn.commit()
                    conn.close()

                    asyncio.create_task(
                        background_worker(
                            job_id=jid,
                            job_dir=job_dir,
                            urls=cj.get("urls", []),
                            raw_text=cj.get("raw_text", ""),
                            focus_instruction=cj.get("focus_instruction", ""),
                            output_mode=cj.get("output_mode", "standard_4lock"),
                            image_files=saved_images,
                        )
                    )
        except Exception:
            pass
        await asyncio.sleep(3)


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(supabase_cloud_poller())


@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = STATIC_DIR / "index.html"
    return index_file.read_text(encoding="utf-8")


@app.get("/api/system-info")
async def system_info():
    lan_ip = get_lan_ip()
    tunnel_url = ""
    if TUNNEL_FILE.exists():
        try:
            tunnel_url = TUNNEL_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    conn = get_db()
    rows = conn.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status").fetchall()
    conn.close()
    counts = {r["status"]: r["cnt"] for r in rows}

    return {
        "lan_url": f"http://{lan_ip}:8080",
        "local_url": "http://localhost:8080",
        "tunnel_url": tunnel_url,
        "host_online": True,
        "stats": {
            "queued": counts.get("queued", 0),
            "processing": counts.get("processing", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
        },
    }


@app.get("/api/check-duplicate")
async def check_duplicate(url: str):
    if not url.strip():
        return {"duplicate": False}
    norm = normalize_url(url)
    conn = get_db()
    rows = conn.execute("SELECT id, employee_name, urls_json, created_at, status, result_json FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    for r in rows:
        try:
            u_list = json.loads(r["urls_json"] or "[]")
            for existing_u in u_list:
                if normalize_url(existing_u) == norm:
                    res = json.loads(r["result_json"]) if r["result_json"] else {}
                    return {
                        "duplicate": True,
                        "job_id": r["id"],
                        "employee_name": r["employee_name"],
                        "created_at": r["created_at"],
                        "status": r["status"],
                        "headline": res.get("headline", "งานที่ส่งไว้แล้ว"),
                    }
        except Exception:
            continue
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
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    saved_images: List[str] = []
    for idx, img in enumerate(valid_images):
        ext = os.path.splitext(img.filename)[1].lower() or ".png"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".png"
        fname = f"uploaded_{idx + 1}{ext}"
        fpath = job_dir / fname
        content = await img.read()
        if content:
            fpath.write_bytes(content)
            saved_images.append(fname)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ACTIVE_LOCAL_JOBS.add(job_id)
    conn = get_db()
    conn.execute(
        """
        INSERT INTO jobs (id, employee_name, urls_json, raw_text, focus_instruction, output_mode, status, progress_text, progress_pct, result_json, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            employee_name.strip() or "พนักงาน",
            json.dumps(urls, ensure_ascii=False),
            raw_text.strip(),
            focus_instruction.strip(),
            output_mode,
            "queued",
            "อยู่ในคิวรอประมวลผล...",
            5,
            None,
            now_str,
            None,
        ),
    )
    conn.commit()
    conn.close()
    await asyncio.to_thread(sync_row_to_supabase, job_id)

    asyncio.create_task(
        background_worker(
            job_id=job_id,
            job_dir=job_dir,
            urls=urls,
            raw_text=raw_text.strip(),
            focus_instruction=focus_instruction.strip(),
            output_mode=output_mode,
            image_files=saved_images,
        )
    )

    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs")
async def list_jobs(employee: Optional[str] = None, limit: int = 50):
    conn = get_db()
    if employee and employee.strip() and employee != "ALL":
        rows = conn.execute(
            "SELECT * FROM jobs WHERE employee_name = ? ORDER BY created_at DESC LIMIT ?",
            (employee.strip(), limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()

    out = []
    for r in rows:
        item = dict(r)
        item["urls"] = json.loads(item["urls_json"] or "[]")
        item["result"] = json.loads(item["result_json"]) if item["result_json"] else None
        del item["urls_json"]
        del item["result_json"]
        out.append(item)
    return {"jobs": out}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    conn = get_db()
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
    item = dict(r)
    item["urls"] = json.loads(item["urls_json"] or "[]")
    item["result"] = json.loads(item["result_json"]) if item["result_json"] else None
    del item["urls_json"]
    del item["result_json"]
    return item


class UpdateJobBody(BaseModel):
    raw_news: str
    headline: Optional[str] = None


@app.put("/api/jobs/{job_id}")
async def update_job_text(job_id: str, body: UpdateJobBody):
    conn = get_db()
    r = conn.execute("SELECT result_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
    res = json.loads(r["result_json"]) if r["result_json"] else {}
    res["raw_news"] = body.raw_news
    if body.headline:
        res["headline"] = body.headline
    conn.execute("UPDATE jobs SET result_json = ? WHERE id = ?", (json.dumps(res, ensure_ascii=False), job_id))
    conn.commit()
    conn.close()
    await asyncio.to_thread(sync_row_to_supabase, job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    conn = get_db()
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    await asyncio.to_thread(delete_job_from_supabase, job_id)
    return {"ok": True}
