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

# 引入数据库操作
from database import (
    init_db, AsyncSessionLocal, User, RedPacket,
    add_balance, get_user, update_wallet_address,
    create_deposit_order, create_withdrawal_request, get_user_stats
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DEPOSIT_WALLET_ADDRESS = os.getenv("DEPOSIT_WALLET_ADDRESS", "未配置")
DEPOSIT_QR_CODE_FILE_ID = os.getenv("DEPOSIT_QR_CODE_FILE_ID", "")

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

dp = Dispatcher()
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
router = Router()

# === 辅助工具 ===
def fmt_usdt(amount_int):
    return f"{amount_int / 1000000:.2f}"

def parse_usdt(amount_str):
    try:
        val = float(amount_str)
        return int(val * 1000000)
    except (ValueError, TypeError):
        return None

# === 状态机 ===
class BotStates(StatesGroup):
    create_packet_amount = State()
    create_packet_count = State()
    deposit_amount = State()
    withdraw_amount = State()
    bind_wallet_address = State()

# === 键盘定义 ===
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧧 发送红包", callback_data="create_packet"), InlineKeyboardButton(text="💰 充值", callback_data="deposit")],
        [InlineKeyboardButton(text="💸 提现", callback_data="withdraw"), InlineKeyboardButton(text="👤 个人信息", callback_data="my_info")],
        [InlineKeyboardButton(text="🔗 绑定U钱包", callback_data="bind_wallet")]
    ])

def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data="back_to_main")]])

# === 管理员工具 ===
@router.message(F.photo & (F.from_user.id == ADMIN_ID))
async def get_photo_file_id(message: Message):
    photo = message.photo[-1]
    await message.reply(f"🖼 File ID: <code>{photo.file_id}</code>")

# === 基础指令 ===
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        user = await get_user(session, message.from_user.id, message.from_user.username or "User")
    await message.answer(
        f"👋 欢迎 <b>{user.username}</b>\n🆔 ID: <code>{user.tg_id}</code>\n💰 余额: <b>{fmt_usdt(user.balance)} USDT</b>\n\n👇 请选择操作:",
        reply_markup=main_keyboard(),
    )

@router.callback_query(F.data == "back_to_main")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    try:
        await callback.message.edit_text(
            f"👋 欢迎 <b>{user.username}</b>\n🆔 ID: <code>{user.tg_id}</code>\n💰 余额: <b>{fmt_usdt(user.balance)} USDT</b>",
            reply_markup=main_keyboard(),
        )
    except: pass
    await callback.answer()

