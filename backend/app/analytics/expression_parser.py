# backend/app/analytics/expression_parser.py
import ast
import pandas as pd
import numpy as np
import textwrap

class FormulaParser:
    def __init__(self, dataframes):
        """
        dataframes: dict mapping field names to pivoted DataFrames (Dates x Tickers)
                    e.g., {'close': close_df, 'sentiment': sentiment_df, ...}
        """
        self.dfs = {k.lower(): v for k, v in dataframes.items()}

    def evaluate(self, expr_str: str) -> pd.DataFrame:
        """Parses and evaluates an expression string, returning a DataFrame of signals (Dates x Tickers)."""
        if not expr_str or not expr_str.strip():
            raise ValueError("Empty formula expression.")
        try:
            # Dedent to strip common leading indentation and strip surrounding empty lines
            dedented = textwrap.dedent(expr_str).strip()
            
            # Parse in exec mode to handle assignments and multi-line structures
            tree = ast.parse(dedented, mode='exec')
            if not tree.body:
                raise ValueError("No valid statements found in formula.")
                
            local_vars = {}
            result = None
            
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign):
                    # Evaluate RHS value
                    value = self._eval_node(stmt.value, local_vars)
                    # Assign to targets
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            local_vars[target.id.lower()] = value
                        else:
                            raise ValueError("Assignments are only supported for simple variable names (e.g. raw = ...).")
                elif isinstance(stmt, ast.Expr):
                    # Evaluate the expression statement
                    result = self._eval_node(stmt.value, local_vars)
                else:
                    raise ValueError(f"Unsupported statement type: {type(stmt)}")
            
            if result is None:
                raise ValueError("Formula must end with a return expression (e.g., last line must be a formula, not an assignment).")
                
            if not isinstance(result, pd.DataFrame):
                # If expression returns a constant (e.g. "5"), broadcast it to a DataFrame
                first_df = list(self.dfs.values())[0]
                result = pd.DataFrame(result, index=first_df.index, columns=first_df.columns)
            return result
        except Exception as e:
            raise ValueError(f"Formula evaluation failed: {e}")

    def _eval_node(self, node, local_vars=None):
        if local_vars is None:
            local_vars = {}
            
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body, local_vars)
            
        elif isinstance(node, ast.Constant):
            return node.value
        elif hasattr(ast, 'Num') and isinstance(node, ast.Num):  # Fallback for Python < 3.8
            return node.n
            
        elif isinstance(node, ast.Name):
            name = node.id.lower()
            if name in local_vars:
                return local_vars[name]
            elif name in self.dfs:
                return self.dfs[name]
            else:
                raise ValueError(f"Unknown data field or variable: '{name}'. Supported fields: {list(self.dfs.keys())}")
                
        elif isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand, local_vars)
            if isinstance(node.op, ast.USub):
                return -operand
            elif isinstance(node.op, ast.UAdd):
                return operand
            else:
                raise ValueError(f"Unsupported unary operator: {type(node.op)}")
                
        elif isinstance(node, ast.BinOp):
            left = self._eval_node(node.left, local_vars)
            right = self._eval_node(node.right, local_vars)
            
            # Binary operations between DataFrames or DataFrames and constants
            if isinstance(node.op, ast.Add):
                return left + right
            elif isinstance(node.op, ast.Sub):
                return left - right
            elif isinstance(node.op, ast.Mult):
                return left * right
            elif isinstance(node.op, ast.Div):
                # Using a small division epsilon to prevent division by zero or NaN explosion
                return left / (right + 1e-15) if isinstance(right, (int, float)) else left / (right.replace(0, 1e-15))
            else:
                raise ValueError(f"Unsupported binary operator: {type(node.op)}")
                
        elif isinstance(node, ast.Compare):
            left = self._eval_node(node.left, local_vars)
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("Comparison operator supports exactly two operands (e.g., x > y).")
            op = node.ops[0]
            right = self._eval_node(node.comparators[0], local_vars)
            
            if isinstance(op, ast.Gt):
                return left > right
            elif isinstance(op, ast.Lt):
                return left < right
            elif isinstance(op, ast.GtE):
                return left >= right
            elif isinstance(op, ast.LtE):
                return left <= right
            elif isinstance(op, ast.Eq):
                return left == right
            elif isinstance(op, ast.NotEq):
                return left != right
            else:
                raise ValueError(f"Unsupported comparison operator: {type(op)}")
                
        elif isinstance(node, ast.Call):
            func_name = node.func.id.lower()
            args = [self._eval_node(arg, local_vars) for arg in node.args]
            return self._eval_function(func_name, args)
            
        else:
            raise ValueError(f"Unsupported syntax construct: {type(node)}")

    def _eval_function(self, name, args):
        # 1. Cross-sectional operators
        if name == 'rank':
            if len(args) != 1:
                raise ValueError("rank() expects exactly 1 argument: rank(expression)")
            x = args[0]
            if not isinstance(x, pd.DataFrame):
                raise ValueError("rank() expects a data series/dataframe as argument.")
            return x.rank(axis=1, pct=True)
            
        elif name == 'scale':
            if len(args) != 1:
                raise ValueError("scale() expects exactly 1 argument: scale(expression)")
            x = args[0]
            if not isinstance(x, pd.DataFrame):
                raise ValueError("scale() expects a data series/dataframe.")
            abs_sum = x.abs().sum(axis=1)
            # Avoid division by zero if daily weights sum to 0
            return x.div(abs_sum.replace(0, 1e-15), axis=0)
            
        elif name == 'neutralize':
            if len(args) != 1:
                raise ValueError("neutralize() expects exactly 1 argument: neutralize(expression)")
            x = args[0]
            if not isinstance(x, pd.DataFrame):
                raise ValueError("neutralize() expects a data series/dataframe.")
            daily_mean = x.mean(axis=1)
            return x.sub(daily_mean, axis=0)
            
        elif name == 'zscore':
            if len(args) != 1:
                raise ValueError("zscore() expects exactly 1 argument: zscore(expression)")
            x = args[0]
            if not isinstance(x, pd.DataFrame):
                raise ValueError("zscore() expects a data series/dataframe.")
            mean = x.mean(axis=1)
            std = x.std(axis=1).replace(0, 1e-15)
            return x.sub(mean, axis=0).div(std, axis=0)

        # 2. Arithmetic / Unary functions
        elif name == 'abs':
            if len(args) != 1:
                raise ValueError("abs() expects exactly 1 argument.")
            x = args[0]
            return x.abs() if isinstance(x, pd.DataFrame) else abs(x)
            
        elif name == 'log':
            if len(args) != 1:
                raise ValueError("log() expects exactly 1 argument.")
            x = args[0]
            if isinstance(x, pd.DataFrame):
                return np.log(x.clip(lower=1e-15))
            return np.log(max(x, 1e-15))
            
        elif name == 'exp':
            if len(args) != 1:
                raise ValueError("exp() expects exactly 1 argument.")
            x = args[0]
            return np.exp(x) if isinstance(x, pd.DataFrame) else np.exp(x)
            
        elif name == 'sqrt':
            if len(args) != 1:
                raise ValueError("sqrt() expects exactly 1 argument.")
            x = args[0]
            if isinstance(x, pd.DataFrame):
                return np.sqrt(x.clip(lower=0.0))
            return np.sqrt(max(x, 0.0))
            
        elif name == 'sign':
            if len(args) != 1:
                raise ValueError("sign() expects exactly 1 argument.")
            x = args[0]
            return np.sign(x) if isinstance(x, pd.DataFrame) else np.sign(x)

        # 3. Time-series operators (2 arguments)
        elif name in ('delay', 'ts_delay', 'ts_mean', 'ts_std', 'ts_std_dev', 'ts_sum', 'ts_max', 'ts_min', 'ts_rank', 'ts_delta', 'ts_zscore', 'ts_decay_linear'):
            if len(args) != 2:
                raise ValueError(f"{name}() expects exactly 2 arguments: {name}(expression, window_days)")
            
            x = args[0]
            if not isinstance(x, pd.DataFrame):
                raise ValueError(f"{name}() first argument must be a dataframe.")
                
            try:
                d = int(args[1])
            except (ValueError, TypeError):
                raise ValueError(f"{name}() second argument must be a numeric integer window size.")
                
            if d <= 0:
                raise ValueError(f"{name}() window size must be a positive integer.")
                
            if name in ('delay', 'ts_delay'):
                return x.shift(d)
            elif name == 'ts_mean':
                return x.rolling(window=d, min_periods=1).mean()
            elif name in ('ts_std', 'ts_std_dev'):
                return x.rolling(window=d, min_periods=1).std()
            elif name == 'ts_sum':
                return x.rolling(window=d, min_periods=1).sum()
            elif name == 'ts_max':
                return x.rolling(window=d, min_periods=1).max()
            elif name == 'ts_min':
                return x.rolling(window=d, min_periods=1).min()
            elif name == 'ts_rank':
                # Fast rolling rank implementation
                return x.rolling(window=d, min_periods=1).apply(
                    lambda s: pd.Series(s).rank(pct=True).iloc[-1], raw=True
                )
            elif name == 'ts_delta':
                return x - x.shift(d)
            elif name == 'ts_zscore':
                mean = x.rolling(window=d, min_periods=1).mean()
                std = x.rolling(window=d, min_periods=1).std().replace(0, 1e-15)
                return (x - mean) / std
            elif name == 'ts_decay_linear':
                weights = np.arange(1, d + 1)
                weights = weights / weights.sum()
                def linear_decay(s):
                    w = weights[-len(s):]
                    w = w / w.sum()
                    return np.dot(s, w)
                return x.rolling(window=d, min_periods=1).apply(linear_decay, raw=True)
                
        elif name == 'returns':
            # Returns over rolling window. Usage: returns(d) or returns(x, d)
            if len(args) == 1:
                # If only one arg, it's the period, default to close price
                try:
                    d = int(args[0])
                except (ValueError, TypeError):
                    raise ValueError("returns() argument must be a numeric integer window size.")
                if d <= 0:
                    raise ValueError("returns() window size must be a positive integer.")
                price_df = self.dfs.get('close')
                if price_df is None:
                    raise ValueError("returns() requires close price dataframe in input dataset.")
                return price_df.pct_change(periods=d)
            elif len(args) == 2:
                x = args[0]
                if not isinstance(x, pd.DataFrame):
                    raise ValueError("returns() first argument must be a dataframe.")
                try:
                    d = int(args[1])
                except (ValueError, TypeError):
                    raise ValueError("returns() second argument must be a numeric integer window size.")
                if d <= 0:
                    raise ValueError("returns() window size must be a positive integer.")
                return x.pct_change(periods=d)
            else:
                raise ValueError("returns() takes 1 or 2 arguments.")
                
        elif name in ('correlation', 'ts_corr', 'ts_covariance'):
            if len(args) != 3:
                raise ValueError(f"{name}() expects exactly 3 arguments: {name}(x, y, window_days)")
            x = args[0]
            y = args[1]
            if not isinstance(x, pd.DataFrame) or not isinstance(y, pd.DataFrame):
                raise ValueError(f"{name}() first two arguments must be dataframes.")
            try:
                d = int(args[2])
            except (ValueError, TypeError):
                raise ValueError(f"{name}() third argument must be a numeric integer window size.")
            if d <= 0:
                raise ValueError(f"{name}() window size must be a positive integer.")
            
            if name in ('correlation', 'ts_corr'):
                return x.rolling(window=d, min_periods=1).corr(y)
            elif name == 'ts_covariance':
                return x.rolling(window=d, min_periods=1).cov(y)

        # 4. Logical Operators
        elif name == 'if_else':
            if len(args) != 3:
                raise ValueError("if_else() expects exactly 3 arguments: if_else(condition, true_val, false_val)")
            cond = args[0]
            x = args[1]
            y = args[2]
            
            # Perform element-wise boolean indexing/where selection
            if isinstance(x, pd.DataFrame):
                return x.where(cond, y)
            elif isinstance(y, pd.DataFrame):
                x_df = pd.DataFrame(x, index=y.index, columns=y.columns)
                return x_df.where(cond, y)
            else:
                if isinstance(cond, pd.DataFrame):
                    x_df = pd.DataFrame(x, index=cond.index, columns=cond.columns)
                    return x_df.where(cond, y)
                else:
                    return x if cond else y
            
        else:
            raise ValueError(f"Unsupported function call: '{name}'")
