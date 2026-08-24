# backend/app/auth.py
import hmac
import hashlib
import json
import base64
import time
import secrets
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, Security, Depends
from fastapi.security import APIKeyCookie, HTTPBearer, HTTPAuthorizationCredentials
from .database import get_db_connection_dict
from .config import get_settings

settings = get_settings()

SECRET_KEY = getattr(settings, "jwt_secret", "MIMIR_SECRET_KEY_CHANGE_IN_PRODUCTION_98237498")

cookie_scheme = APIKeyCookie(name="mimir_session", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with salt."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return f"{salt}${key.hex()}"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against PBKDF2 hash string."""
    try:
        parts = hashed_password.split('$')
        if len(parts) != 2:
            return False
        salt, key_hex = parts[0], parts[1]
        key = hashlib.pbkdf2_hmac(
            'sha256',
            plain_password.encode('utf-8'),
            salt.encode('utf-8'),
            100000
        )
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False

def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def _b64_decode(data: str) -> bytes:
    padding = '=' * (4 - (len(data) % 4))
    return base64.urlsafe_b64decode(data + padding)

def create_access_token(data: dict, expires_in_seconds: int = 86400 * 7) -> str:
    """Generates an HMAC-SHA256 signed session token."""
    payload = data.copy()
    payload['exp'] = int(time.time()) + expires_in_seconds
    
    json_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    payload_b64 = _b64_encode(json_bytes)
    
    signature = hmac.new(SECRET_KEY.encode('utf-8'), payload_b64.encode('utf-8'), hashlib.sha256).digest()
    sig_b64 = _b64_encode(signature)
    
    return f"{payload_b64}.{sig_b64}"

def decode_access_token(token: str) -> Optional[dict]:
    """Decodes and validates signature and expiration of session token."""
    try:
        parts = token.split('.')
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts[0], parts[1]
        
        expected_sig = hmac.new(SECRET_KEY.encode('utf-8'), payload_b64.encode('utf-8'), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64_encode(expected_sig), sig_b64):
            return None
        
        payload_json = _b64_decode(payload_b64)
        payload = json.loads(payload_json)
        
        if payload.get('exp', 0) < time.time():
            return None
            
        return payload
    except Exception:
        return None

def get_token_from_request(request: Request) -> Optional[str]:
    """Extracts session token from Authorization header or cookie."""
    # 1. Header check (Explicit Authorization header takes priority)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    # 2. Cookie check
    token = request.cookies.get("mimir_session")
    if token:
        return token
    return None

def get_current_user(request: Request) -> Dict[str, Any]:
    """FastAPI Dependency: Returns current logged-in user or raises 401."""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
        
    payload = decode_access_token(token)
    if not payload or "user_id" not in payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
        
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, username, role, daily_token_quota, created_at FROM {settings.mimir_schema}.mimir_users WHERE id = %s",
            (payload["user_id"],)
        )
        user = cur.fetchone()
        cur.close()
        
        if not user:
            raise HTTPException(status_code=401, detail="User account not found")
            
        return dict(user)
    finally:
        conn.close()

def get_optional_current_user(request: Request) -> Optional[Dict[str, Any]]:
    """FastAPI Dependency: Returns current user if authenticated, else None."""
    try:
        return get_current_user(request)
    except HTTPException:
        return None

def get_admin_user(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """FastAPI Dependency: Ensures current user is an Admin."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return current_user
