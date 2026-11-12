import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Annotated

from anyio import to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from gspread.exceptions import GSpreadException
from requests.exceptions import RequestException
from telegram.error import TelegramError

from app.ai_scorer import AIScorer, ScoringError
from app.config import Settings
from app.models import HealthResponse, LeadRow, ScoreRowRequest, ScoreRowResponse
from app.sheets_service import InvalidLead, LeadChanged, SheetsService
from app.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class CRM:
    def __init__(self, settings: Settings, sheets: SheetsService, scorer: AIScorer) -> None:
        self.settings = settings
        self.sheets = sheets
        self.scorer = scorer
        self.lock = asyncio.Lock()
        self.telegram = TelegramNotifier(settings, sheets, self.lock)
        self.bulk_task: asyncio.Task[None] | None = None
        self.bulk_status: dict[str, int | str] = {"state": "idle"}
        self.delivered: set[str] = set()

    async def score_row(self, row_index: int, *, rescore: bool = True) -> ScoreRowResponse:
        async with self.lock:
            lead = await to_thread.run_sync(self.sheets.get_lead_by_row, row_index)
            return await self._score(lead, rescore=rescore)

    async def _score(self, lead: LeadRow | None, *, rescore: bool) -> ScoreRowResponse:
        if lead is None:
            raise HTTPException(404, "Lead not found")
        lead = await to_thread.run_sync(self.sheets.ensure_identity, lead)
        if rescore or lead.score is None:
            result = await self.scorer.score(lead)
            current = await to_thread.run_sync(self.sheets.get_lead_by_id, lead.id)
            if current is None or current.scoring_input() != lead.scoring_input():
                raise LeadChanged("Lead changed during scoring")
            await to_thread.run_sync(
                self.sheets.update_lead_scoring, current.row_index, result, current.notified,
            )
            lead = current.model_copy(update=result.model_dump())
            logger.info("Scored row %s (%s)", lead.row_index, result.category)

        if not lead.notified and lead.status not in ("В работе", "Отказ"):
            if lead.id not in self.delivered and await self.telegram.send_hot_lead(lead):
                self.delivered.add(lead.id)
            if lead.id in self.delivered:
                await to_thread.run_sync(self.sheets.mark_notified, lead.id)
                self.delivered.discard(lead.id)
                lead = lead.model_copy(update={"notified": True})
                logger.info("Recorded Telegram delivery for row %s", lead.row_index)

        return ScoreRowResponse(
            row_index=lead.row_index, lead_id=lead.id, score=lead.score,
            category=lead.category, notified=lead.notified,
        )

    async def score_all(self) -> None:
        counts = {"processed": 0, "failed": 0, "total": 0}
        self.bulk_status = {"state": "running", **counts}
        try:
            async with self.lock:
                rows = await to_thread.run_sync(self.sheets.get_unscored_rows)
                pending = await to_thread.run_sync(
                    self.sheets.pending_notifications, self.settings.hot_lead_threshold,
                )
            targets = list(dict.fromkeys(rows + pending))
            counts["total"] = len(targets)
            for row in targets:
                try:
                    await self.score_row(row, rescore=False)
                    counts["processed"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    logger.warning("Bulk scoring failed for row %s (%s)", row, type(exc).__name__)
                self.bulk_status = {"state": "running", **counts}
            self.bulk_status = {"state": "complete", **counts}
        except asyncio.CancelledError:
            self.bulk_status = {"state": "cancelled", **counts}
            raise
        except Exception as exc:
            self.bulk_status = {"state": "failed", **counts}
            logger.error("Bulk scoring failed (%s)", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP transport logs can contain Telegram URLs with the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings()
    sheets = await to_thread.run_sync(SheetsService, settings)
    await to_thread.run_sync(lambda: sheets.leads)
    scorer = AIScorer(settings)
    try:
        crm = CRM(settings, sheets, scorer)
        app.state.crm = crm
        telegram = crm.telegram.application
        updater = telegram.updater
        assert updater is not None
        async with telegram:
            await telegram.start()
            try:
                await updater.start_polling(allowed_updates=["callback_query"], bootstrap_retries=0)
                yield
            finally:
                if updater.running:
                    await updater.stop()
                if crm.bulk_task and not crm.bulk_task.done():
                    crm.bulk_task.cancel()
                    await asyncio.gather(crm.bulk_task, return_exceptions=True)
                await telegram.stop()
    finally:
        await scorer.client.close()


app = FastAPI(title="AI CRM", lifespan=lifespan)


def authorized(
    request: Request, x_webhook_secret: Annotated[str | None, Header()] = None,
) -> CRM:
    crm: CRM = request.app.state.crm
    expected = crm.settings.webhook_secret.get_secret_value().encode()
    if x_webhook_secret is None or not compare_digest(x_webhook_secret.encode(), expected):
        raise HTTPException(401, "Invalid webhook secret")
    return crm


Authorized = Annotated[CRM, Depends(authorized)]


def upstream_error(exc: Exception) -> HTTPException:
    logger.warning("Request failed (%s)", type(exc).__name__)
    if isinstance(exc, InvalidLead):
        return HTTPException(422, str(exc))
    if isinstance(exc, LeadChanged):
        return HTTPException(409, str(exc))
    return HTTPException(503, "Upstream service unavailable or rejected the request")


UPSTREAM_ERRORS = (InvalidLead, LeadChanged, ScoringError, GSpreadException, RequestException, TelegramError)


@app.get("/health")
async def health(crm: Authorized) -> HealthResponse:
    updater = crm.telegram.application.updater
    polling = bool(updater and updater.running)
    if not polling:
        raise HTTPException(503, "Telegram polling is inactive")
    return HealthResponse(telegram_polling=polling, bulk_scoring=crm.bulk_status)


@app.post("/score-row")
async def score_row(body: ScoreRowRequest, crm: Authorized) -> ScoreRowResponse:
    try:
        return await crm.score_row(body.row_index)
    except UPSTREAM_ERRORS as exc:
        raise upstream_error(exc) from exc


@app.post("/score-all", status_code=202)
async def score_all(crm: Authorized) -> dict[str, int | str]:
    if crm.bulk_task is None or crm.bulk_task.done():
        crm.bulk_status = {"state": "queued"}
        crm.bulk_task = asyncio.create_task(crm.score_all())
    return crm.bulk_status


@app.post("/refresh-dashboard")
async def refresh_dashboard(crm: Authorized) -> dict[str, str]:
    try:
        async with crm.lock:
            await to_thread.run_sync(crm.sheets.build_dashboard)
        return {"status": "updated"}
    except UPSTREAM_ERRORS as exc:
        raise upstream_error(exc) from exc


@app.post("/simulate-lead", status_code=201)
async def simulate_lead(crm: Authorized) -> ScoreRowResponse:
    lead_id: str | None = None
    try:
        async with crm.lock:
            lead_id = await to_thread.run_sync(crm.sheets.append_demo_lead)
            lead = await to_thread.run_sync(crm.sheets.get_lead_by_id, lead_id)
            return await crm._score(lead, rescore=False)
    except UPSTREAM_ERRORS as exc:
        error = upstream_error(exc)
        if lead_id:
            error.detail = {"message": error.detail, "lead_id": lead_id}
        raise error from exc
