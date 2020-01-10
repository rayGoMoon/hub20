from django.conf import settings

DEFAULT_TRACKED_ETHEREUM_TOKENS = ["ETH", "WETH", "DAI", "BAT", "RDN"]
DEFAULT_TRACKED_FIAT_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CNY", "BRL", "RUB", "INR"]


TRACKED_FIAT_CURRENCIES = getattr(
    settings, "ETHEREUM_MONEY_TRACKED_FIAT_CURRENCIES", DEFAULT_TRACKED_FIAT_CURRENCIES
)
TRACKED_TOKENS = getattr(
    settings, "ETHEREUM_MONEY_TRACKED_TOKENS", DEFAULT_TRACKED_ETHEREUM_TOKENS
)

TRANSFER_GAS_LIMIT = getattr(settings, "ETHEREUM_MONEY_TRANSFER_GAS_LIMIT", 200_000)
