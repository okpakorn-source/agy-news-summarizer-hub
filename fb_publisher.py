#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Server-Only Facebook Graph API v25.0 Publisher for IG.dara Studio
=================================================================
STRICT SECURITY RULES:
1. `FB_SYSTEM_TOKEN` (System User "ADSAFF Publisher", App ADSAFF, non-expiring) and
   all `page_token`s stay strictly SERVER-SIDE.
2. NEVER return any token to the browser, NEVER log any token, NEVER commit any token.
3. If `FB_APP_SECRET` is set, attach `appsecret_proof = HMAC-SHA256(token, app_secret)`
   to every Graph API request using the specific token of that request.
4. Default mode during development is `FB_MOCK=1` (never posts to live Facebook until
   explicitly switched to `FB_MOCK=0` by the owner).
"""

import datetime
import hashlib
import hmac
import json
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from supabase_client import (
    FB_CONFIG_ENC_KEY,
    FB_PAGES_META_KEY,
    FB_PAGE_TOKENS_ENC_KEY,
    FB_SCHEDULE_STATE_KEY,
    get_setting,
    load_encrypted_setting,
    save_encrypted_setting,
    set_setting,
)

TH_TZ = datetime.timezone(datetime.timedelta(hours=7))

# Server-memory cache of page_id -> page_access_token (NEVER serialized to client!)
_SERVER_PAGE_TOKENS: Dict[str, str] = {}

# The 6 target pages of the owner's network
DEFAULT_6_PAGES: List[Dict[str, str]] = [
    {
        "id": "100064810000001",
        "name": "ติดจอ",
        "picture_url": "https://ui-avatars.com/api/?name=%E0%B8%95%E0%B8%B4%E0%B8%94%E0%B8%88%E0%B8%AD&background=4F46E5&color=fff&size=128&bold=true",
    },
    {
        "id": "100064810000002",
        "name": "ดารา-Entertain",
        "picture_url": "https://ui-avatars.com/api/?name=%E0%B8%94%E0%B8%B2%E0%B8%A3%E0%B8%B2&background=EC4899&color=fff&size=128&bold=true",
    },
    {
        "id": "100064810000003",
        "name": "เล่าเรื่อง ดารา",
        "picture_url": "https://ui-avatars.com/api/?name=%E0%B9%80%E0%B8%A5%E0%B9%88%E0%B8%B2%E0%B9%80%E0%B8%A3%E0%B8%B7%E0%B9%88%E0%B8%AD%E0%B8%87&background=F59E0B&color=fff&size=128&bold=true",
    },
    {
        "id": "100064810000004",
        "name": "Thaimarketing",
        "picture_url": "https://ui-avatars.com/api/?name=TM&background=0EA5E9&color=fff&size=128&bold=true",
    },
    {
        "id": "100064810000005",
        "name": "สุขภาพดี",
        "picture_url": "https://ui-avatars.com/api/?name=%E0%B8%AA%E0%B8%B8%E0%B8%82%E0%B8%A0%E0%B8%B2%E0%B8%9E&background=10B981&color=fff&size=128&bold=true",
    },
    {
        "id": "100064810000006",
        "name": "บันเทิงไทยแลนด์",
        "picture_url": "https://ui-avatars.com/api/?name=%E0%B8%9A%E0%B8%B1%E0%B8%99%E0%B9%80%E0%B8%97%E0%B8%B4%E0%B8%87&background=8B5CF6&color=fff&size=128&bold=true",
    },
]


# ---------------------------------------------------------------------------
# Configuration & Cryptographic Helpers (Server-Only)
# ---------------------------------------------------------------------------

def _get_server_fb_config() -> Dict[str, Any]:
    """Load FB settings from environment variables first, falling back to encrypted Supabase store."""
    enc_cfg = load_encrypted_setting(FB_CONFIG_ENC_KEY)

    sys_token = (
        os.environ.get("FB_SYSTEM_TOKEN", "").strip()
        or str(enc_cfg.get("fb_system_token") or "").strip()
    )
    app_secret = (
        os.environ.get("FB_APP_SECRET", "").strip()
        or str(enc_cfg.get("fb_app_secret") or "").strip()
    )
    graph_version = (
        os.environ.get("FB_GRAPH_VERSION", "").strip()
        or str(enc_cfg.get("fb_graph_version") or "v25.0").strip()
        or "v25.0"
    )

    # Check FB_MOCK from env or encrypted config; default to "1" (safe test mode)
    env_mock = os.environ.get("FB_MOCK", "").strip()
    if env_mock in ("0", "1", "true", "false", "True", "False"):
        is_mock = env_mock.lower() in ("1", "true")
    elif "fb_mock" in enc_cfg:
        is_mock = bool(enc_cfg.get("fb_mock"))
    else:
        is_mock = True

    return {
        "fb_system_token": sys_token,
        "fb_app_secret": app_secret,
        "fb_graph_version": graph_version,
        "fb_mock": is_mock,
    }


def get_public_fb_status() -> Dict[str, Any]:
    """Return safe status metadata for UI display — NEVER includes any token or secret values."""
    cfg = _get_server_fb_config()
    return {
        "graph_version": cfg["fb_graph_version"],
        "fb_mock": bool(cfg["fb_mock"]),
        "has_system_token": bool(cfg["fb_system_token"]),
        "has_app_secret": bool(cfg["fb_app_secret"]),
        "system_user_label": "ADSAFF Publisher (แอป ADSAFF)",
    }


def update_server_fb_config(
    fb_system_token: Optional[str] = None,
    fb_app_secret: Optional[str] = None,
    fb_mock: Optional[bool] = None,
) -> Dict[str, Any]:
    """Allow owner to update encrypted server-side FB config without exposing tokens."""
    current = load_encrypted_setting(FB_CONFIG_ENC_KEY)
    if fb_system_token is not None and fb_system_token.strip():
        current["fb_system_token"] = fb_system_token.strip()
        os.environ["FB_SYSTEM_TOKEN"] = fb_system_token.strip()
    if fb_app_secret is not None:
        current["fb_app_secret"] = fb_app_secret.strip()
        os.environ["FB_APP_SECRET"] = fb_app_secret.strip()
    if fb_mock is not None:
        current["fb_mock"] = bool(fb_mock)
        os.environ["FB_MOCK"] = "1" if fb_mock else "0"
    current.setdefault("fb_graph_version", "v25.0")
    save_encrypted_setting(FB_CONFIG_ENC_KEY, current)
    return get_public_fb_status()


def compute_appsecret_proof(token: str, app_secret: str) -> str:
    """Compute HMAC-SHA256(token, app_secret) as required by Facebook Graph API."""
    if not token or not app_secret:
        return ""
    return hmac.new(
        app_secret.encode("utf-8"),
        msg=token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def translate_fb_error_to_thai(code: Optional[int], subcode: Optional[int], raw_msg: str = "") -> str:
    """Translate Facebook Graph API error codes into actionable Thai messages (no auto-retry)."""
    if code == 190:
        return "โทเคนเสียหรือหมดอายุ (รหัส 190) — กรุณาตรวจสอบ FB_SYSTEM_TOKEN ของ ADSAFF Publisher หรือกดรีเฟรชรายชื่อเพจ"
    if code in (10, 200) or (code is not None and 200 <= code <= 299):
        return f"สิทธิ์ไม่เพียงพอในการโพสต์ลงเพจนี้ (รหัส {code}) — กรุณาตรวจสอบว่าให้สิทธิ์ pages_manage_posts และ pages_read_engagement กับ ADSAFF Publisher แล้ว"
    if code == 368:
        return "เพจถูกบล็อกหรือจำกัดการโพสต์ชั่วคราวตามนโยบายชุมชนของ Facebook (รหัส 368)"
    if code in (4, 17, 32, 613):
        return f"ยิงคำขอถี่เกินขีดจำกัดของ Facebook (Rate Limit รหัส {code}) — กรุณาเว้นระยะสักครู่แล้วค่อยกดโพสต์ใหม่ (ระบบจะไม่ยิงซ้ำวนเอง)"
    if code == 100 and "scheduled_publish_time" in (raw_msg or ""):
        return "เวลาตั้งโพสต์ล่วงหน้าไม่อยู่ในช่วงที่ Facebook กำหนด (ต้องอยู่ระหว่าง 10 นาที ถึง 30 วันข้างหน้า)"
    clean_msg = (raw_msg or "เกิดข้อผิดพลาดจาก Facebook Graph API").strip()
    return f"โพสต์ไม่สำเร็จ (รหัส {code or 'N/A'}{f'/{subcode}' if subcode else ''}): {clean_msg}"


# ---------------------------------------------------------------------------
# Server-Side Page Token Store (Strictly Server-Only)
# ---------------------------------------------------------------------------

def _load_page_tokens_map() -> Dict[str, str]:
    global _SERVER_PAGE_TOKENS
    if _SERVER_PAGE_TOKENS:
        return _SERVER_PAGE_TOKENS
    enc = load_encrypted_setting(FB_PAGE_TOKENS_ENC_KEY)
    tokens = enc.get("tokens")
    if isinstance(tokens, dict):
        _SERVER_PAGE_TOKENS = {str(k): str(v) for k, v in tokens.items() if v}
    return _SERVER_PAGE_TOKENS


def _save_page_tokens_map(tokens_map: Dict[str, str]) -> None:
    global _SERVER_PAGE_TOKENS
    _SERVER_PAGE_TOKENS = dict(tokens_map)
    save_encrypted_setting(FB_PAGE_TOKENS_ENC_KEY, {"tokens": _SERVER_PAGE_TOKENS})


def _get_page_token_for_page(page_id: str) -> str:
    tokens_map = _load_page_tokens_map()
    return tokens_map.get(str(page_id), "")


# ---------------------------------------------------------------------------
# Per-Page Auto-Schedule State (20–30 Minute Random Spacing per Page)
# ---------------------------------------------------------------------------

def get_schedule_state() -> Dict[str, int]:
    """Return mapping of `page_id -> latest_scheduled_publish_time (unix seconds)`."""
    raw = get_setting(FB_SCHEDULE_STATE_KEY)
    now_ts = int(time.time())
    state: Dict[str, int] = {}
    if isinstance(raw, dict):
        for pid, ts in raw.items():
            try:
                ts_int = int(ts)
                if ts_int > now_ts:
                    state[str(pid)] = ts_int
            except Exception:
                pass
    return state


def save_schedule_state(state: Dict[str, int]) -> None:
    set_setting(FB_SCHEDULE_STATE_KEY, state)


def compute_next_auto_schedule_time(page_id: str, schedule_state: Dict[str, int], min_base_ts: int = 0) -> int:
    """
    Calculate the next `scheduled_publish_time` (unix seconds) for `page_id`:
    - Random 20–30 minutes (1200–1800 seconds) after the latest scheduled post of that page
      (or after `max(now, min_base_ts)` if the page has no future scheduled posts).
    - Clamped strictly within [now + 11 minutes, now + 29 days] to satisfy Facebook Graph API rules.
    """
    now_ts = int(time.time())
    last_ts = int(schedule_state.get(str(page_id), 0))
    base_ts = max(now_ts, last_ts, min_base_ts)
    gap_sec = random.randint(20 * 60, 30 * 60)  # 20 to 30 minutes random
    scheduled_ts = base_ts + gap_sec

    min_allowed = now_ts + (11 * 60)           # >= 10 minutes ahead (use 11m for safety margin)
    max_allowed = now_ts + (29 * 24 * 3600)    # <= 30 days ahead (use 29d for safety margin)
    scheduled_ts = max(min_allowed, min(scheduled_ts, max_allowed))

    schedule_state[str(page_id)] = scheduled_ts
    return scheduled_ts


def format_thai_datetime(unix_ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(unix_ts, tz=TH_TZ)
    return dt.strftime("%d/%m/%Y %H:%M น.")


# ---------------------------------------------------------------------------
# 1) Fetch & Refresh Facebook Pages (`GET /v25.0/me/accounts`)
# ---------------------------------------------------------------------------

def fetch_fb_pages(refresh: bool = False) -> Dict[str, Any]:
    """
    Fetch Facebook pages accessible by `FB_SYSTEM_TOKEN`:
      GET https://graph.facebook.com/v25.0/me/accounts?fields=id,name,picture{url},access_token&limit=100
    Stores `access_token` strictly server-side and returns ONLY `id`, `name`, `picture_url`,
    plus queue scheduling metadata to the web client.
    """
    cfg = _get_server_fb_config()
    sys_token = cfg["fb_system_token"]
    app_secret = cfg["fb_app_secret"]
    ver = cfg["fb_graph_version"]
    is_mock = cfg["fb_mock"]
    schedule_state = get_schedule_state()

    if not refresh:
        cached_meta = get_setting(FB_PAGES_META_KEY)
        if isinstance(cached_meta, dict) and isinstance(cached_meta.get("pages"), list) and cached_meta["pages"]:
            pages_out = []
            for p in cached_meta["pages"]:
                pid = str(p.get("id", ""))
                last_sched = schedule_state.get(pid, 0)
                pages_out.append({
                    "id": pid,
                    "name": str(p.get("name", "")),
                    "picture_url": str(p.get("picture_url", "")),
                    "last_scheduled_time": last_sched,
                    "last_scheduled_str": format_thai_datetime(last_sched) if last_sched else "ไม่มีคิวค้าง (พร้อมตั้งเวลา)",
                })
            return {
                "ok": True,
                "fb_mock": is_mock,
                "synced_at": cached_meta.get("synced_at", ""),
                "source": cached_meta.get("source", "cache"),
                "pages": pages_out,
                "warning": cached_meta.get("warning", ""),
            }

    live_pages: List[Dict[str, str]] = []
    tokens_map = _load_page_tokens_map()
    api_warning = ""
    source_label = "mock_6_pages"

    if sys_token:
        params: Dict[str, Any] = {
            "fields": "id,name,picture{url},access_token",
            "limit": "100",
            "access_token": sys_token,
        }
        if app_secret:
            params["appsecret_proof"] = compute_appsecret_proof(sys_token, app_secret)

        url = f"https://graph.facebook.com/{ver}/me/accounts"
        try:
            resp = requests.get(url, params=params, timeout=20)
            data = resp.json()
            if resp.status_code == 200 and isinstance(data.get("data"), list):
                for item in data["data"]:
                    pid = str(item.get("id") or "").strip()
                    pname = str(item.get("name") or "").strip()
                    ptoken = str(item.get("access_token") or "").strip()
                    pic_url = (
                        item.get("picture", {}).get("data", {}).get("url")
                        if isinstance(item.get("picture"), dict)
                        else ""
                    ) or f"https://ui-avatars.com/api/?name={pname}&background=4F46E5&color=fff&size=128"
                    if pid and pname:
                        live_pages.append({
                            "id": pid,
                            "name": pname,
                            "picture_url": pic_url,
                        })
                        if ptoken:
                            tokens_map[pid] = ptoken
                _save_page_tokens_map(tokens_map)
                source_label = "graph_api_v25"
            else:
                err_obj = data.get("error", {}) if isinstance(data, dict) else {}
                api_warning = translate_fb_error_to_thai(
                    err_obj.get("code"),
                    err_obj.get("error_subcode"),
                    err_obj.get("message", ""),
                )
        except Exception as exc:
            api_warning = f"ไม่สามารถเชื่อมต่อ Facebook Graph API ได้ชั่วคราว: {str(exc)[:120]}"

    # Combine or use DEFAULT_6_PAGES when in FB_MOCK=1 or when fewer than 6 pages are connected
    final_pages: List[Dict[str, str]] = list(live_pages)
    if is_mock and len(final_pages) < len(DEFAULT_6_PAGES):
        existing_names = {p["name"].strip().lower() for p in final_pages}
        existing_ids = {p["id"] for p in final_pages}
        for dp in DEFAULT_6_PAGES:
            if dp["name"].strip().lower() not in existing_names and dp["id"] not in existing_ids:
                final_pages.append(dict(dp))
                tokens_map.setdefault(dp["id"], f"mock_page_token_{dp['id']}")
        _save_page_tokens_map(tokens_map)
        if live_pages:
            source_label = "graph_api_plus_mock_6"
        else:
            source_label = "mock_6_pages"

    synced_at_str = datetime.datetime.now(TH_TZ).strftime("%d/%m/%Y %H:%M:%S")
    set_setting(
        FB_PAGES_META_KEY,
        {
            "pages": final_pages,
            "synced_at": synced_at_str,
            "source": source_label,
            "warning": api_warning,
        },
    )

    pages_out = []
    for p in final_pages:
        pid = p["id"]
        last_sched = schedule_state.get(pid, 0)
        pages_out.append({
            "id": pid,
            "name": p["name"],
            "picture_url": p["picture_url"],
            "last_scheduled_time": last_sched,
            "last_scheduled_str": format_thai_datetime(last_sched) if last_sched else "ไม่มีคิวค้าง (พร้อมตั้งเวลา)",
        })

    return {
        "ok": True if pages_out else False,
        "fb_mock": is_mock,
        "synced_at": synced_at_str,
        "source": source_label,
        "pages": pages_out,
        "warning": api_warning,
    }


# ---------------------------------------------------------------------------
# 2) Publish Photo + Caption (`POST /v25.0/{page_id}/photos`)
# ---------------------------------------------------------------------------

def _publish_single_page_photo(
    page_id: str,
    page_name: str,
    message: str,
    image_url: str,
    image_bytes: Optional[bytes],
    mode: str,
    scheduled_publish_time: Optional[int],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Publish or schedule a single 4:5 photo + caption to `{page_id}/photos` (NO external link).
    Never auto-retries on failure.
    """
    is_mock = bool(cfg["fb_mock"])
    ver = cfg["fb_graph_version"]
    app_secret = cfg["fb_app_secret"]
    now_iso = datetime.datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # Safety Guard: When FB_MOCK=1, NEVER make live POST calls to Facebook!
    if is_mock:
        mock_post_id = f"{page_id}_{int(time.time())}{random.randint(100, 999)}"
        status_val = "scheduled" if mode == "auto_schedule" else "published"
        return {
            "ok": True,
            "is_mock": True,
            "page_id": page_id,
            "page_name": page_name,
            "post_id": mock_post_id,
            "post_url": f"https://www.facebook.com/{mock_post_id}",
            "status": status_val,
            "mode": mode,
            "scheduled_publish_time": scheduled_publish_time if mode == "auto_schedule" else None,
            "scheduled_time_str": format_thai_datetime(scheduled_publish_time) if (mode == "auto_schedule" and scheduled_publish_time) else "",
            "published_at": now_iso,
        }

    # Live Mode (FB_MOCK=0): Require valid server-side page_token
    page_token = _get_page_token_for_page(page_id)
    if not page_token or page_token.startswith("mock_page_token_"):
        # Attempt a single page refresh if page_token isn't cached yet
        fetch_fb_pages(refresh=True)
        page_token = _get_page_token_for_page(page_id)

    if not page_token or page_token.startswith("mock_page_token_"):
        return {
            "ok": False,
            "is_mock": False,
            "page_id": page_id,
            "page_name": page_name,
            "status": "failed",
            "error_code": 190,
            "error_thai": translate_fb_error_to_thai(190, None, "ไม่พบ Page Access Token ของเพจนี้ในระบบฝั่งเซิร์ฟเวอร์"),
            "published_at": now_iso,
        }

    endpoint = f"https://graph.facebook.com/{ver}/{page_id}/photos"
    payload: Dict[str, Any] = {
        "message": message,
        "access_token": page_token,
    }
    if app_secret:
        payload["appsecret_proof"] = compute_appsecret_proof(page_token, app_secret)

    if mode == "auto_schedule" and scheduled_publish_time:
        payload["published"] = "false"
        payload["scheduled_publish_time"] = str(int(scheduled_publish_time))
    else:
        payload["published"] = "true"

    try:
        # Prefer public 4:5 Supabase Storage URL if available; otherwise multipart "source"
        if image_url and image_url.startswith(("https://", "http://")) and "127.0.0.1" not in image_url and "localhost" not in image_url:
            payload["url"] = image_url
            resp = requests.post(endpoint, data=payload, timeout=45)
        elif image_bytes:
            files = {"source": ("cover_4x5_1080x1350.jpg", image_bytes, "image/jpeg")}
            resp = requests.post(endpoint, data=payload, files=files, timeout=60)
        else:
            return {
                "ok": False,
                "is_mock": False,
                "page_id": page_id,
                "page_name": page_name,
                "status": "failed",
                "error_code": 100,
                "error_thai": "ไม่พบไฟล์ภาพปก 4:5 หรือ URL สาธารณะสำหรับส่งไปยัง Facebook",
                "published_at": now_iso,
            }

        data = resp.json()
        if resp.status_code in (200, 201) and (data.get("post_id") or data.get("id")):
            fb_post_id = str(data.get("post_id") or data.get("id"))
            status_val = "scheduled" if mode == "auto_schedule" else "published"
            return {
                "ok": True,
                "is_mock": False,
                "page_id": page_id,
                "page_name": page_name,
                "post_id": fb_post_id,
                "post_url": f"https://www.facebook.com/{fb_post_id}",
                "status": status_val,
                "mode": mode,
                "scheduled_publish_time": scheduled_publish_time if mode == "auto_schedule" else None,
                "scheduled_time_str": format_thai_datetime(scheduled_publish_time) if (mode == "auto_schedule" and scheduled_publish_time) else "",
                "published_at": now_iso,
            }

        err_obj = data.get("error", {}) if isinstance(data, dict) else {}
        code = err_obj.get("code")
        subcode = err_obj.get("error_subcode")
        raw_msg = err_obj.get("error_user_msg") or err_obj.get("message") or f"HTTP {resp.status_code}"
        return {
            "ok": False,
            "is_mock": False,
            "page_id": page_id,
            "page_name": page_name,
            "status": "failed",
            "error_code": code,
            "error_subcode": subcode,
            "error_thai": translate_fb_error_to_thai(code, subcode, raw_msg),
            "published_at": now_iso,
        }
    except Exception as exc:
        return {
            "ok": False,
            "is_mock": False,
            "page_id": page_id,
            "page_name": page_name,
            "status": "failed",
            "error_code": None,
            "error_thai": f"การเชื่อมต่อระหว่างโพสต์ล้มเหลว (ไม่ยิงซ้ำอัตโนมัติ): {str(exc)[:150]}",
            "published_at": now_iso,
        }


