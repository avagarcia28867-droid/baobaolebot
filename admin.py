import secrets
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from pydantic import BaseModel
from datetime import datetime   

# === 1. 引入数据库 ===
# 必须导入 Transaction 才能查流水
from database import (
    AsyncSessionLocal, User, Deposit, Withdrawal, Transaction,
    add_balance 
)

# === 2. 安全认证配置 ===
security = HTTPBasic()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    is_user_ok = secrets.compare_digest(credentials.username, "admin")
    is_pass_ok = secrets.compare_digest(credentials.password, "9688996889")
    
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized", headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# === 3. 初始化 App ===
app = FastAPI(dependencies=[Depends(verify_credentials)])
templates = Jinja2Templates(directory="templates")

# === Pydantic 模型 ===
class DepositAction(BaseModel):
    action: str # approve, reject

class WithdrawalAction(BaseModel):
    action: str 

async def get_db_session():
    async with AsyncSessionLocal() as session:
        yield session

# === 页面路由 ===
@app.get("/admin")
async def admin_panel(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# === API 接口 ===

@app.get("/api/users")
async def get_all_users(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(User).order_by(User.id.desc()))
    # 格式化数据
    return [{"tg_id": u.tg_id, "username": u.username, "balance": u.balance / 1000000, "wallet_address": u.wallet_address} for u in result.scalars().all()]

@app.get("/api/deposits")
async def get_all_deposits(session: AsyncSession = Depends(get_db_session)):
    # 按ID倒序 (最新在最前)
    result = await session.execute(select(Deposit).order_by(Deposit.id.desc()))
    data = []
    for d in result.scalars().all():
        data.append({
            "id": d.id,
            "user_id": d.user_id,
            "real_pay": d.random_amount / 1000000, 
            "status": d.status,
            # 格式化时间，如果数据库里没有时间则显示 -
            "created_at": d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "-"
        })
    return data

@app.get("/api/withdrawals")
async def get_all_withdrawals(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Withdrawal).order_by(Withdrawal.id.desc()))
    return [{"id": w.id, "user_id": w.user_id, "amount": w.amount / 1000000, "wallet_address": w.wallet_address, "status": w.status} for w in result.scalars().all()]

# 👇 【新增核心功能】获取指定用户的详细流水
@app.get("/api/transactions/{user_id}")
async def get_user_transactions_api(user_id: int, session: AsyncSession = Depends(get_db_session)):
    # 查询最近 50 条流水
    result = await session.execute(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.id.desc()).limit(50))
    data = []
    for tx in result.scalars().all():
        data.append({
            "id": tx.id,
            "type": tx.type,
            "amount": tx.amount / 1000000, 
            "note": tx.note,
            "time": tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "-"
        })
    return data

# === 操作 API ===

# 👇 【修改】支持批准(approve) 和 拒绝(reject)
@app.post("/api/handle_deposit/{order_id}")
async def handle_deposit(order_id: int, payload: DepositAction, session: AsyncSession = Depends(get_db_session)):
    async with session.begin():
        order = (await session.execute(select(Deposit).where(Deposit.id == order_id))).scalars().first()
        if not order: raise HTTPException(404, "订单不存在")
        
        if order.status != 'pending':
            raise HTTPException(400, "只能处理 pending 状态的订单")
        
        if payload.action == 'approve':
            # 批准：加余额，并自动记流水 (使用 add_balance)
            success, msg = await add_balance(order.user_id, order.random_amount, "deposit_manual", f"Admin批准:Order:{order.id}")
            if not success: raise HTTPException(500, f"加款失败: {msg}")
            order.status = 'completed'
            return {"message": "✅ 已批准并加款"}
            
        elif payload.action == 'reject':
            # 拒绝：只改状态，不加款
            order.status = 'rejected'
            return {"message": "🚫 订单已拒绝"}
        
    return {"status": "error"}

@app.post("/api/handle_withdrawal/{request_id}")
async def handle_withdrawal(request_id: int, payload: WithdrawalAction, session: AsyncSession = Depends(get_db_session)):
    async with session.begin():
        req = (await session.execute(select(Withdrawal).where(Withdrawal.id == request_id))).scalars().first()
        if not req or req.status != 'pending': raise HTTPException(400, "无效请求")

        if payload.action == "approve":
            # 批准提现：扣款 (使用 add_balance 处理扣款和流水)
            success, msg = await add_balance(req.user_id, -req.amount, "withdraw_approved", f"提现ID:{req.id}")
            if not success: raise HTTPException(400, f"失败: {msg}")
            req.status = 'approved'
            
        elif payload.action == "reject":
            # 拒绝提现：如果不涉及退款逻辑，直接改状态
            req.status = 'rejected'
            
    return {"message": "操作成功"}

if __name__ == "__main__":
    import uvicorn
    # === 启动提示 ===
    print("\n" + "="*50)
    print("🚀 后台服务已启动！")
    print("👉 管理入口: http://127.0.0.1:8000/admin")
    print(f"🔒 账号: admin")
    print(f"🔑 密码: 9688996889")
    print("="*50 + "\n")
    

    uvicorn.run("admin:app", host="0.0.0.0", port=8000, reload=True)
