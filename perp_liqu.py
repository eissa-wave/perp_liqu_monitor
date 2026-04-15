import requests
import time
import hmac
import hashlib
import json
from typing import Union
from urllib.parse import urlencode
from datetime import datetime
import os

# ============================================================
#  CONFIG
# ============================================================
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_USER = os.environ.get("HL_USER")
HL_DEX = ""

BINANCE_KEY = os.environ.get("BINANCE_KEY")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")
BINANCE_BASE = "https://fapi.binance.com"

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK")

LIQ_THRESHOLD_PCT = 12.0   # alert when distance-to-liq < this %
TIMEOUT = 15
RETRIES = 4
BACKOFF_S = 0.4


# ============================================================
#  SLACK
# ============================================================
def _send_slack_alert(alerts: list[dict]):
    """Post a single Slack message summarizing all breached positions."""
    if not alerts:
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f":rotating_light: *Liquidation Warning* — {ts}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Liquidation Warning"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ts}]},
    ]

    for a in alerts:
        fields_text = (
            f"*{a['exchange']}  |  {a['symbol']}  ({a['direction']})*\n"
            f"Mark: `{a['mark']:,.6f}`    Liq: `{a['liq']:,.6f}`\n"
            f"Distance: *{a['dist_pct']:.2f}%*  (threshold: {LIQ_THRESHOLD_PCT}%)"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": fields_text}})
        blocks.append({"type": "divider"})

    payload = {
        "text": header,          # fallback for notifications
        "blocks": blocks,
    }

    try:
        resp = requests.post(
            SLACK_WEBHOOK,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"[Slack error] status={resp.status_code} body={resp.text}")
        else:
            print(f"Slack alert sent for {len(alerts)} position(s).")
    except requests.RequestException as exc:
        print(f"[Slack error] {exc}")


# ============================================================
#  HYPERLIQUID
# ============================================================
_hl_session = requests.Session()
_hl_session.headers.update({"Content-Type": "application/json"})


def _hl_post(payload: dict) -> Union[list, dict]:
    for attempt in range(RETRIES):
        try:
            resp = _hl_session.post(HL_INFO_URL, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == RETRIES - 1:
                raise
            time.sleep(BACKOFF_S * (2 ** attempt))


def check_hl_liquidations() -> list[dict]:
    ch_payload = {"type": "clearinghouseState", "user": HL_USER}
    if HL_DEX:
        ch_payload["dex"] = HL_DEX
    state = _hl_post(ch_payload)

    meta_payload = {"type": "metaAndAssetCtxs"}
    if HL_DEX:
        meta_payload["dex"] = HL_DEX
    meta_resp = _hl_post(meta_payload)

    universe = meta_resp[0]["universe"]
    ctxs = meta_resp[1]
    mark_prices = {}
    for asset, ctx in zip(universe, ctxs):
        mark_prices[asset["name"]] = float(ctx["markPx"])

    results = []
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        szi = float(pos.get("szi", "0"))
        if szi == 0:
            continue

        coin = pos["coin"]
        direction = "LONG" if szi > 0 else "SHORT"
        mark_px = mark_prices.get(coin)
        liq_px_str = pos.get("liquidationPx")

        if not mark_px or not liq_px_str:
            continue

        liq_px = float(liq_px_str)
        if mark_px == 0:
            continue

        dist_pct = abs((mark_px - liq_px) / mark_px) * 100

        results.append({
            "exchange": "Hyperliquid",
            "symbol": coin,
            "direction": direction,
            "mark": mark_px,
            "liq": liq_px,
            "dist_pct": dist_pct,
        })

    return results


# ============================================================
#  BINANCE
# ============================================================
def _bn_signed_get(path: str) -> Union[list, dict]:
    params = {"timestamp": int(time.time() * 1000)}
    qs = urlencode(params)
    params["signature"] = hmac.new(
        BINANCE_SECRET.encode("utf-8"),
        qs.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    r = requests.get(
        BINANCE_BASE + path,
        params=params, headers=headers, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def check_binance_liquidations() -> list[dict]:
    all_positions = _bn_signed_get("/fapi/v3/positionRisk")

    results = []
    for p in all_positions:
        amt = float(p.get("positionAmt", "0"))
        if amt == 0:
            continue

        symbol = p["symbol"]
        direction = "LONG" if amt > 0 else "SHORT"
        mark = float(p["markPrice"])
        liq_px = float(p["liquidationPrice"])

        if liq_px == 0 or mark == 0:
            continue

        dist_pct = abs((mark - liq_px) / mark) * 100

        results.append({
            "exchange": "Binance",
            "symbol": symbol,
            "direction": direction,
            "mark": mark,
            "liq": liq_px,
            "dist_pct": dist_pct,
        })

    return results


# ============================================================
#  STATUS TABLE (logged to Jenkins console)
# ============================================================
def _print_status(all_results: list[dict]):
    ts = datetime.utcnow().strftime("%H:%M:%S UTC")
    sorted_results = sorted(all_results, key=lambda r: r["dist_pct"])

    print(f"\n{'='*72}")
    print(f"  Liquidation Distance Monitor  |  {ts}  |  threshold: {LIQ_THRESHOLD_PCT}%")
    print(f"{'='*72}")
    print(f"  {'Exchange':<14} {'Symbol':<14} {'Dir':<6} {'Mark':>14} {'Liq':>14} {'Dist':>8}")
    print(f"  {'-'*14} {'-'*14} {'-'*6} {'-'*14} {'-'*14} {'-'*8}")

    for r in sorted_results:
        flag = " <<" if r["dist_pct"] < LIQ_THRESHOLD_PCT else ""
        print(
            f"  {r['exchange']:<14} {r['symbol']:<14} {r['direction']:<6} "
            f"{r['mark']:>14,.4f} {r['liq']:>14,.4f} {r['dist_pct']:>7.2f}%{flag}"
        )

    if not sorted_results:
        print("  (no open positions)")
    print()


# ============================================================
#  MAIN (single run, Jenkins-friendly)
# ============================================================
def run():
    print(f"Liquidation check (threshold={LIQ_THRESHOLD_PCT}%)\n")

    all_results = []

    try:
        all_results += check_hl_liquidations()
    except Exception as exc:
        print(f"[HL error] {exc}")

    if BINANCE_KEY and BINANCE_SECRET:
        try:
            all_results += check_binance_liquidations()
        except Exception as exc:
            print(f"[Binance error] {exc}")
    else:
        print("  (Binance skipped, no API key configured)")

    _print_status(all_results)

    # Filter for breached positions and fire Slack alert
    breached = [r for r in all_results if r["dist_pct"] < LIQ_THRESHOLD_PCT]

    if breached:
        _send_slack_alert(breached)
    else:
        print("All positions above threshold. No alerts fired.")


if __name__ == "__main__":
    run()
