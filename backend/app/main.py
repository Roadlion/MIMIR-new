# backend/app/main.py
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
import os

from .routers import articles, sentiment, prices, refresh, taxonomy, niche, portfolio, backtest, trade_alerts, research
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
    from .utils.logo_downloader import preseed_portfolio_logos
    import threading
    start_background_worker()
    threading.Thread(target=preseed_portfolio_logos, daemon=True).start()

# --- API Routes ---
app.include_router(articles.router, prefix="/api/v1", tags=["articles"])
app.include_router(sentiment.router, prefix="/api/v1", tags=["sentiment"])
app.include_router(prices.router, prefix="/api/v1", tags=["prices"])
app.include_router(refresh.router, prefix="/api/v1", tags=["refresh"])
app.include_router(taxonomy.router, prefix="/api/v1", tags=["taxonomy"])
app.include_router(niche.router, prefix="/api/v1", tags=["niche"])
app.include_router(portfolio.router, prefix="/api/v1", tags=["portfolio"])
app.include_router(trade_alerts.router, prefix="/api/v1", tags=["alerts"])
app.include_router(research.router, prefix="/api/v1/research", tags=["research"])
app.include_router(backtest.router)
# --- Static files (for CSS, JS, images) ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(BASE_DIR, "frontend", "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --- Templates ---
TEMPLATES_DIR = os.path.join(BASE_DIR, "frontend", "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "img", "mimir_logo.png"))

# --- HTML Pages (served from templates) ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/articles", response_class=HTMLResponse)
async def articles_page(request: Request):
    return templates.TemplateResponse(request, "articles.html")

@app.get("/social")
async def social_page(request: Request):
    return RedirectResponse(url="/articles")

@app.get("/asset/{ticker}", response_class=HTMLResponse)
async def finance_page(request: Request, ticker: str):
    return templates.TemplateResponse(request, "finance.html", {"ticker": ticker.upper()})

@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    return templates.TemplateResponse(request, "watchlist.html")

@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse(request, "portfolio.html")

@app.get("/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request):
    return templates.TemplateResponse(request, "alerts.html")

@app.get("/taxonomy", response_class=HTMLResponse)
async def taxonomy_page(request: Request):
    return templates.TemplateResponse(request, "taxonomy.html")

@app.get("/guerilla", response_class=HTMLResponse)
async def guerilla_page(request: Request):
    return templates.TemplateResponse(request, "guerilla.html")

@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    return templates.TemplateResponse(request, "map.html")

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request):
    return templates.TemplateResponse(request, "backtest.html")

@app.get("/alphas", response_class=HTMLResponse)
async def alphas_page(request: Request):
    return templates.TemplateResponse(request, "alphas.html")

@app.get("/oracle", response_class=HTMLResponse)
async def oracle_page(request: Request):
    return templates.TemplateResponse(request, "research_chat.html")

@app.get("/health")
async def health():
    return {"status": "MIMIR is awake", "mode": settings.mode}

@app.get("/ping")
async def ping():
    return {"ping": "pong"}