#!/usr/bin/env python3
"""
Bitfinex 自动放贷脚本
适用于青龙面板定时运行

青龙环境变量（必填）：
  BFX_API_KEY        - Bitfinex API Key
  BFX_API_SECRET     - Bitfinex API Secret

青龙环境变量（选填，不填则使用下方默认值）：
  BFX_FRR_OFFSET     - FRR 偏移值（%/天），例：-0.001 或 0.002，默认 0
  BFX_PERIOD         - 挂单天数（2~120），默认 2
  BFX_RESERVE        - 预留资金 USD，默认 0
  BFX_REORDER_AMOUNT_THRESHOLD - 金额变化超过此 USD 金额时重挂，默认 1
  BFX_TRANSFER_SMALL_UNCOMMITTED_TO_EXCHANGE - true 时将低于金额阈值的未挂出资金转至现货账户，默认 false
  BFX_DCA_ENABLED    - true 时在小额划转成功后自动市价买入指定标的，默认 false
  BFX_DCA_SYMBOL     - 定投标的，如 ETH、BTC，默认空（未设置则不买入）
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import requests

# ===== 脚本内固定配置 =====

SYMBOL = "fUSD"
USE_FRR = True
FIXED_RATE = 0.018
MIN_OFFER_AMOUNT = 150.0
FRR_CHANGE_THRESHOLD = 0.002
DRY_RUN = False

# ===== 从青龙环境变量读取 =====

API_KEY        = os.environ.get("BFX_API_KEY", "")
API_SECRET     = os.environ.get("BFX_API_SECRET", "")
FRR_OFFSET     = float(os.environ.get("BFX_FRR_OFFSET", "0"))
PERIOD         = int(os.environ.get("BFX_PERIOD", "2"))
RESERVE_AMOUNT = float(os.environ.get("BFX_RESERVE", "0"))
REORDER_AMOUNT_THRESHOLD = float(
    os.environ.get("BFX_REORDER_AMOUNT_THRESHOLD", "1")
)
TRANSFER_SMALL_UNCOMMITTED_TO_EXCHANGE = (
    os.environ.get("BFX_TRANSFER_SMALL_UNCOMMITTED_TO_EXCHANGE", "false")
    .strip()
    .lower()
    == "true"
)
DCA_ENABLED = (
    os.environ.get("BFX_DCA_ENABLED", "false")
    .strip()
    .lower()
    == "true"
)
DCA_SYMBOL = os.environ.get("BFX_DCA_SYMBOL", "").strip().upper()

# ===== 配置区结束 =====

API_BASE = "https://api.bitfinex.com"

# 青龙通知：收集所有日志，最后统一发送
_notify_lines = []

def log(msg: str):
    """打印日志并加入通知队列"""
    print(msg)
    _notify_lines.append(msg)

def send_ql_notify(title: str):
    """调用青龙内置通知"""
    try:
        from notify import send
        send(title, "\n".join(_notify_lines))
        print("[通知] 已发送青龙通知")
    except Exception as e:
        print(f"[通知] 发送失败（非青龙环境或未配置）：{e}")


# ─── Bitfinex API ─────────────────────────────────────────────────────────────

def bfx_auth_headers(path: str, body: dict) -> dict:
    return bfx_auth_headers_for_json(path, json.dumps(body))


def bfx_auth_headers_for_json(path: str, body_json: str) -> dict:
    nonce = str(int(time.time() * 1000))
    sig_payload = f"/api{path}{nonce}{body_json}"
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        sig_payload.encode("utf-8"),
        hashlib.sha384,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "bfx-apikey": API_KEY,
        "bfx-nonce": nonce,
        "bfx-signature": signature,
    }

def get_frr(symbol: str = "fUSD") -> float:
    url = f"https://api-pub.bitfinex.com/v2/tickers?symbols={symbol}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return float(resp.json()[0][1])

def get_wallet_balance(currency: str = "USD") -> float:
    path = "/v2/auth/r/wallets"
    headers = bfx_auth_headers(path, {})
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json={}, timeout=10)
    resp.raise_for_status()
    for w in resp.json():
        if w[0] == "funding" and w[1] == currency:
            return float(w[4] if w[4] is not None else w[2])
    return 0.0

def get_explicit_funding_available_balance(currency: str = "USD"):
    """仅返回交易所明确给出的 funding 可用余额；未知时返回 None。"""
    path = "/v2/auth/r/wallets"
    headers = bfx_auth_headers(path, {})
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json={}, timeout=10)
    resp.raise_for_status()
    for w in resp.json():
        if w[0] == "funding" and w[1] == currency:
            if len(w) > 4 and w[4] is not None:
                return Decimal(str(w[4]))
            return None
    return None


def format_transfer_amount(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


def transfer_funding_to_exchange(currency: str, amount: Decimal):
    """将 funding 钱包中的可用资金划转到 exchange（现货）钱包。"""
    amount = format_transfer_amount(amount)
    if amount <= 0:
        raise ValueError("划转金额必须大于 0")

    path = "/v2/auth/w/transfer"
    body = {
        "from": "funding",
        "to": "exchange",
        "currency": currency,
        "amount": format(amount, "f"),
    }
    body_json = json.dumps(body)
    headers = bfx_auth_headers_for_json(path, body_json)
    resp = requests.post(
        f"{API_BASE}{path}", headers=headers, data=body_json, timeout=10
    )
    # [修复 #6] HTTP 非 2xx 时安全记录响应正文后重新抛出，不重试
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        try:
            body_text = resp.text
        except Exception as body_error:
            body_text = f"<unavailable: {body_error}>"
        limit = 2048
        suffix = " …[truncated]" if len(body_text) > limit else ""
        log(
            f"[错误] 划转 HTTP 响应: "
            f"status={resp.status_code} body={repr(body_text[:limit] + suffix)}"
        )
        raise

    result = resp.json()
    if not isinstance(result, list) or len(result) < 8 or result[6] != "SUCCESS":
        raise RuntimeError(f"划转未成功：{result}")

    transfer = result[4]
    if not isinstance(transfer, list) or len(transfer) < 8:
        raise RuntimeError(f"划转响应格式异常：{result}")
    if transfer[1] != "funding" or transfer[2] != "exchange":
        raise RuntimeError(f"划转钱包方向异常：{result}")
    if transfer[4] != currency or transfer[5] is not None:
        raise RuntimeError(f"划转币种异常：{result}")
    if Decimal(str(transfer[7])) != amount:
        raise RuntimeError(f"划转金额异常：{result}")

    log(f"[划转] 已从 funding 转入现货账户: {amount:.8f} {currency}")


def dca_market_buy(target_crypto: str, usd_amount: Decimal):
    """
    定投：在现货账户（Exchange）按市价买入指定币种。
    只花费划转出的 usd_amount，不影响现货账户原有的资金。
    如果金额太小低于交易所最小交易量，捕获并友好跳过。
    """
    if not DCA_ENABLED:
        return
    if not target_crypto:
        log("[定投] 已启用定投但未设置 BFX_DCA_SYMBOL，跳过买入")
        return
    if usd_amount <= 0:
        log("[定投] 划转金额 <= 0，跳过买入")
        return

    pair = f"t{target_crypto}USD"
    log(f"[定投] 准备使用划转的 {usd_amount:.4f} USD 市价买入 {target_crypto} (交易对: {pair})")

    if DRY_RUN:
        log(f"[DRY RUN] 跳过实际定投买入")
        return

    # 1. 查询当前市价以计算买入数量
    try:
        url = f"https://api-pub.bitfinex.com/v2/ticker/{pair}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        ticker = resp.json()
        # ticker 结构: [BID, BID_SIZE, ASK, ASK_SIZE, DAILY_CHANGE, DAILY_CHANGE_RELATIVE, LAST_PRICE, ...]
        ask_price = float(ticker[2])  # 用卖一价估算买入数量
        if ask_price <= 0:
            ask_price = float(ticker[6])  # 降级到最新成交价
    except Exception as e:
        log(f"[定投警告] 获取 {pair} 行情失败，跳过本次定投: {e}")
        return

    if ask_price <= 0:
        log(f"[定投警告] 价格异常 ({ask_price})，跳过本次定投")
        return

    # 2. 计算可购买的基础代币数量 (例如 ETH 数量)
    # Bitfinex 大多数代币支持 8 位小数，稍微留一点滑点余量 (比如 99.5%) 避免市价单因价格波动导致余额不足
    est_crypto_amount = (usd_amount * Decimal("0.995")) / Decimal(str(ask_price))
    # 向下截断到 8 位小数
    crypto_amount = est_crypto_amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    if crypto_amount <= Decimal("0"):
        log(f"[定投跳过] 计算出的买入数量过小 ({crypto_amount})，跳过本次定投")
        return

    log(f"[定投] 当前卖一价 ~{ask_price:.2f} USD，计划买入 {crypto_amount:.8f} {target_crypto} (约 {usd_amount * Decimal('0.995'):.4f} USD)")

    # 3. 提交市价单
    path = "/v2/auth/w/order/submit"
    body = {
        "type": "EXCHANGE MARKET",
        "symbol": pair,
        "amount": format(crypto_amount, "f"),  # 正数表示买入
    }
    body_json = json.dumps(body)
    headers = bfx_auth_headers_for_json(path, body_json)

    try:
        resp = requests.post(
            f"{API_BASE}{path}", headers=headers, data=body_json, timeout=10
        )
        # 即使返回 4xx/500，也解析正文以防是业务拒绝（如 minimum order size）
        result = None
        try:
            result = resp.json()
        except Exception:
            pass

        if resp.status_code != 200:
            err_msg = resp.text[:300]
            # 常见情况：金额太小，交易所拒绝
            if "minimum size" in err_msg.lower() or "not enough balance" in err_msg.lower() or "amount" in err_msg.lower():
                log(f"[定投跳过] 交易所拒绝下单（通常因金额太小低于最小交易量）: {err_msg}")
                return
            log(f"[定投警告] 下单失败 HTTP {resp.status_code}: {err_msg}")
            return

        # 验证 notification
        notif = result
        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
            notif = result[0]
        if isinstance(notif, list) and len(notif) >= 7:
            status = notif[6]
            text = notif[7] if len(notif) > 7 else ""
            if status == "SUCCESS":
                log(f"[定投成功] 已成功市价买入 {crypto_amount:.8f} {target_crypto} ✅")
            else:
                log(f"[定投跳过] 下单被交易所拒绝 ({status}: {text})，可能是金额太小低于最小限额")
        else:
            log(f"[定投] 下单响应: {result}")
    except Exception as e:
        log(f"[定投警告] 下单请求异常，跳过本次定投: {e}")



def maybe_transfer_small_uncommitted_to_exchange(
    active_offers, currency: str, target_amount: float
):
    """在无需重挂时，将低于阈值的未挂出 funding 余额转入现货账户。"""
    if not TRANSFER_SMALL_UNCOMMITTED_TO_EXCHANGE:
        return None
    if len(active_offers) != 1:
        return None
    if REORDER_AMOUNT_THRESHOLD <= 0:
        log("[划转] 金额阈值必须大于 0，跳过小额划转")
        return False
    if DRY_RUN:
        log("[DRY RUN] 跳过实际小额划转")
        return None

    existing_amount = abs(Decimal(str(active_offers[0][4])))
    amount_delta = Decimal(str(target_amount)) - existing_amount
    threshold = Decimal(str(REORDER_AMOUNT_THRESHOLD))
    if not Decimal("0") < amount_delta < threshold:
        return None

    try:
        available_balance = get_explicit_funding_available_balance(currency)
    except Exception as e:
        log(f"[错误] 获取可用 funding 余额失败，跳过小额划转：{e}")
        return False

    if available_balance is None:
        log("[错误] 未取得明确的 funding 可用余额，跳过小额划转")
        return False

    reserve = Decimal(str(RESERVE_AMOUNT))
    transferable = min(amount_delta, max(Decimal("0"), available_balance - reserve))
    transferable = format_transfer_amount(transferable)
    if transferable <= 0:
        log("[划转] 扣除预留后没有可划转的未挂出余额")
        return None

    try:
        transfer_funding_to_exchange(currency, transferable)
    except (InvalidOperation, ValueError, requests.RequestException, RuntimeError) as e:
        log(f"[错误] 小额划转失败：{e}")
        return False

    # 划转成功后，如果启用了定投，则使用划转出来的这笔金额买入指定代币
    if DCA_ENABLED and DCA_SYMBOL:
        try:
            dca_market_buy(DCA_SYMBOL, transferable)
        except Exception as e:
            log(f"[定投错误] 定投执行异常（不影响划转结果）：{e}")

    return True


def get_active_offers(symbol: str) -> list:
    path = f"/v2/auth/r/funding/offers/{symbol}"
    headers = bfx_auth_headers(path, {})
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json={}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def get_active_credits(symbol: str) -> list:
    """获取已成交（放贷中）的订单"""
    path = f"/v2/auth/r/funding/credits/{symbol}"
    headers = bfx_auth_headers(path, {})
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json={}, timeout=10)
    resp.raise_for_status()
    return resp.json()

def cancel_all_funding_offers(symbol: str):
    """[修复 #3] 撤单后验证 notification 业务状态"""
    path = "/v2/auth/w/funding/offer/cancel/all"
    body = {"currency": symbol.lstrip("f")}
    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    # Bitfinex notification 格式: [MTS, TYPE, MESSAGE_ID, null, null, null, STATUS, TEXT]
    # 或嵌套在外层数组中
    notif = result
    if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
        notif = result[0]
    if not isinstance(notif, list) or len(notif) < 7:
        raise RuntimeError(f"撤单响应格式异常，无法确认成功: {result}")
    notif_type = notif[1] if len(notif) > 1 else None
    notif_status = notif[6] if len(notif) > 6 else None
    if notif_type != "foc_all-req" or notif_status != "SUCCESS":
        raise RuntimeError(
            f"撤单 notification 未确认成功: type={notif_type} status={notif_status} raw={result}"
        )
    log("[撤单] 已取消所有未成交挂单（notification 确认 SUCCESS）")

def calc_target_rate(use_frr, frr_offset, fixed_rate, current_frr):
    MIN_RATE = 0.000001
    if use_frr and frr_offset >= 0:
        offer_type = "FRRDELTAVAR"
        offer_rate = frr_offset / 100
        rate_desc  = f"FRRDELTAVAR 偏移 {frr_offset:+.3f}%/天（服务端浮动）"
    elif use_frr and frr_offset < 0:
        offer_type = "LIMIT"
        offer_rate = current_frr + (frr_offset / 100)
        rate_desc  = f"LIMIT {offer_rate*100:.6f}%/天（FRR {current_frr*100:.6f}% {frr_offset:+.3f}%）"
    else:
        offer_type = "LIMIT"
        offer_rate = fixed_rate / 100
        rate_desc  = f"LIMIT 固定 {fixed_rate:.6f}%/天"
    if offer_type == "LIMIT" and offer_rate < MIN_RATE:
        log(f"[警告] 利率过低，已调整为最低值 {MIN_RATE}")
        offer_rate = MIN_RATE
    return offer_type, offer_rate, rate_desc

def needs_reorder(active_offers, target_type, target_rate, target_amount, target_period):
    if not active_offers:
        return True, "无挂单，需要新建"
    if len(active_offers) > 1:
        return True, f"存在 {len(active_offers)} 笔挂单，合并重挂"
    o = active_offers[0]
    ex_type   = o[6]
    ex_rate   = float(o[14])
    ex_amount = abs(float(o[4]))
    ex_period = int(o[15])
    if ex_type != target_type:
        return True, f"类型变更 {ex_type} → {target_type}"
    if ex_period != target_period:
        return True, f"天数变更 {ex_period} → {target_period}"
    if abs(ex_amount - target_amount) > REORDER_AMOUNT_THRESHOLD:
        return True, f"金额变化 {ex_amount:.2f} → {target_amount:.2f}"
    if target_type == "FRRDELTAVAR":
        if abs(ex_rate - target_rate) < 1e-9:
            return False, "FRRDELTAVAR 无变化，保持现有挂单"
        return True, "FRRDELTAVAR 偏移值变更"
    rate_chg = abs(ex_rate - target_rate) * 100
    if rate_chg > FRR_CHANGE_THRESHOLD:
        return True, f"利率变动 {rate_chg:.4f}%/天 超过阈值"
    return False, f"利率变动 {rate_chg:.4f}%/天 未超阈值，保持现有挂单"

def submit_funding_offer(symbol, amount, period, offer_type, offer_rate, rate_desc):
    """[修复 #5] 提交后验证 notification 业务状态"""
    path = "/v2/auth/w/funding/offer/submit"
    body = {
        "type": offer_type, "symbol": symbol,
        "amount": str(round(amount, 8)),
        "rate":   str(round(offer_rate, 10)),
        "period": period, "flags": 0,
    }
    log(f"[下单] {rate_desc} | 金额={amount:.2f} 天数={period}d")
    if DRY_RUN:
        log("[DRY RUN] 跳过实际下单")
        return
    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    # Bitfinex notification 格式: [MTS, TYPE, MESSAGE_ID, null, OFFER_DATA, null, STATUS, TEXT]
    notif = result
    if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
        notif = result[0]
    if not isinstance(notif, list) or len(notif) < 7:
        raise RuntimeError(f"下单响应格式异常，无法确认成功: {result}")
    notif_type = notif[1] if len(notif) > 1 else None
    notif_status = notif[6] if len(notif) > 6 else None
    if notif_type != "fon-req" or notif_status != "SUCCESS":
        raise RuntimeError(
            f"下单 notification 未确认成功: type={notif_type} status={notif_status} raw={result}"
        )
    log(f"[结果] 下单成功（notification 确认 SUCCESS）")


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    if not API_KEY or not API_SECRET:
        log("[错误] 请设置 BFX_API_KEY 和 BFX_API_SECRET 环境变量")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    currency = SYMBOL.lstrip("f")

    log("=" * 50)
    log(f"Bitfinex 自动放贷 | {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(
        f"币种: {SYMBOL} | FRR模式: {USE_FRR} | 天数: {PERIOD} | "
        f"预留: {RESERVE_AMOUNT} | 金额重挂阈值: {REORDER_AMOUNT_THRESHOLD} | "
        f"小额转现货: {TRANSFER_SMALL_UNCOMMITTED_TO_EXCHANGE} | "
        f"定投: {DCA_ENABLED} (标的: {DCA_SYMBOL or '未设置'})"
    )
    log("=" * 50)

    # 1. 获取 FRR
    current_frr = 0.0
    try:
        current_frr = get_frr(SYMBOL)
        frr_pct = current_frr * 100
        log(f"[FRR] 当前 FRR = {frr_pct:.6f}%/天（年化约 {frr_pct*365:.2f}%）")
    except Exception as e:
        if USE_FRR:
            log(f"[错误] 获取 FRR 失败：{e}")
            send_ql_notify("Bitfinex 放贷 ❌")
            return
        log(f"[警告] 获取 FRR 失败（不影响固定利率模式）：{e}")

    # 2. 显示已成交订单
    try:
        credits = get_active_credits(SYMBOL)
        if credits:
            log(f"\n[已成交订单] 共 {len(credits)} 笔放贷中：")
            total_lent = 0.0
            now_ms = time.time() * 1000
            for c in credits:
                amount    = abs(float(c[5]))
                rate_pct  = float(c[11]) * 100
                period    = int(c[12])
                mts_open  = float(c[13]) if c[13] else 0
                if mts_open > 0:
                    expire_ms    = mts_open + period * 86400 * 1000
                    remain_days  = max(0, (expire_ms - now_ms) / 86400 / 1000)
                    remain_str   = f"{remain_days:.1f}天"
                else:
                    remain_str = "未知"
                total_lent += amount
                log(f"  • {amount:.2f} {currency} | 利率 {rate_pct:.6f}%/天"
                    f"（年化 {rate_pct*365:.2f}%）| 剩余 {remain_str}")
            log(f"  合计放贷中: {total_lent:.2f} {currency}")
        else:
            log("[已成交订单] 暂无放贷中订单")
    except Exception as e:
        log(f"[警告] 获取已成交订单失败：{e}")

    log("")

    # 3. 计算目标利率
    target_type, target_rate, rate_desc = calc_target_rate(
        USE_FRR, FRR_OFFSET, FIXED_RATE, current_frr
    )
    log(f"[目标] {rate_desc}")

    # 4. 查询余额和挂单
    try:
        balance = get_wallet_balance(currency)
    except Exception as e:
        log(f"[错误] 获取余额失败：{e}")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    # [修复 #1] get_active_offers 失败时记录错误并返回，不伪造空列表
    try:
        active_offers = get_active_offers(SYMBOL)
    except Exception as e:
        log(f"[错误] 获取挂单失败，无法确定当前状态，停止执行：{e}")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    locked_amount  = sum(abs(float(o[4])) for o in active_offers)
    total_available = balance + locked_amount - RESERVE_AMOUNT

    log(f"[余额] 钱包可用: {balance:.2f} | 已挂出: {locked_amount:.2f} | 预留: {RESERVE_AMOUNT:.2f} | 可放贷: {total_available:.2f}")

    if active_offers:
        for o in active_offers:
            o_type   = o[6]
            o_rate   = float(o[14]) * 100
            o_amount = abs(float(o[4]))
            o_period = int(o[15])
            log(f"[挂单] {o_type} | {o_amount:.2f} {currency} | {o_rate:.6f}%/天 | {o_period}天")

    # 5. 判断是否需要重挂
    need, reason = needs_reorder(
        active_offers, target_type, target_rate, total_available, PERIOD
    )
    log(f"[判断] {'⚡ 需要重挂' if need else '✅ 无需重挂'} — {reason}")

    if not need:
        transfer_result = maybe_transfer_small_uncommitted_to_exchange(
            active_offers, currency, total_available
        )
        if transfer_result is False:
            send_ql_notify("Bitfinex 放贷 ❌")
            return
        if transfer_result is True:
            log("[完成] 保持现有挂单，小额未挂出余额已转入现货账户")
        else:
            log("[完成] 现有挂单无变化，跳过")
        send_ql_notify("Bitfinex 放贷 ✅")
        return

    # 6. 撤单 — [修复 #2] 任何异常都通知并返回，禁止后续下单
    try:
        cancel_all_funding_offers(SYMBOL)
    except Exception as e:
        log(f"[错误] 取消挂单失败，停止执行：{e}")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    log("[等待] 撤单确认中，等待 5 秒...")
    time.sleep(5)

    # [修复 #4] 撤单后重新读取 active offers，确认已全部撤除
    try:
        remaining_offers = get_active_offers(SYMBOL)
    except Exception as e:
        log(f"[错误] 撤单后无法确认挂单状态，停止执行：{e}")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    if remaining_offers:
        log(f"[错误] 撤单后仍有 {len(remaining_offers)} 笔挂单未消失，停止执行")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    # 7. 重新查余额
    try:
        balance = get_wallet_balance(currency)
    except Exception as e:
        log(f"[错误] 撤单后获取余额失败：{e}")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    available = balance - RESERVE_AMOUNT
    log(f"[余额] 撤单后可放贷: {available:.2f} {currency}")

    if available < MIN_OFFER_AMOUNT:
        log(f"[跳过] 可放贷金额 {available:.2f} 低于最低限额 {MIN_OFFER_AMOUNT:.2f}")
        # 金额不够挂单：仅当金额严格小于阈值时转到现货；≥阈值则留着等攒够再放贷
        if (TRANSFER_SMALL_UNCOMMITTED_TO_EXCHANGE
                and 0 < available < REORDER_AMOUNT_THRESHOLD
                and not DRY_RUN):
            log(f"[划转] 余额 {available:.2f} < 阈值 {REORDER_AMOUNT_THRESHOLD:.2f}，转入现货账户")
            transfer_amount = format_transfer_amount(Decimal(str(available)))
            if transfer_amount > 0:
                try:
                    transfer_funding_to_exchange(currency, transfer_amount)
                    if DCA_ENABLED and DCA_SYMBOL:
                        try:
                            dca_market_buy(DCA_SYMBOL, transfer_amount)
                        except Exception as e:
                            log(f"[定投错误] 定投执行异常（不影响划转结果）：{e}")
                except (InvalidOperation, ValueError, requests.RequestException, RuntimeError) as e:
                    log(f"[错误] 小额划转失败：{e}")
        elif available >= REORDER_AMOUNT_THRESHOLD:
            log(f"[等待] 余额 {available:.2f} ≥ 阈值 {REORDER_AMOUNT_THRESHOLD:.2f}，保留在 funding 等待攒够放贷")
        send_ql_notify("Bitfinex 放贷 ⚠️")
        return

    # 8. 提交新挂单
    try:
        submit_funding_offer(
            symbol=SYMBOL, amount=available, period=PERIOD,
            offer_type=target_type, offer_rate=target_rate, rate_desc=rate_desc,
        )
    except Exception as e:
        log(f"[错误] 下单失败：{e}")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    log("[完成] 放贷挂单已提交 ✅")
    send_ql_notify("Bitfinex 放贷 ✅")


if __name__ == "__main__":
    main()
