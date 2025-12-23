# bot.py
import asyncio
import logging
import os
import uuid
import random
import sys  
import json
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

from database import (
    init_db, AsyncSessionLocal, User, RedPacket,
    add_balance, get_user, update_wallet_address,
    create_deposit_order, create_withdrawal_request, get_user_stats, Transaction
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DEPOSIT_WALLET_ADDRESS = os.getenv("DEPOSIT_WALLET_ADDRESS", "T_Fake_Address_Check_Env")
DEPOSIT_QR_CODE_FILE_ID = os.getenv("DEPOSIT_QR_CODE_FILE_ID", "")

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

dp = Dispatcher()
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
router = Router()

# ==========================================
# 💰 核心汇率逻辑 (1 USDT = 100 积分)
# ==========================================
# 数据库存 1,000,000 微单位 = 1 USDT
# 积分显示 100.00 积分 = 1 USDT
# 因此：1 积分 = 10,000 微单位

def fmt_credits(db_amount):
    """数据库微单位 -> 显示积分"""
    if db_amount is None: return "0.00"
    return f"{db_amount / 10000:.2f}"

def parse_credits(input_str):
    """输入积分 -> 数据库微单位"""
    try:
        val = float(input_str)
        return int(val * 10000)
    except:
        return None

def fmt_usdt_from_credits(db_amount):
    """数据库微单位 -> 估算USDT价值"""
    return f"{db_amount / 1000000:.2f}"

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
        [InlineKeyboardButton(text="🧧 发积分红包", callback_data="create_packet"), InlineKeyboardButton(text="💎 充值积分", callback_data="deposit")],
        [InlineKeyboardButton(text="💸 积分提现", callback_data="withdraw"), InlineKeyboardButton(text="👤 个人中心", callback_data="my_info")],
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

        # === 🎁 新人体验金风控 ===
        if not user:
            # 赠送 50 积分 (= 0.5 USDT = 500,000 微单位)
            bonus_credits = 50 
            bonus_db = parse_credits(bonus_credits)
            
            user = User(tg_id=message.from_user.id, username=message.from_user.username, balance=bonus_db)
            session.add(user)
            # 必须记录这是系统赠送，方便以后查账
            session.add(Transaction(user_id=message.from_user.id, amount=bonus_db, type="system_bonus", note="新人体验金"))
            await session.commit()
            
            await message.answer(f"🎁 <b>欢迎加入！</b>\n系统已赠送 <b>{bonus_credits} 积分</b> 体验金！\n(⚠️体验金需充值激活后方可提现)")
        else:
            if user.username != message.from_user.username:
                user.username = message.from_user.username
                await session.commit()
    
    await message.answer(
        f"👋 欢迎 <b>{user.username}</b>\n"
        f"🆔 ID: <code>{user.tg_id}</code>\n"
        f"💰 积分余额: <b>{fmt_credits(user.balance)}</b>\n"
        f"(≈ {fmt_usdt_from_credits(user.balance)} USDT)\n\n"
        f"👇 开始你的游戏:",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data == "back_to_main")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    await callback.message.edit_text(
        f"👋 欢迎 <b>{user.username}</b>\n💰 积分: <b>{fmt_credits(user.balance)}</b>",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data == "my_info")
async def my_info_callback(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    await callback.message.edit_text(
        f"<b>👤 个人信息</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user.tg_id}</code>\n"
        f"💰 <b>积分:</b> {fmt_credits(user.balance)}\n"
        f"💵 <b>估值:</b> {fmt_usdt_from_credits(user.balance)} USDT\n\n"
        f"🔗 <b>钱包:</b>\n<code>{user.wallet_address or '未绑定'}</code>",
        reply_markup=back_keyboard(),
    )

@router.callback_query(F.data == "deposit")
async def deposit_callback(callback: CallbackQuery, state: FSMContext):
    # 充值逻辑：用户输入 USDT，我们给他转成积分
    await callback.message.edit_text(
        "💎 <b>充值积分</b>\n\n"
        "汇率: 1 USDT = 100 积分\n"
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
    
    # 计算：充值 10 U -> 10,000,000 微单位 -> 1000 积分
    # 实际支付需要稍微波动一点小数位以便识别
    amount_db = usdt_val * 1000000 
    # 为了识别订单，加一点随机小数 (例如 10.000123)
    final_amount_db = amount_db + random.randint(100, 5000) 
    
    # 显示给用户的应付金额
    pay_usdt_str = f"{final_amount_db / 1000000:.6f}"
    
    # 预计到账积分
    expected_credits = fmt_credits(amount_db)

    order = await create_deposit_order(message.from_user.id, amount_db, final_amount_db)
    
    qr_text = (
        f"<b>💎 充值订单 #{order.id}</b>\n"
        f"预计到账: <b>{expected_credits} 积分</b>\n\n"
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
    await callback.answer("✅ 已通知管理员核对，到账后自动加分", show_alert=True)
    # 这里真实环境应触发后台查链脚本

# === 发红包 (积分版 + 扫雷) ===

@router.callback_query(F.data == "create_packet")
async def create_packet_callback(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private": return await callback.answer("请私聊设置", show_alert=True)
    await callback.message.edit_text("🧧 <b>发送积分红包</b>\n\n请输入总积分 (例如 100):", reply_markup=back_keyboard())
    await state.set_state(BotStates.create_packet_amount)

@router.message(BotStates.create_packet_amount)
async def process_packet_amount(message: Message, state: FSMContext):
    amount_db = parse_credits(message.text)
    if not amount_db or amount_db < 10000: # 最小 1 积分
        return await message.answer("❌ 至少发送 1 积分")
    
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
    packet_total_db = int(amount_db * (1 - fee_rate)) # 实际进包金额
    
    # 扣款
    success, msg = await add_balance(user_id, -amount_db, "send_packet", f"雷{mine}")
    if not success:
        await message.answer(f"❌ {msg}")
        return

    # 入库
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
        f"💰 总额: {fmt_credits(amount_db)} 积分\n"
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
            f"🧧 <b>{packet.sender_name} 的积分红包</b>\n"
            f"💰 <b>{fmt_credits(packet.total_amount)} 积分</b>\n"
            f"📦 数量: {packet.total_count} 个\n"
            f"{mine_text}\n"
            f"👇 <i>拼手气，踩雷赔付 1.5 倍！</i>"
        ), parse_mode="HTML"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧧 抢积分", callback_data=f"grab:{packet_id}")]])
    
    item = InlineQueryResultArticle(
        id=packet_id, title=f"发 {fmt_credits(packet.total_amount)} 积分红包",
        description=f"雷号: {packet.mine_number if packet.mine_number>=0 else '无'} | 点击发送",
        input_message_content=result_content, reply_markup=kb,
        thumbnail_url="https://img.icons8.com/color/96/money-bag.png"
    )
    await inline_query.answer([item], cache_time=1, is_personal=True)

# === 抢包逻辑 (防薅羊毛 + 扫雷) ===
@router.callback_query(F.data.startswith("grab:"))
async def grab_packet(callback: CallbackQuery):
    packet_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # 悲观锁防止并发
            res = await session.execute(select(RedPacket).where(RedPacket.id == packet_id).with_for_update())
            packet = res.scalars().first()
            
            if not packet or packet.status != 'active' or packet.remaining_count <= 0:
                return await callback.answer("😔 抢光了！", show_alert=True)
            
            claimed = json.loads(packet.claimed_users)
            if user_id in claimed: return await callback.answer("🛑 只能抢一次", show_alert=True)
            
            # === 风控检查 ===
            u_res = await session.execute(select(User).where(User.tg_id == user_id))
            claimer = u_res.scalars().first()
            if not claimer: 
                claimer = User(tg_id=user_id, balance=0)
                session.add(claimer)
            
            # 规则：扫雷包要求余额 > 500 积分 (防止小号用体验金碰瓷)
            if packet.mine_number >= 0 and claimer.balance < 5000000: # 500万微单位 = 500积分
                return await callback.answer("🚫 积分不足 500，无法参与扫雷！请先充值。", show_alert=True)

            # === 计算金额 ===
            if packet.remaining_count == 1: grab_db = packet.remaining_amount
            else:
                avg = packet.remaining_amount / packet.remaining_count
                grab_db = random.randint(1, int(avg * 2))
                if grab_db >= packet.remaining_amount: grab_db = packet.remaining_amount - 1
            
            # 更新红包
            packet.remaining_amount -= grab_db
            packet.remaining_count -= 1
            claimed.append(user_id)
            packet.claimed_users = json.dumps(claimed)
            if packet.remaining_count == 0: packet.status = 'finished'
            
            # 发放积分
            claimer.balance += grab_db
            session.add(Transaction(user_id=user_id, amount=grab_db, type="grab", note=f"P:{packet_id}"))
            
            # === 扫雷判定 (基于显示的积分尾数) ===
            alert_text = f"🎉 抢到 {fmt_credits(grab_db)} 积分"
            
            if packet.mine_number >= 0:
                # 逻辑：12.58 积分 -> 尾数 8
                # 数据库 125800 -> 除以 100 (去掉最后两位微数) -> 1258 -> 模 10 -> 8
                # 必须确保这个逻辑与 fmt_credits 显示的一致
                last_digit = int((grab_db // 100) % 10)
                
                if last_digit == packet.mine_number:
                    # 💥 中雷！赔付 1.5 倍
                    boom_rate = 1.5
                    # 赔付基数是红包的总金额 (packet.total_amount) 还是原价? 通常是原价
                    # 这里简化，按实际发包量赔付
                    penalty_db = int(packet.total_amount * boom_rate)
                    
                    claimer.balance -= penalty_db
                    session.add(Transaction(user_id=user_id, amount=-penalty_db, type="boom_penalty", note=f"中雷:{packet_id}"))
                    
                    # 给发包者
                    s_res = await session.execute(select(User).where(User.tg_id == packet.sender_id))
                    sender = s_res.scalars().first()
                    sender.balance += penalty_db
                    session.add(Transaction(user_id=packet.sender_id, amount=penalty_db, type="boom_income", note=f"中雷赔付:{packet_id}"))
                    
                    alert_text += f"\n💥 踩雷 (尾数{last_digit})！\n💸 自动赔付 {fmt_credits(penalty_db)} 积分"
                else:
                    alert_text += f"\n🛡️ 安全 (尾数{last_digit})"

    await callback.answer(alert_text, show_alert=True)

async def main() -> None:
    await init_db()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 积分扫雷机器人启动...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
