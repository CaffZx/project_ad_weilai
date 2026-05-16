import tomllib
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "app" / "config"


def load_toml(filename: str) -> dict:
    """加载 TOML 配置文件，失败时带文件名和行号"""
    filepath = CONFIG_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"配置文件不存在: {filepath}")
    try:
        with open(filepath, "rb") as f:
            data = tomllib.load(f)
        if data is None:
            raise ValueError(f"配置文件为空: {filepath}")
        return data
    except tomllib.TOMLDecodeError as e:
        lineno = getattr(e, "lineno", None)
        line_info = f" (第 {lineno} 行)" if lineno else ""
        raise ValueError(f"TOML 格式错误 [{filename}{line_info}]: {e}")


class Settings(BaseSettings):
    """应用配置 — 敏感值从 .env 文件/环境变量读取"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "广告方向决策子智能体"
    app_version: str = "0.1.0"
    debug: bool = True

    host: str = "0.0.0.0"
    port: int = 8000

    data_source: str = "db"

    # CSV适配器配置
    csv_filename: str = "asin_test_data.xlsx"
    csv_key_column: str = "asin"
    request_timeout: float = 5.0

    # 数据库配置（Doris/StarRocks）— 从环境变量读取，此处仅占位，无 .env 不工作
    db_host: str = ""
    db_port: int = 0
    db_user: str = ""
    db_pass: str = ""
    db_database: str = ""

    # 数据源目录
    data_source_dir: str = "data/source"
    # 持久化目录
    persistence_dir: str = "config"

    # LLM 配置 — DeepSeek API — 从环境变量读取
    deepseek_api_key: str = ""
    deepseek_api_keys: str = ""  # 多 Key 逗号分隔，优先于 deepseek_api_key
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    llm_timeout: int = 60

    # 原始配置数据（启动时加载）
    _raw_tags: dict | None = None
    _raw_thresholds: dict | None = None
    _raw_layer_options: dict | None = None

    def _ensure_loaded(self):
        if self._raw_tags is None or self._raw_thresholds is None:
            self.reload_config()

    def reload_config(self):
        """重新加载 TOML 配置（开发/运维调整后调用）"""
        self._raw_tags = load_toml("tags.toml")
        self._raw_thresholds = load_toml("thresholds.toml")
        try:
            self._raw_layer_options = load_toml("layer_options.toml")
        except FileNotFoundError:
            self._raw_layer_options = None

    @property
    def llm_config(self) -> dict:
        # 多 Key：优先从 api_keys.txt 读取（每行一个），其次逗号分隔，兜底单 Key
        keys = []
        keys_file = BASE_DIR / "api_keys.txt"
        if keys_file.exists():
            keys = [line.strip() for line in keys_file.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        elif self.deepseek_api_keys:
            keys = [k.strip() for k in self.deepseek_api_keys.split(",") if k.strip()]
        elif self.deepseek_api_key:
            keys = [self.deepseek_api_key]
        return {
            "api_key": self.deepseek_api_key,
            "api_keys": keys,
            "base_url": self.deepseek_base_url,
            "model": self.deepseek_model,
            "timeout": self.llm_timeout,
        }

    @property
    def tags_config(self) -> dict:
        self._ensure_loaded()
        return self._raw_tags

    @property
    def thresholds_config(self) -> dict:
        self._ensure_loaded()
        return self._raw_thresholds

    @property
    def layer_options_config(self) -> dict | None:
        self._ensure_loaded()
        return self._raw_layer_options


settings = Settings()
