from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Digital Wallet & Payments System"
    database_url: str
    secret_key: str
    access_token_expire_minutes: int = 30
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    admin_email: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
