# 🌳 MIMIR: Market Intelligence & Macroeconomic Indicator Reactor

Welcome to **MIMIR** (Market Intelligence & Macroeconomic Indicator Reactor), a real-time institutional-grade market intelligence pipeline, macroeconomic sentiment analyzer, statistical arbitrage engine, and **unified quantitative trading reactor**.

This repository is designed to be easily navigated and understood by both **human developers** and **AI assistants** (e.g., Gemini, Claude, ChatGPT) to enable rapid project context bootstrapping and seamless pair programming.

---

## 🏎️ Core Philosophy: The Mad Max War Rig Transmission Architecture

Instead of running disparate, standalone technical indicators or uncoordinated alpha engines that generate contradictory signals and account drawdown, MIMIR enforces the **Single-Shaft War Rig Philosophy**:

> *"All engines drive the SAME crankshaft. No independent motors driving front wheels while another drives the rear. Everything converges into one single high-conviction drive shaft."*

```
                  ┌────────────────────────────────────────────────────────┐
                  │            WAR RIG CRANKSHAFT TRANSMISSION            │
                  └───────────────────────────┬────────────────────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │                                    │                                    │
         ▼                                    ▼                                    ▼
┌──────────────────┐               ┌───────────────────────┐             ┌─────────────────────┐
│    CYLINDER 1    │               │      CYLINDER 2       │             │     CYLINDER 3      │
│  Macro & Sector  │               │   Catalyst V8 Turbo   │             │ Microstructure +    │
│  Flow (Max 25pt) │               │     (Max 50 pts)      │             │ Bong Strats Quant   │
└────────┬─────────┘               └───────────┬───────────┘             │ (Max 28pt + Gate)   │
         │                                     │                         └──────────┬──────────┘
         │ • Stealth Accumulation: +25pt       │ • Turbo A (Pre-Earnings): 0-30pt   │ • Price >= 20D SMA: +10pt
         │ • Markup Momentum: +20pt            │ • Turbo B (Spillovers/News): 0-30pt│ • RSI 42-65 Launchpad: +8pt
         │ • Distribution Outflow: 0pt         │ • Twin-Turbo Synergy: +15pt bonus  │ • Vol Ratio >= 1.2x: +7pt
         │ • Macro Rate Shock: BLOCKED         │ • Quiet Period Aware (PR drop OK)  │ • Bollinger Squeeze: +4pt
         │                                     │                                    │ • Wyckoff Absorption: +5pt
         │                                     │                                    │ • OU Discount Pocket: +4pt
         │                                     │                                    │ • Vol Exhaustion: -8pt
         │                                     │                                    │ • OU Overextended: -8pt
         │                                     │                                    │ • HARD ASYMMETRY: R/R >= 2.5:1
         └─────────────────────────────────────┼────────────────────────────────────┘
                                               │
                                               ▼
                               ┌──────────────────────────────────┐
                               │    CONVICTION GATE: >= 75.0%     │
                               │     ASYMMETRY GATE: >= 2.5:1     │
                               │           PASSED?                │
                               └────────┬─────────────────────────┘
                                        │
                       ┌────────────────┴────────────────┐
                       │ YES                             │ NO
                       ▼                                 ▼
           ┌─────────────────────────┐       ┌────────────────────────┐
           │ EMIT WAR RIG CONVERGENCE│       │ DISCARD / NO ORDER     │
           │   • Entry: T+1 Open     │       │   (Prevents Over-      │
           │   • Target: 2.5-3x ATR  │       │    engineering & Drift)│
           │   • Stop: 1.5x ATR      │       └────────────────────────┘
           │   • Multi-Tier Ratchet  │
           └───────────┬─────────────┘
                       │
                       ▼ [NITROUS POD TRIGGER: >= 75% Conviction]
           ┌────────────────────────────────────────┐
           │     THE CASINO: NITROUS INJECTOR       │
           │ (Non-Linear Asymmetric Options Engine) │
           ├────────────────────────────────────────┤
           │ • Nitro Mode A: Bull Call Spreads      │
           │   3:1 - 5:1 Asymmetry, Capped Debit    │
           │ • Nitro Mode B: Volatility Harvester   │
           │   IV Rank < 35: High-Gamma Rocket      │
           │   IV Rank > 85: Post-Earnings IV Crush │
           └────────────────────────────────────────┘
```

---

## ⚡ The Modular Nitrous Oxide Injector (The Casino)

