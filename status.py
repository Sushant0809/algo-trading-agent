"""
Quick status dashboard — run anytime to see what the agent is doing.
Usage: .venv/bin/python status.py
"""
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

LOG = Path("logs/trading.log")
AUDIT_DIR = Path("logs/audit")


def is_running():
    r = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True)
    return r.returncode == 0


def parse_log_events(n=200):
    if not LOG.exists():
        return []
    lines = LOG.read_text().splitlines()[-n:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def show_status():
    print("\n" + "=" * 60)
    print("  ALGO TRADING AGENT — STATUS")
    print("=" * 60)

    # Agent running?
    status = "RUNNING ✓" if is_running() else "STOPPED ✗"
    print(f"  Agent:     {status}")
    print(f"  Mode:      PAPER TRADING (no real money)")
    print(f"  Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    events = parse_log_events()
    if not events:
        print("\n  No log entries found.")
        return

    # Morning setup results
    regime = next((e for e in reversed(events) if "regime" in e.get("event", "") and "Strategy regime" in e.get("event", "")), None)
    if regime:
        print(f"\n  Market Regime: {regime['event']}")

    # Sentiment highlights (score >= 3)
    print("\n  --- SENTIMENT SCORES (today) ---")
    sentiments = [e for e in events if "Sentiment [" in e.get("event", "")]
    if sentiments:
        scored = []
        for e in sentiments:
            msg = e["event"]
            try:
                symbol = msg.split("[")[1].split("]")[0]
                score = float(msg.split("score=")[1].split("/")[0])
                scored.append((symbol, score, msg.split("|")[1].strip()[:60] if "|" in msg else ""))
            except Exception:
                pass
        scored.sort(key=lambda x: x[1], reverse=True)
        for sym, score, reason in scored[:10]:
            bar = "█" * int(score) + "░" * (10 - int(score))
            flag = " ← HIGH" if score >= 7 else ""
            print(f"    {sym:<15} {score:>4.1f}/10  {bar}{flag}")
            if reason:
                print(f"               {reason[:55]}...")
    else:
        print("    No sentiment scores yet (runs at 8:45am IST)")

    # Signals generated
    print("\n  --- SIGNALS GENERATED ---")
    signals = [e for e in events if "Signal generated" in e.get("event", "") or "signal" in e.get("event", "").lower() and "BUY" in e.get("event", "")]
    buy_signals = [e for e in events if "BUY" in e.get("event", "") and ("signal" in e.get("event","").lower() or "Signal" in e.get("event",""))]
    sell_signals = [e for e in events if "SELL" in e.get("event", "") and ("signal" in e.get("event","").lower() or "Signal" in e.get("event",""))]
    if buy_signals or sell_signals:
        for e in (buy_signals + sell_signals)[-10:]:
            print(f"    {e['timestamp'][:19]}  {e['event'][:70]}")
    else:
        print("    None yet — market opens 9:15am IST")

    # Paper trades placed
    print("\n  --- PAPER TRADES ---")
    trades = [e for e in events if "PAPER-" in e.get("event", "") or "paper order" in e.get("event", "").lower() or "Placed" in e.get("event", "")]
    if trades:
        for e in trades[-10:]:
            print(f"    {e['timestamp'][:19]}  {e['event'][:70]}")
    else:
        print("    None yet")

    # Risk/kill switch alerts
    print("\n  --- RISK ALERTS ---")
    risk_events = [e for e in events if e.get("level") == "error" or "kill" in e.get("event","").lower() or "KillSwitch" in e.get("event","")]
    kite_errors = [e for e in risk_events if "Insufficient" not in e.get("event","") and "credit balance" not in e.get("event","")]
    if kite_errors:
        for e in kite_errors[-5:]:
            print(f"    [{e.get('level','').upper()}] {e['event'][:70]}")
    else:
        print("    None ✓")

    # Recent log tail
    print("\n  --- LAST 5 LOG EVENTS ---")
    for e in events[-5:]:
        lvl = e.get("level", "info").upper()
        ts = e.get("timestamp", "")[:19]
        msg = e.get("event", "")[:65]
        print(f"    {ts}  [{lvl}]  {msg}")

    # Audit trail summary
    print("\n  --- AUDIT TRAIL ---")
    if AUDIT_DIR.exists():
        files = sorted(AUDIT_DIR.glob("*.jsonl"))
        total = sum(1 for f in files for _ in f.open())
        print(f"    {len(files)} audit file(s), {total} decision records")
        if files:
            last_line = files[-1].read_text().strip().splitlines()
            if last_line:
                try:
                    last = json.loads(last_line[-1])
                    print(f"    Last: [{last.get('agent')}] {last.get('decision','')[:55]}")
                except Exception:
                    pass
    else:
        print("    No audit records yet")

    print("\n" + "=" * 60)
    print("  Tip: tail -f logs/trading.log   ← live feed")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    show_status()
