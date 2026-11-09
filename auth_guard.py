#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Authentication Guard for IG.dara Studio (Mandatory Login for Owner & Team)
==========================================================================
Enforces login before accessing ANY page or ANY API endpoint.
Uses HMAC-SHA256 signed HTTP-only session cookies (`igdara_session`).
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from supabase_client import AUTH_USERS_KEY, SUPABASE_KEY, get_setting, set_setting

SESSION_COOKIE_NAME = "igdara_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days


def _get_session_secret() -> bytes:
    seed = (
        os.environ.get("IGDARA_SESSION_SECRET")
        or os.environ.get("TOKEN_ENCRYPTION_KEY")
        or SUPABASE_KEY
        or "igdara-studio-session-secret-v1"
    )
    return hashlib.sha256(f"igdara-session:{seed}".encode("utf-8")).digest()


def _hash_password(password: str, salt: str = "igdara-salt-v1") -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()


def get_auth_users_config() -> Dict[str, Dict[str, str]]:
    """Return user credentials map (role -> {username, password_hash, display_name, role})."""
    owner_pw = os.environ.get("IGDARA_OWNER_PASSWORD", "igdara2026")
    team_pw = os.environ.get("IGDARA_TEAM_PASSWORD", "dara888")

    defaults = {
        "owner": {
            "username": "owner",
            "password_hash": _hash_password(owner_pw),
            "display_name": "เจ้าของระบบ (Owner)",
            "role": "owner",
        },
        "team": {
            "username": "team",
            "password_hash": _hash_password(team_pw),
            "display_name": "ทีมงานโพสต์เพจ (Staff)",
            "role": "team",
        },
    }

    stored = get_setting(AUTH_USERS_KEY)
    if isinstance(stored, dict) and "owner" in stored and "team" in stored:
        return stored
    return defaults


def update_user_passwords(
    new_owner_password: Optional[str] = None,
    new_team_password: Optional[str] = None,
) -> bool:
    cfg = get_auth_users_config()
    if new_owner_password and len(new_owner_password.strip()) >= 4:
        cfg["owner"]["password_hash"] = _hash_password(new_owner_password.strip())
    if new_team_password and len(new_team_password.strip()) >= 4:
        cfg["team"]["password_hash"] = _hash_password(new_team_password.strip())
    return set_setting(AUTH_USERS_KEY, cfg)


def authenticate_credentials(username_or_role: str, password: str) -> Optional[Dict[str, str]]:
    """Verify username/role + password (or direct password shortcut for quick staff login)."""
    u_clean = (username_or_role or "").strip().lower()
    p_clean = (password or "").strip()
    if not p_clean:
        return None

    users = get_auth_users_config()
    pw_hash = _hash_password(p_clean)

    # Match explicit username/role first
    if u_clean in ("owner", "admin", "เจ้าของ"):
        target = users.get("owner", {})
        if hmac.compare_digest(target.get("password_hash", ""), pw_hash):
            return {
                "username": "owner",
                "display_name": target.get("display_name", "เจ้าของระบบ (Owner)"),
                "role": "owner",
            }
    elif u_clean in ("team", "staff", "พนักงาน", "ทีมงาน"):
        target = users.get("team", {})
        if hmac.compare_digest(target.get("password_hash", ""), pw_hash):
            return {
                "username": "team",
                "display_name": target.get("display_name", "ทีมงานโพสต์เพจ (Staff)"),
                "role": "team",
            }

    # Also allow matching by password if user selected role or left username default
    for role_key, target in users.items():
        if hmac.compare_digest(target.get("password_hash", ""), pw_hash):
            return {
                "username": role_key,
                "display_name": target.get("display_name", role_key),
                "role": target.get("role", role_key),
            }

    return None


def create_session_token(user_info: Dict[str, str]) -> str:
    payload = {
        "username": user_info.get("username", "team"),
        "display_name": user_info.get("display_name", "ทีมงาน"),
        "role": user_info.get("role", "team"),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    sig = hmac.new(_get_session_secret(), b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64_payload}.{sig}"


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or "." not in token:
        return None
    try:
        b64_payload, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            _get_session_secret(),
            b64_payload.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64_payload.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def get_current_user_from_request(request: Request) -> Optional[Dict[str, Any]]:
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME, "")
    return verify_session_token(cookie_val)


def require_authenticated_user(request: Request) -> Dict[str, Any]:
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="กรุณาเข้าสู่ระบบก่อนใช้งาน (อนุญาตเฉพาะเจ้าของและทีมงานเท่านั้น)",
        )
    return user


def require_owner_user(request: Request) -> Dict[str, Any]:
    user = require_authenticated_user(request)
    if user.get("role") != "owner":
        raise HTTPException(
            status_code=403,
            detail="คำสั่งนี้สงวนสิทธิ์เฉพาะเจ้าของระบบ (Owner) เท่านั้น",
        )
    return user


