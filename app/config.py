"""应用配置：通过 .env 读取，所有配置项都集中此处。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有可配项都在这里声明；新增配置请按章节分组。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ===== 通用 =====
    wiki_data_dir: Path = Field(default=Path("C:/Users/Administrator/research-wiki/data"))
    wiki_db_path: Path = Field(default=Path("C:/Users/Administrator/research-wiki/data/wiki.sqlite"))
    wiki_host: str = "127.0.0.1"
    wiki_port: int = 5000
    wiki_admin_password: str = ""
    wiki_log_level: str = "INFO"

    # ===== LLM =====
    llm_provider: str = "claude"           # claude | openai_compat | minimax | zhipu
    llm_default_model: str = "claude-3-5-sonnet-latest"
    llm_fallback_model: str = "claude-3-haiku-20240307"

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"

    # OpenAI 兼容（DeepSeek / 通义 / Kimi 都吃这个）
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # 智谱 GLM（与 Igshy 主仓默认一致）
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    zhipu_model: str = "glm-4.6"

    # ===== PDF 解析 =====
    wiki_ocr_text_layer_threshold: int = 30
    wiki_page_text_max_chars: int = 20_000
    wiki_enable_ocr: bool = True
    wiki_ocr_engine: str = "paddleocr"  # paddleocr | tesseract
    wiki_ocr_lang: str = "ch"

    # ===== 检索 =====
    wiki_fts_candidate_limit: int = 30
    wiki_retrieval_final_k: int = 8
    wiki_enable_embedding: bool = False
    embedding_model: str = "BAAI/bge-small-zh-v1.5"

    # ===== 审校 =====
    wiki_audit_cron_hour: int = 2
    wiki_audit_cron_minute: int = 30
    wiki_audit_isolated_days: int = 90

    # ===== 派生 =====
    @property
    def sqlite_url(self) -> str:
        return f"sqlite:///{self.wiki_db_path.as_posix()}"

    def ensure_dirs(self) -> None:
        """保证所有数据目录存在。"""
        for p in [
            self.wiki_data_dir,
            self.wiki_data_dir / "reports",
            self.wiki_data_dir / "pdf_cache",
            self.wiki_data_dir / "exports",
            self.wiki_db_path.parent,
        ]:
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


# 兼容旧代码里 `from app.config import settings`
settings = get_settings()
