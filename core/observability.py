"""Shared structured logging for Django and the standalone Telegram process."""
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

context = ContextVar("request_context", default={})


def request_id(value=None):
    return value if value and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) else uuid4().hex


def event(name, *, traffic=False, level=logging.INFO, **fields):
    logging.getLogger("activity" if traffic else "voting.events").log(
        level, name, extra={"fields": {**context.get(), **fields}})


class JsonFormatter(logging.Formatter):
    def __init__(self, service="web"):
        super().__init__()
        self.service = service

    def format(self, record):
        result = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                  "level": record.levelname, "service": self.service, "pid": os.getpid(),
                  "logger": record.name, "event": record.getMessage(),
                  **context.get(), **getattr(record, "fields", {})}
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        # Protect against accidentally logged credentials, including HTTP exception URLs.
        value = json.dumps(result, ensure_ascii=False, default=str)
        from decouple import config
        for name in ("TELEGRAM_TOKEN", "DJANGO_API_TOKEN", "SECRET_KEY", "POSTGRES_PASSWORD"):
            secret = config(name, default="")
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value


class QuietRequests(logging.Filter):
    def filter(self, record):
        # RequestMiddleware logs HTTP once, including expected no-round responses.
        return not (record.name in {"django.request", "django.server", "aiogram.event"} and record.levelno < logging.ERROR)


class TrafficOnly(logging.Filter):
    def filter(self, record):
        return record.levelno < logging.ERROR


def prune_logs(directory, service, traffic_days=7, app_days=30, dry_run=False):
    removed = []
    for folder, days in ((directory, app_days), (directory / "traffic", traffic_days)):
        for path in folder.glob(f"{service}-*.jsonl*"):
            match = re.fullmatch(r"(web|bot)-(\d+)\.jsonl(?:\.\d+)?", path.name)
            if not match or path.is_symlink():
                continue
            try:
                if path.stat().st_mtime >= time.time() - days * 86400:
                    continue
            except FileNotFoundError:
                continue
            try:
                os.kill(int(match[2]), 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
            else:
                continue
            if not dry_run:
                path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


def logging_config(service):
    from decouple import config
    directory = Path(config("LOG_DIR", default=str(Path(__file__).resolve().parent.parent / "logs")))
    (directory / "traffic").mkdir(parents=True, exist_ok=True)
    prune_logs(directory, service,
               traffic_days=max(1, config("TRAFFIC_LOG_DAYS", default=7, cast=int)),
               app_days=max(1, config("APP_LOG_DAYS", default=30, cast=int)))
    # Per-process files avoid concurrent RotatingFileHandler writers in Gunicorn.
    def handler(path, size, backups):
        return {"class": "logging.handlers.RotatingFileHandler", "filename": str(path),
                "maxBytes": size, "backupCount": backups, "encoding": "utf-8", "formatter": "json"}
    return {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter, "service": service}},
        "filters": {"quiet": {"()": QuietRequests}, "traffic_only": {"()": TrafficOnly}},
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "json", "filters": ["quiet"]},
            "file": {**handler(directory / f"{service}-{os.getpid()}.jsonl", 10 * 1024**2, 5), "filters": ["quiet"]},
            "traffic": {**handler(directory / "traffic" / f"{service}-{os.getpid()}.jsonl",
                               config("TRAFFIC_LOG_MAX_BYTES", default=5 * 1024**2, cast=int),
                               config("TRAFFIC_LOG_BACKUPS", default=2, cast=int)), "filters": ["traffic_only"]},
        },
        "root": {"handlers": ["console", "file"], "level": "INFO"},
        "loggers": {
            "activity": {"handlers": ["traffic"], "level": "INFO", "propagate": False},
            "aiogram.event": {"handlers": ["traffic", "console", "file"], "level": "INFO", "propagate": False},
            "django.request": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "django.server": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "django": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        },
    }
