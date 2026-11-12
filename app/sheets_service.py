import logging
from collections import Counter
from collections.abc import Callable
from typing import ParamSpec, TypeVar
from uuid import uuid4

import gspread
import pandas as pd
from babel.numbers import get_decimal_symbol
from gspread.exceptions import APIError, WorksheetNotFound
from pydantic import ValidationError
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.config import SheetsSettings
from app.demo_data import Cell, demo_rows, simulated_lead
from app.models import AIScoringResult, FinalStatus, LeadRow, utc_now

logger = logging.getLogger(__name__)
P = ParamSpec("P")
T = TypeVar("T")
HEADERS = [
    "ID", "Created_At", "Name", "Contact", "Source", "Budget", "Activity", "Status",
    "Score", "Category", "Next_Best_Action", "First_Message_Draft", "Red_Flags",
    "Notified", "Taken_At", "Scored_At",
]
COLUMNS = [header.lower() for header in HEADERS]


def google_call(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return retry(
        retry=retry_if_exception(lambda exc: isinstance(exc, APIError) and exc.code == 429),
        wait=wait_random_exponential(multiplier=2, max=25),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )(operation)(*args, **kwargs)


class InvalidLead(ValueError):
    pass


class LeadChanged(RuntimeError):
    pass


class SheetsService:
    def __init__(self, settings: SheetsSettings) -> None:
        self.settings = settings
        self.client = gspread.service_account(filename=str(settings.google_sheets_credentials_path))
        self.client.set_timeout((4, 10))
        self.book = google_call(self.client.open_by_key, settings.spreadsheet_id)
        self._leads: gspread.Worksheet | None = None

    @property
    def leads(self) -> gspread.Worksheet:
        if self._leads is None:
            tab = google_call(self.book.worksheet, self.settings.leads_sheet_name)
            headers = google_call(tab.row_values, 1)
            if headers[:len(HEADERS)] != HEADERS:
                raise RuntimeError("Unexpected Leads header. Run the setup command.")
            self._leads = tab
        return self._leads

    def _tab(self, title: str, cols: int) -> gspread.Worksheet:
        try:
            return google_call(self.book.worksheet, title)
        except WorksheetNotFound:
            return google_call(self.book.add_worksheet, title=title, rows=1000, cols=cols)

    def initialize(self) -> None:
        tab = self._tab(self.settings.leads_sheet_name, len(HEADERS))
        if tab.col_count < len(HEADERS):
            google_call(tab.resize, cols=len(HEADERS))
        headers = google_call(tab.row_values, 1)
        if not headers:
            if self._has_data(tab):
                raise RuntimeError("Leads contains data without a header")
            google_call(tab.update, [HEADERS], "A1:P1")
        elif headers[:len(HEADERS)] != HEADERS:
            raise RuntimeError("Unexpected Leads header")
        google_call(tab.freeze, rows=1)
        self._leads = tab

    @staticmethod
    def _has_data(tab: gspread.Worksheet) -> bool:
        rows = google_call(tab.get, "A2:P", value_render_option="FORMULA")
        return any(cell not in ("", None) for row in rows for cell in row)

    def has_lead_data(self) -> bool:
        return self._has_data(self.leads)

    @staticmethod
    def _parse(row_index: int, raw: list[Cell]) -> LeadRow | None:
        values = dict(zip(COLUMNS, (raw + [""] * len(HEADERS))[:len(HEADERS)], strict=True))
        if not any(values[key] != "" for key in COLUMNS[2:7]):
            return None
        if values["budget"] != "" and type(values["budget"]) not in (int, float):
            raise InvalidLead(f"Row {row_index}: Budget must be a numeric cell")
        score = values["score"]
        if type(score) is float and score.is_integer():
            score = int(score)
        data = {key: str(value).strip() for key, value in values.items()}
        try:
            return LeadRow.model_validate({
                **data, "row_index": row_index, "budget": values["budget"] if values["budget"] != "" else None,
                "score": score if score != "" else None, "category": data["category"] or None,
                "notified": data["notified"].casefold() == "true",
            })
        except ValidationError as exc:
            raise InvalidLead(f"Row {row_index}: invalid lead fields") from exc

    def _get(self, address: str) -> list[list[Cell]]:
        return google_call(self.leads.get, address, value_render_option="UNFORMATTED_VALUE")

    def get_lead_by_row(self, row_index: int) -> LeadRow | None:
        if row_index < 2:
            raise InvalidLead("Row must be at least 2")
        try:
            rows = self._get(f"A{row_index}:P{row_index}")
        except APIError as exc:
            if exc.code == 400 and row_index > self.leads.row_count:
                return None
            raise
        return self._parse(row_index, rows[0]) if rows else None

    def get_unscored_rows(self) -> list[int]:
        rows = self._get("A2:P")
        if not rows:
            return []
        frame = pd.DataFrame(rows, dtype=object).reindex(columns=range(len(HEADERS)), fill_value="")
        frame.columns = COLUMNS
        inputs = frame[COLUMNS[2:7]]
        has_input = (inputs.notna() & inputs.ne("")).any(axis=1)
        score = frame["score"]
        return [int(index) + 2 for index in frame.index[has_input & (score.isna() | score.eq(""))]]

    def pending_notifications(self, threshold: int) -> list[int]:
        pending = []
        for index, row in enumerate(self._get("A2:P"), 2):
            score = row[8] if len(row) > 8 else None
            notified = len(row) > 13 and str(row[13]).strip().casefold() == "true"
            status = str(row[7]).strip() if len(row) > 7 else ""
            if type(score) in (int, float) and score >= threshold and not notified and status not in ("В работе", "Отказ"):
                pending.append(index)
        return pending

    def _row_for_id(self, lead_id: str) -> int | None:
        ids = [str(row[0]).strip() if row else "" for row in self._get("A2:A")]
        if Counter(ids)[lead_id] > 1:
            raise InvalidLead("Duplicate lead ID")
        return ids.index(lead_id) + 2 if lead_id in ids else None

    def get_lead_by_id(self, lead_id: str) -> LeadRow | None:
        index = self._row_for_id(lead_id)
        if index is None:
            return None
        lead = self.get_lead_by_row(index)
        if lead and lead.id != lead_id:
            raise LeadChanged("Lead moved during lookup")
        return lead

    def ensure_identity(self, lead: LeadRow) -> LeadRow:
        if lead.id:
            if len(("reject:" + lead.id).encode()) > 64 or self._row_for_id(lead.id) != lead.row_index:
                raise InvalidLead("Invalid, moved or duplicate lead ID")
            return lead
        current = self.get_lead_by_row(lead.row_index)
        if current != lead:
            raise LeadChanged("Lead changed before ID assignment")
        lead_id = uuid4().hex
        changes = [{"range": f"A{lead.row_index}", "values": [[lead_id]]}]
        if not lead.created_at:
            created_at = utc_now()
            changes.append({"range": f"B{lead.row_index}", "values": [[created_at]]})
        else:
            created_at = lead.created_at
        google_call(self.leads.batch_update, changes)
        return lead.model_copy(update={"id": lead_id, "created_at": created_at})

    def update_lead_scoring(self, row_index: int, scoring_result: AIScoringResult, notified: bool) -> None:
        data = scoring_result
        changes = [
            {"range": f"I{row_index}:N{row_index}", "values": [[
                data.score, data.category, data.next_best_action, data.first_message_draft, data.red_flags, notified,
            ]]},
            {"range": f"P{row_index}", "values": [[utc_now()]]},
        ]
        google_call(self.leads.batch_update, changes)

    def mark_notified(self, lead_id: str) -> None:
        lead = self.get_lead_by_id(lead_id)
        if lead is None:
            raise LeadChanged("Lead disappeared after Telegram delivery")
        google_call(self.leads.update, [[True]], f"N{lead.row_index}")

    def update_lead_status(self, lead_id: str, new_status: FinalStatus, taken_at: str) -> tuple[str, str]:
        lead = self.get_lead_by_id(lead_id)
        if lead is None:
            raise InvalidLead("Lead not found")
        if lead.status in ("В работе", "Отказ"):
            return lead.status, lead.taken_at
        google_call(self.leads.batch_update, [
            {"range": f"H{lead.row_index}", "values": [[new_status]]},
            {"range": f"O{lead.row_index}", "values": [[taken_at]]},
        ])
        return new_status, taken_at

    def append_demo_lead(self) -> str:
        values = simulated_lead()
        google_call(self.leads.append_row, values, table_range="A1:P", insert_data_option="INSERT_ROWS")
        return str(values[0])

    def seed_demo_data(self) -> None:
        rows = [HEADERS, *demo_rows(self.settings.hot_lead_threshold)]
        if self.leads.row_count < len(rows):
            google_call(self.leads.resize, rows=len(rows))
        def cell(value: Cell) -> dict[str, dict[str, Cell]]:
            key = "boolValue" if isinstance(value, bool) else "numberValue" if isinstance(value, (int, float)) else "stringValue"
            return {"userEnteredValue": {key: value}}

        # UpdateCells clears the remainder of the range in the same atomic request.
        google_call(self.book.batch_update, {"requests": [{"updateCells": {
            "range": {"sheetId": self.leads.id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 16},
            "rows": [{"values": [cell(value) for value in row]} for row in rows],
            "fields": "userEnteredValue",
        }}]})

    def build_dashboard(self) -> None:
        self.leads  # Validate schema before writing formulas.
        tab = self._tab(self.settings.dashboard_sheet_name, 5)
        if tab.col_count < 5 or tab.row_count < 10:
            google_call(tab.resize, rows=max(tab.row_count, 10), cols=max(tab.col_count, 5))
        locale = google_call(self.book.fetch_sheet_metadata, params={"fields": "properties(locale)"})["properties"]["locale"]
        separator = ";" if get_decimal_symbol(locale) == "," else ","
        quote = "'" + self.settings.leads_sheet_name.replace("'", "''") + "'!"
        ids, source, budget, status, score, category = (f"{quote}{col}2:{col}" for col in "AEFHIJ")
        hot = f'--({category}="Hot")'
        query = (
            'select Col2,count(Col1),sum(Col3),avg(Col4),sum(Col5) '
            "where Col1 is not null and Col1 <> '' and Col2 is not null and Col2 <> '' group by Col2 "
            "label Col2 '',count(Col1) '',sum(Col3) '',avg(Col4) '',sum(Col5) ''"
        )
        series = separator.join((ids, source, budget, score, hot))
        # Numeric sentinel supplies column types even before the first lead is scored.
        sentinel = separator.join(('""', '""', '0', '0', '0'))
        data = f'VSTACK(ARRAYFORMULA(HSTACK({series})){separator}HSTACK({sentinel}))'
        formulas = [
            ["Показатель", "Значение"],
            ["Всего лидов", f"=COUNTA({ids})"],
            ["Сумма воронки", f'=SUMIF({ids}{separator}"<>"{separator}{budget})'],
            ["Сумма Hot-лидов", f'=SUMIFS({budget}{separator}{category}{separator}"Hot"{separator}{ids}{separator}"<>")'],
            ["Средний Score", f'=IFERROR(AVERAGEIF({ids}{separator}"<>"{separator}{score}){separator}0)'],
            ["Неразобранные Hot-лиды", f'=COUNTIFS({ids}{separator}"<>"{separator}{category}{separator}"Hot"{separator}{status}{separator}"<>В работе"{separator}{status}{separator}"<>Отказ")'],
            [],
            ["Источник", "Кол-во", "Сумма бюджета", "Средний Score", "Кол-во Hot"],
            [f'=IFNA(QUERY({data}{separator}"{query}"{separator}0){separator}"")'],
        ]
        google_call(tab.update, [row + [""] * (5 - len(row)) for row in formulas], "A1:E9", raw=False)
        google_call(tab.batch_format, [
            {"range": "A1:B1", "format": {"textFormat": {"bold": True}}},
            {"range": "A8:E8", "format": {"textFormat": {"bold": True}}},
            {"range": "B3:B4", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
            {"range": "B5", "format": {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}},
            {"range": "C9:D", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
        ])
        # Columns A and B hold both KPI and source-table values.
        google_call(self.book.batch_update, {"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": tab.id, "dimension": "COLUMNS", "startIndex": start, "endIndex": end},
                "properties": {"pixelSize": width}, "fields": "pixelSize",
            }}
            for start, end, width in [(0, 1, 245), (1, 5, 145)]
        ]})
