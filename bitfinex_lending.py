#!/usr/bin/env python3
"""
Bitfinex 自动放贷脚本
适用于青龙面板定时运行

青龙环境变量（必填）：
  BFX_API_KEY        - Bitfinex API Key
  BFX_API_SECRET     - Bitfinex API Secret

青龙环境变量（选填，不填则使用下方默认值）：
  BFX_FRR_OFFSET     - FRR 偏移值（%/天），例：-0.001 或 0.002，默认 -0.001
  BFX_PERIOD         - 挂单天数（2~120），默认 2
  BFX_RESERVE        - 预留资金 USD，默认 0
"""

import hashlib
import hmac
import json
import os
import time

import requests

# ===== 脚本内固定配置（不常改的放这里）=====

SYMBOL = "fUSD"           # 放贷币种，fUSD / fBTC / fETH 等

USE_FRR = True            # True  = 以 FRR 为基准下单（推荐）
                          # False = 使用固定日利率下单

FIXED_RATE = 0.018        # 固定日利率（%/天），仅 USE_FRR=False 时生效

MIN_OFFER_AMOUNT = 150.0   # 单笔最低放贷金额（Bitfinex 要求最低 50 USD）

# FRR 变动阈值（%/天），LIMIT 负偏移模式下 FRR 变动超过此值才撤单重挂
FRR_CHANGE_THRESHOLD = 0.002

DRY_RUN = False           # True = 仅打印不实际下单，调试用

# ===== 从青龙环境变量读取（可在青龙面板随时修改）=====

API_KEY    = os.environ.get("BFX_API_KEY", "")
API_SECRET = os.environ.get("BFX_API_SECRET", "")

FRR_OFFSET     = float(os.environ.get("BFX_FRR_OFFSET", "-0.001"))
PERIOD         = int(os.environ.get("BFX_PERIOD", "2"))
RESERVE_AMOUNT = float(os.environ.get("BFX_RESERVE", "0"))

# ===== 配置区结束 =====

API_BASE = "https://api.bitfinex.com"


def bfx_auth_headers(path: str, body: dict) -> dict:
    nonce = str(int(time.time() * 1000))
    body_json = json.dumps(body)
    signature_payload = f"/api{path}{nonce}{body_json}"
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha384,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "bfx-apikey": API_KEY,
        "bfx-nonce": nonce,
        "bfx-signature": signature,
    }


def get_wallet_balance(currency: str = "USD") -> float:
    path = "/v2/auth/r/wallets"
    body = {}
    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    for w in resp.json():
        if w[0] == "funding" and w[1] == currency:
            available = w[4] if w[4] is not None else w[2]
            return float(available)
    return 0.0


def get_frr(symbol: str = "fUSD") -> float:
    """获取当前 FRR（日利率小数），例：0.00045678 = 0.045678%/天"""
    url = f"https://api-pub.bitfinex.com/v2/tickers?symbols={symbol}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return float(resp.json()[0][1])


def get_active_offers(symbol: str) -> list:
    """获取当前未成交的放贷挂单列表"""
    path = f"/v2/auth/r/funding/offers/{symbol}"
    body = {}
    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    return resp.json()


def cancel_all_funding_offers(symbol: str):
    path = "/v2/auth/w/funding/offer/cancel/all"
    body = {"currency": symbol.lstrip("f")}
    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    print(f"[撤单] 已取消所有 {symbol} 挂单")


