"""Runtime settings, read once from the environment (.env supported)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'shk.db'}"))
    data_dir: Path = BASE_DIR / "data"

    # Groq (LLM provider)
    llm_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "openai/gpt-oss-120b"))
    # Reasoning effort for gpt-oss/qwen models: low | medium | high
    llm_effort: str = field(default_factory=lambda: _env("GROQ_REASONING_EFFORT", "medium"))

    # Signs the customer-portal login cookie. Set a long random value in production.
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me"))

    # Staff dashboard login (HTTP basic auth)
    dashboard_user: str = field(default_factory=lambda: _env("DASHBOARD_USER", "admin"))
    dashboard_password: str = field(default_factory=lambda: _env("DASHBOARD_PASSWORD", "change-me"))
    # Websites allowed to embed the chat widget.
    allowed_origins: list[str] = field(default_factory=lambda: _env_list("ALLOWED_ORIGINS")
                                       or ["https://shkraipur.com", "https://www.shkraipur.com"])
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000"))

    # Email (SMTP) for notifications. Blank host = dry-run: messages only go to the portal inbox.
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM", "sales@shkraipur.com"))

    # NVIDIA Nemotron OCR (NIM). Full endpoint URL, e.g. http://<gpu-server>:8000/v1/infer for a self-hosted
    # nemotron-ocr-v1 NIM (v2 uses /v1/ocr). Blank = OCR off; staff type the fields in by hand.
    ocr_url: str = field(default_factory=lambda: _env("NEMOTRON_OCR_URL"))
    # API key for NVIDIA-hosted endpoints (sent as "Authorization: Bearer ..."); blank for self-hosted.
    ocr_api_key: str = field(default_factory=lambda: _env("NEMOTRON_OCR_API_KEY"))
    # Optional merge level sent to the NIM: word | sentence | paragraph. Blank = server default.
    ocr_merge_level: str = field(default_factory=lambda: _env("NEMOTRON_OCR_MERGE_LEVEL"))

    # Business rules
    gst_rate: float = field(default_factory=lambda: _env_float("GST_RATE", 0.18))
    # Quotes above this value (INR, before GST) or with any discount need a manager's approval.
    quote_auto_limit: float = field(default_factory=lambda: _env_float("QUOTE_AUTO_LIMIT_INR", 5_000_000))
    # Customer is "lapsed" once days since last order exceed this multiple of their usual cycle.
    reorder_lapse_factor: float = field(default_factory=lambda: _env_float("REORDER_LAPSE_FACTOR", 1.5))
    # Directors who receive the 08:00 brief by email.
    management_emails: list[str] = field(default_factory=lambda: _env_list("MANAGEMENT_EMAILS"))
    company_name: str = "Shri Hare-Krishna Sponge Iron Ltd"
    company_address: str = "Phase-II, Plot No.106, Industrial Growth Centre, Siltara, Raipur 493111, Chhattisgarh"
    company_phone: str = "0771-264984"

    @property
    def llm_enabled(self) -> bool:
        return bool(_env("GROQ_API_KEY"))

    @property
    def ocr_enabled(self) -> bool:
        return bool(self.ocr_url)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host)


settings = Settings()
