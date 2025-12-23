import os
import asyncio
from datetime import datetime
from sqlalchemy import Column, Integer, String, BigInteger, DateTime, Text, select, update, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

# === 关键修改：读取环境变量中的数据库地址 ===
# 如果没有设置环境变量(本地测试)，则报错提醒
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("❌ 未设置 DATABASE_URL 环境变量！")

# Supabase 的连接串如果是 postgres:// 开头，SQLAlchemy 需要 postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 必须指定驱动为 asyncpg
if "postgresql" in DATABASE_URL and "+asyncpg" not in DATABASE_URL:
     DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

print(f"---------> 🟢 正在连接云端数据库...")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# === 数据表模型 (保持不变) ===

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    tg_id = Column(BigInteger, unique=True, index=True)
    username = Column(String, nullable=True)
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

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, index=True)
    amount = Column(BigInteger)
    type = Column(String)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

# === 操作工具 (保持不变) ===

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_user(session, tg_id: int, username: str = None):
    result = await session.execute(select(User).filter(User.tg_id == tg_id))
    user = result.scalars().first()
    if not user:
        user = User(tg_id=tg_id, username=username, balance=0, created_at=datetime.now())
        session.add(user)
        await session.commit()
    elif username and user.username != username:
        user.username = username
        await session.commit()
    return user

async def add_balance(tg_id: int, amount: int, tx_type: str, note: str = ""):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(select(User).filter(User.tg_id == tg_id))
            user = result.scalars().first()
            if not user: return False, "用户不存在"
            if amount < 0 and user.balance < abs(amount): return False, "余额不足"
            user.balance += amount
            tx = Transaction(user_id=tg_id, amount=amount, type=tx_type, note=note, created_at=datetime.now())
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
        result = await session.execute(select(Deposit).where(Deposit.id == order.id))
        return result.scalars().first()

async def create_withdrawal_request(user_id: int, amount: int, wallet_address: str):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            user_result = await session.execute(select(User).filter(User.tg_id == user_id))
            user = user_result.scalars().first()
            if not user: return None, "用户不存在"
            if user.balance < amount: return None, "余额不足"
            req = Withdrawal(user_id=user_id, amount=amount, wallet_address=wallet_address, created_at=datetime.now())
            session.add(req)
            await session.commit()
            return req, "成功"

async def get_user_stats(user_id: int):
    async with AsyncSessionLocal() as session:
        sent = await session.execute(select(func.sum(Transaction.amount)).where(Transaction.user_id == user_id, Transaction.type == 'send_packet'))
        total_sent = sent.scalar() or 0
        grabbed = await session.execute(select(func.sum(Transaction.amount)).where(Transaction.user_id == user_id, Transaction.type == 'grab_packet'))
        total_grabbed = grabbed.scalar() or 0
    return {"total_sent": abs(total_sent), "total_grabbed": total_grabbed}