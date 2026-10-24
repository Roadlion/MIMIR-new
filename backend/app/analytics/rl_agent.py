import numpy as np
import random
from collections import defaultdict

class MimirRLAgent:
    """
    Q-Learning Agent for Dynamic Trade Execution.
    State: (Predicted_Overshoot, Current_PnL_Tier, Volatility_Tier, Days_In_Trade)
    Action Space: 
      0: HOLD
      1: SELL_25%
      2: SELL_50%
      3: SELL_100%
    """
    def __init__(self, alpha=0.1, gamma=0.9, epsilon=0.1):
        self.q_table = defaultdict(lambda: np.zeros(4))
        self.alpha = alpha     # Learning rate
        self.gamma = gamma     # Discount factor
        self.epsilon = epsilon # Exploration rate
        self.actions = [0, 1, 2, 3]
        
    def discretize_state(self, predicted_overshoot, current_pnl, volatility, days_in_trade):
        """Quantizes continuous variables into discrete bins for Q-table states."""
        # 3 tiers of overshoot: Low (<1%), Med (1-3%), High (>3%)
        os_tier = 0 if predicted_overshoot < 0.01 else (1 if predicted_overshoot < 0.03 else 2)
        
        # 5 tiers of PnL: Severe Loss, Loss, Flat, Profit, Huge Profit
        pnl_tier = 0
        if current_pnl < -0.05: pnl_tier = 0
        elif current_pnl < -0.01: pnl_tier = 1
        elif current_pnl < 0.02: pnl_tier = 2
        elif current_pnl < 0.10: pnl_tier = 3
        else: pnl_tier = 4
            
        # Volatility: Low, High
        vol_tier = 0 if volatility < 0.02 else 1
        
        # Days in trade: Fresh (0-2), Mid (3-7), Old (>7)
        time_tier = 0 if days_in_trade <= 2 else (1 if days_in_trade <= 7 else 2)
        
        return (os_tier, pnl_tier, vol_tier, time_tier)
        
    def choose_action(self, state, is_training=True):
        if is_training and random.uniform(0, 1) < self.epsilon:
            return random.choice(self.actions) # Explore
        return np.argmax(self.q_table[state])  # Exploit
        
    def update(self, state, action, reward, next_state):
        best_next_action = np.argmax(self.q_table[next_state])
        td_target = reward + self.gamma * self.q_table[next_state][best_next_action]
        td_error = td_target - self.q_table[state][action]
        self.q_table[state][action] += self.alpha * td_error
        
    def calculate_reward(self, pnl_change, days_in_trade, is_terminal_loss=False):
        """
        Rewards the agent for locking in profits, heavily penalizes 
        letting trades turn into severe losses.
        Applies a time-decay penalty (-0.5 points per day) to encourage faster capital turnover.
        """
        if is_terminal_loss:
            return -100.0 # Severe penalty for hitting stop loss
        
        base_reward = pnl_change * 100.0 # Standard reward is basis point PnL gain
        time_penalty = days_in_trade * 0.5
        
        return base_reward - time_penalty
