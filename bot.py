import asyncio
import logging
import os
import uuid
import random
import sys  
import json
import aiohttp # 🔥 必须安装: pip install aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from sqlalchemy.future import select

# 引入数据库操作
from database import (
    init_db, AsyncSessionLocal, User, RedPacket, Deposit,
    add_balance, get_user, update_wallet_address,
    create_deposit_order, create_withdrawal_request, get_user_stats, Transaction
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
# ⚠️ 必须设置这个地址，否则无法监听
DEPOSIT_WALLET_ADDRESS = os.getenv("DEPOSIT_WALLET_ADDRESS", "") 
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

dp = Dispatcher()
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
router = Router()

# ==========================================
# 💰 核心汇率逻辑 (1 USDT = 1 USDT)
# ==========================================
def fmt_credits(db_amount):
    if db_amount is None: return "0.00"
    return f"{db_amount / 1000000:.2f}"

def parse_credits(input_str):
    try:
        val = float(input_str)
        return int(val * 1000000)
    except:
        return None

def fmt_usdt_from_credits(db_amount):
    return fmt_credits(db_amount)

# ==========================================
# 🔥 新增：异步链上监听服务 (Watcher)
# ==========================================
async def watch_deposits():
    """后台任务：每60秒检查一次链上充值"""
    if not DEPOSIT_WALLET_ADDRESS or len(DEPOSIT_WALLET_ADDRESS) < 30:
        logger.warning("⚠️ 未配置收款地址，充值监听未启动")
        return

    # TronGrid API (建议去申请一个 API Key 填入 header，否则有限制)
    # 如果有 API Key: headers = {"TRON-PRO-API-KEY": "你的KEY"}
    url = f"https://api.trongrid.io/v1/accounts/{DEPOSIT_WALLET_ADDRESS}/transactions/trc20"
    params = {
        "limit": 20,
        "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", # USDT合约
        "only_confirmed": "true"
    }

    logger.info("📡 充值监听服务已启动...")
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # 🔥 关键修改：使用 await 异步请求，不会阻塞主线程！
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        transactions = data.get("data", [])
                        
                        if transactions:
                            async with AsyncSessionLocal() as db_session:
                                await process_chain_txs(db_session, transactions)
                    else:
                        logger.error(f"TronGrid API Error: {resp.status}")

            except Exception as e:
                logger.error(f"Watcher Error: {e}")
            
            # 🔥 关键修改：每 60 秒检查一次，降低 CPU 负载
            await asyncio.sleep(60) 

async def process_chain_txs(session, txs):
    """处理链上交易数据"""
    for tx in txs:
        tx_hash = tx.get("transaction_id")
        value_str = tx.get("value")
        to_address = tx.get("to")
        
        # 1. 基本过滤
        if to_address != DEPOSIT_WALLET_ADDRESS: continue
        
        # 2. 查重：如果这个 Hash 已经处理过，跳过
        stmt = select(Deposit).where(Deposit.tx_hash == tx_hash)
        res = await session.execute(stmt)
        if res.scalars().first(): continue

        # 3. 匹配金额
        # 链上金额是微单位 (6位小数)，数据库也是微单位，可以直接比对
        # 但我们订单有随机小数，所以需要用“范围匹配”或者“精确匹配”
        try:
            amount_int = int(value_str)
        except: continue

        # 查找等待中的订单：金额完全一致的
        # 注意：这里假设用户转账金额与订单金额必须 100% 一致 (包含随机小数)
        stmt_order = select(Deposit).where(
            Deposit.status == "pending",
            Deposit.random_amount == amount_int # 匹配那个带小数的金额
        )
        res_order = await session.execute(stmt_order)
        order = res_order.scalars().first()

        if order:
            # ✅ 匹配成功：上分
            order.status = "success"
            order.tx_hash = tx_hash
            
            # 给用户加余额
            # 注意：这里要重新获取用户 Session 进行更新，或者直接调用 add_balance
            # 为了安全，我们简单地调用 add_balance (虽然它会开新 Session，稍微低效但安全)
            await add_balance(order.user_id, order.amount, "deposit", f"充值:{tx_hash[:6]}")
            
            # 通知用户
            try:
                await bot.send_message(
                    order.user_id, 
                    f"✅ <b>充值到账！</b>\n💎 USDT: {fmt_credits(order.amount)}"
                )
                if ADMIN_ID:
                    await bot.send_message(ADMIN_ID, f"💰 用户 {order.user_id} 充值 {fmt_credits(order.amount)} USDT")
            except: pass
            
            # 提交订单状态更新
            await session.commit()
            logger.info(f"✅ 处理充值: {tx_hash} - 用户 {order.user_id}")


# ==========================================
# 状态机与键盘
# ==========================================
class BotStates(StatesGroup):
    create_packet_amount = State()
    create_packet_count = State()
    create_packet_mine = State()
    deposit_amount = State()
    withdraw_amount = State()
    bind_wallet_address = State()

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧧 发 USDT 红包", callback_data="create_packet"), InlineKeyboardButton(text="💎 充值 USDT", callback_data="deposit")],
        [InlineKeyboardButton(text="💸 USDT 提现", callback_data="withdraw"), InlineKeyboardButton(text="👤 个人中心", callback_data="my_info")],
        [InlineKeyboardButton(text="🔗 绑定钱包", callback_data="bind_wallet")]
    ])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data="back_to_main")]])

