# backend/app/routers/taxonomy.py
from fastapi import APIRouter, HTTPException, Body
from typing import List, Dict, Optional
from backend.app.database import get_db_connection
from backend.app.sentiment.asset_mapper import ASSET_TO_TICKER

router = APIRouter()

@router.get("/taxonomy/assets")
def get_taxonomy_assets():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Fetch dynamic mappings
        cur.execute("SELECT asset_name, ticker FROM yggdrasil.mimir_dynamic_tickers")
        dynamic_rows = cur.fetchall()
        dynamic_mappings = {row[0].lower().strip(): (row[0], row[1]) for row in dynamic_rows}
        
        # 2. Fetch all unique assets in mimir_sentiment_impacts and their currently assigned tickers
        cur.execute("""
            SELECT DISTINCT asset_name, ticker 
            FROM yggdrasil.mimir_sentiment_impacts
        """)
        impact_rows = cur.fetchall()
        impact_assets = {}
        for row in impact_rows:
            if row[0]:
                name = row[0].strip()
                ticker = row[1]
                impact_assets[name.lower().strip()] = (name, ticker)
                
        cur.close()
        conn.close()
        
        # Combine everything
        all_assets_map = {}
        
        # A. Process static mappings
        for static_name, ticker in ASSET_TO_TICKER.items():
            display_name = static_name.title()
            all_assets_map[static_name] = {
                "asset_name": display_name,
                "ticker": ticker,
                "source": "static"
            }
            
        # B. Process dynamic mappings
        for key, (display_name, ticker) in dynamic_mappings.items():
            all_assets_map[key] = {
                "asset_name": display_name,
                "ticker": ticker,
                "source": "dynamic"
            }
            
        # C. Process impact mappings (to capture any missing/unmapped ones)
        for key, (display_name, ticker) in impact_assets.items():
            if key not in all_assets_map:
                all_assets_map[key] = {
                    "asset_name": display_name,
                    "ticker": ticker if ticker else None,
                    "source": "missing" if not ticker else "historical"
                }
            elif not all_assets_map[key]["ticker"] and ticker:
                all_assets_map[key]["ticker"] = ticker
                
        # Convert to sorted list
        assets_list = list(all_assets_map.values())
        assets_list.sort(key=lambda x: x["asset_name"].lower())
        
        return {"assets": assets_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/taxonomy/update")
def update_taxonomy_asset(payload: Dict = Body(...)):
    asset_name = payload.get("asset_name")
    ticker = payload.get("ticker")
    
    if not asset_name:
        raise HTTPException(status_code=400, detail="asset_name is required")
        
    clean_name = asset_name.strip()
    clean_ticker = ticker.strip().upper() if ticker else None
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        if clean_ticker:
            # 1. Update dynamic tickers
            cur.execute("""
                INSERT INTO yggdrasil.mimir_dynamic_tickers (asset_name, ticker)
                VALUES (%s, %s)
                ON CONFLICT (asset_name) DO UPDATE SET ticker = EXCLUDED.ticker
            """, (clean_name, clean_ticker))
            
            # 2. Update sentiment impacts
            cur.execute("""
                UPDATE yggdrasil.mimir_sentiment_impacts
                SET ticker = %s
                WHERE asset_name = %s
            """, (clean_ticker, clean_name))
            
            # 3. Update active memory cache
            import backend.app.sentiment.asset_mapper as am
            am._DYNAMIC_TICKERS[clean_name.lower().strip()] = clean_ticker
            
            # 4. Trigger price fetch in background
            from backend.app.routers.prices import fetch_and_cache_ticker
            import threading
            
            def fetch_bg(t):
                bg_conn = get_db_connection()
                try:
                    fetch_and_cache_ticker(t, bg_conn)
                except Exception as err:
                    print(f"[TAXONOMY BG] Error fetching price for {t}: {err}")
                finally:
                    bg_conn.close()
                    
            threading.Thread(target=fetch_bg, args=(clean_ticker,), daemon=True).start()
        else:
            # Remove mapping
            cur.execute("""
                DELETE FROM yggdrasil.mimir_dynamic_tickers
                WHERE asset_name = %s
            """, (clean_name,))
            
            cur.execute("""
                UPDATE yggdrasil.mimir_sentiment_impacts
                SET ticker = NULL
                WHERE asset_name = %s
            """, (clean_name,))
            
            import backend.app.sentiment.asset_mapper as am
            am._DYNAMIC_TICKERS.pop(clean_name.lower().strip(), None)
            
        conn.commit()
        cur.close()
        conn.close()
        
        return {"status": "success", "asset_name": clean_name, "ticker": clean_ticker}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
