"""
Realistic backtesting using backtrader with NSE-specific settings.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class MomentumStrategy_BT:
    """Backtrader momentum strategy (defined inside run function to avoid import issues)."""
    pass


def run_backtrader_momentum(
    df: pd.DataFrame,
    init_cash: float = 1_000_000,
    commission: float = 0.001,
    ema_fast: int = 20,
    ema_mid: int = 50,
    ema_slow: int = 200,
) -> dict:
    """Run a realistic backtrader momentum backtest."""
    try:
        import backtrader as bt

        class MomentumBT(bt.Strategy):
            params = dict(
                ema_fast=ema_fast,
                ema_mid=ema_mid,
                ema_slow=ema_slow,
                rsi_period=14,
                rsi_min=50,
                rsi_max=70,
                atr_period=14,
                atr_mult=1.5,
                printlog=False,
            )

            def __init__(self):
                self.ema_f = bt.ind.EMA(period=self.p.ema_fast)
                self.ema_m = bt.ind.EMA(period=self.p.ema_mid)
                self.ema_s = bt.ind.EMA(period=self.p.ema_slow)
                self.rsi = bt.ind.RSI(period=self.p.rsi_period)
                self.atr = bt.ind.ATR(period=self.p.atr_period)
                self.macd = bt.ind.MACD()
                self.stop_price = None

            def next(self):
                if not self.position:
                    ema_stack = self.ema_f[0] > self.ema_m[0] > self.ema_s[0]
                    rsi_ok = self.p.rsi_min <= self.rsi[0] <= self.p.rsi_max
                    macd_exp = self.macd.macd[0] > self.macd.macd[-1]

                    if ema_stack and rsi_ok and macd_exp:
                        size = int(self.broker.cash * 0.02 / self.data.close[0])
                        if size > 0:
                            self.buy(size=size)
                            self.stop_price = self.data.close[0] - self.p.atr_mult * self.atr[0]
                else:
                    # Exit conditions
                    if self.stop_price and self.data.close[0] < self.stop_price:
                        self.close()
                    elif self.rsi[0] > 75:
                        self.close()

        # Convert DataFrame to backtrader data feed
        data_feed = bt.feeds.PandasData(
            dataname=df.rename(columns={
                "open": "open", "high": "high", "low": "low",
                "close": "close", "volume": "volume"
            }),
        )

        cerebro = bt.Cerebro()
        cerebro.addstrategy(MomentumBT)
        cerebro.adddata(data_feed)
        cerebro.broker.setcash(init_cash)
        cerebro.broker.setcommission(commission=commission)
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, riskfreerate=0.07, annualize=True)
        cerebro.addanalyzer(bt.analyzers.DrawDown)
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer)
        cerebro.addanalyzer(bt.analyzers.Returns)

        results = cerebro.run()
        strat = results[0]

        sharpe = strat.analyzers.sharperatio.get_analysis().get("sharperatio", 0) or 0
        drawdown = strat.analyzers.drawdown.get_analysis().get("max", {}).get("drawdown", 0) or 0
        trade_analysis = strat.analyzers.tradeanalysis.get_analysis()
        total_closed = trade_analysis.get("total", {}).get("closed", 0) or 0
        won = trade_analysis.get("won", {}).get("total", 0) or 0
        win_rate = (won / total_closed * 100) if total_closed > 0 else 0

        final_value = cerebro.broker.getvalue()
        total_return = (final_value - init_cash) / init_cash * 100

        return {
            "total_return_pct": round(float(total_return), 2),
            "final_value": round(float(final_value), 2),
            "sharpe_ratio": round(float(sharpe), 3),
            "max_drawdown_pct": round(float(drawdown), 2),
            "win_rate_pct": round(float(win_rate), 2),
            "total_trades": int(total_closed),
        }

    except ImportError:
        logger.error("backtrader not installed.")
        return {"error": "backtrader not installed"}
    except Exception as exc:
        logger.error(f"Backtrader backtest failed: {exc}")
        return {"error": str(exc)}