def publish_to_selected_pages(
    case_data: Dict[str, Any],
    targets: List[Dict[str, Any]],
    mode: str,
    cover_rel: str,
    cover_public_url: str,
    cover_bytes: Optional[bytes],
    force_duplicate: bool = False,
    published_by: str = "ทีมงาน",
) -> Dict[str, Any]:
    """
    Publish or auto-schedule a news card to multiple selected Facebook pages.
    Each target in `targets` specifies:
      `{"page_id": "...", "page_name": "...", "caption_index": 1}`
    """
    cfg = _get_server_fb_config()
    existing_posts: List[Dict[str, Any]] = list(case_data.get("fb_posts") or [])

    # Check duplicate pages first (warn before re-posting same news to same page)
    already_posted_pages = {}
    for ep in existing_posts:
        if ep.get("status") in ("published", "scheduled"):
            already_posted_pages[str(ep.get("page_id"))] = ep

    duplicate_conflicts = []
    for t in targets:
        pid = str(t.get("page_id", "")).strip()
        if pid in already_posted_pages:
            prev = already_posted_pages[pid]
            duplicate_conflicts.append({
                "page_id": pid,
                "page_name": prev.get("page_name") or t.get("page_name") or pid,
                "previous_post_id": prev.get("post_id", ""),
                "previous_post_url": prev.get("post_url", ""),
                "previous_status": prev.get("status", ""),
                "previous_time": prev.get("scheduled_time_str") or prev.get("published_at", ""),
            })

    if duplicate_conflicts and not force_duplicate:
        return {
            "ok": False,
            "requires_duplicate_confirm": True,
            "duplicate_conflicts": duplicate_conflicts,
            "message": "พบเพจที่เคยโพสต์หรือตั้งเวลาข่าวนี้ไว้แล้ว กรุณายืนยันหากต้องการโพสต์ซ้ำลงเพจเดิม",
        }

    # Map caption variants by index
    caption_variants = case_data.get("caption_variants") or []
    cap_by_idx: Dict[int, Dict[str, Any]] = {}
    for idx, cv in enumerate(caption_variants, start=1):
        if isinstance(cv, dict):
            c_idx = int(cv.get("index") or idx)
            cap_by_idx[c_idx] = cv

    fallback_text = str(case_data.get("full_text") or "").strip()
    schedule_state = get_schedule_state()
    results: List[Dict[str, Any]] = []
    prev_scheduled_ts = 0

    for idx, t in enumerate(targets):
        pid = str(t.get("page_id", "")).strip()
        pname = str(t.get("page_name", "")).strip() or pid
        cap_idx = int(t.get("caption_index") or 1)
        chosen_cap = cap_by_idx.get(cap_idx) or (caption_variants[0] if caption_variants else {})
        message_text = str(chosen_cap.get("text") if isinstance(chosen_cap, dict) else "").strip() or fallback_text
        caption_label = str(chosen_cap.get("label") if isinstance(chosen_cap, dict) else f"แบบที่ {cap_idx}")

        scheduled_ts: Optional[int] = None
        if mode == "auto_schedule":
            scheduled_ts = compute_next_auto_schedule_time(
                pid,
                schedule_state,
                min_base_ts=prev_scheduled_ts,
            )
            prev_scheduled_ts = scheduled_ts

        res = _publish_single_page_photo(
            page_id=pid,
            page_name=pname,
            message=message_text,
            image_url=cover_public_url,
            image_bytes=cover_bytes,
            mode=mode,
            scheduled_publish_time=scheduled_ts,
            cfg=cfg,
        )
        res["cover_rel"] = cover_rel
        res["cover_url"] = cover_public_url
        res["caption_index"] = cap_idx
        res["caption_label"] = caption_label
        res["published_by"] = published_by
        results.append(res)

        if res.get("ok"):
            existing_posts.append(res)

    if mode == "auto_schedule":
        save_schedule_state(schedule_state)

    success_count = sum(1 for r in results if r.get("ok"))
    fail_count = len(results) - success_count

    return {
        "ok": success_count > 0 and fail_count == 0,
        "partial_success": success_count > 0 and fail_count > 0,
        "fb_mock": bool(cfg["fb_mock"]),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
        "updated_fb_posts": existing_posts,
    }