When the War Rig crankshaft converges on $\ge 75\%$ conviction (e.g., MU pre-earnings at 78% conviction or a high-magnitude spillover), the trader can hit the **NITROUS** button to deploy options leverage instead of linear common shares:
- **War Rig -> Casino Auto-Express Bridge (`backend/app/analytics/war_rig_nitrous.py`):** Ingests exact trigger price ($S_0$), stop loss ($1.5\times\text{ATR}$), target ($3.0\times\text{ATR}$), and holding window.
- **Nitro Mode A (Skew-Optimized Bull Call Vertical Spreads):** Solves for optimal call debit spread with the short strike anchored to the $3.0\times\text{ATR}$ target and long strike near ATM. Targets $3:1$ to $5:1$ payoff asymmetry with strictly capped debit downside, completely immune to overnight gap-downs below stop loss.
- **Nitro Mode B (Gamma Straddles & IV Crush Harvester):** Dynamically branches based on options implied volatility:
  - If $\text{IV Rank} < 35$: recommends cheap directional high-gamma calls or long straddles to ride volatility explosion into the catalyst.
  - If $\text{IV Rank} > 85$ (bloated retail hype): recommends bull put credit spreads beneath the $1.5\times\text{ATR}$ stop loss to harvest post-earnings volatility crush while preserving directional tailwinds.

---

## ⚡ The 3 Cylinders of the Crankshaft

### 1. Cylinder 1: Macro & Sector Inflow Transmission (Max 25 Points)
- **Sector Rotation Matrix (`backend/app/services/sector_rotation_service.py`):**
  - Evaluates 5-day and 20-day relative strength ($RS$) vs $SPY$ across all 11 US sectors.
  - Classifies sector phases into `STEALTH_ACCUMULATION` (+25 pts), `MARKUP` (+20 pts), `ACCUMULATION` (+15 pts), `CONSOLIDATION` (+10 pts), and `DISTRIBUTION` (0 pts).
- **Macro Rate & CPI Shock Gatekeeper (`backend/app/services/macro_tracker.py`):**
  - Detects hawkish Fed shocks, liquidity crunches, and 30-Year yield breakouts to dynamically block interest-rate-sensitive entries.

### 2. Cylinder 2: Catalyst V8 Twin-Turbo Engine (Max 50 Points)
- **Turbo A — Pre-Earnings Beat Engine:**
  - Evaluates SEC earnings calendar 2 to 14 days prior to quarterly print.
  - Combines pre-earnings institutional sentiment acceleration with historical EPS growth and operating margins.
- **Turbo B — Supply Chain Spillover & High-Magnitude News:**
  - Exploits the **"Trade the Ripple, Not the Splash"** principle: propagates sentiment impacts from Tier 1 headline movers across 21,300+ mapped supply chain edges with exponential decay.
- **Twin-Turbo Supercharging Synergy:**
  - When both Pre-Earnings Beat Momentum and Supply Chain Spillovers fire concurrently, awards a **+15.0 points synergy bonus**.

### 3. Cylinder 3: Microstructure & Quantitative Superchargers (Max 28 Points + Invalidation Gate)
- **Base Microstructure:** 20D/50D trend alignment (+10 pts), RSI momentum sweet spot $42 \le RSI \le 65$ (+8 pts), and institutional volume ratio $\ge 1.20x$ (+7 pts).
- **Bong Strats Quantitative Integrations (`bong_strats/`):**
  - **Bollinger Bandwidth Squeeze:** $Z < -1.0$ coiling consolidation awards **+4.0 pts**.
  - **Wyckoff Institutional Seller Absorption:** Bullish volume exhaustion reversal awards **+5.0 pts**.
  - **Ornstein-Uhlenbeck (OU) Mean-Reversion Calibration ($dX_t = 	heta(\mu - X_t)dt + \sigma dW_t$):** Statistical discount pocket ($-1.8 \le Z \le -0.5$) awards **+4.0 pts**.
  - **Capital Protection Penalties:** Volatility Exhaustion blow-off tops dock **-8.0 pts**; OU overextended traps ($Z > 2.2$) dock **-8.0 pts**.
- **Hard Asymmetry Gatekeeper:** Enforces a mandatory minimum **2.5:1 Risk/Reward ratio** and $\ge 10\%$ target upside before an order can be emitted.

---

## 🛡️ Rigorous Zero-Bias Execution & Risk Controls

All simulations, signals, and live executions enforce institutional bias controls:
1. **Zero Look-Ahead Bias:** Signal generated strictly on Day $T$; orders executed at Day $T+1$ Market Open.
2. **Realistic Execution Friction:** $10	ext{ bps}$ ($0.10\%$) round-trip transaction costs deducted from every single trade.
3. **Gap-Down Slippage:** Full overnight gap-down open penalties enforced on stop-loss breaches.
4. **Multi-Tier Dynamic Profit Trail:**
   - At $+3.0\%$ runup: Stop Loss moved to $+0.5\%$ (Breakeven).
   - At $+6.0\%$ runup: Stop Loss trailed to $+3.0\%$.
   - At $+10.0\%$ runup: Stop Loss trailed to $+6.5\%$.

