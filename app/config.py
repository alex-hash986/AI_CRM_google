from pathlib import Path
from typing import Annotated

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SheetsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    google_sheets_credentials_path: Path
    spreadsheet_id: Annotated[str, Field(min_length=1)]
    leads_sheet_name: Annotated[str, Field(min_length=1)] = "Leads"
    dashboard_sheet_name: Annotated[str, Field(min_length=1)] = "Dashboard"
    hot_lead_threshold: int = Field(default=75, ge=1, le=100)

    @model_validator(mode="after")
    def different_tabs(self) -> "SheetsSettings":
        if self.leads_sheet_name.casefold() == self.dashboard_sheet_name.casefold():
            raise ValueError("Leads and Dashboard must use different tabs")
        return self


class Settings(SheetsSettings):
    openai_api_key: SecretStr = Field(min_length=1)
    openai_base_url: AnyHttpUrl | None = None
    openai_model: str = "gpt-4o-mini"
    telegram_bot_token: SecretStr = Field(min_length=1)
    telegram_chat_id: int
    webhook_secret: SecretStr = Field(min_length=1)
