from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JTDT_", env_file=".env", extra="ignore")

    app_name: str = "Jason Tools 文件工具箱"
    host: str = "127.0.0.1"
    port: int = 8765
    debug: bool = False

    project_root: Path = Path(__file__).resolve().parent.parent
    data_dir: Path = Path(__file__).resolve().parent.parent / "data"

    # 清理迴圈多久跑一次（`main._sweep_temp_files_loop`）。
    #
    # **保留多久不在這裡設** —— 暫存檔與作業結果檔的保留期在管理頁「檔案保留 /
    # 清理」（`data/retention.json`，`retention.get()`）。以前這裡另有
    # `job_ttl_seconds`（6 小時）與 `temp_ttl_seconds`（2 小時）兩個期限，
    # 跟管理頁的設定對不上，而且最後都沒有任何程式在讀 —— 拿掉了，免得有人
    # 以為設環境變數改得動它們。
    cleanup_interval_seconds: int = 60 * 30

    default_paper_mm: tuple[float, float] = (210.0, 297.0)

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def assets_files_dir(self) -> Path:
        return self.data_dir / "assets" / "files"

    @property
    def assets_meta_path(self) -> Path:
        return self.data_dir / "assets" / "assets.json"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def fonts_dir(self) -> Path:
        return self.data_dir / "fonts"

    @property
    def branding_dir(self) -> Path:
        return self.data_dir / "branding"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.assets_dir, self.assets_files_dir,
                  self.jobs_dir, self.temp_dir, self.fonts_dir,
                  self.branding_dir, self.workspace_dir):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
