"""Конфигурация приложения.

Два источника:
- секреты/инфраструктура — из env (.env), через pydantic-settings;
- бизнес-параметры (пороги, паттерны, расписание, формула дедлайна) — из YAML.

Разделение намеренное: бизнес-конфиг правится без передеплоя и без секретов.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Инфраструктурные настройки из окружения."""

    database_url: str = "postgresql+psycopg://fine_defender:change_me@localhost:5432/fine_defender"
    token_encryption_key: str = ""
    log_level: str = "INFO"
    app_config_path: Path = Path("config/config.yaml")

    # Бэкенд репозитория для API: memory (демо) | postgres (бой)
    app_backend: str = "postgres"
    # Режим WB-клиента: live (реальный API) | mock (фикстура — для демо без токена)
    wb_client_mode: str = "live"
    # Путь к фикстуре для wb_client_mode=mock
    sample_report_path: Path = Path("samples/weekly_report_demo.json")
    # Интервал фонового воркера выгрузки+классификации, сек
    worker_interval_seconds: int = 3600
    # Автозасев демо-селлера (mock-режим), если в БД нет ни одного селлера
    demo_seed: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


# --- Бизнес-конфиг (YAML) ---


class RetryConfig(BaseModel):
    max_attempts: int = 5
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0


class WbConfig(BaseModel):
    base_url: str
    report_endpoint: str
    page_limit: int = 100_000
    request_timeout_seconds: float = 60.0


class IngestionConfig(BaseModel):
    schedule_cron: str = "0 6 * * 1"
    default_period_days: int = 7
    retry: RetryConfig = Field(default_factory=RetryConfig)
    consecutive_failure_alert_threshold: int = 3


class ClassifierConfig(BaseModel):
    fine_operation_types: list[str] = Field(default_factory=list)
    # категория -> список паттернов причины
    reason_patterns: dict[str, list[str]] = Field(default_factory=dict)


class DisputeConfig(BaseModel):
    window_days: int = 30
    recoverable_ratio: float = 1.0
    deadline_warning_days: int = 7
    # категория -> список пунктов чек-листа доказательств
    evidence_checklist: dict[str, list[str]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    wb: WbConfig
    ingestion: IngestionConfig
    classifier: ClassifierConfig
    dispute: DisputeConfig

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_app_config() -> AppConfig:
    return AppConfig.load(get_settings().app_config_path)
