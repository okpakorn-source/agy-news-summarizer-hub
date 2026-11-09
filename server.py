#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IG.dara Studio — ระบบคลังข่าวไวรัล 3 ย่อหน้า ภาพปก 4:5 และระบบโพสต์ลงเพจ Facebook (v25.0)
=========================================================================================
Deployed on Vercel + Supabase (`agy-news-hub` Storage & `app_settings` table) + Local/Tunnel.
Enforces mandatory login on all pages & APIs, keeps `FB_SYSTEM_TOKEN` and `page_token`
strictly server-side, attaches `appsecret_proof` when `FB_APP_SECRET` is configured,
and defaults to `FB_MOCK=1` during development/testing until owner enables live posting.
"""

import datetime
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image
from pydantic import BaseModel

from auth_guard import (
    LOGIN_PAGE_HTML,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    authenticate_credentials,
    create_session_token,
    get_current_user_from_request,
    require_authenticated_user,
    require_owner_user,
    update_user_passwords,
)
from fb_publisher import (
    fetch_fb_pages,
    get_public_fb_status,
    publish_to_selected_pages,
    update_server_fb_config,
)
from supabase_client import (
    get_case_from_supabase,
    list_cases_from_supabase,
    save_case_to_supabase,
    upload_file_to_supabase,
)

TH_TZ = datetime.timezone(datetime.timedelta(hours=7))
IS_VERCEL = bool(os.environ.get("VERCEL"))

BASE_DIR = Path(__file__).resolve().parent
LOCAL_OUTPUTS_DIR = Path(r"c:\155cover any\outputs")

app = FastAPI(
    title="IG.dara Studio — ระบบคลังข่าวและโพสต์เพจ Facebook อัตโนมัติ",
    description="ศูนย์รวมเนื้อหาข่าว 3 ย่อหน้า ภาพปก 4:5 (1080x1350) และระบบโพสต์ลงเพจ Facebook หลายเพจ",
)


# ---------------------------------------------------------------------------
# IG.dara 3-Paragraph Copywriting Validator (Pure Python, Serverless-Safe)
# ---------------------------------------------------------------------------

def validate_ig_dara_text(text: str) -> Dict[str, Any]:
    clean = (text or "").strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", clean) if p.strip()]
    char_len = len(clean)
    para_lens = [len(p) for p in paras]
    numbers = re.findall(r"\d[\d,\.]*", clean)
    quotes = re.findall(r'[“"”\']([^“"”\']+)[“"”\']', clean)
    has_hashtag = bool(re.search(r"#\S+", clean))
    has_url = bool(re.search(r"https?://", clean))

    checks = {
        "exactly_3_paragraphs": len(paras) == 3,
        "length_in_sweet_spot": 420 <= char_len <= 1150,
        "has_numeric_anchors": len(numbers) >= 2,
        "has_direct_quotes": len(quotes) >= 1,
        "zero_hashtags": not has_hashtag,
        "zero_external_links": not has_url,
    }
    passed = sum(1 for v in checks.values() if v)
    score = round((passed / len(checks)) * 100)
    return {
        "valid": passed >= 5 and len(paras) == 3,
        "score": score,
        "char_length": char_len,
        "paragraph_count": len(paras),
        "paragraph_lengths": para_lens,
        "numeric_anchors_found": numbers[:10],
        "quotes_found": quotes[:6],
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Case Loading & Synchronization Helpers (Supabase + Local Disk Hybrid)
# ---------------------------------------------------------------------------

def _load_all_cases() -> List[Dict[str, Any]]:
    """Load all IG.dara Studio cases from Supabase (and merge local files if running on Host PC)."""
    supa_cases = list_cases_from_supabase(limit=200)
    by_key: Dict[str, Dict[str, Any]] = {}
    for c in supa_cases:
        ck = str(c.get("case_key") or "").strip()
        if ck:
            by_key[ck] = c

    # If running locally where `c:\155cover any\outputs` exists, ensure local cover URLs fallback cleanly
    if not IS_VERCEL and LOCAL_OUTPUTS_DIR.exists():
        for rec_path in sorted(LOCAL_OUTPUTS_DIR.rglob("record.json")):
            case_dir = rec_path.parent
            rel = case_dir.relative_to(LOCAL_OUTPUTS_DIR).as_posix()
            ck = rel.replace("/", "__")
            if ck not in by_key:
                try:
                    data = json.loads(rec_path.read_text(encoding="utf-8"))
                    full_text = str(data.get("full_text") or "").strip()
                    by_key[ck] = {
                        "case_key": ck,
                        "case_id": data.get("case_id", case_dir.name),
                        "folder_rel": rel,
                        "priority_rank": 10 if rel.startswith("viral_clips_10/") else (20 if rel.startswith("boom_krasae_5/") else 50),
                        "topic_title": data.get("topic_title", case_dir.name),
                        "archetype": data.get("archetype", "classic_circle_box"),
                        "generated_at": data.get("generated_at", ""),
                        "posted_status": bool(data.get("posted_status", False)),
                        "posted_by": data.get("posted_by", ""),
                        "posted_at": data.get("posted_at", ""),
                        "full_text": full_text,
                        "text_validation": data.get("text_validation") or validate_ig_dara_text(full_text),
                        "caption_variants": data.get("caption_variants", []),
                        "sources_researched": data.get("sources_researched", []),
                        "cover_url": data.get("supabase_cover_url") or f"/api/cases/{ck}/file/cover.jpg",
                        "supabase_cover_url": data.get("supabase_cover_url", ""),
                        "selected_cover_rel": data.get("selected_cover_rel", "cover.jpg"),
                        "variants": data.get("supabase_variants", []),
                        "fb_posts": data.get("fb_posts", []),
                    }
                except Exception:
                    pass

    cases_list = list(by_key.values())
    for c in cases_list:
        c.setdefault("fb_posts", [])
        c.setdefault("selected_cover_rel", "cover.jpg")
        # Ensure active display cover matches `selected_cover_rel`
        sel_rel = c.get("selected_cover_rel", "cover.jpg")
        active_url = c.get("supabase_cover_url") or c.get("cover_url") or ""
        if sel_rel and sel_rel != "cover.jpg":
            for v in c.get("variants") or []:
                if v.get("rel") == sel_rel or v.get("name") == sel_rel:
                    active_url = v.get("public_url") or v.get("url") or active_url
                    break
        c["active_cover_url"] = active_url

    cases_list.sort(
        key=lambda x: (
            int(x.get("priority_rank") or 50),
            str(x.get("folder_rel") or x.get("case_key") or ""),
        )
    )
    return cases_list


def _get_single_case(case_key: str) -> Optional[Dict[str, Any]]:
    case_data = get_case_from_supabase(case_key)
    if case_data:
        return case_data
    for c in _load_all_cases():
        if c.get("case_key") == case_key:
            return c
    return None


def _persist_case_update(case_key: str, case_data: Dict[str, Any]) -> None:
    save_case_to_supabase(case_data)
    if not IS_VERCEL and LOCAL_OUTPUTS_DIR.exists():
        folder_rel = str(case_data.get("folder_rel") or case_key.replace("__", "/"))
        local_rec = LOCAL_OUTPUTS_DIR / folder_rel / "record.json"
        if local_rec.exists():
            try:
                raw = json.loads(local_rec.read_text(encoding="utf-8"))
                raw.update({
                    "topic_title": case_data.get("topic_title", raw.get("topic_title")),
                    "full_text": case_data.get("full_text", raw.get("full_text")),
                    "caption_variants": case_data.get("caption_variants", raw.get("caption_variants")),
                    "posted_status": case_data.get("posted_status", raw.get("posted_status")),
                    "posted_by": case_data.get("posted_by", raw.get("posted_by")),
                    "posted_at": case_data.get("posted_at", raw.get("posted_at")),
                    "selected_cover_rel": case_data.get("selected_cover_rel", "cover.jpg"),
                    "fb_posts": case_data.get("fb_posts", []),
                })
                local_rec.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Authentication Routes (`/login`, `/api/auth/*`)
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = "team"
    password: str


class PasswordChangeRequest(BaseModel):
    new_owner_password: Optional[str] = None
    new_team_password: Optional[str] = None


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user_from_request(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(content=LOGIN_PAGE_HTML)


@app.post("/api/auth/login")
async def api_login(payload: LoginRequest):
    user_info = authenticate_credentials(payload.username, payload.password)
    if not user_info:
        raise HTTPException(status_code=401, detail="รหัสผ่านไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง")
    token = create_session_token(user_info)
    resp = JSONResponse({"ok": True, "user": user_info})
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=IS_VERCEL,
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/me")
async def api_auth_me(user: Dict[str, Any] = Depends(require_authenticated_user)):
    return {"ok": True, "user": user}


@app.post("/api/auth/passwords")
async def api_update_passwords(
    payload: PasswordChangeRequest,
    user: Dict[str, Any] = Depends(require_owner_user),
):
    ok = update_user_passwords(payload.new_owner_password, payload.new_team_password)
    return {"ok": ok, "message": "อัปเดตรหัสผ่านเรียบร้อยแล้ว"}


# ---------------------------------------------------------------------------
# Facebook Graph API v25.0 Routes (`/api/fb/*`) — Protected by Login
# ---------------------------------------------------------------------------

class FBSettingsUpdateRequest(BaseModel):
    fb_system_token: Optional[str] = None
    fb_app_secret: Optional[str] = None
    fb_mock: Optional[bool] = None


class FBPublishTarget(BaseModel):
    page_id: str
    page_name: str = ""
    caption_index: int = 1


class FBPublishRequest(BaseModel):
    case_key: str
    cover_rel: str = "cover.jpg"
    mode: str = "immediate"  # "immediate" | "auto_schedule"
    targets: List[FBPublishTarget]
    force_duplicate: bool = False


@app.get("/api/fb/pages")
async def api_get_fb_pages(
    refresh: bool = False,
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    return fetch_fb_pages(refresh=bool(refresh))


@app.post("/api/fb/pages/refresh")
async def api_refresh_fb_pages(user: Dict[str, Any] = Depends(require_authenticated_user)):
    return fetch_fb_pages(refresh=True)


@app.get("/api/fb/settings")
async def api_get_fb_settings(user: Dict[str, Any] = Depends(require_authenticated_user)):
    return {"ok": True, "status": get_public_fb_status(), "user": user}


@app.post("/api/fb/settings")
async def api_update_fb_settings(
    payload: FBSettingsUpdateRequest,
    user: Dict[str, Any] = Depends(require_owner_user),
):
    updated_status = update_server_fb_config(
        fb_system_token=payload.fb_system_token,
        fb_app_secret=payload.fb_app_secret,
        fb_mock=payload.fb_mock,
    )
    if payload.fb_system_token:
        fetch_fb_pages(refresh=True)
    return {"ok": True, "status": updated_status}


@app.post("/api/fb/publish")
async def api_publish_to_facebook(
    payload: FBPublishRequest,
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    if not payload.targets:
        raise HTTPException(status_code=400, detail="กรุณาเลือกอย่างน้อย 1 เพจก่อนสั่งโพสต์")
    if payload.mode not in ("immediate", "auto_schedule"):
        raise HTTPException(status_code=400, detail="รูปแบบการโพสต์ไม่ถูกต้อง (ต้องเป็น immediate หรือ auto_schedule)")

    case_data = _get_single_case(payload.case_key)
    if not case_data:
        raise HTTPException(status_code=404, detail=f"ไม่พบข้อมูลข่าว: {payload.case_key}")

    # Resolve chosen cover image URL (public 4:5 1080x1350 URL on Supabase Storage)
    cover_rel = (payload.cover_rel or case_data.get("selected_cover_rel") or "cover.jpg").strip()
    cover_public_url = str(case_data.get("supabase_cover_url") or case_data.get("cover_url") or "")

    if cover_rel != "cover.jpg":
        for v in case_data.get("variants") or []:
            if v.get("rel") == cover_rel or v.get("name") == cover_rel:
                cover_public_url = str(v.get("public_url") or v.get("url") or cover_public_url)
                break

    # Optional local file bytes fallback if running on local PC
    cover_bytes: Optional[bytes] = None
    if not IS_VERCEL and LOCAL_OUTPUTS_DIR.exists():
        folder_rel = str(case_data.get("folder_rel") or payload.case_key.replace("__", "/"))
        candidate_file = (LOCAL_OUTPUTS_DIR / folder_rel / cover_rel).resolve()
        if candidate_file.exists() and candidate_file.is_file():
            try:
                cover_bytes = candidate_file.read_bytes()
            except Exception:
                pass

    targets_dicts = [t.model_dump() for t in payload.targets]
    pub_result = publish_to_selected_pages(
        case_data=case_data,
        targets=targets_dicts,
        mode=payload.mode,
        cover_rel=cover_rel,
        cover_public_url=cover_public_url,
        cover_bytes=cover_bytes,
        force_duplicate=payload.force_duplicate,
        published_by=user.get("display_name", "ทีมงาน"),
    )

    if pub_result.get("requires_duplicate_confirm"):
        return pub_result

    # Save updated fb_posts & posted_status if at least 1 page succeeded
    if pub_result.get("success_count", 0) > 0:
        now_str = datetime.datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        case_data["fb_posts"] = pub_result.get("updated_fb_posts", [])
        case_data["posted_status"] = True
        case_data["posted_by"] = user.get("display_name", "ทีมงาน")
        case_data["posted_at"] = now_str
        case_data["selected_cover_rel"] = cover_rel
        _persist_case_update(payload.case_key, case_data)

    return pub_result


# ---------------------------------------------------------------------------
# IG.dara Studio Cases API Routes (`/api/cases/*`) — Protected by Login
# ---------------------------------------------------------------------------

class SelectCoverRequest(BaseModel):
    cover_rel: str


class EditCaptionRequest(BaseModel):
    topic_title: Optional[str] = None
    caption_index: int = 1
    text: str


@app.get("/api/cases")
async def api_list_cases(user: Dict[str, Any] = Depends(require_authenticated_user)):
    cases = _load_all_cases()
    return {
        "ok": True,
        "total": len(cases),
        "user": user,
        "fb_status": get_public_fb_status(),
        "cases": cases,
    }


@app.post("/api/cases/{case_key}/select-cover")
async def api_select_cover(
    case_key: str,
    payload: SelectCoverRequest,
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    case_data = _get_single_case(case_key)
    if not case_data:
        raise HTTPException(status_code=404, detail="ไม่พบข่าวนี้ในระบบ")

    sel_rel = payload.cover_rel.strip() or "cover.jpg"
    case_data["selected_cover_rel"] = sel_rel

    active_url = case_data.get("supabase_cover_url") or case_data.get("cover_url") or ""
    if sel_rel != "cover.jpg":
        for v in case_data.get("variants") or []:
            if v.get("rel") == sel_rel or v.get("name") == sel_rel:
                active_url = v.get("public_url") or v.get("url") or active_url
                break
    case_data["active_cover_url"] = active_url
    _persist_case_update(case_key, case_data)
    return {"ok": True, "case_key": case_key, "selected_cover_rel": sel_rel, "active_cover_url": active_url}


@app.post("/api/cases/{case_key}/edit-text")
async def api_edit_case_text(
    case_key: str,
    payload: EditCaptionRequest,
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    case_data = _get_single_case(case_key)
    if not case_data:
        raise HTTPException(status_code=404, detail="ไม่พบข่าวนี้ในระบบ")

    new_text = (payload.text or "").strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="เนื้อหาแคปชั่นห้ามว่างเปล่า")

    val = validate_ig_dara_text(new_text)
    if payload.topic_title and payload.topic_title.strip():
        case_data["topic_title"] = payload.topic_title.strip()

    variants = list(case_data.get("caption_variants") or [])
    updated = False
    for cv in variants:
        if isinstance(cv, dict) and int(cv.get("index") or 1) == int(payload.caption_index):
            cv["text"] = new_text
            cv["validation"] = val
            updated = True
            break
    if not updated:
        variants.append({
            "index": int(payload.caption_index),
            "label": f"แบบที่ {payload.caption_index}",
            "angle": "แก้ไขโดยทีมงาน",
            "text": new_text,
            "validation": val,
        })

    case_data["caption_variants"] = variants
    if int(payload.caption_index) == 1:
        case_data["full_text"] = new_text
        case_data["text_validation"] = val

    _persist_case_update(case_key, case_data)
    return {"ok": True, "validation": val, "caption_variants": variants}


@app.post("/api/cases/{case_key}/status")
async def api_update_case_status(
    case_key: str,
    posted_status: bool = Form(...),
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    case_data = _get_single_case(case_key)
    if not case_data:
        raise HTTPException(status_code=404, detail="ไม่พบข่าวนี้ในระบบ")

    now_str = datetime.datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S")
    case_data["posted_status"] = bool(posted_status)
    case_data["posted_by"] = user.get("display_name", "ทีมงาน") if posted_status else ""
    case_data["posted_at"] = now_str if posted_status else ""
    _persist_case_update(case_key, case_data)
    return {
        "ok": True,
        "case_key": case_key,
        "posted_status": case_data["posted_status"],
        "posted_by": case_data["posted_by"],
        "posted_at": case_data["posted_at"],
    }


@app.post("/api/cases/create")
async def api_create_new_case(
    request: Request,
    topic_title: str = Form(...),
    caption_text_1: str = Form(...),
    caption_text_2: str = Form(""),
    caption_text_3: str = Form(""),
    cover_file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    raw_bytes = await cover_file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="กรุณาอัปโหลดไฟล์ภาพปก 4:5")

    # Normalize cover to 4:5 (1080x1350) JPEG
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    target_w, target_h = 1080, 1350
    src_ratio = img.width / max(1, img.height)
    target_ratio = target_w / target_h
    if abs(src_ratio - target_ratio) > 0.02:
        if src_ratio > target_ratio:
            new_w = int(img.height * target_ratio)
            left = (img.width - new_w) // 2
            img = img.crop((left, 0, left + new_w, img.height))
        else:
            new_h = int(img.width / target_ratio)
            top = (img.height - new_h) // 2
            img = img.crop((0, top, img.width, top + new_h))
    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=93)
    jpeg_bytes = buf.getvalue()

    ts_slug = f"staff_uploads__post_{int(time.time())}"
    public_url = upload_file_to_supabase(f"igdara/{ts_slug}/cover.jpg", jpeg_bytes, "image/jpeg")

    c1 = caption_text_1.strip()
    c2 = caption_text_2.strip() or f"เปิดอีกมุมที่หลายคนยังไม่รู้! {c1}"
    c3 = caption_text_3.strip() or f"เรื่องราวที่ทำเอาชาวเน็ตแห่แชร์สนั่น! {c1}"

    caption_variants = [
        {"index": 1, "label": "แบบที่ 1: เนื้อหาหลัก", "angle": "ต้นฉบับ", "text": c1, "validation": validate_ig_dara_text(c1)},
        {"index": 2, "label": "แบบที่ 2: มุมเบื้องหลัง", "angle": "มุมที่ 2", "text": c2, "validation": validate_ig_dara_text(c2)},
        {"index": 3, "label": "แบบที่ 3: มุมอารมณ์ร่วม", "angle": "มุมที่ 3", "text": c3, "validation": validate_ig_dara_text(c3)},
    ]

    case_record = {
        "case_key": ts_slug,
        "case_id": ts_slug,
        "folder_rel": ts_slug.replace("__", "/"),
        "priority_rank": 5,
        "topic_title": topic_title.strip(),
        "archetype": "custom_upload",
        "generated_at": datetime.datetime.now(TH_TZ).isoformat(),
        "posted_status": False,
        "posted_by": "",
        "posted_at": "",
        "full_text": c1,
        "text_validation": validate_ig_dara_text(c1),
        "caption_variants": caption_variants,
        "sources_researched": [],
        "cover_url": public_url,
        "supabase_cover_url": public_url,
        "selected_cover_rel": "cover.jpg",
        "variants": [
            {"name": "cover.jpg", "label": "ปกหลัก (4:5 1080x1350)", "rel": "cover.jpg", "url": public_url, "public_url": public_url}
        ],
        "fb_posts": [],
    }
    save_case_to_supabase(case_record)
    return {"ok": True, "case": case_record}


@app.get("/api/cases/{case_key}/file/{file_path:path}")
async def api_serve_case_file(
    case_key: str,
    file_path: str,
    user: Dict[str, Any] = Depends(require_authenticated_user),
):
    if not IS_VERCEL and LOCAL_OUTPUTS_DIR.exists():
        folder_rel = case_key.replace("__", "/")
        target = (LOCAL_OUTPUTS_DIR / folder_rel / file_path).resolve()
        if target.exists() and target.is_file():
            return FileResponse(str(target))
    case_data = _get_single_case(case_key)
    if case_data and case_data.get("supabase_cover_url"):
        return RedirectResponse(url=str(case_data["supabase_cover_url"]))
    raise HTTPException(status_code=404, detail="ไม่พบไฟล์รูปภาพ")


@app.get("/api/health")
async def api_health():
    return {"ok": True, "service": "ig-dara-studio", "graph_version": "v25.0"}


# ---------------------------------------------------------------------------
# Main Dashboard HTML (`/`) — Protected by Mandatory Login
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>IG.dara Studio — ระบบคลังข่าวไวรัลและโพสต์ลงเพจ Facebook อัตโนมัติ</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Kanit:wght@400;500;600;700&family=Sarabun:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-page: #F8FAFC;
      --bg-card: #FFFFFF;
      --bg-subtle: #F1F5F9;
      --text-main: #0F172A;
      --text-secondary: #475569;
      --text-muted: #64748B;
      --border: #E2E8F0;
      --primary: #2563EB;
      --primary-hover: #1D4ED8;
      --primary-light: #EFF6FF;
      --fb-blue: #1877F2;
      --success: #059669;
      --success-bg: #ECFDF5;
      --warning: #D97706;
      --warning-bg: #FFFBEB;
      --danger: #DC2626;
      --danger-bg: #FEF2F2;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Sarabun', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-page);
      color: var(--text-main);
      line-height: 1.6;
      padding-bottom: 60px;
    }
    /* Top Navbar */
    .navbar {
      position: sticky;
      top: 0;
      z-index: 40;
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--border);
      padding: 14px 24px;
      box-shadow: 0 2px 10px rgba(15, 23, 42, 0.03);
    }
    .nav-container {
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
    }
    .brand-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-logo {
      width: 42px;
      height: 42px;
      border-radius: 12px;
      background: linear-gradient(135deg, #2563EB, #EC4899);
      color: #fff;
      font-family: 'Kanit', sans-serif;
      font-weight: 700;
      font-size: 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.25);
    }
    .brand-title {
      font-family: 'Kanit', sans-serif;
      font-size: 20px;
      font-weight: 700;
      color: var(--text-main);
      line-height: 1.2;
    }
    .brand-sub {
      font-size: 13px;
      color: var(--text-secondary);
    }
    .nav-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    .badge-mode {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: 'Kanit', sans-serif;
      font-size: 13px;
      font-weight: 600;
      padding: 6px 12px;
      border-radius: 999px;
    }
    .badge-mock {
      background: #FFFBEB;
      color: #B45309;
      border: 1px solid #FDE68A;
    }
    .badge-live {
      background: #ECFDF5;
      color: #047857;
      border: 1px solid #A7F3D0;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-family: 'Kanit', sans-serif;
      font-size: 14px;
      font-weight: 500;
      padding: 8px 14px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #FFFFFF;
      color: var(--text-main);
      cursor: pointer;
      transition: all 0.15s ease;
      text-decoration: none;
    }
    .btn:hover {
      background: var(--bg-subtle);
      border-color: #CBD5E1;
    }
    .btn-primary {
      background: var(--primary);
      color: #FFFFFF;
      border-color: var(--primary);
    }
    .btn-primary:hover {
      background: var(--primary-hover);
      border-color: var(--primary-hover);
    }
    .btn-fb {
      background: linear-gradient(135deg, #1877F2 0%, #1D4ED8 100%);
      color: #FFFFFF;
      border: none;
      font-weight: 600;
      box-shadow: 0 4px 12px rgba(24, 119, 242, 0.25);
    }
    .btn-fb:hover {
      background: linear-gradient(135deg, #166FE5 0%, #1E40AF 100%);
      transform: translateY(-1px);
    }
    /* Pages Bar & Filter Bar */
    .main-wrap {
      max-width: 1440px;
      margin: 20px auto 0;
      padding: 0 24px;
    }
    .fb-pages-strip {
      background: #FFFFFF;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px 20px;
      margin-bottom: 20px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.02);
    }
    .pages-chips {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    .page-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 5px 12px 5px 6px;
      background: #F8FAFC;
      border: 1px solid #E2E8F0;
      border-radius: 999px;
      font-size: 13.5px;
      font-weight: 600;
      color: #1E293B;
    }
    .page-chip img {
      width: 26px;
      height: 26px;
      border-radius: 50%;
      object-fit: cover;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 20px;
    }
    .filter-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .tab-btn {
      font-family: 'Kanit', sans-serif;
      font-size: 14px;
      font-weight: 500;
      padding: 8px 16px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #FFFFFF;
      color: var(--text-secondary);
      cursor: pointer;
    }
    .tab-btn.active {
      background: var(--primary);
      color: #FFFFFF;
      border-color: var(--primary);
      font-weight: 600;
    }
    .search-input {
      min-width: 280px;
      padding: 9px 14px;
      border-radius: 10px;
      border: 1px solid #CBD5E1;
      background: #FFFFFF;
      font-size: 14.5px;
      font-family: 'Sarabun', sans-serif;
    }
    /* News Grid */
    .cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(620px, 1fr));
      gap: 22px;
    }
    @media (max-width: 720px) {
      .cards-grid { grid-template-columns: 1fr; }
    }
    .news-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 4px 12px rgba(15, 23, 42, 0.03);
      display: flex;
      flex-direction: column;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .news-card:hover {
      border-color: #CBD5E1;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    }
    .card-header {
      padding: 16px 20px;
      border-bottom: 1px solid #F1F5F9;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }
    .card-title {
      font-family: 'Kanit', sans-serif;
      font-size: 17.5px;
      font-weight: 600;
      color: #0F172A;
      line-height: 1.35;
    }
    .status-pill {
      white-space: nowrap;
      font-family: 'Kanit', sans-serif;
      font-size: 12.5px;
      font-weight: 600;
      padding: 4px 11px;
      border-radius: 999px;
    }
    .status-ready {
      background: #EFF6FF;
      color: #2563EB;
      border: 1px solid #BFDBFE;
    }
    .status-posted {
      background: #ECFDF5;
      color: #059669;
      border: 1px solid #A7F3D0;
    }
    .card-body {
      display: grid;
      grid-template-columns: 270px 1fr;
      gap: 18px;
      padding: 18px 20px;
    }
    @media (max-width: 640px) {
      .card-body { grid-template-columns: 1fr; }
    }
    .cover-col {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .cover-frame {
      width: 100%;
      aspect-ratio: 4 / 5;
      background: #F1F5F9;
      border-radius: 12px;
      overflow: hidden;
      border: 1px solid var(--border);
      position: relative;
      cursor: zoom-in;
    }
    .cover-frame img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .variant-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .var-pill {
      font-family: 'Kanit', sans-serif;
      font-size: 11.5px;
      padding: 4px 8px;
      border-radius: 7px;
      border: 1px solid #CBD5E1;
      background: #F8FAFC;
      color: #334155;
      cursor: pointer;
    }
    .var-pill.active {
      background: #2563EB;
      color: #FFFFFF;
      border-color: #2563EB;
      font-weight: 600;
    }
    .text-col {
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-width: 0;
    }
    .cap-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .cap-tab {
      font-family: 'Kanit', sans-serif;
      font-size: 12.5px;
      padding: 5px 11px;
      border-radius: 8px;
      border: 1px solid #CBD5E1;
      background: #F8FAFC;
      color: #334155;
      cursor: pointer;
    }
    .cap-tab.active {
      background: #0F172A;
      color: #FFFFFF;
      border-color: #0F172A;
      font-weight: 600;
    }
    .caption-box {
      background: #F8FAFC;
      border: 1px solid #E2E8F0;
      border-radius: 12px;
      padding: 13px 15px;
      font-size: 14.5px;
      color: #1E293B;
      white-space: pre-wrap;
      max-height: 255px;
      overflow-y: auto;
      line-height: 1.65;
    }
    .fb-history-box {
      background: #F0FDF4;
      border: 1px solid #BBF7D0;
      border-radius: 10px;
      padding: 9px 12px;
      font-size: 13px;
    }
    .fb-history-title {
      font-family: 'Kanit', sans-serif;
      font-weight: 600;
      color: #166534;
      margin-bottom: 5px;
    }
    .fb-post-links {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .fb-post-link {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #FFFFFF;
      border: 1px solid #86EFAC;
      color: #15803D;
      padding: 3px 9px;
      border-radius: 999px;
      text-decoration: none;
      font-weight: 600;
      font-size: 12px;
    }
    .fb-post-link:hover {
      background: #DCFCE7;
    }
    .card-footer {
      padding: 13px 20px;
      background: #F8FAFC;
      border-top: 1px solid #E2E8F0;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    /* Modal Backdrop */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      z-index: 100;
      background: rgba(15, 23, 42, 0.55);
      backdrop-filter: blur(4px);
      align-items: center;
      justify-content: center;
      padding: 18px;
      overflow-y: auto;
    }
    .modal-backdrop.open {
      display: flex;
    }
    .modal-panel {
      background: #FFFFFF;
      border-radius: 20px;
      width: 100%;
      max-width: 860px;
      max-height: 92vh;
      overflow-y: auto;
      border: 1px solid var(--border);
      box-shadow: 0 25px 50px -12px rgba(15, 23, 42, 0.25);
      display: flex;
      flex-direction: column;
    }
    .modal-header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .modal-header h2 {
      font-family: 'Kanit', sans-serif;
      font-size: 20px;
      font-weight: 700;
    }
    .modal-body {
      padding: 22px 24px;
      overflow-y: auto;
    }
    .modal-footer {
      padding: 16px 24px;
      border-top: 1px solid var(--border);
      background: #F8FAFC;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
    }
    /* Publisher Modal Layout */
    .pub-grid {
      display: grid;
      grid-template-columns: 230px 1fr;
      gap: 20px;
    }
    @media (max-width: 680px) {
      .pub-grid { grid-template-columns: 1fr; }
    }
    .page-select-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 11px 14px;
      border: 1.5px solid #E2E8F0;
      border-radius: 12px;
      margin-bottom: 9px;
      background: #FFFFFF;
      transition: all 0.12s ease;
    }
    .page-select-row.checked {
      border-color: #2563EB;
      background: #F8FAFC;
    }
    .page-left {
      display: flex;
      align-items: center;
      gap: 10px;
      cursor: pointer;
    }
    .page-left input[type="checkbox"] {
      width: 18px;
      height: 18px;
      accent-color: #2563EB;
      cursor: pointer;
    }
    .page-left img {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      object-fit: cover;
    }
    .dup-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #FFFBEB;
      color: #B45309;
      border: 1px solid #FDE68A;
      font-size: 11.5px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 999px;
    }
    .mode-cards {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-top: 14px;
    }
    .mode-card {
      border: 1.5px solid #CBD5E1;
      border-radius: 12px;
      padding: 12px 14px;
      cursor: pointer;
      background: #FFFFFF;
    }
    .mode-card.active {
      border-color: #2563EB;
      background: #EFF6FF;
    }
    .mode-card-title {
      font-family: 'Kanit', sans-serif;
      font-weight: 600;
      font-size: 14.5px;
      color: #0F172A;
    }
    .mode-card-desc {
      font-size: 12.5px;
      color: #475569;
      margin-top: 3px;
    }
    .confirm-summary {
      background: #F8FAFC;
      border: 1.5px solid #BFDBFE;
      border-radius: 14px;
      padding: 18px;
    }
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 200;
      background: #0F172A;
      color: #FFFFFF;
      padding: 12px 18px;
      border-radius: 12px;
      font-family: 'Kanit', sans-serif;
      font-size: 14px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.25);
      display: none;
    }
  </style>
</head>
<body>
  <!-- Sticky Bright Navbar -->
  <header class="navbar">
    <div class="nav-container">
      <div class="brand-group">
        <div class="brand-logo">IG</div>
        <div>
          <div class="brand-title">IG.dara Studio · ระบบจัดการข่าวและโพสต์เพจ Facebook</div>
          <div class="brand-sub">เลือกภาพปก 4:5 (1080×1350) + เลือกชุดข้อความแยกตามเพจ + โพสต์ทันทีหรือตั้งเวลาอัตโนมัติ (Graph API v25.0)</div>
        </div>
      </div>
      <div class="nav-actions">
        <span id="fbModeBadge" class="badge-mode badge-mock">🧪 โหมดทดสอบ FB_MOCK=1 (ไม่โพสต์จริง)</span>
        <button class="btn" onclick="refreshFbPages()">🔄 รีเฟรชรายชื่อเพจ</button>
        <button class="btn" onclick="openNewPostModal()">➕ เพิ่มข่าวใหม่</button>
        <button class="btn" id="settingsBtn" onclick="openSettingsModal()" style="display:none;">⚙️ ตั้งค่าโทเคน/ระบบ</button>
        <span id="userBadge" style="font-size:13.5px;font-weight:600;color:#334155;padding:0 6px;"></span>
        <button class="btn" onclick="logoutUser()">🚪 ออกจากระบบ</button>
      </div>
    </div>
  </header>

  <main class="main-wrap">
    <!-- Connected Facebook Pages Bar -->
    <section class="fb-pages-strip">
      <div>
        <div style="font-family:'Kanit',sans-serif;font-weight:600;font-size:14.5px;color:#0F172A;margin-bottom:6px;">
          📡 เพจ Facebook ที่เชื่อมต่อในระบบ (<span id="fbPagesCount">0</span> เพจ · System User: ADSAFF Publisher)
        </div>
        <div id="fbPagesStripList" class="pages-chips">
          <span style="font-size:13px;color:#64748B;">กำลังโหลดรายชื่อเพจ...</span>
        </div>
      </div>
      <div style="text-align:right;font-size:12.5px;color:#64748B;">
        <div>อัปเดตล่าสุด: <span id="fbSyncedAt">-</span></div>
        <div id="fbWarningText" style="color:#D97706;font-weight:600;margin-top:2px;"></div>
      </div>
    </section>

    <!-- Filter & Search Toolbar -->
    <section class="toolbar">
      <div class="filter-tabs">
        <button class="tab-btn active" id="tab-all" onclick="setFilter('all')">ทั้งหมด (<span id="cnt-all">0</span>)</button>
        <button class="tab-btn" id="tab-ready" onclick="setFilter('ready')">รอเลือกโพสต์ (<span id="cnt-ready">0</span>)</button>
        <button class="tab-btn" id="tab-posted" onclick="setFilter('posted')">โพสต์แล้ว ✅ (<span id="cnt-posted">0</span>)</button>
      </div>
      <input type="search" id="searchInput" class="search-input" placeholder="🔍 ค้นหาชื่อดารา / หัวข้อข่าว / เนื้อหา..." oninput="renderCards()" />
    </section>

    <!-- News Cards Grid -->
    <section id="cardsGrid" class="cards-grid"></section>
  </main>

  <!-- Multi-Page Facebook Publish Modal -->
  <div id="publishModal" class="modal-backdrop">
    <div class="modal-panel">
      <div class="modal-header">
        <h2 id="pubModalTitle">📲 โพสต์ข่าวลงเพจ Facebook (เลือกเพจ + เลือกชุดข้อความแยกเพจ)</h2>
        <button class="btn" onclick="closePublishModal()">✕ ปิด</button>
      </div>
      <div class="modal-body">
        <!-- Step 1: Configuration View -->
        <div id="pubStepConfig">
          <div class="pub-grid">
            <!-- Left: Selected 4:5 Cover Preview & Switcher -->
            <div>
              <div style="font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:6px;">
                1) ภาพปก 4:5 (1080×1350) ที่เลือก:
              </div>
              <div class="cover-frame" style="cursor:default;">
                <img id="pubCoverImg" src="" alt="Selected Cover" />
              </div>
              <div style="font-size:12px;color:#475569;margin-top:6px;">สลับแบบภาพปกได้ทันที:</div>
              <div id="pubCoverVariants" class="variant-pills" style="margin-top:6px;"></div>
            </div>

            <!-- Right: Page Selection + Per-Page Caption Variant + Timing Mode -->
            <div>
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <div style="font-family:'Kanit',sans-serif;font-weight:600;font-size:14.5px;">
                  2) เลือกเพจและชุดข้อความของแต่ละเพจ:
                </div>
                <div style="display:flex;gap:6px;">
                  <button type="button" class="btn" style="padding:4px 10px;font-size:12px;" onclick="toggleAllModalPages(true)">☑️ เลือกทั้ง 6 เพจ</button>
                  <button type="button" class="btn" style="padding:4px 10px;font-size:12px;" onclick="distributeCaptionsAcrossPages()">🎲 สลับข้อความไม่ให้ซ้ำกัน</button>
                  <button type="button" class="btn" style="padding:4px 10px;font-size:12px;" onclick="toggleAllModalPages(false)">ล้าง</button>
                </div>
              </div>

              <div id="pubPagesList"></div>

              <!-- Caption Preview Box inside Modal -->
              <div style="margin-top:12px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:10px 12px;">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;">
                  <span style="font-family:'Kanit',sans-serif;font-size:12.5px;font-weight:600;color:#334155;">👁️ ตัวอย่างข้อความที่จะใช้โพสต์ (คลิกดูแต่ละแบบ):</span>
                  <div id="pubPreviewCapTabs" style="display:flex;gap:4px;"></div>
                </div>
                <div id="pubPreviewCapText" style="font-size:13px;color:#1E293B;white-space:pre-wrap;max-height:110px;overflow-y:auto;"></div>
              </div>

              <!-- Timing Mode Selection -->
              <div style="font-family:'Kanit',sans-serif;font-weight:600;font-size:14.5px;margin-top:14px;">
                3) เลือกจังหวะการโพสต์ (ไม่มีลิงก์ภายนอก):
              </div>
              <div class="mode-cards">
                <div id="modeCardImmediate" class="mode-card active" onclick="selectPubMode('immediate')">
                  <div class="mode-card-title">🚀 โพสต์เลยทันที (Immediate)</div>
                  <div class="mode-card-desc">ยิง POST /{page_id}/photos (published=true) ลงหน้าเพจที่เลือกทันที</div>
                </div>
                <div id="modeCardSchedule" class="mode-card" onclick="selectPubMode('auto_schedule')">
                  <div class="mode-card-title">⏰ ตั้งเวลาอัตโนมัติ (ห่าง 20–30 นาที/เพจ)</div>
                  <div class="mode-card-desc">ตั้งเวลาบน Facebook โดยตรง (published=false + scheduled_publish_time) สุ่มห่างเพจละ 20–30 นาทีต่อจากคิวล่าสุด</div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Step 2: Mandatory Confirmation View -->
        <div id="pubStepConfirm" style="display:none;">
          <div class="confirm-summary">
            <h3 style="font-family:'Kanit',sans-serif;font-size:18px;color:#1E3A8A;margin-bottom:10px;">
              🛡️ ยืนยันความถูกต้องก่อนสั่งโพสต์ลงเพจ Facebook
            </h3>
            <div id="pubConfirmWarning" style="display:none;background:#FFFBEB;border:1px solid #F59E0B;color:#B45309;padding:10px 14px;border-radius:10px;margin-bottom:12px;font-weight:600;font-size:13.5px;"></div>
            <div id="pubConfirmDetails" style="font-size:14px;color:#1E293B;line-height:1.65;"></div>
          </div>
        </div>

        <!-- Step 3: Publish Result View -->
        <div id="pubStepResult" style="display:none;">
          <div id="pubResultBox"></div>
        </div>
      </div>

      <div class="modal-footer" id="pubModalFooter">
        <button type="button" class="btn" id="pubBackBtn" style="display:none;" onclick="backToPubConfig()">⬅️ ย้อนกลับไปแก้ไข</button>
        <button type="button" class="btn" onclick="closePublishModal()">ยกเลิก</button>
        <button type="button" class="btn btn-fb" id="pubNextBtn" onclick="proceedToConfirmStep()">ตรวจสอบและยืนยันการโพสต์ →</button>
        <button type="button" class="btn btn-fb" id="pubExecuteBtn" style="display:none;background:#059669;" onclick="executeFacebookPublish(false)">✅ ยืนยันสั่งโพสต์ลงเพจทันที</button>
      </div>
    </div>
  </div>

  <!-- Admin Settings Modal (Owner Only) -->
  <div id="settingsModal" class="modal-backdrop">
    <div class="modal-panel" style="max-width:560px;">
      <div class="modal-header">
        <h2>⚙️ ตั้งค่าการเชื่อมต่อ Facebook & ความปลอดภัย (เฉพาะเจ้าของ)</h2>
        <button class="btn" onclick="closeSettingsModal()">✕ ปิด</button>
      </div>
      <div class="modal-body">
        <div style="margin-bottom:16px;padding:12px 14px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:12px;font-size:13.5px;color:#1E3A8A;">
          🔒 <strong>ความปลอดภัยระดับสูงสุด:</strong> โทเคน <code>FB_SYSTEM_TOKEN</code> (ADSAFF Publisher) และ <code>FB_APP_SECRET</code> จะถูกเข้ารหัสและเก็บเฉพาะฝั่งเซิร์ฟเวอร์เท่านั้น ไม่ส่งกลับมายังเบราว์เซอร์และไม่บันทึกลง Git
        </div>
        <div style="margin-bottom:14px;">
          <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:6px;">
            โหมดการทำงานของระบบโพสต์ Facebook (`FB_MOCK`)
          </label>
          <select id="settingFbMock" style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;font-size:14.5px;">
            <option value="1">🧪 โหมดทดสอบ (FB_MOCK=1) — จำลองการโพสต์ ไม่ยิง Facebook จริง (ปลอดภัยระหว่างพัฒนา)</option>
            <option value="0">🟢 โหมดใช้งานจริง (FB_MOCK=0) — ยิง Graph API v25.0 ลงเพจจริง</option>
          </select>
        </div>
        <div style="margin-bottom:14px;">
          <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:6px;">
            อัปเดต FB_SYSTEM_TOKEN (เว้นว่างไว้หากใช้ค่าเดิมในเซิร์ฟเวอร์)
          </label>
          <input type="password" id="settingSysToken" placeholder="วาง System User Token (ADSAFF Publisher)..." style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;font-size:14px;" />
        </div>
        <div style="margin-bottom:14px;">
          <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:6px;">
            อัปเดต FB_APP_SECRET สำหรับคำนวณ `appsecret_proof` (เว้นว่างไว้หากใช้ค่าเดิม)
          </label>
          <input type="password" id="settingAppSecret" placeholder="วาง App Secret (HMAC-SHA256)..." style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;font-size:14px;" />
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" onclick="closeSettingsModal()">ยกเลิก</button>
        <button class="btn btn-primary" onclick="saveServerSettings()">💾 บันทึกการตั้งค่าฝั่งเซิร์ฟเวอร์</button>
      </div>
    </div>
  </div>

  <!-- Create New Post Modal -->
  <div id="newPostModal" class="modal-backdrop">
    <div class="modal-panel" style="max-width:640px;">
      <div class="modal-header">
        <h2>➕ เพิ่มการ์ดข่าวใหม่เข้าคลัง IG.dara Studio</h2>
        <button class="btn" onclick="closeNewPostModal()">✕ ปิด</button>
      </div>
      <form onsubmit="submitNewPost(event)">
        <div class="modal-body">
          <div style="margin-bottom:12px;">
            <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:4px;">หัวข้อข่าว / ชื่อดารา</label>
            <input type="text" id="newTopicTitle" required placeholder="เช่น ณเดชน์ คูกิมิยะ เปิดใจดูแลพ่อบุญธรรม..." style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;" />
          </div>
          <div style="margin-bottom:12px;">
            <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:4px;">อัปโหลดภาพปก 4:5 (ระบบปรับขนาด 1080×1350 อัตโนมัติ)</label>
            <input type="file" id="newCoverFile" accept="image/*" required style="width:100%;padding:8px;border-radius:10px;border:1px solid #CBD5E1;" />
          </div>
          <div style="margin-bottom:12px;">
            <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:4px;">ข้อความแบบที่ 1 (3 ย่อหน้า สไตล์รวมไอจีดารา)</label>
            <textarea id="newCap1" rows="5" required placeholder="วางเนื้อหา 3 ย่อหน้า แบบที่ 1..." style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;font-family:'Sarabun',sans-serif;"></textarea>
          </div>
          <div style="margin-bottom:12px;">
            <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:4px;">ข้อความแบบที่ 2 (สำหรับสลับลงเพจอื่น - เว้นว่างให้ระบบสร้างให้อัตโนมัติได้)</label>
            <textarea id="newCap2" rows="3" placeholder="วางเนื้อหาแบบที่ 2 (ถ้ามี)..." style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;font-family:'Sarabun',sans-serif;"></textarea>
          </div>
          <div style="margin-bottom:4px;">
            <label style="display:block;font-family:'Kanit',sans-serif;font-weight:600;font-size:14px;margin-bottom:4px;">ข้อความแบบที่ 3 (สำหรับสลับลงเพจอื่น - เว้นว่างได้)</label>
            <textarea id="newCap3" rows="3" placeholder="วางเนื้อหาแบบที่ 3 (ถ้ามี)..." style="width:100%;padding:10px;border-radius:10px;border:1px solid #CBD5E1;font-family:'Sarabun',sans-serif;"></textarea>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn" onclick="closeNewPostModal()">ยกเลิก</button>
          <button type="submit" id="newPostSubmitBtn" class="btn btn-primary">✨ บันทึกการ์ดข่าวขึ้นระบบ</button>
        </div>
      </form>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let ALL_CASES = [];
    let FB_PAGES = [];
    let FB_STATUS = { fb_mock: true, graph_version: 'v25.0' };
    let CURRENT_USER = null;
    let ACTIVE_FILTER = 'all';

    // Per-card UI state: active caption tab & active cover variant
    const CARD_CAP_INDEX = {};
    const CARD_COVER_REL = {};

    // Active Publish Modal State
    let PUB_CASE_KEY = null;
    let PUB_MODE = 'immediate';
    let PUB_COVER_REL = 'cover.jpg';
    let PUB_COVER_URL = '';

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.display = 'block';
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => { t.style.display = 'none'; }, 3600);
    }

    function escapeHtml(str) {
      return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    async function initApp() {
      await Promise.all([loadCases(), loadFbPages(false)]);
    }

    async function loadCases() {
      const res = await fetch('/api/cases');
      if (res.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await res.json();
      ALL_CASES = data.cases || [];
      CURRENT_USER = data.user || {};
      FB_STATUS = data.fb_status || FB_STATUS;

      document.getElementById('userBadge').textContent = `👤 ${CURRENT_USER.display_name || 'ทีมงาน'}`;
      if (CURRENT_USER.role === 'owner') {
        document.getElementById('settingsBtn').style.display = 'inline-flex';
      }
      updateFbModeBadge();
      renderCards();
    }

    function updateFbModeBadge() {
      const badge = document.getElementById('fbModeBadge');
      if (FB_STATUS.fb_mock) {
        badge.className = 'badge-mode badge-mock';
        badge.textContent = '🧪 โหมดทดสอบ FB_MOCK=1 (ไม่โพสต์จริง)';
      } else {
        badge.className = 'badge-mode badge-live';
        badge.textContent = '🟢 โหมดโพสต์จริง Graph API v25.0';
      }
    }

    async function loadFbPages(refresh = false) {
      const url = refresh ? '/api/fb/pages/refresh' : '/api/fb/pages';
      const method = refresh ? 'POST' : 'GET';
      const res = await fetch(url, { method });
      if (!res.ok) return;
      const data = await res.json();
      FB_PAGES = data.pages || [];
      FB_STATUS.fb_mock = data.fb_mock;
      updateFbModeBadge();

      document.getElementById('fbPagesCount').textContent = FB_PAGES.length;
      document.getElementById('fbSyncedAt').textContent = data.synced_at || '-';
      document.getElementById('fbWarningText').textContent = data.warning || '';

      const strip = document.getElementById('fbPagesStripList');
      if (!FB_PAGES.length) {
        strip.innerHTML = '<span style="color:#DC2626;font-size:13px;">ไม่พบเพจที่เชื่อมต่อ กรุณาตรวจสอบ FB_SYSTEM_TOKEN</span>';
        return;
      }
      strip.innerHTML = FB_PAGES.map(p => `
        <div class="page-chip" title="คิวล่าสุด: ${escapeHtml(p.last_scheduled_str)}">
          <img src="${escapeHtml(p.picture_url)}" alt="${escapeHtml(p.name)}" />
          <span>${escapeHtml(p.name)}</span>
        </div>
      `).join('');

      if (refresh) {
        showToast(`✅ รีเฟรชรายชื่อเพจสำเร็จ (${FB_PAGES.length} เพจ)`);
      }
    }

    async function refreshFbPages() {
      showToast('🔄 กำลังดึงรายชื่อเพจล่าสุดจาก Facebook Graph API v25.0...');
      await loadFbPages(true);
    }

    function setFilter(f) {
      ACTIVE_FILTER = f;
      ['all', 'ready', 'posted'].forEach(k => {
        document.getElementById(`tab-${k}`).classList.toggle('active', k === f);
      });
      renderCards();
    }

    function getCaseByKey(ck) {
      return ALL_CASES.find(c => c.case_key === ck);
    }

    function getCoverOptionsForCase(c) {
      const mainUrl = c.supabase_cover_url || c.cover_url || '';
      const opts = [{ rel: 'cover.jpg', label: 'ปกหลัก', url: mainUrl }];
      const seen = new Set(['cover.jpg']);
      (c.variants || []).forEach((v, idx) => {
        const rel = v.rel || v.name;
        if (!seen.has(rel)) {
          seen.add(rel);
          opts.push({
            rel,
            label: v.label || `แบบที่ ${idx + 1}`,
            shortLabel: `ปกแบบ ${idx + 1}`,
            url: v.public_url || v.url || mainUrl
          });
        }
      });
      return opts;
    }

    function renderCards() {
      const q = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      let readyCount = 0;
      let postedCount = 0;

      ALL_CASES.forEach(c => {
        if (c.posted_status) postedCount++;
        else readyCount++;
      });

      document.getElementById('cnt-all').textContent = ALL_CASES.length;
      document.getElementById('cnt-ready').textContent = readyCount;
      document.getElementById('cnt-posted').textContent = postedCount;

      const filtered = ALL_CASES.filter(c => {
        if (ACTIVE_FILTER === 'ready' && c.posted_status) return false;
        if (ACTIVE_FILTER === 'posted' && !c.posted_status) return false;
        if (q) {
          const hay = `${c.topic_title} ${c.full_text} ${c.case_key}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });

      const grid = document.getElementById('cardsGrid');
      if (!filtered.length) {
        grid.innerHTML = `<div style="grid-column:1/-1;background:#fff;padding:40px;border-radius:16px;text-align:center;color:#64748B;">ไม่พบการ์ดข่าวในหมวดหมู่นี้</div>`;
        return;
      }

      grid.innerHTML = filtered.map(c => {
        const ck = c.case_key;
        const coverOpts = getCoverOptionsForCase(c);
        const selectedRel = CARD_COVER_REL[ck] || c.selected_cover_rel || 'cover.jpg';
        const activeCoverObj = coverOpts.find(o => o.rel === selectedRel) || coverOpts[0];

        const capVars = c.caption_variants && c.caption_variants.length
          ? c.caption_variants
          : [{ index: 1, label: 'แบบที่ 1', text: c.full_text || '' }];
        const activeCapIdx = CARD_CAP_INDEX[ck] || 1;
        const activeCapObj = capVars.find(cv => cv.index === activeCapIdx) || capVars[0];

        const fbPosts = c.fb_posts || [];
        const historyHtml = fbPosts.length ? `
          <div class="fb-history-box">
            <div class="fb-history-title">✅ ประวัติการโพสต์ลงเพจ (${fbPosts.length} รายการ):</div>
            <div class="fb-post-links">
              ${fbPosts.map(fp => `
                <a class="fb-post-link" href="${escapeHtml(fp.post_url || 'https://www.facebook.com/' + fp.post_id)}" target="_blank" rel="noopener">
                  ${fp.status === 'scheduled' ? '⏰' : '🟢'} ${escapeHtml(fp.page_name)}
                  (${escapeHtml(fp.caption_label || 'แบบ ' + fp.caption_index)})
                  ${fp.scheduled_time_str ? '· ' + escapeHtml(fp.scheduled_time_str) : ''}
                  ${fp.is_mock ? '[MOCK]' : ''} ↗
                </a>
              `).join('')}
            </div>
          </div>
        ` : '';

        return `
          <article class="news-card" id="card-${escapeHtml(ck)}">
            <div class="card-header">
              <div>
                <div style="font-size:12px;color:#64748B;font-weight:600;margin-bottom:2px;">📁 ${escapeHtml(c.folder_rel || ck)}</div>
                <div class="card-title">${escapeHtml(c.topic_title)}</div>
              </div>
              <span class="status-pill ${c.posted_status ? 'status-posted' : 'status-ready'}">
                ${c.posted_status ? 'โพสต์แล้ว ✅' : 'พร้อมโพสต์'}
              </span>
            </div>

            <div class="card-body">
              <!-- Left Column: 4:5 Cover Image + Variant Selector -->
              <div class="cover-col">
                <div class="cover-frame" onclick="window.open('${escapeHtml(activeCoverObj.url)}', '_blank')">
                  <img id="cover-img-${escapeHtml(ck)}" src="${escapeHtml(activeCoverObj.url)}" alt="${escapeHtml(c.topic_title)}" loading="lazy" />
                </div>
                <div style="font-size:12px;color:#475569;font-weight:600;">🎨 คลิกเลือกภาพปกที่ชอบ (${coverOpts.length} แบบ):</div>
                <div class="variant-pills">
                  ${coverOpts.map((o, idx) => `
                    <button type="button" class="var-pill ${o.rel === activeCoverObj.rel ? 'active' : ''}"
                      onclick="selectCardCover('${escapeHtml(ck)}', '${escapeHtml(o.rel)}')"
                      title="${escapeHtml(o.label)}">
                      ${idx === 0 ? '⭐ ปกหลัก' : (o.shortLabel || 'แบบ ' + idx)}
                    </button>
                  `).join('')}
                </div>
              </div>

              <!-- Right Column: 3-Paragraph Captions + FB History -->
              <div class="text-col">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;">
                  <div class="cap-tabs">
                    ${capVars.map(cv => `
                      <button type="button" class="cap-tab ${cv.index === activeCapObj.index ? 'active' : ''}"
                        onclick="selectCardCaption('${escapeHtml(ck)}', ${cv.index})">
                        📝 แบบที่ ${cv.index}
                      </button>
                    `).join('')}
                  </div>
                  <span style="font-size:12px;color:#059669;font-weight:600;">
                    3 ย่อหน้า · ${(activeCapObj.text || '').length} ตัวอักษร
                  </span>
                </div>

                <div class="caption-box" id="cap-box-${escapeHtml(ck)}">${escapeHtml(activeCapObj.text)}</div>

                ${historyHtml}
              </div>
            </div>

            <div class="card-footer">
              <div style="display:flex;flex-wrap:wrap;gap:8px;">
                <button type="button" class="btn" onclick="copyCardCaption('${escapeHtml(ck)}')">📋 คัดลอกข้อความ</button>
                <a class="btn" href="${escapeHtml(activeCoverObj.url)}" target="_blank" download="cover_${escapeHtml(ck)}.jpg">🖼️ ดาวน์โหลดภาพปก</a>
              </div>
              <div style="display:flex;flex-wrap:wrap;gap:8px;">
                <button type="button" class="btn btn-fb" onclick="openPublishModal('${escapeHtml(ck)}')">
                  📲 โพสต์ลงเพจ Facebook
                </button>
              </div>
            </div>
          </article>
        `;
      }).join('');
    }

    async function selectCardCover(ck, rel) {
      CARD_COVER_REL[ck] = rel;
      const c = getCaseByKey(ck);
      if (c) c.selected_cover_rel = rel;
      renderCards();
      try {
        await fetch(`/api/cases/${encodeURIComponent(ck)}/select-cover`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cover_rel: rel })
        });
        showToast('✅ เลือกภาพปกนี้สำหรับโพสต์ลงเพจแล้ว');
      } catch (e) {}
    }

    function selectCardCaption(ck, capIdx) {
      CARD_CAP_INDEX[ck] = capIdx;
      renderCards();
    }

    async function copyCardCaption(ck) {
      const c = getCaseByKey(ck);
      if (!c) return;
      const capVars = c.caption_variants || [];
      const activeIdx = CARD_CAP_INDEX[ck] || 1;
      const chosen = capVars.find(cv => cv.index === activeIdx) || capVars[0] || { text: c.full_text };
      await navigator.clipboard.writeText(chosen.text || c.full_text || '');
      showToast(`📋 คัดลอกข้อความ (แบบที่ ${activeIdx}) เรียบร้อยแล้ว`);
    }

    // -----------------------------------------------------------------------
    // Multi-Page Facebook Publisher Modal Workflow
    // -----------------------------------------------------------------------

    function openPublishModal(ck) {
      const c = getCaseByKey(ck);
      if (!c) return;
      PUB_CASE_KEY = ck;
      PUB_MODE = 'immediate';

      const coverOpts = getCoverOptionsForCase(c);
      PUB_COVER_REL = CARD_COVER_REL[ck] || c.selected_cover_rel || 'cover.jpg';
      const chosenCover = coverOpts.find(o => o.rel === PUB_COVER_REL) || coverOpts[0];
      PUB_COVER_URL = chosenCover.url;

      document.getElementById('pubModalTitle').textContent = `📲 โพสต์ลงเพจ: ${c.topic_title}`;
      document.getElementById('pubStepConfig').style.display = 'block';
      document.getElementById('pubStepConfirm').style.display = 'none';
      document.getElementById('pubStepResult').style.display = 'none';
      document.getElementById('pubBackBtn').style.display = 'none';
      document.getElementById('pubNextBtn').style.display = 'inline-flex';
      document.getElementById('pubExecuteBtn').style.display = 'none';

      renderModalCoverSection(c);
      renderModalPagesList(c);
      renderModalCaptionPreview(c, CARD_CAP_INDEX[ck] || 1);
      selectPubMode('immediate');

      document.getElementById('publishModal').classList.add('open');
    }

    function closePublishModal() {
      document.getElementById('publishModal').classList.remove('open');
    }

    function renderModalCoverSection(c) {
      const coverOpts = getCoverOptionsForCase(c);
      const chosen = coverOpts.find(o => o.rel === PUB_COVER_REL) || coverOpts[0];
      PUB_COVER_URL = chosen.url;
      document.getElementById('pubCoverImg').src = chosen.url;

      document.getElementById('pubCoverVariants').innerHTML = coverOpts.map((o, idx) => `
        <button type="button" class="var-pill ${o.rel === PUB_COVER_REL ? 'active' : ''}"
          onclick="switchModalCover('${escapeHtml(o.rel)}')">
          ${idx === 0 ? '⭐ ปกหลัก' : (o.shortLabel || 'แบบ ' + idx)}
        </button>
      `).join('');
    }

    function switchModalCover(rel) {
      const c = getCaseByKey(PUB_CASE_KEY);
      if (!c) return;
      PUB_COVER_REL = rel;
      CARD_COVER_REL[PUB_CASE_KEY] = rel;
      c.selected_cover_rel = rel;
      renderModalCoverSection(c);
      renderCards();
    }

    function renderModalPagesList(c) {
      const capVars = c.caption_variants && c.caption_variants.length
        ? c.caption_variants
        : [{ index: 1, label: 'แบบที่ 1', text: c.full_text || '' }];

      const postedMap = {};
      (c.fb_posts || []).forEach(fp => {
        if (fp.status === 'published' || fp.status === 'scheduled') {
          postedMap[String(fp.page_id)] = fp;
        }
      });

      const container = document.getElementById('pubPagesList');
      container.innerHTML = FB_PAGES.map((p, idx) => {
        const prev = postedMap[String(p.id)];
        // Default caption rotates 1, 2, 3 across pages so pages don't use identical text!
        const defaultCapIdx = capVars[idx % capVars.length].index;
        return `
          <div class="page-select-row ${!prev ? 'checked' : ''}" id="prow-${escapeHtml(p.id)}">
            <label class="page-left">
              <input type="checkbox" class="pub-page-cb" data-page-id="${escapeHtml(p.id)}" data-page-name="${escapeHtml(p.name)}"
                ${!prev ? 'checked' : ''} onchange="onPageCheckboxChange('${escapeHtml(p.id)}', this.checked)" />
              <img src="${escapeHtml(p.picture_url)}" alt="${escapeHtml(p.name)}" />
              <div>
                <div style="font-family:'Kanit',sans-serif;font-weight:600;font-size:14.5px;color:#0F172A;">
                  ${escapeHtml(p.name)}
                </div>
                <div style="font-size:11.5px;color:#64748B;">
                  คิวตั้งเวลาล่าสุด: ${escapeHtml(p.last_scheduled_str || 'พร้อมตั้งเวลา')}
                </div>
                ${prev ? `
                  <div class="dup-badge" style="margin-top:3px;">
                    ⚠️ เคยลงเพจนี้แล้ว (${escapeHtml(prev.status === 'scheduled' ? 'ตั้งเวลา' : 'โพสต์แล้ว')})
                    <a href="${escapeHtml(prev.post_url)}" target="_blank" style="color:#92400E;text-decoration:underline;margin-left:4px;">ดูโพสต์ ↗</a>
                  </div>
                ` : ''}
              </div>
            </label>

            <div style="display:flex;align-items:center;gap:6px;">
              <span style="font-size:12px;color:#475569;font-weight:600;">ชุดข้อความ:</span>
              <select class="pub-cap-select" id="pcap-${escapeHtml(p.id)}"
                 onchange="renderModalCaptionPreviewByKey(this.value)"
                 style="padding:6px 10px;border-radius:8px;border:1px solid #CBD5E1;font-family:'Kanit',sans-serif;font-size:13px;background:#fff;">
                ${capVars.map(cv => `
                  <option value="${cv.index}" ${cv.index === defaultCapIdx ? 'selected' : ''}>
                    📝 แบบที่ ${cv.index} (${(cv.text || '').length} ตัวอักษร)
                  </option>
                `).join('')}
              </select>
            </div>
          </div>
        `;
      }).join('');
    }

    function onPageCheckboxChange(pid, checked) {
      const row = document.getElementById(`prow-${pid}`);
      if (row) row.classList.toggle('checked', checked);
    }

    function toggleAllModalPages(checkAll) {
      document.querySelectorAll('.pub-page-cb').forEach(cb => {
        cb.checked = checkAll;
        onPageCheckboxChange(cb.dataset.pageId, checkAll);
      });
    }

    function distributeCaptionsAcrossPages() {
      const c = getCaseByKey(PUB_CASE_KEY);
      if (!c) return;
      const capVars = c.caption_variants || [{ index: 1 }];
      let counter = 0;
      document.querySelectorAll('.pub-page-cb').forEach(cb => {
        if (cb.checked) {
          const sel = document.getElementById(`pcap-${cb.dataset.pageId}`);
          if (sel) {
            sel.value = String(capVars[counter % capVars.length].index);
            counter++;
          }
        }
      });
      showToast('🎲 สลับแบบข้อความให้แต่ละเพจไม่ซ้ำกันเรียบร้อยแล้ว');
    }

    function renderModalCaptionPreviewByKey(capIdxStr) {
      const c = getCaseByKey(PUB_CASE_KEY);
      if (c) renderModalCaptionPreview(c, parseInt(capIdxStr, 10) || 1);
    }

    function renderModalCaptionPreview(c, activeIdx) {
      const capVars = c.caption_variants && c.caption_variants.length
        ? c.caption_variants
        : [{ index: 1, label: 'แบบที่ 1', text: c.full_text || '' }];
      const chosen = capVars.find(cv => cv.index === activeIdx) || capVars[0];

      document.getElementById('pubPreviewCapTabs').innerHTML = capVars.map(cv => `
        <button type="button" class="var-pill ${cv.index === chosen.index ? 'active' : ''}"
          onclick="renderModalCaptionPreviewByKey(${cv.index})">
          แบบที่ ${cv.index}
        </button>
      `).join('');
      document.getElementById('pubPreviewCapText').textContent = chosen.text || '';
    }

    function selectPubMode(mode) {
      PUB_MODE = mode;
      document.getElementById('modeCardImmediate').classList.toggle('active', mode === 'immediate');
      document.getElementById('modeCardSchedule').classList.toggle('active', mode === 'auto_schedule');
    }

    function getSelectedTargetsFromModal() {
      const targets = [];
      document.querySelectorAll('.pub-page-cb').forEach(cb => {
        if (cb.checked) {
          const pid = cb.dataset.pageId;
          const pname = cb.dataset.pageName;
          const sel = document.getElementById(`pcap-${pid}`);
          const capIdx = sel ? parseInt(sel.value, 10) || 1 : 1;
          targets.push({ page_id: pid, page_name: pname, caption_index: capIdx });
        }
      });
      return targets;
    }

    function proceedToConfirmStep() {
      const c = getCaseByKey(PUB_CASE_KEY);
      if (!c) return;
      const targets = getSelectedTargetsFromModal();
      if (!targets.length) {
        alert('กรุณาติ๊กเลือกเพจอย่างน้อย 1 เพจก่อนดำเนินการต่อ');
        return;
      }

      // Check if any selected page was already posted for this news card
      const postedMap = {};
      (c.fb_posts || []).forEach(fp => {
        if (fp.status === 'published' || fp.status === 'scheduled') {
          postedMap[String(fp.page_id)] = fp;
        }
      });
      const dupPages = targets.filter(t => postedMap[String(t.page_id)]);
      const warnBox = document.getElementById('pubConfirmWarning');
      if (dupPages.length) {
        warnBox.style.display = 'block';
        warnBox.innerHTML = `⚠️ <strong>คำเตือนป้องกันการลงซ้ำ:</strong> ข่าวนี้เคยโพสต์หรือตั้งเวลาลงเพจ <u>${dupPages.map(d => escapeHtml(d.page_name)).join(', ')}</u> ไปแล้ว หากกดยืนยันด้านล่าง ระบบจะโพสต์ซ้ำลงเพจดังกล่าวอีกครั้ง`;
      } else {
        warnBox.style.display = 'none';
      }

      const modeLabel = PUB_MODE === 'auto_schedule'
        ? '⏰ ตั้งเวลาอัตโนมัติ (สุ่มห่างเพจละ 20–30 นาที ต่อจากโพสต์ล่าสุดของเพจนั้น · Facebook โพสต์เองโดยไม่ต้องใช้ cron)'
        : '🚀 โพสต์ทันที (published=true)';

      const detailsHtml = `
        <div style="margin-bottom:8px;"><strong>ข่าว:</strong> ${escapeHtml(c.topic_title)}</div>
        <div style="margin-bottom:8px;"><strong>ภาพปก 4:5 (1080×1350) ที่เลือก:</strong> <code>${escapeHtml(PUB_COVER_REL)}</code></div>
        <div style="margin-bottom:8px;"><strong>โหมดการโพสต์:</strong> ${modeLabel}</div>
        <div style="margin-bottom:8px;"><strong>สถานะระบบ:</strong> ${FB_STATUS.fb_mock ? '<span style="color:#B45309;font-weight:700;">🧪 โหมดทดสอบ FB_MOCK=1 (จำลองผลลัพธ์ ไม่ยิง Facebook จริง)</span>' : '<span style="color:#047857;font-weight:700;">🟢 โหมดโพสต์จริง Graph API v25.0</span>'}</div>
        <div style="margin-top:12px;font-weight:700;">รายชื่อเพจและชุดข้อความที่เลือก (${targets.length} เพจ):</div>
        <ul style="margin-top:6px;padding-left:20px;">
          ${targets.map(t => `
            <li style="margin-bottom:4px;">
              เพจ <strong>${escapeHtml(t.page_name)}</strong> → ใช้ข้อความ <strong>แบบที่ ${t.caption_index}</strong>
              ${postedMap[String(t.page_id)] ? '<span style="color:#D97706;font-weight:600;"> (เคยลงเพจนี้แล้ว)</span>' : ''}
            </li>
          `).join('')}
        </ul>
      `;

      document.getElementById('pubConfirmDetails').innerHTML = detailsHtml;
      document.getElementById('pubStepConfig').style.display = 'none';
      document.getElementById('pubStepConfirm').style.display = 'block';
      document.getElementById('pubStepResult').style.display = 'none';
      document.getElementById('pubBackBtn').style.display = 'inline-flex';
      document.getElementById('pubNextBtn').style.display = 'none';
      document.getElementById('pubExecuteBtn').style.display = 'inline-flex';
    }

    function backToPubConfig() {
      document.getElementById('pubStepConfig').style.display = 'block';
      document.getElementById('pubStepConfirm').style.display = 'none';
      document.getElementById('pubStepResult').style.display = 'none';
      document.getElementById('pubBackBtn').style.display = 'none';
      document.getElementById('pubNextBtn').style.display = 'inline-flex';
      document.getElementById('pubExecuteBtn').style.display = 'none';
    }

    async function executeFacebookPublish(forceDup = false) {
      const btn = document.getElementById('pubExecuteBtn');
      const targets = getSelectedTargetsFromModal();
      btn.disabled = true;
      btn.textContent = '⏳ กำลังส่งคำสั่งโพสต์ทีละเพจ...';

      try {
        const res = await fetch('/api/fb/publish', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            case_key: PUB_CASE_KEY,
            cover_rel: PUB_COVER_REL,
            mode: PUB_MODE,
            targets: targets,
            force_duplicate: true
          })
        });
        const data = await res.json();
        btn.disabled = false;
        btn.textContent = '✅ ยืนยันสั่งโพสต์ลงเพจทันที';

        document.getElementById('pubStepConfig').style.display = 'none';
        document.getElementById('pubStepConfirm').style.display = 'none';
        document.getElementById('pubStepResult').style.display = 'block';
        document.getElementById('pubBackBtn').style.display = 'none';
        document.getElementById('pubExecuteBtn').style.display = 'none';

        const results = data.results || [];
        document.getElementById('pubResultBox').innerHTML = `
          <div style="padding:16px;border-radius:14px;background:${data.fail_count === 0 ? '#ECFDF5' : '#FFFBEB'};border:1px solid ${data.fail_count === 0 ? '#A7F3D0' : '#FDE68A'};">
            <h3 style="font-family:'Kanit',sans-serif;font-size:18px;color:#0F172A;margin-bottom:10px;">
              ${data.fail_count === 0 ? '🎉 ดำเนินการโพสต์/ตั้งเวลาสำเร็จครบทุกเพจ!' : `⚠️ สำเร็จ ${data.success_count} เพจ / ล้มเหลว ${data.fail_count} เพจ`}
              ${data.fb_mock ? '<span style="font-size:13px;color:#B45309;"> (โหมดทดสอบ FB_MOCK=1)</span>' : ''}
            </h3>
            <div style="display:flex;flex-direction:column;gap:8px;">
              ${results.map(r => r.ok ? `
                <div style="background:#FFFFFF;border:1px solid #86EFAC;padding:10px 14px;border-radius:10px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
                  <div>
                    <strong style="color:#166534;">✅ ${escapeHtml(r.page_name)}</strong>
                    <span style="font-size:13px;color:#475569;margin-left:6px;">(${escapeHtml(r.caption_label)})</span>
                    ${r.scheduled_time_str ? `<div style="font-size:12.5px;color:#0369A1;">⏰ ตั้งเวลาเผยแพร่: ${escapeHtml(r.scheduled_time_str)}</div>` : '<div style="font-size:12.5px;color:#15803D;">🚀 เผยแพร่ทันที (published=true)</div>'}
                  </div>
                  <a href="${escapeHtml(r.post_url)}" target="_blank" rel="noopener" class="btn" style="padding:5px 12px;font-size:12.5px;color:#15803D;border-color:#86EFAC;">
                    🔗 เปิดดูโพสต์ (${escapeHtml(r.post_id)}) ↗
                  </a>
                </div>
              ` : `
                <div style="background:#FEF2F2;border:1px solid #FECACA;padding:10px 14px;border-radius:10px;color:#DC2626;">
                  <strong>❌ ${escapeHtml(r.page_name)}:</strong> ${escapeHtml(r.error_thai || 'เกิดข้อผิดพลาด')}
                </div>
              `).join('')}
            </div>
          </div>
        `;

        await Promise.all([loadCases(), loadFbPages(false)]);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = '✅ ยืนยันสั่งโพสต์ลงเพจทันที';
        alert('เกิดข้อผิดพลาดในการเชื่อมต่อเซิร์ฟเวอร์: ' + err.message);
      }
    }

    // -----------------------------------------------------------------------
    // Admin Settings & New Post Modals
    // -----------------------------------------------------------------------

    function openSettingsModal() {
      document.getElementById('settingFbMock').value = FB_STATUS.fb_mock ? '1' : '0';
      document.getElementById('settingSysToken').value = '';
      document.getElementById('settingAppSecret').value = '';
      document.getElementById('settingsModal').classList.add('open');
    }

    function closeSettingsModal() {
      document.getElementById('settingsModal').classList.remove('open');
    }

    async function saveServerSettings() {
      const fb_mock = document.getElementById('settingFbMock').value === '1';
      const fb_system_token = document.getElementById('settingSysToken').value.trim() || null;
      const fb_app_secret = document.getElementById('settingAppSecret').value.trim() || null;

      const res = await fetch('/api/fb/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fb_mock, fb_system_token, fb_app_secret })
      });
      if (res.ok) {
        const data = await res.json();
        FB_STATUS = data.status || FB_STATUS;
        updateFbModeBadge();
        closeSettingsModal();
        await loadFbPages(true);
        showToast('✅ บันทึกการตั้งค่าฝั่งเซิร์ฟเวอร์เรียบร้อยแล้ว');
      } else {
        const err = await res.json();
        alert(err.detail || 'ไม่สามารถบันทึกการตั้งค่าได้');
      }
    }

    function openNewPostModal() {
      document.getElementById('newPostModal').classList.add('open');
    }

    function closeNewPostModal() {
      document.getElementById('newPostModal').classList.remove('open');
    }

    async function submitNewPost(e) {
      e.preventDefault();
      const btn = document.getElementById('newPostSubmitBtn');
      btn.disabled = true;
      btn.textContent = '⏳ กำลังอัปโหลดภาพปก 4:5 ขึ้นระบบ...';

      const fd = new FormData();
      fd.append('topic_title', document.getElementById('newTopicTitle').value);
      fd.append('caption_text_1', document.getElementById('newCap1').value);
      fd.append('caption_text_2', document.getElementById('newCap2').value);
      fd.append('caption_text_3', document.getElementById('newCap3').value);
      fd.append('cover_file', document.getElementById('newCoverFile').files[0]);

      try {
        const res = await fetch('/api/cases/create', { method: 'POST', body: fd });
        if (res.ok) {
          closeNewPostModal();
          await loadCases();
          showToast('🎉 เพิ่มการ์ดข่าวใหม่เข้าคลังเรียบร้อยแล้ว!');
        } else {
          const err = await res.json();
          alert(err.detail || 'อัปโหลดไม่สำเร็จ');
        }
      } catch (err) {
        alert('เกิดข้อผิดพลาด: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '✨ บันทึกการ์ดข่าวขึ้นระบบ';
      }
    }

    async function logoutUser() {
      await fetch('/api/auth/logout', { method: 'POST' });
      window.location.href = '/login';
    }

    initApp();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def main_dashboard(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return HTMLResponse(content=INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8088"))
    uvicorn.run(app, host="0.0.0.0", port=port)