def calc_target_rate(use_frr: bool, frr_offset: float,
                     fixed_rate: float, current_frr: float) -> tuple:
    """
    计算目标利率和订单类型
    返回 (offer_type, offer_rate, rate_desc)
    """
    MIN_RATE = 0.000001

    if use_frr and frr_offset >= 0:
        offer_type = "FRRDELTAVAR"
        offer_rate = frr_offset / 100
        rate_desc = f"FRRDELTAVAR 偏移 {frr_offset:+.3f}%/天（服务端浮动）"
    elif use_frr and frr_offset < 0:
        offer_type = "LIMIT"
        offer_rate = current_frr + (frr_offset / 100)
        rate_pct = offer_rate * 100
        rate_desc = f"LIMIT {rate_pct:.6f}%/天（FRR {current_frr*100:.6f}% {frr_offset:+.3f}%）"
    else:
        offer_type = "LIMIT"
        offer_rate = fixed_rate / 100
        rate_desc = f"LIMIT 固定 {fixed_rate:.6f}%/天"

    if offer_type == "LIMIT" and offer_rate < MIN_RATE:
        print(f"[警告] 计算利率 {offer_rate:.8f} 过低，已调整为最低值 {MIN_RATE}")
        offer_rate = MIN_RATE

    return offer_type, offer_rate, rate_desc


def needs_reorder(active_offers: list, target_type: str, target_rate: float,
                  target_amount: float, target_period: int,
                  current_frr: float) -> tuple:
    """
    判断是否需要撤单重挂
    返回 (need: bool, reason: str)

    跳过重挂的条件（同时满足）：
      1. 有且只有一笔挂单
      2. 订单类型相同
      3. FRRDELTAVAR：偏移值相同即可（服务端自动跟随，无需重挂）
         LIMIT：利率变化在阈值内
      4. 金额差异在 1 USD 以内
      5. 挂单天数相同
    """
    if not active_offers:
        return True, "无挂单，需要新建"

    if len(active_offers) > 1:
        return True, f"存在 {len(active_offers)} 笔挂单，合并重挂"

    offer = active_offers[0]
    # offer 结构: [ID, SYMBOL, MTS_CREATE, MTS_UPDATE, AMOUNT, AMOUNT_ORIG,
    #              TYPE, ..., RATE, PERIOD, ...]
    existing_type = offer[6]   # 订单类型字符串
    existing_rate = float(offer[14])  # 利率小数
    existing_amount = abs(float(offer[4]))  # 剩余金额
    existing_period = int(offer[15])  # 天数

    # 类型变了
    if existing_type != target_type:
        return True, f"订单类型变更 {existing_type} → {target_type}"

    # 天数变了
    if existing_period != target_period:
        return True, f"挂单天数变更 {existing_period} → {target_period}"

    # 金额差异超过 1 USD（余额有变动）
    if abs(existing_amount - target_amount) > 1.0:
        return True, f"可用金额变化 {existing_amount:.2f} → {target_amount:.2f}"

    # FRRDELTAVAR 模式：服务端自动跟随，不需要因 FRR 变动而重挂
    if target_type == "FRRDELTAVAR":
        if abs(existing_rate - (target_rate)) < 1e-9:
            return False, "FRRDELTAVAR 挂单无变化，保持现有挂单"
        else:
            return True, f"FRRDELTAVAR 偏移值变更"

    # LIMIT 模式：FRR 变动超过阈值才重挂
    rate_change_pct = abs(existing_rate - target_rate) * 100
    if rate_change_pct > FRR_CHANGE_THRESHOLD:
        return True, f"利率变动 {rate_change_pct:.4f}%/天 超过阈值 {FRR_CHANGE_THRESHOLD}%/天"

    return False, f"利率变动 {rate_change_pct:.4f}%/天 未超过阈值，保持现有挂单"


