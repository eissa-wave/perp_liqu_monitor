from __future__ import annotations
import requests
import time
import hmac
import hashlib
import base64
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Union, Dict, Any
from urllib.parse import urlencode
from datetime import datetime, timezone
import os

# ============================================================
#  CONFIG
# ============================================================
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_USER = os.environ.get("HL_USER")
# DEXs to query. "" is the main validator-operated perp dex.
# Add HIP-3 builder dex names (e.g. "xyz" for xyz:CL). Comma-separated env override.
HL_DEXS = [d.strip() for d in os.environ.get("HL_DEXS", ",xyz").split(",")]

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
OKX_MGN_RATIO_FLOOR = 1.4

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK")

# Email config
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = [x.strip() for x in os.environ.get("EMAIL_TO", "").split(",") if x.strip()]

LIQ_THRESHOLD_PCT = 25.0   # alert when distance-to-liq < this %
DELTA_THRESHOLD_USD = 7_500_000.0   # alert when |net delta| on any exchange > this
TIMEOUT = 15
RETRIES = 4
BACKOFF_S = 0.4


# ============================================================
#  EMAIL HELPER
# ============================================================
def _send_email(subject: str, html_body: str, text_body: str):
    """Send a plaintext+HTML email. Silently no-ops if SMTP not configured."""
    if not (SMTP_USER and SMTP_PASSWORD and EMAIL_TO):
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"Email alert sent to {len(EMAIL_TO)} recipient(s): {subject}")
    except Exception as exc:
        print(f"[Email error] {exc}")


# ============================================================
#  SLACK
# ============================================================
def _send_slack_alert(alerts: list[dict]):
    """Post a single Slack message summarizing all breached positions, and mirror via email."""
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
            print(f"Slack alert sent for {len(alerts)} position(s).")
    except requests.RequestException as exc:
        print(f"[Slack error] {exc}")

    # ---- email mirror ----
    subject = f"[Liq Warning] {len(alerts)} position(s) within {LIQ_THRESHOLD_PCT}% of liq"
    rows_html = "".join(
        f"<tr><td>{a['exchange']}</td><td>{a['symbol']}</td><td>{a['direction']}</td>"
        f"<td align='right'>{a['size']:,.4f}</td>"
        f"<td align='right'>${a['notional_usd']:,.0f}</td>"
        f"<td align='right'>{a['mark']:,.6f}</td>"
        f"<td align='right'>{a['liq']:,.6f}</td>"
        f"<td align='right'><b>{a['dist_pct']:.2f}%</b></td></tr>"
        for a in alerts
    )
    html = f"""
    <h3>Liquidation Warning &ndash; {ts}</h3>
    <table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:monospace'>
      <tr><th>Exchange</th><th>Symbol</th><th>Dir</th><th>Size</th>
          <th>Notional</th><th>Mark</th><th>Liq</th><th>Dist</th></tr>
      {rows_html}
    </table>
    <p>Threshold: {LIQ_THRESHOLD_PCT}%</p>
    """
    text = "\n".join(
        f"{a['exchange']} {a['symbol']} {a['direction']} "
        f"dist={a['dist_pct']:.2f}% notional=${a['notional_usd']:,.0f} "
        f"mark={a['mark']:,.6f} liq={a['liq']:,.6f}"
        for a in alerts
    )
    _send_email(subject, html, text)


