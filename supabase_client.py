#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabase Client for IG.dara Studio (Permanent Web & Facebook Multi-Page Publisher)
==================================================================================
Uses existing Supabase infrastructure:
- Project URL: SUPABASE_URL (https://zkfwnfqefikmctdpyeui.supabase.co)
- Storage Bucket: agy-news-hub (public 4:5 1080x1350 cover images)
- Table: app_settings (key, value, updated_at)

Keys used in `app_settings`:
- `igdara_case:<case_key>`          -> News package record + cover URLs + caption variants + fb_posts history
- `igdara_fb_pages_meta`            -> Sanitized page list (id, name, picture_url, synced_at) — NO TOKENS
- `igdara_fb_schedule_state`        -> Per-page latest scheduled_publish_time (unix seconds)
- `igdara_fb_config_enc`            -> Server-only encrypted FB config (never sent to client)
- `igdara_fb_page_tokens_enc`       -> Server-only encrypted page_id -> page_token map (never sent to client)
- `igdara_auth_users`               -> Hashed login credentials for owner & team
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


def _load_dotenv_if_exists() -> None:
    for candidate in [
        Path(__file__).resolve().parent / ".env",
        Path(r"c:\155cover any\.env"),
    ]:
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except Exception:
                pass


_load_dotenv_if_exists()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zkfwnfqefikmctdpyeui.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
BUCKET_NAME = "agy-news-hub"

CASE_PREFIX = "igdara_case:"
FB_PAGES_META_KEY = "igdara_fb_pages_meta"
FB_SCHEDULE_STATE_KEY = "igdara_fb_schedule_state"
FB_CONFIG_ENC_KEY = "igdara_fb_config_enc"
FB_PAGE_TOKENS_ENC_KEY = "igdara_fb_page_tokens_enc"
AUTH_USERS_KEY = "igdara_auth_users"

TH_TZ = datetime.timezone(datetime.timedelta(hours=7))


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _parse_val(val: Any) -> Any:
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return None


# ---------------------------------------------------------------------------
# Generic App Settings Helpers (PostgREST `app_settings`)
# ---------------------------------------------------------------------------

def get_setting(key: str) -> Any:
    if not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = requests.get(
            url,
            headers=_headers(),
            params={"key": f"eq.{key}", "select": "value"},
            timeout=10,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows:
                return _parse_val(rows[0].get("value"))
    except Exception:
        pass
    return None


def set_setting(key: str, value: Any) -> bool:
    if not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    payload = {
        "key": key,
        "value": value,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(
            url,
            headers=_headers({"Prefer": "resolution=merge-duplicates"}),
            json=payload,
            timeout=15,
        )
        return resp.status_code in (200, 201, 204)
    except Exception:
        return False


def delete_settings_by_prefix(prefix: str) -> int:
    """Delete keys starting with `prefix` in `app_settings`."""
    if not SUPABASE_KEY:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = requests.delete(
            url,
            headers=_headers({"Prefer": "return=representation"}),
            params={"key": f"like.{prefix}*"},
            timeout=20,
        )
        if resp.status_code in (200, 204):
            try:
                return len(resp.json())
            except Exception:
                return 1
    except Exception:
        pass
    return 0


def wipe_legacy_hub_data() -> Dict[str, int]:
    """Remove old agy_hub_job:*, agy_hub_topic:*, and agy_hub_host_status entries."""
    deleted_jobs = delete_settings_by_prefix("agy_hub_job:")
    deleted_topics = delete_settings_by_prefix("agy_hub_topic:")
    deleted_host = delete_settings_by_prefix("agy_hub_host_status")
    return {
        "deleted_jobs": deleted_jobs,
        "deleted_topics": deleted_topics,
        "deleted_host": deleted_host,
    }


# ---------------------------------------------------------------------------
# Server-Side Authenticated Encryption (Zero External Dependencies)
# Uses Counter-Mode SHA-256 stream + HMAC-SHA256 integrity tag keyed by SUPABASE_KEY
# ---------------------------------------------------------------------------

def _derive_enc_keys() -> tuple[bytes, bytes]:
    secret_seed = (
        os.environ.get("TOKEN_ENCRYPTION_KEY")
        or os.environ.get("IGDARA_SESSION_SECRET")
        or SUPABASE_KEY
        or "igdara-fallback-server-key"
    ).encode("utf-8")
    enc_key = hashlib.sha256(b"igdara-enc-v1:" + secret_seed).digest()
    mac_key = hashlib.sha256(b"igdara-mac-v1:" + secret_seed).digest()
    return enc_key, mac_key


def _xor_keystream(data: bytes, enc_key: bytes, nonce: bytes) -> bytes:
    out = bytearray(len(data))
    block_count = (len(data) + 31) // 32
    for counter in range(block_count):
        ctr_bytes = counter.to_bytes(8, "big")
        keystream = hashlib.sha256(enc_key + nonce + ctr_bytes).digest()
        start = counter * 32
        end = min(start + 32, len(data))
        for i in range(start, end):
            out[i] = data[i] ^ keystream[i - start]
    return bytes(out)


def encrypt_server_payload(obj: Dict[str, Any]) -> str:
    plaintext = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    enc_key, mac_key = _derive_enc_keys()
    nonce = os.urandom(16)
    ciphertext = _xor_keystream(plaintext, enc_key, nonce)
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    blob = nonce + tag + ciphertext
    return "v1:" + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_server_payload(token_str: str) -> Optional[Dict[str, Any]]:
    if not token_str or not isinstance(token_str, str) or not token_str.startswith("v1:"):
        return None
    try:
        raw = base64.urlsafe_b64decode(token_str[3:].encode("ascii"))
        if len(raw) < 48:
            return None
        nonce = raw[:16]
        tag = raw[16:48]
        ciphertext = raw[48:]
        enc_key, mac_key = _derive_enc_keys()
        expected_tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            return None
        plaintext = _xor_keystream(ciphertext, enc_key, nonce)
        data = json.loads(plaintext.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_encrypted_setting(key: str, payload: Dict[str, Any]) -> bool:
    enc = encrypt_server_payload(payload)
    return set_setting(key, {"cipher": enc, "updated_ts": int(time.time())})


def load_encrypted_setting(key: str) -> Dict[str, Any]:
    row = get_setting(key)
    if isinstance(row, dict) and isinstance(row.get("cipher"), str):
        dec = decrypt_server_payload(row["cipher"])
        if isinstance(dec, dict):
            return dec
    return {}


# ---------------------------------------------------------------------------
# Supabase Storage Helpers (`agy-news-hub` public bucket)
# ---------------------------------------------------------------------------

def upload_file_to_supabase(storage_path: str, content_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """Upload bytes to Supabase Storage bucket `agy-news-hub` and return public URL."""
    storage_path = storage_path.lstrip("/")
    if not SUPABASE_KEY:
        return ""
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{storage_path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    resp = requests.post(url, headers=headers, data=content_bytes, timeout=30)
    if resp.status_code not in (200, 201):
        requests.put(url, headers=headers, data=content_bytes, timeout=30)
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{storage_path}"


def get_public_storage_url(storage_path: str) -> str:
    storage_path = storage_path.lstrip("/")
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{storage_path}"


# ---------------------------------------------------------------------------
# IG.dara Studio Cases CRUD in Supabase
# ---------------------------------------------------------------------------

def save_case_to_supabase(case_dict: Dict[str, Any]) -> bool:
    case_key = str(case_dict.get("case_key") or case_dict.get("case_id") or "").strip()
    if not case_key:
        return False
    key = f"{CASE_PREFIX}{case_key}"
    return set_setting(key, case_dict)


def get_case_from_supabase(case_key: str) -> Optional[Dict[str, Any]]:
    key = f"{CASE_PREFIX}{case_key}"
    val = get_setting(key)
    return val if isinstance(val, dict) else None


def list_cases_from_supabase(limit: int = 200) -> List[Dict[str, Any]]:
    if not SUPABASE_KEY:
        return []
    url = (
        f"{SUPABASE_URL}/rest/v1/app_settings"
        f"?key=like.{CASE_PREFIX}*&select=key,value,updated_at&order=updated_at.desc&limit={int(limit)}"
    )
    try:
        resp = requests.get(url, headers=_headers(), timeout=12)
        if resp.status_code != 200:
            return []
        rows = resp.json()
        cases: List[Dict[str, Any]] = []
        for r in rows:
            val = _parse_val(r.get("value"))
            if isinstance(val, dict) and val.get("case_key"):
                cases.append(val)
        return cases
    except Exception:
        return []


def delete_case_from_supabase(case_key: str) -> bool:
    if not SUPABASE_KEY:
        return False
    key = f"{CASE_PREFIX}{case_key}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = requests.delete(url, headers=_headers(), params={"key": f"eq.{key}"}, timeout=10)
        return resp.status_code in (200, 204)
    except Exception:
        return False
