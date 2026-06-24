# backend/app/main.py
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

from .routers import articles, sentiment, prices, refresh
from .config import get_settings

settings = get_settings()

app = FastAPI(
    title="MIMIR",
    version="1.0.0",
    description="Market Intelligence & Macroeconomic Indicator Reactor"
)

# --- API Routes ---
app.include_router(articles.router, prefix="/api/v1", tags=["articles"])
app.include_router(sentiment.router, prefix="/api/v1", tags=["sentiment"])
app.include_router(prices.router, prefix="/api/v1", tags=["prices"])
app.include_router(refresh.router, prefix="/api/v1", tags=["refresh"])

# --- Static files (for CSS, JS, images) ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(BASE_DIR, "frontend", "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --- Templates ---
TEMPLATES_DIR = os.path.join(BASE_DIR, "frontend", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# --- HTML Pages (served from templates) ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/articles", response_class=HTMLResponse)
async def articles_page(request: Request):
    return templates.TemplateResponse(request, "articles.html")

@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    return templates.TemplateResponse(request, "watchlist.html")

@app.get("/health")
async def health():
    return {"status": "MIMIR is awake", "mode": settings.mode}

@app.get("/ping")
async def ping():
    return {"ping": "pong"}