def _send_slack_delta_alert(breaches: list[dict]):
    """Post a Slack message summarizing exchanges that breached the delta threshold, and mirror via email."""
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

    # ---- email mirror ----
    subject = f"[Delta Warning] {len(breaches)} exchange(s) above ${DELTA_THRESHOLD_USD:,.0f}"
    rows_html = "".join(
        f"<tr><td>{b['exchange']}</td>"
        f"<td>{'LONG' if b['net_delta_usd'] > 0 else 'SHORT'}</td>"
        f"<td align='right'><b>${b['net_delta_usd']:,.0f}</b></td>"
        f"<td align='right'>${b['gross_long_usd']:,.0f}</td>"
        f"<td align='right'>${b['gross_short_usd']:,.0f}</td></tr>"
        for b in breaches
    )
    html = f"""
    <h3>Delta Exposure Warning &ndash; {ts}</h3>
    <table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:monospace'>
      <tr><th>Exchange</th><th>Skew</th><th>Net Delta</th>
          <th>Gross Long</th><th>Gross Short</th></tr>
      {rows_html}
    </table>
    <p>Threshold: ${DELTA_THRESHOLD_USD:,.0f}</p>
    """
    text = "\n".join(
        f"{b['exchange']} {'LONG' if b['net_delta_usd'] > 0 else 'SHORT'} "
        f"net=${b['net_delta_usd']:,.0f} long=${b['gross_long_usd']:,.0f} "
        f"short=${b['gross_short_usd']:,.0f}"
        for b in breaches
    )
    _send_email(subject, html, text)


def _send_slack_okx_mgn_alert(mgn_breach: dict, cross_positions: list[dict]):
    """Post a Slack message when OKX cross-margin mgnRatio breaches the floor, and mirror via email."""
    if not mgn_breach:
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f":rotating_light: *OKX Liquidation Warning* - {ts}"

    mgn_ratio = mgn_breach['mgn_ratio']
    if mgn_ratio > 0:
        ratio_above_liq = mgn_ratio
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

    # ---- email mirror ----
    subject = f"[OKX Liq Warning] mgnRatio {mgn_ratio:.2f}x below floor {OKX_MGN_RATIO_FLOOR:.2f}x"

    pos_rows_html = ""
    if cross_positions:
        pos_rows_html = "<h4>Open cross positions</h4>" + (
            "<table border='1' cellpadding='6' cellspacing='0' "
            "style='border-collapse:collapse;font-family:monospace'>"
            "<tr><th>Symbol</th><th>Dir</th><th>Size</th><th>Notional</th><th>Mark</th></tr>"
            + "".join(
                f"<tr><td>{p['symbol']}</td><td>{p['direction']}</td>"
                f"<td align='right'>{p['size']:,.4f}</td>"
                f"<td align='right'>${p['notional_usd']:,.0f}</td>"
                f"<td align='right'>{p['mark']:,.6f}</td></tr>"
                for p in cross_positions
            )
            + "</table>"
        )

    html = f"""
    <h3>OKX Liquidation Warning &ndash; {ts}</h3>
    <p><b>OKX cross-margin account</b></p>
    <ul>
      <li>mgnRatio: <b>{mgn_ratio:.2f}x</b> (floor: {OKX_MGN_RATIO_FLOOR:.2f}x, liquidates at 1.00x)</li>
      <li>Equity-to-maintenance: <b>{ratio_above_liq:.2f}x</b>
          ({(ratio_above_liq - 1.0):.2f}x above liquidation boundary)</li>
      <li>adjEq: ${mgn_breach['adj_eq']:,.0f}</li>
      <li>mmr: ${mgn_breach['mmr']:,.0f}</li>
      <li>Cross positions: {mgn_breach['cross_pos_count']}</li>
    </ul>
    {pos_rows_html}
    <p><i>Note: OKX does not return per-position liq prices for cross positions;
    safety is evaluated at account level via mgnRatio.</i></p>
    """

    text_lines = [
        f"OKX cross-margin account mgnRatio: {mgn_ratio:.2f}x "
        f"(floor: {OKX_MGN_RATIO_FLOOR:.2f}x, liquidates at 1.00x)",
        f"adjEq: ${mgn_breach['adj_eq']:,.0f}    mmr: ${mgn_breach['mmr']:,.0f}",
        f"Cross positions: {mgn_breach['cross_pos_count']}",
    ]
    if cross_positions:
        text_lines.append("")
        text_lines.append("Open cross positions:")
        for p in cross_positions:
            text_lines.append(
                f"  {p['symbol']} {p['direction']} "
                f"size={p['size']:,.4f} notional=${p['notional_usd']:,.0f} "
                f"mark={p['mark']:,.6f}"
            )
    text = "\n".join(text_lines)
    _send_email(subject, html, text)


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