LOGIN_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>เข้าสู่ระบบ — IG.dara Studio (สำหรับเจ้าของและทีมงาน)</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Kanit:wght@400;500;600;700&family=Sarabun:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Sarabun', -apple-system, BlinkMacSystemFont, sans-serif;
      background: linear-gradient(135deg, #F8FAFC 0%, #EFF6FF 50%, #FDF2F8 100%);
      color: #0F172A;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .login-card {
      width: 100%;
      max-width: 440px;
      background: #FFFFFF;
      border-radius: 20px;
      border: 1px solid #E2E8F0;
      box-shadow: 0 20px 40px -15px rgba(15, 23, 42, 0.08);
      padding: 36px 32px;
    }
    .brand-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: #EFF6FF;
      color: #2563EB;
      border: 1px solid #BFDBFE;
      font-family: 'Kanit', sans-serif;
      font-size: 13px;
      font-weight: 600;
      padding: 6px 14px;
      border-radius: 999px;
      margin-bottom: 16px;
    }
    h1 {
      font-family: 'Kanit', sans-serif;
      font-size: 26px;
      font-weight: 700;
      color: #0F172A;
      margin-bottom: 6px;
    }
    .subtitle {
      font-size: 14.5px;
      color: #475569;
      line-height: 1.5;
      margin-bottom: 24px;
    }
    .form-group {
      margin-bottom: 18px;
    }
    label {
      display: block;
      font-family: 'Kanit', sans-serif;
      font-size: 14px;
      font-weight: 600;
      color: #1E293B;
      margin-bottom: 8px;
    }
    select, input {
      width: 100%;
      padding: 12px 14px;
      font-size: 15px;
      font-family: 'Sarabun', sans-serif;
      border: 1.5px solid #CBD5E1;
      border-radius: 12px;
      background: #F8FAFC;
      color: #0F172A;
      transition: all 0.15s ease;
    }
    select:focus, input:focus {
      outline: none;
      border-color: #2563EB;
      background: #FFFFFF;
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }
    .btn-login {
      width: 100%;
      padding: 13px 20px;
      font-family: 'Kanit', sans-serif;
      font-size: 16px;
      font-weight: 600;
      color: #FFFFFF;
      background: linear-gradient(135deg, #2563EB 0%, #4F46E5 100%);
      border: none;
      border-radius: 12px;
      cursor: pointer;
      box-shadow: 0 8px 16px -4px rgba(37, 99, 235, 0.35);
      transition: transform 0.12s ease, box-shadow 0.12s ease;
      margin-top: 6px;
    }
    .btn-login:hover {
      transform: translateY(-1px);
      box-shadow: 0 12px 20px -4px rgba(37, 99, 235, 0.45);
    }
    .btn-login:disabled {
      opacity: 0.65;
      cursor: not-allowed;
      transform: none;
    }
    .err-box {
      display: none;
      background: #FEF2F2;
      border: 1px solid #FECACA;
      color: #DC2626;
      padding: 11px 14px;
      border-radius: 10px;
      font-size: 14px;
      margin-bottom: 16px;
      font-weight: 500;
    }
    .sec-note {
      margin-top: 22px;
      padding-top: 16px;
      border-top: 1px solid #F1F5F9;
      font-size: 12.5px;
      color: #64748B;
      line-height: 1.5;
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="login-card">
    <div class="brand-badge">✨ IG.dara Studio · ระบบโพสต์เพจอัตโนมัติ</div>
    <h1>เข้าสู่ระบบสำหรับทีมงาน</h1>
    <p class="subtitle">ระบบคลังข่าวไวรัล 3 ย่อหน้า + ภาพปก 4:5 และระบบโพสต์ลงเพจ Facebook อัตโนมัติ (เฉพาะเจ้าของและทีมงาน)</p>

    <div id="errBox" class="err-box"></div>

    <form id="loginForm" onsubmit="handleLogin(event)">
      <div class="form-group">
        <label for="roleSelect">สิทธิ์ผู้ใช้งาน</label>
        <select id="roleSelect" name="username">
          <option value="team">👥 ทีมงานโพสต์เพจ (Staff / Team)</option>
          <option value="owner">👑 เจ้าของระบบ (Owner / Admin)</option>
        </select>
      </div>
      <div class="form-group">
        <label for="passwordInput">รหัสผ่านเข้าใช้งาน</label>
        <input type="password" id="passwordInput" name="password" placeholder="กรอกรหัสผ่าน..." required autofocus />
      </div>
      <button type="submit" id="submitBtn" class="btn-login">🔐 เข้าสู่ระบบ IG.dara Studio</button>
    </form>

    <div class="sec-note">
      🛡️ ระบบรักษาความปลอดภัย: โทเคน Facebook (ADSAFF Publisher) และ Page Token ถูกเก็บรักษาฝั่งเซิร์ฟเวอร์เท่านั้น
    </div>
  </div>

  <script>
    async function handleLogin(e) {
      e.preventDefault();
      const errBox = document.getElementById('errBox');
      const btn = document.getElementById('submitBtn');
      const username = document.getElementById('roleSelect').value;
      const password = document.getElementById('passwordInput').value;

      errBox.style.display = 'none';
      btn.disabled = true;
      btn.textContent = 'กำลังตรวจสอบรหัสผ่าน...';

      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          window.location.href = '/';
        } else {
          errBox.textContent = data.detail || 'รหัสผ่านไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง';
          errBox.style.display = 'block';
          btn.disabled = false;
          btn.textContent = '🔐 เข้าสู่ระบบ IG.dara Studio';
        }
      } catch (err) {
        errBox.textContent = 'เกิดข้อผิดพลาดในการเชื่อมต่อเซิร์ฟเวอร์';
        errBox.style.display = 'block';
        btn.disabled = false;
        btn.textContent = '🔐 เข้าสู่ระบบ IG.dara Studio';
      }
    }
  </script>
</body>
</html>
"""
