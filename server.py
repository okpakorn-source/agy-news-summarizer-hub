import os
import re
import uuid
import json
import time
import socket
import sqlite3
import asyncio
import datetime
import subprocess
import requests
from pathlib import Path
from typing import List, Optional, Set

TH_TZ = datetime.timezone(datetime.timedelta(hours=7))


def now_th() -> datetime.datetime:
    return datetime.datetime.now(TH_TZ)

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from supabase_client import (
    upload_file_to_supabase,
    save_job_to_supabase,
    get_job_from_supabase,
    list_jobs_from_supabase,
    delete_job_from_supabase,
    update_host_heartbeat,
    get_host_heartbeat,
)

IS_VERCEL = bool(os.environ.get("VERCEL"))

BASE_DIR = Path(__file__).parent.resolve()
JOBS_DIR = BASE_DIR / "jobs_data"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "jobs.db"
TUNNEL_FILE = BASE_DIR / "tunnel_url.txt"
VERCEL_PUBLIC_URL = "https://agy-news-summarizer-hub.vercel.app"

app = FastAPI(title="Antigravity News Summarizer Hub (สำหรับทีมข่าว 10+ คน)")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if not IS_VERCEL:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/jobs", StaticFiles(directory=str(JOBS_DIR)), name="jobs")

# Limit concurrent heavy AI + video jobs to 2 at a time so Host PC remains smooth
WORKER_SEMAPHORE = asyncio.Semaphore(2)
ACTIVE_LOCAL_JOBS: Set[str] = set()


def extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    pattern = r"https?://[^\s<>\"')\]+]+"
    found = re.findall(pattern, text)
    seen = set()
    out = []
    for u in found:
        u = u.rstrip(".,;!)'\"]>")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_chat_style_submission(
    urls_input: str,
    raw_text: str,
    focus_instruction: str,
    output_mode: str,
):
    """Allow pasting 'ทำข่าวนี้มุมปกติ 1 ข่าว บิดประเด็น 1 ข่าว ขอแนว... https://...' in one box."""
    urls = extract_urls_from_text(urls_input)
    leftover = re.sub(r"https?://[^\s<>\"')\]+]+", " ", urls_input or "").strip()
    leftover = re.sub(r"\s+", " ", leftover).strip(" \t\r\n,-—:")

    if not urls and raw_text:
        urls = extract_urls_from_text(raw_text)

    merged_focus = (focus_instruction or "").strip()
    if len(leftover) >= 3:
        merged_focus = f"{merged_focus}\n{leftover}".strip() if merged_focus else leftover

    # Smart auto-detect mode if employee wrote explicit multi-angle instructions in chat style
    low_focus = merged_focus.lower()
    if "3 ย่อหน้า" in low_focus or "สามย่อหน้า" in low_focus:
        output_mode = "viral_3_para"
    elif ("บิดประเด็น 2" in low_focus or "บิด 2" in low_focus or "บิดประเด็น2" in low_focus) and "ปกติ" in low_focus:
        output_mode = "normal_1_spin_2"
    elif "ปกติ" in low_focus and "บิด" in low_focus and output_mode == "standard_4lock":
        output_mode = "normal_1_spin_1"

    return urls, merged_focus, output_mode


def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if IS_VERCEL:
        return
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
    if IS_VERCEL:
        return
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
        now_str = now_th().strftime("%Y-%m-%d %H:%M:%S")
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
    from processor import process_job

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
            s_err = str(e)
            if isinstance(e, subprocess.TimeoutExpired) or "timed out" in s_err.lower():
                short_err = "ใช้เวลาค้นหาและประมวลผลเกินกำหนด (Timeout) กรุณาลองส่งใหม่อีกครั้ง"
            else:
                short_err = s_err[:220] + "..." if len(s_err) > 220 else s_err
            err_res = {
                "headline": "เกิดข้อผิดพลาดในการประมวลผล",
                "raw_news": f"ระบบพบข้อผิดพลาด: {short_err}",
                "visual_audio_breakdown": "-",
                "context_connected": "-",
                "audit_tag": "⚠️ กรุณาตรวจสอบลิงก์หรือลองใหม่อีกครั้ง",
                "contact_sheets": [],
                "uploaded_images": [f"/jobs/{job_id}/{img}" for img in image_files],
            }
            await asyncio.to_thread(update_job_progress, job_id, "failed", f"ผิดพลาด: {short_err}", 100, err_res)
        finally:
            ACTIVE_LOCAL_JOBS.discard(job_id)


