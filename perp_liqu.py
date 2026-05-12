from __future__ import annotations
import requests
import time
import hmac
import hashlib
import base64
import json
from typing import Union, Dict, Any
from urllib.parse import urlencode
from datetime import datetime, timezone
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

BYBIT_KEY = os.environ.get("BYBIT_KEY")
BYBIT_SECRET = os.environ.get("BYBIT_SECRET")
BYBIT_BASE = "https://api.bybit.com"
BYBIT_RECV_WINDOW = "5000"

OKX_KEY = os.environ.get("OKX_KEY")
OKX_SECRET = os.environ.get("OKX_SECRET")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
OKX_BASE = "https://www.okx.com"
# OKX cross-margin uses account-level mgnRatio (adjEq / mmr); liquidation at <=1.0.
# 1.33 ~= 25% equity buffer above mmr.
OKX_MGN_RATIO_FLOOR = 8.33

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK")

LIQ_THRESHOLD_PCT = 20.0   # alert when distance-to-liq < this %
DELTA_THRESHOLD_USD = 6_000_000.0   # alert when |net delta| on any exchange > this
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
    header = f":rotating_light: *Liquidation Warning* - {ts}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Liquidation Warning"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ts}]},
    ]

    for a in alerts:
        fields_text = (
            f"*{a['exchange']}  |  {a['symbol']}  ({a['direction']})*\n"
            f"Size: `{a['size']:,.4f}`    Notional: `${a['notional_usd']:,.0f}`\n"
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


def _send_slack_delta_alert(breaches: list[dict]):
    """Post a Slack message summarizing exchanges that breached the delta threshold."""
    if not breaches:
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f":warning: *Delta Exposure Warning* - {ts}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Delta Exposure Warning"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ts}]},
    ]

    for b in breaches:
        skew = "LONG" if b["net_delta_usd"] > 0 else "SHORT"
        fields_text = (
            f"*{b['exchange']}  ({skew} skew)*\n"
            f"Net Delta: `${b['net_delta_usd']:,.0f}`\n"
            f"Gross Long: `${b['gross_long_usd']:,.0f}`    "
            f"Gross Short: `${b['gross_short_usd']:,.0f}`\n"
            f"Threshold: `${DELTA_THRESHOLD_USD:,.0f}`"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": fields_text}})
        blocks.append({"type": "divider"})

    payload = {
        "text": header,
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
            print(f"Slack delta alert sent for {len(breaches)} exchange(s).")
    except requests.RequestException as exc:
        print(f"[Slack error] {exc}")


def _send_slack_okx_mgn_alert(mgn_breach: dict, cross_positions: list[dict]):
    """Post a Slack message when OKX cross-margin mgnRatio breaches the floor."""
    if not mgn_breach:
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f":rotating_light: *OKX Liquidation Warning* - {ts}"

    mgn_ratio = mgn_breach['mgn_ratio']
    # mgnRatio = adjEq / mmr. Liquidation begins at mgnRatio = 1.0.
    # The "buffer above liq" expressed as a multiplier: how many times equity
    # exceeds the maintenance requirement.
    if mgn_ratio > 0:
        ratio_above_liq = mgn_ratio  # e.g. 6.58x means equity is 6.58x mmr
    else:
        ratio_above_liq = 0.0

    fields_text = (
        f"*OKX cross-margin account*\n"
        f"mgnRatio: *{mgn_ratio:.2f}x*  (floor: {OKX_MGN_RATIO_FLOOR:.2f}x, "
        f"account liquidates at 1.00x)\n"
        f"Account is *{ratio_above_liq:.2f}x* equity-to-maintenance, "
        f"i.e. {(ratio_above_liq - 1.0):.2f}x above the liquidation boundary\n"
        f"adjEq: `${mgn_breach['adj_eq']:,.0f}`    "
        f"mmr: `${mgn_breach['mmr']:,.0f}`\n"
        f"Cross positions: `{mgn_breach['cross_pos_count']}`"
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "OKX Liquidation Warning"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ts}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": fields_text}},
    ]

    # Open cross positions
    if cross_positions:
        pos_lines = ["*Open cross positions:*"]
        for p in cross_positions:
            pos_lines.append(
                f"`{p['symbol']}` ({p['direction']})  "
                f"Size: `{p['size']:,.4f}`    "
                f"Notional: `${p['notional_usd']:,.0f}`    "
                f"Mark: `{p['mark']:,.6f}`"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(pos_lines)},
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": (
            "_Note: OKX does not return per-position liq prices for cross positions; "
            "safety is evaluated at account level via mgnRatio._"
        )}],
    })

    payload = {"text": header, "blocks": blocks}

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
            print("Slack OKX liquidation alert sent.")
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
        notional = abs(szi) * mark_px
        signed_notional = szi * mark_px

        results.append({
            "exchange": "Hyperliquid",
            "symbol": coin,
            "direction": direction,
            "size": abs(szi),
            "notional_usd": notional,
            "signed_notional_usd": signed_notional,
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
        notional = abs(amt) * mark
        signed_notional = amt * mark

        results.append({
            "exchange": "Binance",
            "symbol": symbol,
            "direction": direction,
            "size": abs(amt),
            "notional_usd": notional,
            "signed_notional_usd": signed_notional,
            "mark": mark,
            "liq": liq_px,
            "dist_pct": dist_pct,
        })

    return results


# ============================================================
#  BYBIT
# ============================================================
def _bb_signed_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    timestamp = str(int(time.time() * 1000))
    query_string = urlencode(params)
    pre_sign = timestamp + BYBIT_KEY + BYBIT_RECV_WINDOW + query_string
    signature = hmac.new(
        BYBIT_SECRET.encode("utf-8"),
        pre_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": BYBIT_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": BYBIT_RECV_WINDOW,
        "Content-Type": "application/json",
    }

    resp = requests.get(BYBIT_BASE + path, params=params, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')} (path={path})")
    return data


def check_bybit_liquidations() -> list[dict]:
    results = []
    for settle in ("USDT", "USDC"):
        params: Dict[str, Any] = {
            "category": "linear",
            "settleCoin": settle,
            "limit": "200",
        }
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            else:
                params.pop("cursor", None)

            data = _bb_signed_get("/v5/position/list", params)
            result = data.get("result", {})
            rows = result.get("list", []) or []

            for p in rows:
                size = float(p.get("size", "0") or 0)
                if size == 0:
                    continue

                symbol = p["symbol"]
                side = p.get("side", "")
                direction = "LONG" if side == "Buy" else "SHORT"
                mark = float(p.get("markPrice", "0") or 0)
                liq_px_str = p.get("liqPrice", "") or ""

                if not liq_px_str or liq_px_str == "0" or mark == 0:
                    continue

                liq_px = float(liq_px_str)
                dist_pct = abs((mark - liq_px) / mark) * 100
                notional = size * mark
                signed_notional = notional if direction == "LONG" else -notional

                results.append({
                    "exchange": "Bybit",
                    "symbol": symbol,
                    "direction": direction,
                    "size": size,
                    "notional_usd": notional,
                    "signed_notional_usd": signed_notional,
                    "mark": mark,
                    "liq": liq_px,
                    "dist_pct": dist_pct,
                })

            cursor = result.get("nextPageCursor")
            if not cursor:
                break

    return results


# ============================================================
#  OKX
# ============================================================
def _okx_timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _okx_signed_get(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    params = params or {}
    query = urlencode(params)
    request_path = f"{path}?{query}" if query else path

    timestamp = _okx_timestamp()
    prehash = f"{timestamp}GET{request_path}"
    sign = base64.b64encode(
        hmac.new(OKX_SECRET.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    headers = {
        "OK-ACCESS-KEY": OKX_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }

    r = requests.get(OKX_BASE + request_path, headers=headers, timeout=TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        r.raise_for_status()
        raise RuntimeError(f"OKX non-JSON response: {r.text}")

    if r.status_code != 200:
        raise RuntimeError(f"OKX HTTP error ({r.status_code}): {data}")
    if isinstance(data, dict) and data.get("code") not in (None, "0"):
        raise RuntimeError(f"OKX API error: {data}")
    return data


def check_okx_liquidations() -> tuple[list[dict], dict | None]:
    """
    Returns (per-position liq distance rows, mgn_status).

    Per-position rows are only emitted for ISOLATED positions (cross positions on
    OKX don't return meaningful per-position liq prices). Cross positions still
    contribute to delta aggregation via the signed-notional rows we emit with
    dist_pct=None marker (filtered out before the liq alert).

    mgn_status is a dict with account-level cross margin health, or None if there
    are no cross positions.
    """
    pos_resp = _okx_signed_get("/api/v5/account/positions")
    raw_positions = pos_resp.get("data", []) or []
    positions = [p for p in raw_positions if float(p.get("pos", "0") or 0) != 0]

    results = []
    cross_pos_count = 0

    for p in positions:
        inst_id = p.get("instId", "")
        pos_qty = float(p.get("pos", "0") or 0)
        pos_side = (p.get("posSide", "") or "").lower()

        if pos_side == "long":
            direction = "LONG"
            size = abs(pos_qty)
        elif pos_side == "short":
            direction = "SHORT"
            size = abs(pos_qty)
        else:  # net mode
            direction = "LONG" if pos_qty > 0 else "SHORT"
            size = abs(pos_qty)

        mark = float(p.get("markPx", "0") or 0)
        notional_abs = float(p.get("notionalUsd", "0") or 0)
        signed_notional = notional_abs if direction == "LONG" else -notional_abs
        liq_px_str = p.get("liqPx", "") or ""
        mgn_mode = (p.get("mgnMode", "") or "").lower()
        is_isolated = mgn_mode == "isolated"

        if is_isolated and liq_px_str and liq_px_str not in ("", "0") and mark != 0:
            liq_px = float(liq_px_str)
            dist_pct = abs((mark - liq_px) / mark) * 100
            results.append({
                "exchange": "OKX",
                "symbol": inst_id,
                "direction": direction,
                "size": size,
                "notional_usd": notional_abs,
                "signed_notional_usd": signed_notional,
                "mark": mark,
                "liq": liq_px,
                "dist_pct": dist_pct,
            })
        else:
            # Cross (or isolated with no liq px). Emit a row with dist_pct=None
            # so delta aggregation captures it but the liq alert filter drops it.
            cross_pos_count += 1
            results.append({
                "exchange": "OKX",
                "symbol": inst_id,
                "direction": direction,
                "size": size,
                "notional_usd": notional_abs,
                "signed_notional_usd": signed_notional,
                "mark": mark,
                "liq": None,
                "dist_pct": None,
            })

    # Account-level mgnRatio (cross-margin safety)
    mgn_status = None
    if cross_pos_count > 0:
        acct_resp = _okx_signed_get("/api/v5/account/balance")
        acct_data = acct_resp.get("data", []) or []
        acct = acct_data[0] if acct_data else {}
        try:
            mgn_ratio = float(acct.get("mgnRatio") or 0)
        except (TypeError, ValueError):
            mgn_ratio = 0.0
        try:
            adj_eq = float(acct.get("adjEq") or 0)
        except (TypeError, ValueError):
            adj_eq = 0.0
        try:
            mmr = float(acct.get("mmr") or 0)
        except (TypeError, ValueError):
            mmr = 0.0
        mgn_status = {
            "mgn_ratio": mgn_ratio,
            "adj_eq": adj_eq,
            "mmr": mmr,
            "cross_pos_count": cross_pos_count,
            "breached": mgn_ratio > 0 and mgn_ratio <= OKX_MGN_RATIO_FLOOR,
        }

    return results, mgn_status


# ============================================================
#  DELTA AGGREGATION
# ============================================================
def _compute_exchange_deltas(all_results: list[dict]) -> list[dict]:
    """Aggregate signed notional per exchange. Returns one row per exchange."""
    by_exchange: Dict[str, Dict[str, float]] = {}
    for r in all_results:
        ex = r["exchange"]
        signed = r.get("signed_notional_usd", 0.0)
        bucket = by_exchange.setdefault(ex, {"net": 0.0, "long": 0.0, "short": 0.0})
        bucket["net"] += signed
        if signed > 0:
            bucket["long"] += signed
        else:
            bucket["short"] += signed  # negative number

    return [
        {
            "exchange": ex,
            "net_delta_usd": v["net"],
            "gross_long_usd": v["long"],
            "gross_short_usd": v["short"],
        }
        for ex, v in by_exchange.items()
    ]


def _print_delta_summary(deltas: list[dict]):
    print(f"\n{'-'*108}")
    print(f"  Per-Exchange Net Delta  |  threshold: ${DELTA_THRESHOLD_USD:,.0f}")
    print(f"{'-'*108}")
    print(f"  {'Exchange':<14} {'Net Delta':>20} {'Gross Long':>20} {'Gross Short':>20}  Status")
    for d in sorted(deltas, key=lambda x: -abs(x["net_delta_usd"])):
        breached = abs(d["net_delta_usd"]) > DELTA_THRESHOLD_USD
        flag = " <<" if breached else ""
        print(
            f"  {d['exchange']:<14} ${d['net_delta_usd']:>19,.0f} "
            f"${d['gross_long_usd']:>19,.0f} ${d['gross_short_usd']:>19,.0f}{flag}"
        )
    print()


# ============================================================
#  STATUS TABLE (logged to Jenkins console)
# ============================================================
def _print_status(all_results: list[dict], okx_mgn_status: dict | None):
    ts = datetime.utcnow().strftime("%H:%M:%S UTC")
    # Sort all rows: measurable (have dist_pct) first by ascending distance,
    # unmeasurable (OKX cross) at the bottom so the tightest positions are
    # always visible at the top.
    measurable = sorted(
        [r for r in all_results if r.get("dist_pct") is not None],
        key=lambda r: r["dist_pct"],
    )
    unmeasurable = [r for r in all_results if r.get("dist_pct") is None]
    sorted_results = measurable + unmeasurable

    print(f"\n{'='*108}")
    print(f"  Liquidation Distance Monitor  |  {ts}  |  threshold: {LIQ_THRESHOLD_PCT}%")
    print(f"{'='*108}")
    print(
        f"  {'Exchange':<14} {'Symbol':<16} {'Dir':<6} "
        f"{'Size':>14} {'Notional':>15} {'Mark':>12} {'Liq':>12} {'Dist':>8}"
    )
    print(
        f"  {'-'*14} {'-'*16} {'-'*6} "
        f"{'-'*14} {'-'*15} {'-'*12} {'-'*12} {'-'*8}"
    )

    for r in sorted_results:
        if r.get("dist_pct") is None:
            # OKX cross: no per-position liq; safety via account mgnRatio
            print(
                f"  {r['exchange']:<14} {r['symbol']:<16} {r['direction']:<6} "
                f"{r['size']:>14,.4f} ${r['notional_usd']:>14,.0f} "
                f"{r['mark']:>12,.4f} {'--':>12} {'--':>8}  (cross, see mgnRatio)"
            )
        else:
            flag = " <<" if r["dist_pct"] < LIQ_THRESHOLD_PCT else ""
            print(
                f"  {r['exchange']:<14} {r['symbol']:<16} {r['direction']:<6} "
                f"{r['size']:>14,.4f} ${r['notional_usd']:>14,.0f} "
                f"{r['mark']:>12,.4f} {r['liq']:>12,.4f} {r['dist_pct']:>7.2f}%{flag}"
            )

    if not sorted_results:
        print("  (no open positions)")

    if okx_mgn_status:
        s = okx_mgn_status
        flag = " <<" if s["breached"] else ""
        print(
            f"\n  OKX account mgnRatio: {s['mgn_ratio']:.2f}x  "
            f"(floor: {OKX_MGN_RATIO_FLOOR:.2f}x, liquidates at 1.00x,  "
            f"adjEq: ${s['adj_eq']:,.0f},  mmr: ${s['mmr']:,.0f}){flag}"
        )
    print()


# ============================================================
#  MAIN (single run, Jenkins-friendly)
# ============================================================
def run():
    print(f"Liquidation check (threshold={LIQ_THRESHOLD_PCT}%)\n")

    all_results = []
    okx_mgn_status: dict | None = None

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

    if BYBIT_KEY and BYBIT_SECRET:
        try:
            all_results += check_bybit_liquidations()
        except Exception as exc:
            print(f"[Bybit error] {exc}")
    else:
        print("  (Bybit skipped, no API key configured)")

    if OKX_KEY and OKX_SECRET and OKX_PASSPHRASE:
        try:
            okx_results, okx_mgn_status = check_okx_liquidations()
            all_results += okx_results
        except Exception as exc:
            print(f"[OKX error] {exc}")
    else:
        print("  (OKX skipped, no API key configured)")

    _print_status(all_results, okx_mgn_status)

    # Liquidation distance alert (only positions with a measurable distance)
    breached = [
        r for r in all_results
        if r.get("dist_pct") is not None and r["dist_pct"] < LIQ_THRESHOLD_PCT
    ]
    if breached:
        _send_slack_alert(breached)
    else:
        print("All positions above liq threshold. No liq alerts fired.")

    # OKX cross-margin mgnRatio alert
    if okx_mgn_status and okx_mgn_status["breached"]:
        okx_cross_positions = [
            r for r in all_results
            if r["exchange"] == "OKX" and r.get("dist_pct") is None
        ]
        _send_slack_okx_mgn_alert(okx_mgn_status, okx_cross_positions)
    elif okx_mgn_status:
        print(
            f"OKX mgnRatio {okx_mgn_status['mgn_ratio']:.2f}x above floor "
            f"{OKX_MGN_RATIO_FLOOR:.2f}x. No OKX liquidation alert fired."
        )

    # Delta exposure alert
    deltas = _compute_exchange_deltas(all_results)
    _print_delta_summary(deltas)
    delta_breaches = [d for d in deltas if abs(d["net_delta_usd"]) > DELTA_THRESHOLD_USD]
    if delta_breaches:
        _send_slack_delta_alert(delta_breaches)
    else:
        print("All exchanges within delta threshold. No delta alerts fired.")


if __name__ == "__main__":
    run()