def submit_funding_offer(symbol: str, amount: float, period: int,
                         offer_type: str, offer_rate: float, rate_desc: str):
    path = "/v2/auth/w/funding/offer/submit"
    body = {
        "type": offer_type,
        "symbol": symbol,
        "amount": str(round(amount, 8)),
        "rate": str(round(offer_rate, 10)),
        "period": period,
        "flags": 0,
    }
    print(f"[下单] {rate_desc} | amount={amount:.2f} period={period}d")

    if DRY_RUN:
        print("[DRY RUN] 跳过实际下单")
        return

    headers = bfx_auth_headers(path, body)
    resp = requests.post(f"{API_BASE}{path}", headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    print(f"[结果] {result}")
    return result


def main():
    if not API_KEY or not API_SECRET:
        print("[错误] 请在青龙环境变量中设置 BFX_API_KEY 和 BFX_API_SECRET")
        return

    currency = SYMBOL.lstrip("f")

    print(f"{'='*50}")
    print(f"Bitfinex 自动放贷 | {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"币种: {SYMBOL} | FRR模式: {USE_FRR} | 天数: {PERIOD} | 预留: {RESERVE_AMOUNT}")
    print(f"{'='*50}")

    # 1. 获取 FRR
    current_frr = 0.0
    try:
        current_frr = get_frr(SYMBOL)
        frr_pct = current_frr * 100
        print(f"[FRR] 当前 FRR = {frr_pct:.6f}%/天（年化约 {frr_pct*365:.2f}%）")
    except Exception as e:
        if USE_FRR:
            print(f"[错误] 获取 FRR 失败，无法下单：{e}")
            return
        print(f"[警告] 获取 FRR 失败（不影响固定利率模式）：{e}")

    # 2. 计算目标利率
    target_type, target_rate, rate_desc = calc_target_rate(
        USE_FRR, FRR_OFFSET, FIXED_RATE, current_frr
    )

    # 3. 查询可用余额
    try:
        balance = get_wallet_balance(currency)
    except Exception as e:
        print(f"[错误] 获取余额失败：{e}")
        return

    # 4. 查询当前挂单
    try:
        active_offers = get_active_offers(SYMBOL)
        print(f"[挂单] 当前有 {len(active_offers)} 笔未成交挂单")
    except Exception as e:
        print(f"[警告] 获取挂单失败，默认重挂：{e}")
        active_offers = []

    # 计算可放贷金额
    # 如果不需要重挂，余额不含已挂出的资金，需要加回去估算
    locked_amount = sum(abs(float(o[4])) for o in active_offers)
    total_available = balance + locked_amount - RESERVE_AMOUNT
    target_amount = balance - RESERVE_AMOUNT  # 撤单后可用余额

    print(f"[余额] 钱包可用: {balance:.2f}，已挂出: {locked_amount:.2f}，预留: {RESERVE_AMOUNT:.2f}")

    # 5. 判断是否需要重挂
    need, reason = needs_reorder(
        active_offers, target_type, target_rate,
        total_available, PERIOD, current_frr
    )
    print(f"[判断] {'需要重挂' if need else '无需重挂'} — {reason}")

    if not need:
        print("[完成] 现有挂单条件未变化，跳过撤单重挂")
        return

    # 6. 撤单
    try:
        cancel_all_funding_offers(SYMBOL)
    except Exception as e:
        print(f"[警告] 取消挂单失败：{e}")

    # 等待撤单资金释放
    print("[等待] 撤单确认中，等待 5 秒...")
    time.sleep(5)

    # 重新查询余额（撤单后）
    try:
        balance = get_wallet_balance(currency)
    except Exception as e:
        print(f"[错误] 获取余额失败：{e}")
        return

    available = balance - RESERVE_AMOUNT
    print(f"[余额] 撤单后可放贷: {available:.2f} {currency}")

    if available < MIN_OFFER_AMOUNT:
        print(f"[跳过] 可放贷金额 {available:.2f} 低于最低限额 {MIN_OFFER_AMOUNT:.2f}，退出")
        return

    # 7. 提交新挂单
    try:
        submit_funding_offer(
            symbol=SYMBOL,
            amount=available,
            period=PERIOD,
            offer_type=target_type,
            offer_rate=target_rate,
            rate_desc=rate_desc,
        )
    except Exception as e:
        print(f"[错误] 下单失败：{e}")
        return

    print("[完成] 放贷挂单已提交")


if __name__ == "__main__":
    main()