async def supabase_cloud_poller():
    """Background loop on Host PC that picks up queued jobs submitted via Vercel/Supabase."""
    while True:
        try:
            lan_ip = get_lan_ip()
            await asyncio.to_thread(update_host_heartbeat, f"http://{lan_ip}:8080", VERCEL_PUBLIC_URL)
            cloud_jobs = await asyncio.to_thread(list_jobs_from_supabase, 30)

            for cj in cloud_jobs:
                jid = cj.get("id")
                if not jid:
                    continue
                if cj.get("status") == "queued" and jid not in ACTIVE_LOCAL_JOBS:
                    ACTIVE_LOCAL_JOBS.add(jid)
                    job_dir = JOBS_DIR / jid
                    job_dir.mkdir(parents=True, exist_ok=True)

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
                            cj.get("output_mode", "normal_1_spin_1"),
                            "queued",
                            "เครื่องแม่รับงานเข้าคิวแล้ว รอสล็อตประมวลผล...",
                            10,
                            None,
                            cj.get("created_at") or now_th().strftime("%Y-%m-%d %H:%M:%S"),
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
                            output_mode=cj.get("output_mode", "normal_1_spin_1"),
                            image_files=saved_images,
                        )
                    )
        except Exception:
            pass
        await asyncio.sleep(3)


@app.on_event("startup")
async def on_startup():
    if not IS_VERCEL:
        asyncio.create_task(supabase_cloud_poller())


@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = STATIC_DIR / "index.html"
    return index_file.read_text(encoding="utf-8")


@app.get("/api/system-info")
async def system_info():
    if IS_VERCEL:
        hb = await asyncio.to_thread(get_host_heartbeat)
        jobs = await asyncio.to_thread(list_jobs_from_supabase, 100)
        counts = {"queued": 0, "processing": 0, "completed": 0, "failed": 0}
        for j in jobs:
            st = j.get("status", "queued")
            counts[st] = counts.get(st, 0) + 1
        return {
            "lan_url": hb.get("lan_url") or "http://localhost:8080",
            "local_url": "http://localhost:8080",
            "tunnel_url": VERCEL_PUBLIC_URL,
            "host_online": bool(hb.get("online", False)),
            "host_last_seen": hb.get("last_seen_str", ""),
            "stats": counts,
        }

    lan_ip = get_lan_ip()
    conn = get_db()
    rows = conn.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status").fetchall()
    conn.close()
    counts = {r["status"]: r["cnt"] for r in rows}

    return {
        "lan_url": f"http://{lan_ip}:8080",
        "local_url": "http://localhost:8080",
        "tunnel_url": VERCEL_PUBLIC_URL,
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

    if IS_VERCEL:
        jobs = await asyncio.to_thread(list_jobs_from_supabase, 80)
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
    output_mode: str = Form("normal_1_spin_1"),
    images: List[UploadFile] = File(default=[]),
):
    urls, focus_instruction, output_mode = parse_chat_style_submission(
        urls_input, raw_text, focus_instruction, output_mode
    )

    valid_images = [img for img in images if img and img.filename]
    if not urls and not raw_text.strip() and not valid_images:
        raise HTTPException(status_code=400, detail="กรุณาใส่ลิงก์คลิป/ข่าว, ข้อความ หรืออัปโหลดรูปภาพอย่างน้อย 1 อย่าง")

    job_id = now_th().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    now_str = now_th().strftime("%Y-%m-%d %H:%M:%S")

    if IS_VERCEL:
        uploaded_image_urls: List[str] = []
        for idx, img in enumerate(valid_images):
            ext = os.path.splitext(img.filename)[1].lower() or ".png"
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".png"
            fname = f"uploaded_{idx + 1}{ext}"
            content = await img.read()
            if content:
                ctype = "image/png" if ext == ".png" else "image/jpeg"
                pub_url = await asyncio.to_thread(upload_file_to_supabase, f"{job_id}/{fname}", content, ctype)
                uploaded_image_urls.append(pub_url)

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
        await asyncio.to_thread(save_job_to_supabase, job_doc)
        return {"ok": True, "job_id": job_id}

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
    if IS_VERCEL:
        jobs = await asyncio.to_thread(list_jobs_from_supabase, limit)
        if employee and employee.strip() and employee != "ALL":
            jobs = [j for j in jobs if j.get("employee_name") == employee.strip()]
        return {"jobs": jobs}

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
    if IS_VERCEL:
        j = await asyncio.to_thread(get_job_from_supabase, job_id)
        if not j:
            raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
        return j

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
    raw_news: Optional[str] = None
    headline: Optional[str] = None
    articles: Optional[List[dict]] = None


