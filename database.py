import os
import asyncio
from datetime import datetime
from sqlalchemy import Column, Integer, String, BigInteger, DateTime, Text, select, update, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

# === 连接配置 (保持不变) ===
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # ⚠️ 本地测试如果没有配置环境变量，可以使用 SQLite 临时顶替，或者报错
    # raise ValueError("❌ 未设置 DATABASE_URL 环境变量！")
    print("⚠️ 未检测到 DATABASE_URL，使用本地 SQLite 模式 (仅供测试)")
    DATABASE_URL = "sqlite+aiosqlite:///./bot_database.db"

# 适配 Supabase/PostgreSQL 连接串
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if "postgresql" in DATABASE_URL and "+asyncpg" not in DATABASE_URL:
     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

print(f"---------> 🟢 正在连接数据库...")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# === 数据表模型 ===

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    tg_id = Column(BigInteger, unique=True, index=True)
    username = Column(String, nullable=True)
    # 💰 注意：这里存的是微单位 (1,000,000 微单位 = 1 USDT)
    balance = Column(BigInteger, default=0) 
    wallet_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

class Deposit(Base):
    __tablename__ = "deposits"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(BigInteger)
    random_amount = Column(BigInteger)
    status = Column(String, default="pending") 
    tx_hash = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.now)

class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(BigInteger)
    wallet_address = Column(String)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.now)

class RedPacket(Base):
    __tablename__ = "red_packets"
    id = Column(String, primary_key=True)
    sender_id = Column(BigInteger, index=True)
    sender_name = Column(String)
    total_amount = Column(BigInteger)
    total_count = Column(Integer)
    remaining_amount = Column(BigInteger)
    remaining_count = Column(Integer)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.now)
    claimed_users = Column(Text, default="[]")
    # ✨ 新增：雷号字段 (-1为无雷，0-9为雷号)
    mine_number = Column(Integer, default=-1)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(BigInteger)
    type = Column(String)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

# === 操作工具 ===

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_user(session, tg_id: int, username: str = None):
    result = await session.execute(select(User).filter(User.tg_id == tg_id))
    user = result.scalars().first()
    if not user:
        user = User(tg_id=tg_id, username=username, balance=0, created_at=datetime.now())
        session.add(user)
        # 注意：这里去掉了 commit，让外层控制事务提交，减少锁冲突
    elif username and user.username != username:
        user.username = username
        # user.username 更新也不急着 commit
    return user

async def add_balance(tg_id: int, amount: int, tx_type: str, note: str = ""):
    """
    🔥 核心记账函数 (增加了悲观锁)
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # 🔥 with_for_update()：锁住这一行，防止高并发时两个人同时抢导致余额错误
            stmt = select(User).filter(User.tg_id == tg_id).with_for_update()
            result = await session.execute(stmt)
            user = result.scalars().first()
            
            if not user: 
                # 如果用户不存在，尝试创建一个 (针对极端情况)
                user = User(tg_id=tg_id, balance=0)
                session.add(user)
            
            # 检查余额 (如果是扣款)
            if amount < 0 and user.balance + amount < 0: 
                return False, "余额不足"
            
            user.balance += amount
            
            # 记录流水
            tx = Transaction(
                user_id=tg_id, amount=amount, type=tx_type, 
                note=note, created_at=datetime.now()
            )
            session.add(tx)
        
        return True, "成功"

async def get_user_transactions(user_id: int, limit: int = 50):
    async with AsyncSessionLocal() as session:
        stmt = select(Transaction).filter(Transaction.user_id == user_id).order_by(Transaction.id.desc()).limit(limit)
        result = await session.execute(stmt)
        return result.scalars().all()

async def update_wallet_address(tg_id: int, address: str):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = update(User).where(User.tg_id == tg_id).values(wallet_address=address)
            await session.execute(stmt)
    return True

async def create_deposit_order(user_id: int, amount: int, random_amount: int):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            order = Deposit(user_id=user_id, amount=amount, random_amount=random_amount, created_at=datetime.now())
            session.add(order)
            # flush 保证能拿到 ID
            await session.flush()
            # 重新查询一遍以返回完整对象
            result = await session.execute(select(Deposit).where(Deposit.id == order.id))
            return result.scalars().first()

async def create_withdrawal_request(user_id: int, amount: int, wallet_address: str):
    """
    🔥 修正：提现申请必须立刻扣除余额，防止重复提现
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # 1. 锁住用户并查余额
            stmt = select(User).filter(User.tg_id == user_id).with_for_update()
            result = await session.execute(stmt)
            user = result.scalars().first()
            
            if not user: return None, "用户不存在"
            if user.balance < amount: return None, "余额不足"
            
            # 2. 🔥 关键：立刻扣除余额 (冻结)
            user.balance -= amount
            
            # 3. 记录扣款流水
            tx = Transaction(
                user_id=user_id, amount=-amount, 
                type="withdraw_freeze", note="提现冻结"
            )
            session.add(tx)
            
            # 4. 创建提现单
            req = Withdrawal(
                user_id=user_id, amount=amount, 
                wallet_address=wallet_address, created_at=datetime.now()
            )
            session.add(req)
            await session.flush()
            
            # 返回提现单对象
            return req, "成功"

async def get_user_stats(user_id: int):
    async with AsyncSessionLocal() as session:
        sent = await session.execute(select(func.sum(Transaction.amount)).where(Transaction.user_id == user_id, Transaction.type.like('%send_packet%')))
        # 注意：这里取绝对值，因为发包流水是负数
        total_sent = abs(sent.scalar() or 0)
        
        grabbed = await session.execute(select(func.sum(Transaction.amount)).where(Transaction.user_id == user_id, Transaction.type.like('%grab_packet%')))
        total_grabbed = grabbed.scalar() or 0
        
    return {"total_sent": total_sent, "total_grabbed": total_grabbed}
