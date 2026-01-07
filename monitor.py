import asyncio
import httpx
import logging
from datetime import datetime, timedelta
from sqlalchemy.future import select
from database import AsyncSessionLocal, Deposit, RedPacket, User, init_db

# === 配置 ===
WATCH_ADDRESS = "TEhcSVUBxrXmAwQKKhruae1PLE8S8Ja7dG" 
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_API_URL = f"https://api.trongrid.io/v1/accounts/{WATCH_ADDRESS}/transactions/trc20"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def auto_refund_redpackets():
    """🔥 12小时自动退款逻辑"""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            limit = datetime.now() - timedelta(hours=12)
            # 查过期且没抢完的
            stmt = select(RedPacket).where(RedPacket.status == 'active', RedPacket.created_at < limit)
            expired = (await session.execute(stmt)).scalars().all()
            
            for p in expired:
                if p.remaining_amount > 0:
                    logger.info(f"🔙 退款: 红包 {p.id} 退回 {p.remaining_amount} 微元")
                    # 退款
                    u = (await session.execute(select(User).where(User.tg_id == p.sender_id))).scalars().first()
                    if u:
                        u.balance += p.remaining_amount
                        from database import Transaction
                        session.add(Transaction(user_id=u.tg_id, amount=p.remaining_amount, type="refund", note=f"红包过期:{p.id}"))
                p.status = 'refunded'

async def auto_reject_expired_orders():
    """15分钟充值订单过期"""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            limit = datetime.now() - timedelta(minutes=15)
            stmt = select(Deposit).where(Deposit.status == 'pending', Deposit.created_at < limit)
            orders = (await session.execute(stmt)).scalars().all()
            for o in orders:
                o.status = 'expired'

async def check_transactions():
    """查账"""
    logger.info("📡 扫描链上交易...")
    async with httpx.AsyncClient() as client:
        try:
            params = {"contract_address": USDT_CONTRACT, "only_confirmed": "true", "limit": 20}
            resp = await client.get(TRON_API_URL, params=params, timeout=10)
            data = resp.json()
            if not data.get("success", False): return

            for tx in data.get("data", []):
                if tx['to'] != WATCH_ADDRESS: continue
                await process_deposit(tx['transaction_id'], int(tx['value']))
        except Exception as e:
            logger.error(f"监控错误: {e}")

async def process_deposit(tx_id, amount):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # 查重
            if (await session.execute(select(Deposit).where(Deposit.tx_hash == tx_id))).scalars().first(): return
            
            # 匹配
            stmt = select(Deposit).where(Deposit.random_amount == amount, Deposit.status == 'pending').order_by(Deposit.id.desc())
            order = (await session.execute(stmt)).scalars().first()
            
            if order:
                logger.info(f"✅ 到账匹配: {amount/1000000} U")
                order.status = 'completed'
                order.tx_hash = tx_id
                
                u = (await session.execute(select(User).where(User.tg_id == order.user_id))).scalars().first()
                if u:
                    u.balance += amount
                    from database import Transaction
                    session.add(Transaction(user_id=u.tg_id, amount=amount, type="deposit_auto", note=f"充值:{order.id}"))

async def main():
    await init_db()
    logger.info("🚀 监控启动: [自动到账] + [15分钟订单过期] + [12小时红包退款]")
    while True:
        await check_transactions()
        await auto_reject_expired_orders()
        await auto_refund_redpackets()
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())