# === 基础指令 ===
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.tg_id == message.from_user.id)
        result = await session.execute(stmt)
        user = result.scalars().first()

        if not user:
            bonus_credits = 0.5 
            bonus_db = parse_credits(bonus_credits)
            user = User(tg_id=message.from_user.id, username=message.from_user.username, balance=bonus_db)
            session.add(user)
            session.add(Transaction(user_id=message.from_user.id, amount=bonus_db, type="system_bonus", note="新人体验金"))
            await session.commit()
            await message.answer(f"🎁 <b>欢迎加入！</b>\n系统已赠送 <b>{bonus_credits} USDT</b> 体验金！\n(⚠️体验金需充值激活后方可提现)")
        else:
            if user.username != message.from_user.username:
                user.username = message.from_user.username
                await session.commit()
    
    await message.answer(
        f"👋 欢迎 <b>{user.username}</b>\n"
        f"🆔 ID: <code>{user.tg_id}</code>\n"
        f"💰 余额: <b>{fmt_credits(user.balance)} USDT</b>\n\n"
        f"👇 开始你的游戏:",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data == "back_to_main")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    await callback.message.edit_text(
        f"👋 欢迎 <b>{user.username}</b>\n💰 余额: <b>{fmt_credits(user.balance)} USDT</b>",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data == "my_info")
async def my_info_callback(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    await callback.message.edit_text(
        f"<b>👤 个人信息</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user.tg_id}</code>\n"
        f"💰 <b>余额:</b> {fmt_credits(user.balance)} USDT\n\n"
        f"🔗 <b>钱包:</b>\n<code>{user.wallet_address or '未绑定'}</code>",
        reply_markup=back_keyboard(),
    )

@router.callback_query(F.data == "deposit")
async def deposit_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💎 <b>充值 USDT</b>\n\n"
        "汇率: 1 USDT = 1 USDT\n"
        "请输入您要充值的 <b>USDT 金额</b> (例如 10):", 
        reply_markup=back_keyboard()
    )
    await state.set_state(BotStates.deposit_amount)