def _check_hl_dex(dex: str) -> list[dict]:
    """Query one HL perp dex. dex="" is the main validator-operated dex; a
    non-empty name is a HIP-3 builder dex (independent margining). Symbols on a
    builder dex are prefixed dex:coin (e.g. xyz:CL) to match the frontend."""
    ch_payload = {"type": "clearinghouseState", "user": HL_USER}
    if dex:
        ch_payload["dex"] = dex
    state = _hl_post(ch_payload)

    meta_payload = {"type": "metaAndAssetCtxs"}
    if dex:
        meta_payload["dex"] = dex
    meta_resp = _hl_post(meta_payload)

    universe = meta_resp[0]["universe"]
    ctxs = meta_resp[1]
    mark_prices = {}
    for asset, ctx in zip(universe, ctxs):
        mark_prices[asset["name"]] = float(ctx["markPx"])

    prefix = f"{dex}:" if dex else ""

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

        if not mark_px or mark_px == 0:
            continue

        notional = abs(szi) * mark_px
        signed_notional = szi * mark_px

        if liq_px_str:
            liq_px = float(liq_px_str)
            dist_pct = abs((mark_px - liq_px) / mark_px) * 100
        else:
            # cross position with no liq price (e.g. unified-account buffer so
            # large HL reports none). Keep the row for delta aggregation and
            # the status table; excluded from the fixed liq-threshold alert.
            liq_px = None
            dist_pct = None

        results.append({
            "exchange": "Hyperliquid",
            "symbol": f"{prefix}{coin}",
            "direction": direction,
            "size": abs(szi),
            "notional_usd": notional,
            "signed_notional_usd": signed_notional,
            "mark": mark_px,
            "liq": liq_px,
            "dist_pct": dist_pct,
        })

    return results


def check_hl_liquidations() -> list[dict]:
    """Aggregate positions across every configured HL dex (main + HIP-3 builders).
    Each dex margins independently, so they are queried separately and merged."""
    results = []
    for dex in HL_DEXS:
        try:
            results += _check_hl_dex(dex)
        except Exception as exc:
            print(f"[HL error] dex={dex or '(main)'}: {exc}")
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

        if mark == 0:
            continue

        notional = abs(amt) * mark
        signed_notional = amt * mark

        if liq_px == 0:
            liq_out = None
            dist_pct = None
        else:
            liq_out = liq_px
            dist_pct = abs((mark - liq_px) / mark) * 100

        results.append({
            "exchange": "Binance",
            "symbol": symbol,
            "direction": direction,
            "size": abs(amt),
            "notional_usd": notional,
            "signed_notional_usd": signed_notional,
            "mark": mark,
            "liq": liq_out,
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

                if mark == 0:
                    continue

                notional = size * mark
                signed_notional = notional if direction == "LONG" else -notional

                if not liq_px_str or liq_px_str == "0":
                    liq_out = None
                    dist_pct = None
                else:
                    liq_out = float(liq_px_str)
                    dist_pct = abs((mark - liq_out) / mark) * 100

                results.append({
                    "exchange": "Bybit",
                    "symbol": symbol,
                    "direction": direction,
                    "size": size,
                    "notional_usd": notional,
                    "signed_notional_usd": signed_notional,
                    "mark": mark,
                    "liq": liq_out,
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

    Rows also carry pos_mmr (the position's maintenance margin requirement in
    USD), used by the vol check to back out an implied liq distance for cross
    positions from the account mgnRatio.
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
        try:
            pos_mmr = float(p.get("mmr") or 0)
        except (TypeError, ValueError):
            pos_mmr = 0.0

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
                "pos_mmr": pos_mmr,
            })
        else:
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
                "pos_mmr": pos_mmr,
            })

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
#  VOLATILITY-BASED LEVERAGE CHECK
# ============================================================
# Independent of the fixed LIQ_THRESHOLD_PCT check above. For each position we
# estimate recent realized vol (EWMA-weighted daily, most recent day heaviest)
# and require the distance-to-liq to exceed VOL_BUFFER_SIGMAS daily sigmas:
#     required_dist = VOL_BUFFER_SIGMAS * sigma
#     recommended_leverage = 1 / (required_dist + VOL_MMR)
# A position can be comfortably past the fixed 25% threshold and still breach
# this check if the token's vol says the safe distance is larger.
VOL_CHECK_ENABLED = True
VOL_LOOKBACK_DAYS = 5
VOL_HALFLIFE_DAYS = 1.0
VOL_BUFFER_SIGMAS = 5.0
VOL_MMR = 0.0
VOL_LEV_MIN = 1.0
VOL_LEV_MAX = 5.0
# base ticker -> Binance USDT-M symbol override (else BASE + "USDT" is tried)
VOL_SYMBOL_OVERRIDES = {}