---

## 📊 Performance Benchmark Matrix (June – September 2026)

Verified across 5,173 US equities and 10,091 candidate catalyst events via `scripts/backtest_war_rig_pipeline.py`:

| Architecture / Strategy | Trades | Win Rate | Avg Net PnL | Profit Factor | Max Drawdown | Cumulative Net Return |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Rogue Standalone Breakouts** (Naive Baseline) | 60 | 43.3% | -0.15% | 0.92 | 44.12% | -9.00% |
| **Baseline War Rig** (Pre-Bong Strats) | 28 | 71.4% | +2.06% | 2.97 | 16.07% | +57.55% |
| **War Rig + Bong Strats** (Bolted & Calibrated) | **35** | **74.3%** | **+2.25%** | **3.36** | **12.03%** | **+78.75%** |

---

## 📂 Codebase Directory & File Reference

### 📁 Backend Core (`backend/app/`)
* `main.py`: FastAPI application entry point; initializes background daemon workers and registers API routers.
* `config.py`: Environment and system settings manager via `pydantic-settings`.
* `database.py`: PostgreSQL connection pooler for `pantheon_db` schema `yggdrasil`.

### 📁 Quantitative Analytics (`backend/app/analytics/`)
* `war_rig_engine.py`: **The Central Crankshaft Transmission**. Unifies Macro, Sector Rotation, Pre-Earnings, Supply Chain Spillovers, and Bong Strats Microstructure into single-shaft conviction ($\ge 75\%$).
* `war_rig_nitrous.py`: **The Casino Nitrous Express Bridge**. Translates War Rig directional signals into skew-optimized Bull Call Vertical Spreads (3:1 to 5:1 asymmetry) and gamma/volatility harvest options structures.
* `technical_analysis.py`: Vectorized technical indicator computer (RSI, ATR, Bollinger Bands, Moving Averages).
* `paper_trader.py`: Automated paper trading engine enforcing US regular market session execution, staged sizing, and trailing ratchets.
* `signal_fusion.py`: Multi-factor signal combiner orchestrating the 10-minute automated War Rig universe scan.
* `sentiment_momentum.py`: Sentiment regime detection engine calculating 3-day sentiment velocity, acceleration, and retail divergence.
* `backtester.py`: Vectorized quant formula backtester supporting AST expressions and survivorship masks.

### 📁 Quantitative Models (`bong_strats/`)
* `indicators.py`: Ornstein-Uhlenbeck SDE calibration (`OUCalibrator`), Bollinger Bandwidth Z-Score models, and EWMA volatility regime classifiers.
* `static_strategies.py`: Wyckoff Institutional Seller Absorption algorithms (`VolumeExhaustionReversalStrategy`).

### 📁 Services & Pipelines (`backend/app/services/` & `backend/app/pipeline/`)
* `sector_rotation_service.py`: 11-sector relative strength matrix vs SPY for institutional accumulation detection.
* `macro_tracker.py`: Treasury yield monitoring and macro shock gatekeeper.
* `background_worker.py`: Asynchronous orchestrator running 9 background loops (price 5m, scrape 5m, sentiment 5m, War Rig scan 10m, paper trader 3m).
* `live_price_daemon.py`: Intraday price processor managing multi-tier profit ratchets and stop executions.
* `sentiment_processor.py`: DeepSeek LLM batch news sentiment scoring and entity resolution.
* `spillover_engine.py`: 2nd and 3rd order supply chain sentiment propagator across mapped asset dependency graphs.

### 📁 Execution & Verification Scripts (`scripts/`)
* `backtest_war_rig_pipeline.py`: Canonical zero-bias comparative backtest verifying the War Rig single-shaft architecture against legacy standalone technicals.
* `mt5_price_fetcher.py`: MetaTrader 5 API bridge streaming live 1-minute price ticks.
* `run_full_pipeline.py`: CLI master pipeline executor for news scraping, sentiment processing, and spillover generation.

---

## 🚀 Quickstart & Launcher

### Running MIMIR:
```cmd
:: One-click launch on Windows
run.bat
```

Or run manually via terminal:
```powershell
# 1. Start live price daemon in background
start /B .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py

# 2. Start MT5 price fetcher in background (optional if using live MT5)
start /B .venv\Scripts\python.exe scripts\mt5_price_fetcher.py

# 3. Start FastAPI server & automated workers
.venv\Scripts\uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```
- **Local Dashboard:** http://127.0.0.1:8000
- **API Documentation:** http://127.0.0.1:8000/docs
- **War Rig Diagnostic API:** `GET /api/v1/trade-alerts/war-rig/evaluate/{ticker}`