def _rebuild_raw_news_from_articles(articles: List[dict]) -> str:
    cleaned = [a for a in articles if isinstance(a, dict) and str(a.get("content", "")).strip()]
    if len(cleaned) == 1:
        return str(cleaned[0]["content"]).strip()
    return "\n\n══════════════════════════════\n\n".join(
        f"【{a.get('title') or a.get('label') or f'ข่าวที่ {i+1}'}】\n\n{str(a['content']).strip()}"
        for i, a in enumerate(cleaned)
    )


@app.put("/api/jobs/{job_id}")
async def update_job_text(job_id: str, body: UpdateJobBody):
    if IS_VERCEL:
        j = await asyncio.to_thread(get_job_from_supabase, job_id)
        if not j:
            raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
        res = j.get("result") or {}
        if body.articles is not None:
            res["articles"] = body.articles
            res["raw_news"] = _rebuild_raw_news_from_articles(body.articles)
        elif body.raw_news is not None:
            res["raw_news"] = body.raw_news
        if body.headline:
            res["headline"] = body.headline
        j["result"] = res
        await asyncio.to_thread(save_job_to_supabase, j)
        return {"ok": True}

    conn = get_db()
    r = conn.execute("SELECT result_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้")
    res = json.loads(r["result_json"]) if r["result_json"] else {}
    if body.articles is not None:
        res["articles"] = body.articles
        res["raw_news"] = _rebuild_raw_news_from_articles(body.articles)
    elif body.raw_news is not None:
        res["raw_news"] = body.raw_news
    if body.headline:
        res["headline"] = body.headline
    conn.execute("UPDATE jobs SET result_json = ? WHERE id = ?", (json.dumps(res, ensure_ascii=False), job_id))
    conn.commit()
    conn.close()
    await asyncio.to_thread(sync_row_to_supabase, job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    j = await asyncio.to_thread(get_job_from_supabase, job_id)
    if not IS_VERCEL and not j:
        conn = get_db()
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        if r:
            item = dict(r)
            j = {
                "id": item["id"],
                "employee_name": item["employee_name"],
                "urls": json.loads(item["urls_json"] or "[]"),
                "raw_text": item["raw_text"],
                "focus_instruction": item["focus_instruction"],
                "output_mode": item["output_mode"] or "normal_1_spin_1",
                "created_at": item["created_at"],
            }
    if not j:
        raise HTTPException(status_code=404, detail="ไม่พบงานนี้")

    j["status"] = "queued"
    j["progress_text"] = "ส่งเข้าคิวรันใหม่อีกครั้ง รอเครื่องแม่ดึงไปประมวลผล..."
    j["progress_pct"] = 8
    await asyncio.to_thread(save_job_to_supabase, j)

    if not IS_VERCEL:
        ACTIVE_LOCAL_JOBS.discard(job_id)
        conn = get_db()
        conn.execute(
            "UPDATE jobs SET status=?, progress_text=?, progress_pct=? WHERE id=?",
            ("queued", j["progress_text"], 8, job_id),
        )
        conn.commit()
        conn.close()

    return {"ok": True, "job_id": job_id}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    if IS_VERCEL:
        await asyncio.to_thread(delete_job_from_supabase, job_id)
        return {"ok": True}

    conn = get_db()
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()
    await asyncio.to_thread(delete_job_from_supabase, job_id)
    return {"ok": True}