# ---- TEST ONLY: inject a dummy position to exercise the vol alert. ----
# Set to False (or env VOL_TEST_POSITION=0) once verified. LAB at a fake 31%
# distance-to-liq: passes the fixed 25% threshold but should breach the
# vol-implied required distance if LAB's recent vol is high enough.
VOL_TEST_POSITION = 0


def _make_test_position() -> dict:
    return {
        "exchange": "TEST",
        "symbol": "LAB",
        "direction": "LONG",
        "size": 100_000.0,
        "notional_usd": 50_000.0,
        "signed_notional_usd": 50_000.0,
        "mark": 0.5,
        "liq": 0.345,           # 31% below mark
        "dist_pct": 31.0,       # fake distance-to-liq
    }

BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"


def _vol_base_ticker(symbol: str) -> str:
    """Normalize a venue symbol to its base asset (mirrors the sheet script):
    xyz:CL -> CL, CL-USDT-SWAP -> CL, XMRUSDT -> XMR, XMR -> XMR."""
    s = symbol or ""
    if ":" in s:
        s = s.split(":")[-1]
    if "-" in s:
        s = s.split("-")[0]
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _fetch_daily_closes_binance(base: str) -> list[float] | None:
    symbol = VOL_SYMBOL_OVERRIDES.get(base, f"{base}USDT")
    params = {"symbol": symbol, "interval": "1d", "limit": VOL_LOOKBACK_DAYS + 2}
    for attempt in range(RETRIES):
        try:
            r = requests.get(BINANCE_KLINES_URL, params=params, timeout=TIMEOUT)
            if r.status_code in (418, 429, 500, 502, 503, 504):
                time.sleep(BACKOFF_S * (2 ** attempt))
                continue
            if r.status_code == 400:
                return None   # symbol doesn't exist on Binance futures
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) or not data:
                return None
            return [float(k[4]) for k in data]
        except (requests.RequestException, ValueError):
            if attempt == RETRIES - 1:
                return None
            time.sleep(BACKOFF_S * (2 ** attempt))
    return None


