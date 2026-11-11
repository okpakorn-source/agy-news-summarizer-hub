import os
import json
import time
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _load_dotenv_if_exists():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
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
JOB_PREFIX = "agy_hub_job:"
TOPIC_PREFIX = "agy_hub_topic:"
HOST_STATUS_KEY = "agy_hub_host_status"
TH_TZ = datetime.timezone(datetime.timedelta(hours=7))


def _create_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        read=2,
        connect=2,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_SESSION = _create_http_session()


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
    try:
        resp = _SESSION.post(url, headers=headers, data=content_bytes, timeout=30)
        if resp.status_code not in (200, 201):
            _SESSION.put(url, headers=headers, data=content_bytes, timeout=30)
    except Exception:
        pass
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{storage_path}"


def save_job_to_supabase(job_dict: Dict[str, Any]) -> bool:
    """Upsert a job object into Supabase `app_settings` table under `agy_hub_job:<id>`."""
    if not SUPABASE_KEY:
        return False
    job_id = job_dict["id"]
    key = f"{JOB_PREFIX}{job_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    payload = {
        "key": key,
        "value": job_dict,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        resp = _SESSION.post(
            url,
            headers=_headers({"Prefer": "resolution=merge-duplicates"}),
            json=payload,
            timeout=15,
        )
        return resp.status_code in (200, 201, 204)
    except Exception:
        return False


def _parse_val(val: Any) -> Optional[Dict[str, Any]]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def get_job_from_supabase(job_id: str) -> Optional[Dict[str, Any]]:
    if not SUPABASE_KEY:
        return None
    key = f"{JOB_PREFIX}{job_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.get(
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


def list_jobs_from_supabase(limit: int = 100) -> List[Dict[str, Any]]:
    if not SUPABASE_KEY:
        return []
    fetch_limit = max(int(limit), 120)
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.get(
            url,
            headers=_headers(),
            params={
                "key": f"like.{JOB_PREFIX}*",
                "select": "key,value,updated_at",
                "order": "updated_at.desc",
                "limit": str(fetch_limit),
            },
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        rows = resp.json()
        jobs = []
        for r in rows:
            val = _parse_val(r.get("value"))
            if val and val.get("id"):
                jobs.append(val)
        # Sort active (queued/processing) or recently created jobs reliably
        jobs.sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("id") or "")), reverse=True)
        return jobs[: int(limit)]
    except Exception:
        return []


def delete_job_from_supabase(job_id: str) -> bool:
    if not SUPABASE_KEY:
        return False
    key = f"{JOB_PREFIX}{job_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.delete(
            url,
            headers=_headers(),
            params={"key": f"eq.{key}"},
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def save_topic_to_supabase(topic_dict: Dict[str, Any]) -> bool:
    """Upsert a topic item into Supabase `app_settings` table under `agy_hub_topic:<id>`."""
    if not SUPABASE_KEY:
        return False
    topic_id = topic_dict["id"]
    key = f"{TOPIC_PREFIX}{topic_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    payload = {
        "key": key,
        "value": topic_dict,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        resp = _SESSION.post(
            url,
            headers=_headers({"Prefer": "resolution=merge-duplicates"}),
            json=payload,
            timeout=15,
        )
        return resp.status_code in (200, 201, 204)
    except Exception:
        return False


def get_topic_from_supabase(topic_id: str) -> Optional[Dict[str, Any]]:
    if not SUPABASE_KEY:
        return None
    key = f"{TOPIC_PREFIX}{topic_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.get(
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


def list_topics_from_supabase(limit: int = 200) -> List[Dict[str, Any]]:
    if not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.get(
            url,
            headers=_headers(),
            params={
                "key": f"like.{TOPIC_PREFIX}*",
                "select": "key,value",
                "order": "updated_at.desc",
                "limit": str(int(limit)),
            },
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        rows = resp.json()
        topics = []
        for r in rows:
            val = _parse_val(r.get("value"))
            if val and val.get("id"):
                topics.append(val)
        topics.sort(key=lambda x: (int(x.get("order_no") or 9999), x.get("created_at", "")))
        return topics
    except Exception:
        return []


def delete_topic_from_supabase(topic_id: str) -> bool:
    if not SUPABASE_KEY:
        return False
    key = f"{TOPIC_PREFIX}{topic_id}"
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.delete(
            url,
            headers=_headers(),
            params={"key": f"eq.{key}"},
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def update_host_heartbeat(lan_url: str = "", tunnel_url: str = "", online: bool = True):
    if not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    now_epoch = time.time() if online else 0
    payload = {
        "key": HOST_STATUS_KEY,
        "value": {
            "online": bool(online),
            "last_seen": now_epoch,
            "last_seen_epoch": now_epoch,
            "last_seen_str": datetime.datetime.now(TH_TZ).strftime("%Y-%m-%d %H:%M:%S") if online else "⛔ ปิดปรับปรุงชั่วคราว",
            "lan_url": lan_url,
            "tunnel_url": tunnel_url,
        },
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        _SESSION.post(
            url,
            headers=_headers({"Prefer": "resolution=merge-duplicates"}),
            json=payload,
            timeout=8,
        )
    except Exception:
        pass


def get_host_heartbeat() -> Dict[str, Any]:
    if not SUPABASE_KEY:
        return {"online": False}
    url = f"{SUPABASE_URL}/rest/v1/app_settings"
    try:
        resp = _SESSION.get(
            url,
            headers=_headers(),
            params={"key": f"eq.{HOST_STATUS_KEY}", "select": "value"},
            timeout=8,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if rows:
                val = _parse_val(rows[0].get("value"))
                if val:
                    last_ts = float(val.get("last_seen_epoch") or val.get("last_seen") or 0)
                    is_fresh = (time.time() - last_ts) < 45 if last_ts > 0 else False
                    val["online"] = bool(val.get("online", False)) and is_fresh
                    return val
    except Exception:
        pass
    return {"online": False}