@router.callback_query(F.data == "my_info")
async def my_info_callback(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
        stats = await get_user_stats(callback.from_user.id)
    await callback.message.edit_text(
        f"<b>👤 个人信息</b>\n\n🆔 <b>ID:</b> <code>{user.tg_id}</code>\n💰 余额: {fmt_usdt(user.balance)} U\n\n"
        f"🧧 <b>统计:</b>\n📤 发出: {fmt_usdt(stats['total_sent'])} U\n📥 收到: {fmt_usdt(stats['total_grabbed'])} U\n\n"
        f"🔗 <b>钱包:</b>\n<code>{user.wallet_address or '未绑定'}</code>",
        reply_markup=back_keyboard(),
    )
    await callback.answer()

@router.callback_query(F.data == "bind_wallet")
async def bind_wallet_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("请输入您的U钱包地址 (TRC20):", reply_markup=back_keyboard())
    await state.set_state(BotStates.bind_wallet_address)
    await callback.answer()

@router.message(BotStates.bind_wallet_address)
async def process_wallet_address(message: Message, state: FSMContext):
    address = message.text.strip()
    if not (address.startswith("T") and len(address) > 30):
        await message.answer("❌ 地址格式错误 (TRC20)", reply_markup=back_keyboard())
        return
    await update_wallet_address(message.from_user.id, address)
    await message.answer(f"✅ 绑定成功:\n<code>{address}</code>")
    await state.clear()
    await cmd_start(message, state)

@router.callback_query(F.data == "deposit")
async def deposit_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💰 <b>充值</b>\n\n请输入金额 (整数 USDT):", reply_markup=back_keyboard())
    await state.set_state(BotStates.deposit_amount)
    await callback.answer()

@router.message(BotStates.deposit_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val <= 0: raise ValueError
    except: return await message.answer("❌ 请输入整数")
    
    amount = val * 1000000
    final_amount = amount + random.randint(-300000, 300000)
    if final_amount <= 0: final_amount = amount + 10000

    order = await create_deposit_order(message.from_user.id, amount, final_amount)
    
    qr_text = (
        f"<b>💰 充值订单</b>\n请转账至:\n<code>{DEPOSIT_WALLET_ADDRESS}</code>\n\n"
        f"⚠️ <b>必须精确转账:</b>\n👉 <code>{fmt_usdt(final_amount)}</code> <b>USDT</b>\n"
        f"支付后点下方按钮。"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ 我已付款", callback_data=f"paid:{order.id}")],[InlineKeyboardButton(text="⬅️ 返回", callback_data="back_to_main")]])
    
    if DEPOSIT_QR_CODE_FILE_ID:
        await message.answer_photo(photo=DEPOSIT_QR_CODE_FILE_ID, caption=qr_text, reply_markup=kb)
    else:
        await message.answer(qr_text, reply_markup=kb)
    await state.clear()

@router.callback_query(F.data.startswith("paid:"))
async def paid_callback(callback: CallbackQuery):
    await callback.answer("✅ 已通知管理员核对", show_alert=True)

@router.callback_query(F.data == "withdraw")
async def withdraw_callback(callback: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as session:
        user = await get_user(session, callback.from_user.id)
    if not user.wallet_address: return await callback.answer("⚠️ 请先绑定钱包", show_alert=True)
    await callback.message.edit_text(f"💰 余额: {fmt_usdt(user.balance)} U\n请输入提现金额:", reply_markup=back_keyboard())
    await state.set_state(BotStates.withdraw_amount)
    await callback.answer()

@router.message(BotStates.withdraw_amount)
async def process_withdraw_amount(message: Message, state: FSMContext):
    amount = parse_usdt(message.text)
    if not amount or amount <= 0: return await message.answer("❌ 金额无效")
    req, msg = await create_withdrawal_request(message.from_user.id, amount, "Default")
    if req: 
        await message.answer("✅ 提现申请已提交")
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"📢 新提现: {fmt_usdt(amount)} U")
    else: 
        await message.answer(f"❌ {msg}")
    await state.clear()
    await cmd_start(message, state)

# === 重点：发红包流程 (UI优化版) ===
@router.callback_query(F.data == "create_packet")
async def create_packet_callback(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private": return await callback.answer("请私聊设置", show_alert=True)
    await callback.message.edit_text("🧧 <b>发送红包</b>\n\n请输入总金额 (USDT):", reply_markup=back_keyboard())
    await state.set_state(BotStates.create_packet_amount)
    await callback.answer()

@router.message(BotStates.create_packet_amount)
async def process_packet_amount(message: Message, state: FSMContext):
    amount = parse_usdt(message.text)
    if not amount or amount < 10000: return await message.answer("❌ 金额太小 (至少 0.01 U)")
    await state.update_data(amount=amount)
    await message.answer("🔢 请输入红包个数:")
    await state.set_state(BotStates.create_packet_count)

@router.message(BotStates.create_packet_count)
async def process_packet_count(message: Message, state: FSMContext):
    try: count = int(message.text)
    except: return await message.answer("❌ 必须是整数")
    
    data = await state.get_data()
    total_amount = data['amount']
    user_id = message.from_user.id
    
    # 1. 扣款
    success, msg = await add_balance(user_id, -total_amount, "send_packet", "制作红包")
    if not success:
        await message.answer(f"❌ {msg}")
        await state.clear()
        return

    # 2. 存入数据库
    packet_id = str(uuid.uuid4())[:8]
    async with AsyncSessionLocal() as session:
        async with session.begin():
            packet = RedPacket(
                id=packet_id,
                sender_id=user_id,
                sender_name=message.from_user.first_name,
                total_amount=total_amount,
                total_count=count,
                remaining_amount=total_amount,
                remaining_count=count,
                status="active",
                claimed_users="[]"
            )
            session.add(packet)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 发送给群友", switch_inline_query=packet_id)]])
    
    # === UI 优化点 1: 制作成功的提示 ===
    await message.answer(
        f"✅ <b>红包制作成功！</b>\n"
        f"➖➖➖➖➖➖➖➖\n"
        f"💎 <b>金额:</b> {fmt_usdt(total_amount)} USDT\n"
        f"📦 <b>数量:</b> {count} 个\n\n"
        f"👇 <b>点击下方按钮，选择一个群组发出去：</b>",
        reply_markup=kb
    )
    await state.clear()

# === 内联查询 (UI优化版) ===
@router.inline_query()
async def inline_redpacket_handler(inline_query: InlineQuery):
    packet_id = inline_query.query.strip()
    if not packet_id: return
    
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(RedPacket).where(RedPacket.id == packet_id))
        packet = res.scalars().first()
    
    if not packet or packet.status != 'active': return

    # === UI 优化点 2: 群内显示的消息气泡 ===
    # 增加了空行，调整了排版，使其看起来更“大”
    result_content = InputTextMessageContent(
        message_text=(
            f"🧧 <b>{packet.sender_name} 发了一个大红包！</b>\n\n"
            f"💵 <b>总额:</b> {fmt_usdt(packet.total_amount)} USDT\n"
            f"📦 <b>数量:</b> {packet.total_count} 个\n\n"
            f"👇 <i>手慢无，点击下方按钮立即领取！</i>"
        ),
        parse_mode="HTML"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧧 抢红包", callback_data=f"grab:{packet_id}")]])
    
    item = InlineQueryResultArticle(
        id=packet_id,
        title=f"发送 {fmt_usdt(packet.total_amount)} U 红包",
        description=f"数量: {packet.total_count} 个 | 点击发送",
        input_message_content=result_content,
        reply_markup=kb,
        thumbnail_url="https://img.icons8.com/emoji/96/red-envelope.png", # 换了个更好看的图标
        thumbnail_width=96,
        thumbnail_height=96
    )
    await inline_query.answer([item], cache_time=1, is_personal=True)

# === 抢红包 (逻辑不变，仅微调提示) ===
@router.callback_query(F.data.startswith("grab:"))
async def grab_packet(callback: CallbackQuery):
    packet_id = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await session.execute(select(RedPacket).where(RedPacket.id == packet_id).with_for_update())
            packet = res.scalars().first()
            
            if not packet: return await callback.answer("❌ 红包不存在", show_alert=True)
            if packet.status != 'active' or packet.remaining_count <= 0: return await callback.answer("😔 来晚了，红包已被抢完！", show_alert=True)
            
            claimed = json.loads(packet.claimed_users)
            if user_id in claimed: return await callback.answer("🛑 你已经抢过了，请勿重复点击！", show_alert=True)
            
            if packet.remaining_count == 1: grab = packet.remaining_amount
            else:
                avg = packet.remaining_amount / packet.remaining_count
                grab = random.randint(1, int(avg * 2))
                if grab >= packet.remaining_amount: grab = packet.remaining_amount - 1
            
            packet.remaining_amount -= grab
            packet.remaining_count -= 1
            claimed.append(user_id)
            packet.claimed_users = json.dumps(claimed)
            if packet.remaining_count == 0: packet.status = 'finished'
            
            u_res = await session.execute(select(User).where(User.tg_id == user_id))
            user = u_res.scalars().first()
            if not user: 
                user = User(tg_id=user_id, balance=0)
                session.add(user)
            
            user.balance += grab
            from database import Transaction
            session.add(Transaction(user_id=user_id, amount=grab, type="grab_packet", note=f"红包:{packet_id}"))

    await callback.answer(f"🎉 恭喜！抢到 {fmt_usdt(grab)} USDT", show_alert=True)

async def main() -> None:
    await init_db()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 机器人已启动...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())