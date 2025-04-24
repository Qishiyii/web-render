# -*- coding: utf-8 -*-
"""
完整链上交易监控系统（监听 + 风控打分 + 告警 + FastAPI 接口 + 日志接口）
适配 Render 云部署（支持动态风险分阈值）
"""
import asyncio
import json
from web3 import Web3
from datetime import datetime
import redis.asyncio as aioredis
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import threading
import nest_asyncio
import os

# Render 会通过环境变量提供 Redis 地址（或用默认本地）
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
PORT = int(os.getenv("PORT", 8080))

# 初始化 Web3 和 Redis
w3 = Web3(Web3.LegacyWebSocketProvider(os.getenv("ALCHEMY_WS", "wss://eth-mainnet.g.alchemy.com/v2/X4bmm7I5BQeOpOFIKhVDPQzfWcgOoJ18")))
redis_conn = aioredis.from_url(REDIS_URL, decode_responses=True)

# 初始化 FastAPI 接口服务
app = FastAPI()
latest_alerts = []  # 告警缓存队列（最多保留10条）
transaction_logs = []  # 原始交易日志（最多保留20条）

# 日志记录函数（带缓存）
def log_message(msg):
    print(msg)
    transaction_logs.insert(0, msg)
    if len(transaction_logs) > 20:
        transaction_logs.pop()

# 风控评分逻辑（可扩展为机器学习模型）
def simple_risk_score(tx):
    score = 0
    if tx['value'] > 100:
        score += 0.5
    if tx['gas'] > 50:
        score += 0.3
    if tx['from'].lower().startswith("0xabc"):
        score += 0.3
    return min(score, 1.0)

# 异步监听链上交易并写入 Redis 队列
async def listen_pending_tx():
    pending_filter = w3.eth.filter("pending")
    log_message("✅ 启动监听中...")
    while True:
        tx_hashes = pending_filter.get_new_entries()
        for tx_hash in tx_hashes:
            try:
                tx = w3.eth.get_transaction(tx_hash)
                tx_data = {
                    "hash": tx["hash"].hex(),
                    "from": tx["from"],
                    "to": tx["to"],
                    "value": tx["value"] / 1e18,
                    "gas": tx["gasPrice"] / 1e9,
                    "timestamp": datetime.utcnow().isoformat()
                }
                await redis_conn.lpush("tx_queue", json.dumps(tx_data))
                log_message(f"📥 入队交易：{tx_data['hash']}")
            except Exception:
                continue
        await asyncio.sleep(1)

# 异步消费 Redis 队列并做风险评分与告警缓存
async def consume_tx():
    global latest_alerts
    log_message("✅ 启动消费模块...")
    while True:
        tx_json = await redis_conn.rpop("tx_queue")
        if tx_json:
            tx = json.loads(tx_json)
            score = simple_risk_score(tx)
            log_message(f"📤 消费交易：{tx['hash']} | 风险分：{score}")
            if score >= 0.3:  # 存入缓存但由 /alerts 再按 threshold 过滤
                alert = {
                    "tx_hash": tx["hash"],
                    "risk_score": score,
                    "from": tx["from"],
                    "to": tx["to"],
                    "timestamp": tx["timestamp"]
                }
                latest_alerts.insert(0, alert)
                latest_alerts = latest_alerts[:10]
        else:
            await asyncio.sleep(0.5)

# 提供给 Coze Agent 的接口（动态风险阈值）
@app.get("/alerts")
async def get_alerts(threshold: float = Query(0.8, ge=0.0, le=1.0)):
    filtered_alerts = [a for a in latest_alerts if a["risk_score"] >= threshold]
    return JSONResponse(content={"threshold": threshold, "alerts": filtered_alerts})

# 提供原始交易日志接口（供 Coze 查询输出）
@app.get("/logs")
async def get_logs():
    return JSONResponse(content={"logs": transaction_logs})

# 启动监听逻辑并集成 FastAPI
@app.on_event("startup")
async def startup_event():
    nest_asyncio.apply()
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()

# 异步主程序并发运行监听与消费
async def main():
    await asyncio.gather(
        listen_pending_tx(),
        consume_tx()
    )
