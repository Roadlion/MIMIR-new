# backend/app/main.py
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

from .routers import articles, sentiment, prices, refresh, taxonomy, niche, portfolio
from .config import get_settings

settings = get_settings()

app = FastAPI(
    title="MIMIR",
    version="1.0.0",
    description="Market Intelligence & Macroeconomic Indicator Reactor"
)

@app.on_event("startup")
async def startup_event():
    from .pipeline.background_worker import start_background_worker
    start_background_worker()

# --- API Routes ---
app.include_router(articles.router, prefix="/api/v1", tags=["articles"])
app.include_router(sentiment.router, prefix="/api/v1", tags=["sentiment"])
app.include_router(prices.router, prefix="/api/v1", tags=["prices"])
app.include_router(refresh.router, prefix="/api/v1", tags=["refresh"])
app.include_router(taxonomy.router, prefix="/api/v1", tags=["taxonomy"])
app.include_router(niche.router, prefix="/api/v1", tags=["niche"])
app.include_router(portfolio.router, prefix="/api/v1", tags=["portfolio"])
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

@app.get("/asset/{ticker}", response_class=HTMLResponse)
async def finance_page(request: Request, ticker: str):
    return templates.TemplateResponse(request, "finance.html", {"ticker": ticker.upper()})

@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    return templates.TemplateResponse(request, "watchlist.html")

@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse(request, "portfolio.html")

@app.get("/taxonomy", response_class=HTMLResponse)
async def taxonomy_page(request: Request):
    return templates.TemplateResponse(request, "taxonomy.html")

@app.get("/guerilla", response_class=HTMLResponse)
async def guerilla_page(request: Request):
    return templates.TemplateResponse(request, "guerilla.html")

@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    return templates.TemplateResponse(request, "map.html")

@app.get("/health")
async def health():
    return {"status": "MIMIR is awake", "mode": settings.mode}

@app.get("/ping")
async def ping():
    return {"ping": "pong"}