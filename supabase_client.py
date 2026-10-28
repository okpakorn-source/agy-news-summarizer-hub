import os
import json
import time
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests

def _load_dotenv_if_exists():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv_if_exists()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zkfwnfqefikmctdpyeui.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")
BUCKET_NAME = "agy-news-hub"
JOB_PREFIX = "agy_hub_job:"
HOST_STATUS_KEY = "agy_hub_host_status"


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def upload_file_to_supabase(storage_path: str, content_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """Upload bytes to Supabase Storage bucket `agy-news-hub` and return public URL."""
    storage_path = storage_path.lstrip("/")
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{storage_path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    resp = requests.post(url, headers=headers, data=content_bytes, timeout=30)
    if resp.status_code not in (200, 201):
        # Try PUT if POST fails
        requests.put(url, headers=headers, data=content_bytes, timeout=30)
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{storage_path}"


def save_job_to_supabase(job_dict: Dict[str, Any]) -> bool:
    """Upsert a job object into Supabase `app_settings` table under `agy_hub_job:<id>`."""
    job_id = job_dict["id"]
    key = f"{JOB_PREFIX}{job_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    payload = {
        "key": key,
        "value": job_dict,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    resp = requests.post(
        url,
        headers=_headers({"Prefer": "resolution=merge-duplicates"}),
        json=payload,
        timeout=15,
    )
    return resp.status_code in (200, 201, 204)


def get_job_from_supabase(job_id: str) -> Optional[Dict[str, Any]]:
    key = f"{JOB_PREFIX}{job_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    resp = requests.get(
        url,
        headers=_headers(),
        params={"key": f"eq.{key}", "select": "value"},
        timeout=10,
    )
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            return rows[0].get("value")
    return None


def list_jobs_from_supabase(limit: int = 50) -> List[Dict[str, Any]]:
    url = f"{SUPABASE_URL}/rest/v1/app_settings?key=like.{JOB_PREFIX}*&select=key,value&order=key.desc&limit={int(limit)}"
    resp = requests.get(
        url,
        headers=_headers(),
        timeout=12,
    )
    if resp.status_code != 200:
        return []
    rows = resp.json()
    jobs = []
    for r in rows:
        val = r.get("value")
        if isinstance(val, dict) and val.get("id"):
            jobs.append(val)
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jobs


def delete_job_from_supabase(job_id: str) -> bool:
    key = f"{JOB_PREFIX}{job_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    resp = requests.delete(
        url,
        headers=_headers(),
        params={"key": f"eq.{key}"},
        timeout=10,
    )
    return resp.status_code in (200, 204)


def update_host_heartbeat(lan_url: str = "", tunnel_url: str = ""):
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    payload = {
        "key": HOST_STATUS_KEY,
        "value": {
            "online": True,
            "last_seen": time.time(),
            "last_seen_str": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "lan_url": lan_url,
            "tunnel_url": tunnel_url,
        },
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        requests.post(
            url,
            headers=_headers({"Prefer": "resolution=merge-duplicates"}),
            json=payload,
            timeout=8,
        )
    except Exception:
        pass


def get_host_heartbeat() -> Dict[str, Any]:
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = requests.get(
            url,
            headers=_headers(),
            params={"key": f"eq.{HOST_STATUS_KEY}", "select": "value"},
            timeout=8,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows and isinstance(rows[0].get("value"), dict):
                val = rows[0]["value"]
                # Consider online if heartbeat within last 45 seconds
                is_online = (time.time() - float(val.get("last_seen", 0))) < 45
                val["online"] = is_online
                return val
    except Exception:
        pass
    return {"online": False}