@router.message(BotStates.deposit_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        usdt_val = int(message.text)
        if usdt_val <= 0: raise ValueError
    except: return await message.answer("❌ 请输入整数 USDT 金额")
    
    amount_db = usdt_val * 1000000 
    final_amount_db = amount_db + random.randint(100, 5000) 
    
    pay_usdt_str = f"{final_amount_db / 1000000:.6f}"
    expected_credits = fmt_credits(amount_db)

    order = await create_deposit_order(message.from_user.id, amount_db, final_amount_db)
    
    qr_text = (
        f"<b>💎 充值订单 #{order.id}</b>\n"
        f"预计到账: <b>{expected_credits} USDT</b>\n\n"
        f"⚠️ <b>请务必精确转账:</b>\n"
        f"👉 <code>{pay_usdt_str}</code> <b>USDT (TRC20)</b>\n\n"
        f"收款地址:\n<code>{DEPOSIT_WALLET_ADDRESS}</code>\n"
        f"支付后请点击下方按钮。"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ 我已付款", callback_data=f"paid:{order.id}")],[InlineKeyboardButton(text="⬅️ 返回", callback_data="back_to_main")]])
    await message.answer(qr_text, reply_markup=kb)
    await state.clear()

@router.callback_query(F.data.startswith("paid:"))
async def paid_callback(callback: CallbackQuery):
    await callback.answer("✅ 系统将在1分钟内自动检测到账", show_alert=True)

# === 发红包 (积分版 + 扫雷) ===

@router.callback_query(F.data == "create_packet")
async def create_packet_callback(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private": return await callback.answer("请私聊设置", show_alert=True)
    await callback.message.edit_text("🧧 <b>发送 USDT 红包</b>\n\n请输入总金额 (例如 1.0):", reply_markup=back_keyboard())
    await state.set_state(BotStates.create_packet_amount)

@router.message(BotStates.create_packet_amount)
async def process_packet_amount(message: Message, state: FSMContext):
    amount_db = parse_credits(message.text)
    if not amount_db or amount_db < 100000:
        return await message.answer("❌ 至少发送 0.1 USDT")
    
    await state.update_data(amount_db=amount_db)
    await message.answer("🔢 请输入红包个数:")
    await state.set_state(BotStates.create_packet_count)

@router.message(BotStates.create_packet_count)
async def process_packet_count(message: Message, state: FSMContext):
    try: count = int(message.text)
    except: return await message.answer("❌ 必须是整数")
    if count < 2: return await message.answer("❌ 扫雷包至少发给 2 个人")

    await state.update_data(count=count)
    await message.answer("💣 <b>请输入雷号 (0-9):</b>\n发送 -1 代表普通福利包(无雷)", parse_mode="HTML")
    await state.set_state(BotStates.create_packet_mine)

@router.message(BotStates.create_packet_mine)
async def process_packet_mine(message: Message, state: FSMContext):
    try: mine = int(message.text)
    except: mine = -1
    if mine > 9 or mine < -1: return await message.answer("❌ 雷号 0-9")

    data = await state.get_data()
    amount_db = data['amount_db']
    count = data['count']
    user_id = message.from_user.id

    # === 抽水 (5%) ===
    fee_rate = 0.05
    packet_total_db = int(amount_db * (1 - fee_rate))
    
    success, msg = await add_balance(user_id, -amount_db, "send_packet", f"雷{mine}")
    if not success:
        await message.answer(f"❌ {msg}")
        return

    packet_id = str(uuid.uuid4())[:8]
    async with AsyncSessionLocal() as session:
        async with session.begin():
            packet = RedPacket(
                id=packet_id, sender_id=user_id, sender_name=message.from_user.first_name,
                total_amount=packet_total_db, total_count=count,
                remaining_amount=packet_total_db, remaining_count=count,
                status="active", claimed_users="[]", mine_number=mine
            )
            session.add(packet)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 发到群里", switch_inline_query=packet_id)]])
    mine_info = f"💣 <b>雷号: {mine}</b>" if mine >= 0 else "🎉 <b>福利包</b>"
    
    await message.answer(
        f"✅ <b>准备就绪</b>\n"
        f"💰 总额: {fmt_credits(amount_db)} USDT\n"
        f"📦 数量: {count} 个\n"
        f"{mine_info}\n"
        f"🧾 服务费: 5%\n"
        f"👇 点击转发:", reply_markup=kb, parse_mode="HTML"
    )
    await state.clear()

# === 内联显示 ===
@router.inline_query()
async def inline_redpacket_handler(inline_query: InlineQuery):
    packet_id = inline_query.query.strip()
    if not packet_id: return
    
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(RedPacket).where(RedPacket.id == packet_id))
        packet = res.scalars().first()
    
    if not packet or packet.status != 'active': return

    mine_text = f"💣 <b>雷号: {packet.mine_number}</b>" if packet.mine_number >= 0 else "🎉 <b>福利包 (无雷)</b>"
    
    result_content = InputTextMessageContent(
        message_text=(
            f"🧧 <b>{packet.sender_name} 的 USDT 红包</b>\n"
            f"💰 <b>{fmt_credits(packet.total_amount)} USDT</b>\n"
            f"📦 数量: {packet.total_count} 个\n"
            f"{mine_text}\n"
            f"👇 <i>拼手气，踩雷赔付 1.5 倍！</i>"
        ), parse_mode="HTML"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧧 抢 USDT", callback_data=f"grab:{packet_id}")]])
    
    item = InlineQueryResultArticle(
        id=packet_id, title=f"发 {fmt_credits(packet.total_amount)} USDT 红包",
        description=f"雷号: {packet.mine_number if packet.mine_number>=0 else '无'} | 点击发送",
        input_message_content=result_content, reply_markup=kb,
        thumbnail_url="https://img.icons8.com/color/96/money-bag.png"
    )
    await inline_query.answer([item], cache_time=1, is_personal=True)

# === 抢包逻辑 ===
@router.callback_query(F.data.startswith("grab:"))
async def grab_packet(callback: CallbackQuery):
    packet_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await session.execute(select(RedPacket).where(RedPacket.id == packet_id).with_for_update())
            packet = res.scalars().first()
            
            if not packet or packet.status != 'active' or packet.remaining_count <= 0:
                return await callback.answer("😔 抢光了！", show_alert=True)
            
            claimed = json.loads(packet.claimed_users)
            if user_id in claimed: return await callback.answer("🛑 只能抢一次", show_alert=True)
            
            u_res = await session.execute(select(User).where(User.tg_id == user_id))
            claimer = u_res.scalars().first()
            if not claimer: 
                claimer = User(tg_id=user_id, balance=0)
                session.add(claimer)
            
            # 风控：扫雷包要求余额 > 5 USDT
            if packet.mine_number >= 0 and claimer.balance < 5000000:
                return await callback.answer("🚫 余额不足 5 USDT，无法参与扫雷！请先充值。", show_alert=True)

            if packet.remaining_count == 1: grab_db = packet.remaining_amount
            else:
                avg = packet.remaining_amount / packet.remaining_count
                grab_db = random.randint(1, int(avg * 2))
                if grab_db >= packet.remaining_amount: grab_db = packet.remaining_amount - 1
            
            packet.remaining_amount -= grab_db
            packet.remaining_count -= 1
            claimed.append(user_id)
            packet.claimed_users = json.dumps(claimed)
            if packet.remaining_count == 0: packet.status = 'finished'
            
            claimer.balance += grab_db
            session.add(Transaction(user_id=user_id, amount=grab_db, type="grab", note=f"P:{packet_id}"))
            
            alert_text = f"🎉 抢到 {fmt_credits(grab_db)} USDT"
            
            if packet.mine_number >= 0:
                last_digit = int((grab_db // 100) % 10)
                if last_digit == packet.mine_number:
                    boom_rate = 1.5
                    penalty_db = int(packet.total_amount * boom_rate)
                    
                    claimer.balance -= penalty_db
                    session.add(Transaction(user_id=user_id, amount=-penalty_db, type="boom_penalty", note=f"中雷:{packet_id}"))
                    
                    s_res = await session.execute(select(User).where(User.tg_id == packet.sender_id))
                    sender = s_res.scalars().first()
                    sender.balance += penalty_db
                    session.add(Transaction(user_id=packet.sender_id, amount=penalty_db, type="boom_income", note=f"中雷赔付:{packet_id}"))
                    
                    alert_text += f"\n💥 踩雷 (尾数{last_digit})！\n💸 自动赔付 {fmt_credits(penalty_db)} USDT"
                else:
                    alert_text += f"\n🛡️ 安全 (尾数{last_digit})"

    await callback.answer(alert_text, show_alert=True)

# === 启动 ===
async def main() -> None:
    await init_db()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # 🔥 关键修改：将监听任务作为后台任务启动，不阻塞主线程
    asyncio.create_task(watch_deposits())
    
    print("🤖 USDT 扫雷机器人启动...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
