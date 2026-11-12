from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Category = Literal["Hot", "Warm", "Cold"]
FinalStatus = Literal["В работе", "Отказ"]
Score = Annotated[int, Field(strict=True, ge=0, le=100)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(score: int, threshold: int) -> Category:
    if score >= threshold:
        return "Hot"
    return "Warm" if score >= 45 else "Cold"


class LeadRow(BaseModel):
    row_index: int = Field(ge=2)
    id: str
    created_at: str
    name: str
    contact: str
    source: str
    budget: float | None = Field(ge=0, allow_inf_nan=False)
    activity: str
    status: str
    score: Score | None
    category: Category | None
    next_best_action: str
    first_message_draft: str
    red_flags: str
    notified: bool
    taken_at: str
    scored_at: str

    def scoring_input(self) -> tuple[str, str, float | None, str]:
        return self.name, self.source, self.budget, self.activity


class AIScoringResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: Score
    category: Category
    next_best_action: Text
    first_message_draft: Text
    red_flags: Text


class ScoreRowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_index: int = Field(strict=True, ge=2, le=10_000_000)


class ScoreRowResponse(BaseModel):
    row_index: int
    lead_id: str
    score: Score | None
    category: Category | None
    notified: bool


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    telegram_polling: bool
    bulk_scoring: dict[str, int | str]
