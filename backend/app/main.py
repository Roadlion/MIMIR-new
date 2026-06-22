# backend/app/main.py
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
from app.routers import articles
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="MIMIR",
    version="1.0.0",
    description="Market Intelligence & Macroeconomic Indicator Reactor"
)

# --- API Routes ---
app.include_router(articles.router, prefix="/api/v1", tags=["articles"])

# --- Frontend (Standalone) ---
static_dir = os.path.join(os.path.dirname(__file__), "../../frontend/static")
templates_dir = os.path.join(os.path.dirname(__file__), "../../frontend/templates")

if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
else:
    print("[WARN] frontend/static not found. Create it later.")

templates = Jinja2Templates(directory=templates_dir)

@app.get("/")
def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
def health():
    return {"status": "MIMIR is awake", "mode": settings.mode}

# --- Plugin export for ASGARD ---
from app.integration.plugin import MIMIRPlugin