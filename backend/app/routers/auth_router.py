# backend/app/routers/auth_router.py
from fastapi import APIRouter, HTTPException, Depends, Response, status
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
from ..database import get_db_connection_dict
from ..config import get_settings
from ..auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    get_admin_user
)

router = APIRouter()
settings = get_settings()
schema = settings.mimir_schema

class LoginRequest(BaseModel):
    username: str
    password: str

class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"
    daily_token_quota: Optional[int] = 100

class ResetPasswordRequest(BaseModel):
    new_password: str

class UpdateQuotaRequest(BaseModel):
    daily_token_quota: int

@router.post("/auth/login")
def login(payload: LoginRequest, response: Response):
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, username, password_hash, role, daily_token_quota FROM {schema}.mimir_users WHERE username = %s",
            (payload.username.strip(),)
        )
        user = cur.fetchone()
        cur.close()
        
        if not user or not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
            
        token = create_access_token({
            "user_id": user["id"],
            "username": user["username"],
            "role": user["role"]
        })
        
        # Set HttpOnly cookie for seamless web browsing
        response.set_cookie(
            key="mimir_session",
            value=token,
            httponly=True,
            samesite="lax",
            max_age=86400 * 7,
            path="/"
        )
        
        return {
            "status": "success",
            "message": "Login successful",
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "daily_token_quota": user["daily_token_quota"]
            }
        }
    finally:
        conn.close()

@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(key="mimir_session", path="/")
    return {"status": "success", "message": "Logged out successfully"}

@router.get("/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection_dict()
    try:
        today = date.today()
        cur = conn.cursor()
        cur.execute(
            f"SELECT message_count FROM {schema}.mimir_oracle_daily_usage WHERE user_id = %s AND usage_date = %s",
            (current_user["id"], today)
        )
        usage_row = cur.fetchone()
        cur.close()
        
        usage_count = usage_row["message_count"] if usage_row else 0
        quota = current_user["daily_token_quota"]
        
        return {
            "id": current_user["id"],
            "username": current_user["username"],
            "role": current_user["role"],
            "daily_token_quota": quota,
            "today_usage": usage_count,
            "remaining_quota": max(0, quota - usage_count)
        }
    finally:
        conn.close()

# --- ADMIN ENDPOINTS ---

@router.get("/admin/users")
def list_users(admin_user: dict = Depends(get_admin_user)):
    conn = get_db_connection_dict()
    try:
        today = date.today()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT u.id, u.username, u.role, u.daily_token_quota, u.created_at,
                   COALESCE(use.message_count, 0) as today_usage
            FROM {schema}.mimir_users u
            LEFT JOIN {schema}.mimir_oracle_daily_usage use 
              ON u.id = use.user_id AND use.usage_date = %s
            ORDER BY u.id ASC
        """, (today,))
        users = cur.fetchall()
        cur.close()
        return {"users": [dict(u) for u in users]}
    finally:
        conn.close()

@router.post("/admin/users")
def create_user(payload: UserCreateRequest, admin_user: dict = Depends(get_admin_user)):
    if not payload.username or len(payload.username.strip()) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if not payload.password or len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {schema}.mimir_users WHERE username = %s", (payload.username.strip(),))
        if cur.fetchone():
            cur.close()
            raise HTTPException(status_code=400, detail="Username already exists")
            
        hashed = hash_password(payload.password)
        cur.execute(
            f"""INSERT INTO {schema}.mimir_users (username, password_hash, role, daily_token_quota)
               VALUES (%s, %s, %s, %s) RETURNING id, username, role, daily_token_quota""",
            (payload.username.strip(), hashed, payload.role or "user", payload.daily_token_quota or 100)
        )
        new_user = cur.fetchone()
        conn.commit()
        cur.close()
        return {"status": "success", "user": dict(new_user)}
    finally:
        conn.close()

@router.post("/admin/users/{user_id}/reset-password")
def reset_password(user_id: int, payload: ResetPasswordRequest, admin_user: dict = Depends(get_admin_user)):
    if not payload.new_password or len(payload.new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {schema}.mimir_users WHERE id = %s", (user_id,))
        if not cur.fetchone():
            cur.close()
            raise HTTPException(status_code=404, detail="User not found")

        hashed = hash_password(payload.new_password)
        cur.execute(
            f"UPDATE {schema}.mimir_users SET password_hash = %s WHERE id = %s",
            (hashed, user_id)
        )
        conn.commit()
        cur.close()
        return {"status": "success", "message": f"Password reset for user ID {user_id}"}
    finally:
        conn.close()

@router.post("/admin/users/{user_id}/quota")
def update_quota(user_id: int, payload: UpdateQuotaRequest, admin_user: dict = Depends(get_admin_user)):
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {schema}.mimir_users SET daily_token_quota = %s WHERE id = %s RETURNING id, username, daily_token_quota",
            (payload.daily_token_quota, user_id)
        )
        updated = cur.fetchone()
        conn.commit()
        cur.close()
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")
        return {"status": "success", "user": dict(updated)}
    finally:
        conn.close()

@router.delete("/admin/users/{user_id}")
def delete_user(user_id: int, admin_user: dict = Depends(get_admin_user)):
    if user_id == admin_user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own admin account")

    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {schema}.mimir_users WHERE id = %s RETURNING id", (user_id,))
        deleted = cur.fetchone()
        conn.commit()
        cur.close()
        if not deleted:
            raise HTTPException(status_code=404, detail="User not found")
        return {"status": "success", "message": f"User ID {user_id} deleted"}
    finally:
        conn.close()
