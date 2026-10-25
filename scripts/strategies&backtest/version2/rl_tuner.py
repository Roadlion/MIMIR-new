"""
Generic RL parameter tuner.

Unlike the old TradingParamEnv (hardcoded to DualHedge's 6 specific
parameters), GenericParamEnv builds its action space directly from
whatever StaticStrategy you give it -- via strategy.param_space. That
means the SAME PPO training/inference code works for DualHedgeStrategy,
MeanReversionStrategy, or any future strategy, with zero changes here.

Feature columns are also arbitrary: pass any mix of TA columns
(ou_zscore, ewma_vol, rsi_14, ...) and/or sentiment columns
(sentiment_score, sentiment_momentum, ...) -- the env just reads
whatever column names you give it out of the dataframe.
"""

import os
import numpy as np
import pandas as pd

try:
    import gymnasium as gym
    from gymnasium import spaces
    from stable_baselines3 import PPO
    HAS_RL = True
except ImportError:
    HAS_RL = False


if HAS_RL:

    class GenericParamEnv(gym.Env):
        """Gym env where PPO's action IS a full param_space assignment for
        the wrapped strategy, and the reward is that strategy's realized
        PnL (minus a drawdown penalty) under those params.

        Retuning only happens while the strategy is FLAT (mirrored from
        the live runner's freeze-during-position rule), so the agent
        learns "what params should the NEXT trade use" rather than being
        able to yank an open trade's exit target around mid-flight.
        """

        def __init__(self, strategy_cls, base_kwargs: dict, df: pd.DataFrame,
                     feature_cols: list, initial_balance: float = 10_000.0):
            super().__init__()
            self.strategy_cls = strategy_cls
            self.base_kwargs = base_kwargs
            self.df = df.reset_index(drop=True)
            self.feature_cols = list(feature_cols)
            self.initial_balance = initial_balance
            self.n_bars = len(self.df)

            template = strategy_cls(**base_kwargs)
            self.param_names = list(template.param_space.keys())
            self.param_ranges = [template.param_space[n] for n in self.param_names]

            self.action_space = spaces.Box(
                low=-1.0, high=1.0, shape=(len(self.param_names),), dtype=np.float32
            )
            self.observation_space = spaces.Box(
                low=-np.inf, high=np.inf, shape=(len(self.feature_cols) + 1,), dtype=np.float32
            )
            self.reset()

        def _decode_action(self, action) -> dict:
            return {
                name: float(np.interp(a, [-1, 1], [lo, hi]))
                for name, (lo, hi), a in zip(self.param_names, self.param_ranges, action)
            }

        def _get_obs(self):
            idx = min(self.current_step, self.n_bars - 1)
            row = self.df.iloc[idx]
            vals = [float(row.get(c, 0.0)) for c in self.feature_cols]
            dd = (self.balance - self.peak_balance) / self.peak_balance if self.peak_balance > 0 else 0.0
            obs = np.array(vals + [dd], dtype=np.float32)
            return np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)

        def reset(self, seed=None, options=None):
            super().reset(seed=seed)
            self.current_step = min(100, self.n_bars - 1) if self.n_bars > 100 else 0
            self.balance = self.initial_balance
            self.peak_balance = self.initial_balance
            self.strategy = self.strategy_cls(**self.base_kwargs)
            return self._get_obs(), {}

        def step(self, action):
            if self.strategy.state == "FLAT":
                self.strategy.set_params(self._decode_action(action))

            row = self.df.iloc[self.current_step]
            prev_row = self.df.iloc[max(0, self.current_step - 1)]
            price = row["close"]
            prev_price = prev_row["close"]
            features = {c: row.get(c, 0.0) for c in self.feature_cols}

            net_signal = self.strategy.net_position
            mkt_ret = (price - prev_price) / prev_price if prev_price > 0 else 0.0
            step_return = net_signal * mkt_ret
            self.balance *= (1.0 + step_return)
            self.peak_balance = max(self.peak_balance, self.balance)

            self.strategy.tick(price, features)

            self.current_step += 1
            terminated = self.current_step >= self.n_bars - 1
            truncated = self.balance <= 0.5 * self.initial_balance
            dd = (self.balance - self.peak_balance) / self.peak_balance if self.peak_balance > 0 else 0.0
            reward = step_return - 0.25 * abs(dd)
            obs = (self._get_obs() if not terminated
                   else np.zeros(len(self.feature_cols) + 1, dtype=np.float32))
            return obs, reward, terminated, truncated, {}


class RLParameterTuner:
    """Trains/loads a PPO meta-controller that retunes ANY StaticStrategy's
    param_space using arbitrary features (TA, sentiment, ...).

    Usage:
        tuner = RLParameterTuner(DualHedgeStrategy, {}, feature_cols=[
            "ou_zscore", "ewma_vol", "ou_theta", "rsi_14", "sentiment_score",
        ])
        tuner.train(historical_df)                 # or tuner.load()
        params = tuner.predict_params(live_features)
    """

    def __init__(self, strategy_cls, base_kwargs: dict, feature_cols: list,
                 model_path: str = None, total_timesteps: int = 25_000):
        if not HAS_RL:
            raise ImportError("gymnasium and stable_baselines3 are required for RLParameterTuner")
        self.strategy_cls = strategy_cls
        self.base_kwargs = base_kwargs
        self.feature_cols = list(feature_cols)
        self.total_timesteps = total_timesteps
        self.model_path = model_path
        self._model = None
        if model_path and os.path.isfile(model_path):
            self._model = PPO.load(model_path)

    def load(self) -> bool:
        if self.model_path and os.path.isfile(self.model_path):
            self._model = PPO.load(self.model_path)
            return True
        return False

    def train(self, df: pd.DataFrame):
        env = GenericParamEnv(self.strategy_cls, self.base_kwargs, df, self.feature_cols)
        self._model = PPO("MlpPolicy", env, verbose=0, learning_rate=3e-4, n_steps=256)
        self._model.learn(total_timesteps=self.total_timesteps)
        if self.model_path:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            self._model.save(self.model_path)
        return self._model

    def predict_params(self, features: dict, balance_dd: float = 0.0) -> dict:
        if self._model is None:
            return {}
        vals = [float(features.get(c, 0.0)) for c in self.feature_cols] + [balance_dd]
        obs = np.nan_to_num(np.array(vals, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        action, _ = self._model.predict(obs, deterministic=True)

        template = self.strategy_cls(**self.base_kwargs)
        return {
            name: float(np.interp(a, [-1, 1], [lo, hi]))
            for name, (lo, hi), a in zip(template.param_space.keys(),
                                          template.param_space.values(), action)
        }
