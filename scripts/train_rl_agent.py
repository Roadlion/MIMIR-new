import os
import sys
import pickle
import numpy as np
import pandas as pd
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.analytics.rl_agent import MimirRLAgent

settings = get_settings()

def load_environment_data():
    """Loads historical prices to act as the RL environment."""
    conn = get_db_connection()
    sql = f"""
        SELECT ticker, date, open, high, low, close, volume 
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        WHERE date >= '2023-01-01'
        ORDER BY ticker, date ASC
    """
    df = pd.read_sql(sql, conn)
    conn.close()
    
    # Calculate volatility
    df['pct_change'] = df.groupby('ticker')['close'].pct_change()
    df['volatility'] = df.groupby('ticker')['pct_change'].transform(lambda x: x.rolling(20).std())
    df.dropna(subset=['volatility'], inplace=True)
    return df

def simulate_backtest():
    df = load_environment_data()
    agent = MimirRLAgent(epsilon=0.5) # Start with high exploration
    
    print(f"Loaded {len(df)} days of market environments. Beginning simulation...")
    
    episodes = 5000
    stop_loss = -0.05 # 5% hard stop
    
    for episode in range(episodes):
        # Pick a random ticker and a random start point to simulate an "XGBoost BUY Trigger"
        ticker = np.random.choice(df['ticker'].unique())
        df_ticker = df[df['ticker'] == ticker].reset_index(drop=True)
        
        if len(df_ticker) < 30:
            continue
            
        start_idx = np.random.randint(0, len(df_ticker) - 20)
        entry_price = df_ticker.loc[start_idx, 'close']
        
        # Simulate XGBoost predicting a random overshoot between 1% and 5%
        predicted_os = np.random.uniform(0.01, 0.05) 
        
        position_size = 1.0 # Start fully invested
        
        for step in range(1, 20): # Max hold time 20 days
            current_idx = start_idx + step
            if current_idx >= len(df_ticker):
                break
                
            current_price = df_ticker.loc[current_idx, 'close']
            volatility = df_ticker.loc[current_idx, 'volatility']
            
            pnl = (current_price - entry_price) / entry_price
            
            # Check Stop Loss
            if pnl <= stop_loss:
                state = agent.discretize_state(predicted_os, pnl, volatility, step)
                reward = agent.calculate_reward(pnl, step, is_terminal_loss=True)
                agent.update(state, 3, reward, state) # Action 3 = SELL_100%
                break
                
            state = agent.discretize_state(predicted_os, pnl, volatility, step)
            action = agent.choose_action(state, is_training=True)
            
            # Calculate reward based on action
            reward = 0
            if action == 1: # SELL 25%
                reward = agent.calculate_reward(pnl * 0.25, step)
                position_size -= 0.25
            elif action == 2: # SELL 50%
                reward = agent.calculate_reward(pnl * 0.50, step)
                position_size -= 0.50
            elif action == 3: # SELL 100%
                reward = agent.calculate_reward(pnl * 1.0, step)
                position_size = 0.0
            else: # HOLD
                reward = agent.calculate_reward(0, step) # Just takes the time penalty
                
            # Assume next state is roughly similar for TD-update purposes
            next_state = state 
            agent.update(state, action, reward, next_state)
            
            if position_size <= 0.01:
                break
                
        # Decay exploration
        if agent.epsilon > 0.05:
            agent.epsilon *= 0.999

    print("Training complete. Q-Table sample:")
    count = 0
    for state, q_vals in agent.q_table.items():
        if count < 5:
            print(f"State: {state} | Q-Values (HOLD, SELL25, SELL50, SELL100): {q_vals}")
            count += 1
            
    # Save the agent
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    model_dir = PROJECT_ROOT / "backend" / "app" / "analytics" / "models" / "rl_models"
    os.makedirs(model_dir, exist_ok=True)
    
    # Convert defaultdict to dict for pickling
    q_table_dict = dict(agent.q_table)
    out_path = os.path.join(model_dir, "q_table.pkl")
    with open(out_path, "wb") as f:
        pickle.dump(q_table_dict, f)
        
    print(f"Saved Q-Table to {out_path}")

if __name__ == "__main__":
    simulate_backtest()
