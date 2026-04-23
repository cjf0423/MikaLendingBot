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
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime

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
    nonce = str(int(time.time() * 1000))
    body_json = json.dumps(body)
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
    path = "/v2/auth/w/funding/offer/cancel/all"
    body = {"currency": symbol.lstrip("f")}
    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    log("[撤单] 已取消所有未成交挂单")

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
    if abs(ex_amount - target_amount) > 1.0:
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
    log(f"[结果] 下单成功")


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    if not API_KEY or not API_SECRET:
        log("[错误] 请设置 BFX_API_KEY 和 BFX_API_SECRET 环境变量")
        send_ql_notify("Bitfinex 放贷 ❌")
        return

    currency = SYMBOL.lstrip("f")

    log("=" * 50)
    log(f"Bitfinex 自动放贷 | {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"币种: {SYMBOL} | FRR模式: {USE_FRR} | 天数: {PERIOD} | 预留: {RESERVE_AMOUNT}")
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
                # credits 结构: [ID, SYMBOL, SIDE, MTS_CREATE, MTS_UPDATE,
                #                AMOUNT, FLAGS, STATUS, ..., RATE, PERIOD,
                #                MTS_OPENING, MTS_LAST_PAYOUT, NOTIFY, HIDDEN,
                #                RENEW, NO_CLOSE, RATE_REAL, NO_CLOSE]
                amount    = abs(float(c[5]))
                rate_pct  = float(c[11]) * 100          # 日利率%
                period    = int(c[12])                   # 总天数
                mts_open  = float(c[13]) if c[13] else 0 # 开始时间ms
                # 剩余天数 = 开始时间 + 总天数*86400000 - 现在
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

    try:
        active_offers = get_active_offers(SYMBOL)
    except Exception as e:
        log(f"[警告] 获取挂单失败，默认重挂：{e}")
        active_offers = []

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
        log("[完成] 现有挂单无变化，跳过")
        send_ql_notify("Bitfinex 放贷 ✅")
        return

    # 6. 撤单
    try:
        cancel_all_funding_offers(SYMBOL)
    except Exception as e:
        log(f"[警告] 取消挂单失败：{e}")

    log("[等待] 撤单确认中，等待 5 秒...")
    time.sleep(5)

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