def _fetch_daily_closes_hl(coin: str) -> list[float] | None:
    """HL candleSnapshot fallback; works for main-dex coins and HIP-3 coins
    (pass the prefixed name, e.g. xyz:CL)."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (VOL_LOOKBACK_DAYS + 2) * 86_400_000
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1d", "startTime": start_ms, "endTime": end_ms},
    }
    try:
        data = _hl_post(payload)
        if not isinstance(data, list) or not data:
            return None
        closes = [float(c.get("c")) for c in data if c.get("c") is not None]
        return closes if len(closes) >= 3 else None
    except Exception:
        return None


def _weighted_daily_vol(closes: list[float], halflife_days: float) -> tuple[float, float]:
    """Returns (ewma_vol, simple_vol) of daily returns. Most recent day gets
    the highest weight. Zero-mean assumption on the EWMA leg."""
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] != 0]
    n = len(rets)
    if n < 2:
        raise ValueError("not enough returns")
    decay = 0.5 ** (1.0 / halflife_days)
    weights = [decay ** (n - 1 - i) for i in range(n)]   # i = n-1 is most recent
    wsum = sum(weights)
    ewma_var = sum(w * r * r for w, r in zip(weights, rets)) / wsum
    mean = sum(rets) / n
    simple_var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return ewma_var ** 0.5, simple_var ** 0.5


def _fetch_daily_closes_okx(base: str) -> list[float] | None:
    """OKX public market candles fallback (no auth). Tries BASE-USDT-SWAP."""
    inst_id = f"{base}-USDT-SWAP"
    url = f"{OKX_BASE}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": "1D", "limit": str(VOL_LOOKBACK_DAYS + 2)}
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        data = r.json()
        if r.status_code != 200 or data.get("code") not in (None, "0"):
            return None
        rows = data.get("data", []) or []
        if len(rows) < 3:
            return None
        # OKX returns newest-first; close is index 4. Reverse to oldest-first.
        closes = [float(row[4]) for row in reversed(rows)]
        return closes
    except (requests.RequestException, ValueError, IndexError):
        return None


def _get_vol_for_base(base: str, hl_symbol_hint: str | None, cache: dict) -> dict | None:
    """Resolve daily closes for a base ticker (Binance first, HL fallback) and
    compute vol stats. Cached per base. hl_symbol_hint is the HL-native symbol
    (e.g. xyz:CL) if the base was seen on an HL position."""
    if base in cache:
        return cache[base]

    closes = _fetch_daily_closes_binance(base)
    source = "Binance"
    if closes is None:
        candidates = []
        if hl_symbol_hint:
            candidates.append(hl_symbol_hint)
        candidates.append(base)
        for dex in HL_DEXS:
            if dex:
                candidates.append(f"{dex}:{base}")
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            closes = _fetch_daily_closes_hl(cand)
            if closes is not None:
                source = f"Hyperliquid ({cand})"
                break
    if closes is None:
        closes = _fetch_daily_closes_okx(base)
        if closes is not None:
            source = f"OKX ({base}-USDT-SWAP)"

    if closes is None or len(closes) < 3:
        cache[base] = None
        return None

    try:
        ewma_vol, simple_vol = _weighted_daily_vol(closes, VOL_HALFLIFE_DAYS)
    except ValueError:
        cache[base] = None
        return None

    required_dist = VOL_BUFFER_SIGMAS * ewma_vol           # fraction
    denom = required_dist + VOL_MMR
    raw_lev = float("inf") if denom <= 0 else 1.0 / denom
    rec_lev = max(VOL_LEV_MIN, min(VOL_LEV_MAX, raw_lev))

    out = {
        "base": base,
        "source": source,
        "n_returns": len(closes) - 1,
        "ewma_vol": ewma_vol,
        "simple_vol": simple_vol,
        "required_dist_pct": required_dist * 100.0,
        "raw_leverage": raw_lev,
        "recommended_leverage": rec_lev,
    }
    cache[base] = out
    return out


def _okx_implied_cross_dist(direction: str, mgn_ratio: float, acct_mmr: float,
                            pos_mmr: float, notional: float) -> float | None:
    """Implied distance-to-liq for an OKX cross position, holding other
    positions static. The account liquidates when this position's adverse PnL
    consumes the shared buffer adjEq - mmr_total = mmr_total*(R-1), with the
    position's own mmr scaling with its notional as price moves:
      short: dist = mmr_total*(R-1) / (notional * (1 + m_i))
      long:  dist = mmr_total*(R-1) / (notional * (1 - m_i))
    where m_i = pos_mmr/notional. Uses ACCOUNT mmr in the numerator: the whole
    buffer absorbs the move, so small positions sharing the pool with a large
    one have very large implied distances."""
    if mgn_ratio <= 1.0 or notional <= 0 or acct_mmr <= 0:
        return None
    m_i = (pos_mmr / notional) if pos_mmr > 0 else 0.0
    if m_i >= 1.0:
        return None
    buffer_usd = acct_mmr * (mgn_ratio - 1.0)
    if direction == "LONG":
        return buffer_usd / (notional * (1.0 - m_i)) * 100.0
    return buffer_usd / (notional * (1.0 + m_i)) * 100.0


def check_vol_leverage(all_results: list[dict], okx_mgn_status: dict | None) -> list[dict]:
    """Compare each position's distance-to-liq against its vol-implied required
    distance. Returns breach dicts with vol context."""
    if not VOL_CHECK_ENABLED:
        return []

    vol_cache: dict = {}
    breaches = []

    for r in all_results:
        base = _vol_base_ticker(r["symbol"])
        hl_hint = r["symbol"] if r["exchange"] == "Hyperliquid" else None
        vol = _get_vol_for_base(base, hl_hint, vol_cache)
        if vol is None:
            print(f"  [vol check] no price history for {r['symbol']} (base {base}); skipped")
            continue

        dist_pct = r.get("dist_pct")
        dist_source = "exchange liq price"

        if dist_pct is None and r["exchange"] == "OKX" and okx_mgn_status:
            pos_mmr = r.get("pos_mmr") or 0.0
            dist_pct = _okx_implied_cross_dist(
                r["direction"], okx_mgn_status["mgn_ratio"], okx_mgn_status["mmr"],
                pos_mmr, r["notional_usd"]
            )
            dist_source = "implied from OKX mgnRatio (approx)"

        if dist_pct is None:
            print(f"  [vol check] no liq distance available for {r['exchange']} {r['symbol']}; skipped")
            continue

        required = vol["required_dist_pct"]
        if dist_pct < required:
            implied_current_lev = 100.0 / dist_pct if dist_pct > 0 else float("inf")
            breaches.append({
                "exchange": r["exchange"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "notional_usd": r["notional_usd"],
                "dist_pct": dist_pct,
                "dist_source": dist_source,
                "required_dist_pct": required,
                "ewma_vol_pct": vol["ewma_vol"] * 100.0,
                "simple_vol_pct": vol["simple_vol"] * 100.0,
                "n_returns": vol["n_returns"],
                "vol_source": vol["source"],
                "recommended_leverage": vol["recommended_leverage"],
                "implied_current_leverage": implied_current_lev,
            })

    return breaches


def _send_slack_vol_alert(breaches: list[dict]):
    """Slack + email alert for positions whose liq distance is inside the
    vol-implied safe distance."""
    if not breaches:
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f":chart_with_downwards_trend: *Volatility Leverage Warning* - {ts}"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Volatility Leverage Warning"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": ts}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"_Distance-to-liq is inside the vol-implied safe distance "
            f"({VOL_BUFFER_SIGMAS:.0f} weighted daily sigmas). "
            f"*Add collateral or reduce leverage* on the positions below._"
        )}},
    ]

    for b in breaches:
        fields_text = (
            f"*{b['exchange']}  |  {b['symbol']}  ({b['direction']})*\n"
            f"Notional: `${b['notional_usd']:,.0f}`\n"
            f"Distance to liq: *{b['dist_pct']:.2f}%*  ({b['dist_source']})\n"
            f"Vol-required distance: *{b['required_dist_pct']:.2f}%*\n"
            f"Implied current leverage: `{b['implied_current_leverage']:.2f}x`    "
            f"Recommended: *{b['recommended_leverage']:.2f}x*\n"
            f"EWMA daily vol: `{b['ewma_vol_pct']:.2f}%`    "
            f"Simple daily vol: `{b['simple_vol_pct']:.2f}%`    "
            f"({b['n_returns']} returns, {b['vol_source']})"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": fields_text}})
        blocks.append({"type": "divider"})

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
            print(f"Slack vol alert sent for {len(breaches)} position(s).")
    except requests.RequestException as exc:
        print(f"[Slack error] {exc}")

    # ---- email mirror ----
    subject = f"[Vol Warning] {len(breaches)} position(s) need lower leverage or more collateral"
    rows_html = "".join(
        f"<tr><td>{b['exchange']}</td><td>{b['symbol']}</td><td>{b['direction']}</td>"
        f"<td align='right'>${b['notional_usd']:,.0f}</td>"
        f"<td align='right'>{b['dist_pct']:.2f}%</td>"
        f"<td align='right'><b>{b['required_dist_pct']:.2f}%</b></td>"
        f"<td align='right'>{b['implied_current_leverage']:.2f}x</td>"
        f"<td align='right'><b>{b['recommended_leverage']:.2f}x</b></td>"
        f"<td align='right'>{b['ewma_vol_pct']:.2f}%</td></tr>"
        for b in breaches
    )
    html = f"""
    <h3>Volatility Leverage Warning &ndash; {ts}</h3>
    <p>Distance-to-liq is inside the vol-implied safe distance
    ({VOL_BUFFER_SIGMAS:.0f} weighted daily sigmas).
    <b>Add collateral or reduce leverage</b> on these positions.</p>
    <table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:monospace'>
      <tr><th>Exchange</th><th>Symbol</th><th>Dir</th><th>Notional</th>
          <th>Dist to Liq</th><th>Required Dist</th><th>Curr Lev</th><th>Rec Lev</th><th>EWMA Vol</th></tr>
      {rows_html}
    </table>
    <p>Vol params: lookback={VOL_LOOKBACK_DAYS}d, halflife={VOL_HALFLIFE_DAYS}d,
    buffer={VOL_BUFFER_SIGMAS} sigmas</p>
    """
    text = "\n".join(
        f"{b['exchange']} {b['symbol']} {b['direction']} "
        f"dist={b['dist_pct']:.2f}% required={b['required_dist_pct']:.2f}% "
        f"currLev={b['implied_current_leverage']:.2f}x recLev={b['recommended_leverage']:.2f}x "
        f"ewmaVol={b['ewma_vol_pct']:.2f}%"
        for b in breaches
    )
    _send_email(subject, html, text)


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
            bucket["short"] += signed

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
            note = "(cross, see mgnRatio)" if r["exchange"] == "OKX" else "(cross, no liq px)"
            print(
                f"  {r['exchange']:<14} {r['symbol']:<16} {r['direction']:<6} "
                f"{r['size']:>14,.4f} ${r['notional_usd']:>14,.0f} "
                f"{r['mark']:>12,.4f} {'--':>12} {'--':>8}  {note}"
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


def _print_vol_summary(breaches: list[dict]):
    if not breaches:
        print("All positions outside vol-implied required distance. No vol alerts fired.")
        return
    print(f"\n{'-'*108}")
    print(f"  Vol-Implied Leverage Breaches  |  buffer: {VOL_BUFFER_SIGMAS:.0f} sigmas, "
          f"lookback: {VOL_LOOKBACK_DAYS}d, halflife: {VOL_HALFLIFE_DAYS}d")
    print(f"{'-'*108}")
    print(
        f"  {'Exchange':<14} {'Symbol':<16} {'Dir':<6} "
        f"{'Dist':>8} {'Required':>9} {'EWMAVol':>8} {'CurrLev':>8} {'RecLev':>7}"
    )
    for b in breaches:
        print(
            f"  {b['exchange']:<14} {b['symbol']:<16} {b['direction']:<6} "
            f"{b['dist_pct']:>7.2f}% {b['required_dist_pct']:>8.2f}% "
            f"{b['ewma_vol_pct']:>7.2f}% {b['implied_current_leverage']:>7.2f}x "
            f"{b['recommended_leverage']:>6.2f}x"
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

    breached = [
        r for r in all_results
        if r.get("dist_pct") is not None and r["dist_pct"] < LIQ_THRESHOLD_PCT
    ]
    if breached:
        _send_slack_alert(breached)
    else:
        print("All positions above liq threshold. No liq alerts fired.")

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

    # ---- vol-implied leverage check (independent of the fixed threshold) ----
    try:
        vol_input = list(all_results)
        if VOL_TEST_POSITION:
            print("  [vol check] TEST position injected: LAB LONG, dist=31.00% "
                  "(vol-check input only; excluded from liq/delta alerts)")
            vol_input.append(_make_test_position())
        vol_breaches = check_vol_leverage(vol_input, okx_mgn_status)
        _print_vol_summary(vol_breaches)
        if vol_breaches:
            _send_slack_vol_alert(vol_breaches)
    except Exception as exc:
        print(f"[Vol check error] {exc}")

    deltas = _compute_exchange_deltas(all_results)
    _print_delta_summary(deltas)
    delta_breaches = [d for d in deltas if abs(d["net_delta_usd"]) > DELTA_THRESHOLD_USD]
    if delta_breaches:
        _send_slack_delta_alert(delta_breaches)
    else:
        print("All exchanges within delta threshold. No delta alerts fired.")


if __name__ == "__main__":
    run()
