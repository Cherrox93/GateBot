# vault.py

from config import TelegramConfig, ExchangeConfig

TELEGRAM_SCALPER = TelegramConfig(
    token="8352544907:AAFCVw7t2mGpMPZ2Ik-TVmGAveniuL08kk4",
    chat_id="1709174569"
)

TELEGRAM_LOWCAP = TelegramConfig(
    token="8638878426:AAFvkClWyhATahO8KSjTlNnHOXcspqpA9bY",
    chat_id="1709174569"
)

TELEGRAM_PUMP = TelegramConfig(
    token="8782933984:AAGQdAFWHuCvsnOXKvTOVf53J9TUvUmWZsQ",
    chat_id="1709174569"
)

EXCHANGE = ExchangeConfig(
    api_key="TWOJ_GATE_API_KEY",
    secret_key="TWOJ_GATE_SECRET"
)
