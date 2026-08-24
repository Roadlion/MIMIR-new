# backend/app/routers/sitrep.py
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from typing import Optional, List, Dict, Any
from ..services.sitrep_service import compile_sitrep, get_latest_sitrep
from ..database import get_db_connection
from ..config import get_settings

router = APIRouter()
settings = get_settings()

@router.get("/latest")
def get_latest_situation_report():
    """
    Returns the latest MIMIR Situation Report (Sit Rep).
    If no report exists, compiles one automatically.
    """
    sitrep = get_latest_sitrep()
    if not sitrep:
        sitrep = compile_sitrep(report_type="ON_DEMAND")
    return sitrep

@router.post("/generate")
def generate_situation_report(
    background_tasks: BackgroundTasks,
    report_type: str = Query("ON_DEMAND", description="PERIODIC, ON_DEMAND, or EVENT_TRIGGERED")
):
    """
    Triggers an immediate fresh Sit Rep compilation and posts it to Discord.
    """
    try:
        sitrep = compile_sitrep(report_type=report_type)
        return {
            "status": "success",
            "message": "Situation Report generated successfully.",
            "sitrep": sitrep
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate Sit Rep: {e}")

@router.get("/history")
def get_sitrep_history(limit: int = Query(10, ge=1, le=50)):
    """
    Returns recent historical Situation Reports.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, created_at, report_type, title, headline_summary, macro_snapshot
            FROM {settings.mimir_schema}.mimir_sitreps
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        reports = []
        for r in rows:
            reports.append({
                "id": r[0],
                "created_at": r[1].isoformat() if r[1] else None,
                "report_type": r[2],
                "title": r[3],
                "headline_summary": r[4],
                "macro_snapshot": r[5],
            })
        return {"reports": reports}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        cur.close()
        conn.close()
