import logging
from asyncio import Lock
from html import escape

from anyio import to_thread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app.config import Settings
from app.models import FinalStatus, LeadRow, utc_now
from app.sheets_service import SheetsService

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings, sheets: SheetsService, lock: Lock) -> None:
        self.settings = settings
        self.sheets = sheets
        self.lock = lock
        self.application = (
            Application.builder().token(settings.telegram_bot_token.get_secret_value())
            .connect_timeout(5).read_timeout(10).write_timeout(10).build()
        )
        self.application.add_handler(CallbackQueryHandler(self.handle_callback, pattern=r"^(take|reject):.+$"))
        self.application.add_error_handler(self.handle_error)

    @staticmethod
    def card(lead: LeadRow) -> str:
        name = escape(lead.name[:180] or "Без имени")
        budget = f"{lead.budget:,.0f} ₽".replace(",", " ") if lead.budget is not None else "Не указан"
        return (
            f"🔥 <b>{name}</b> · {lead.score}/100\n"
            f"<b>Источник</b> {escape(lead.source[:150] or 'Не указан')}\n"
            f"<b>Бюджет</b> {budget}\n"
            f"<b>Контакт</b> {escape(lead.contact[:180] or 'Не указан')}\n"
            f"<b>Запрос</b> {escape(lead.activity[:500] or 'Не указан')}\n\n"
            f"<b>Следующий шаг</b> {escape(lead.next_best_action[:400])}\n"
            f"<b>Черновик</b> {escape(lead.first_message_draft[:550])}\n"
            f"<b>Риски</b> {escape(lead.red_flags[:300])}"
        )

    async def send_hot_lead(self, lead: LeadRow) -> bool:
        if lead.notified or lead.score is None or lead.score < self.settings.hot_lead_threshold:
            return False
        message = await self.application.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text=self.card(lead),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Взять в работу", callback_data=f"take:{lead.id}"),
                InlineKeyboardButton("❌ Нецелевой", callback_data=f"reject:{lead.id}"),
            ]]),
        )
        logger.info("Telegram accepted card for row %s (message_id=%s)", lead.row_index, message.message_id)
        return True

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not isinstance(query.data, str):
            return
        if update.effective_chat is None or update.effective_chat.id != self.settings.telegram_chat_id:
            await query.answer("Действие недоступно в этом чате.", show_alert=True)
            return

        await query.answer()
        action, lead_id = query.data.split(":", 1)
        target: FinalStatus = "В работе" if action == "take" else "Отказ"
        clicked_at = utc_now()
        async with self.lock:
            status, taken_at = await to_thread.run_sync(self.sheets.update_lead_status, lead_id, target, clicked_at)
            lead = await to_thread.run_sync(self.sheets.get_lead_by_id, lead_id)
        if lead is None:
            return
        try:
            await query.edit_message_text(
                text=f"{self.card(lead)}\n\n<b>Статус</b> {escape(status)}\n"
                f"<b>Время реакции</b> {escape(taken_at or 'Не указано')}",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        logger.info("Telegram callback processed for row %s", lead.row_index)

    @staticmethod
    async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram update failed (%s)", type(context.error).__name__)
