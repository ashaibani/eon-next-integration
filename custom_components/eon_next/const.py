# Constants for the E.ON Next integration.

DOMAIN = "eon_next"

GRAPHQL_URL = "https://api.eonnext-kraken.energy/v1/graphql/"
REQUEST_TIMEOUT_SECONDS = 30

# Meter types
METER_TYPE_GAS = "gas"
METER_TYPE_ELECTRIC = "electricity"
METER_TYPE_UNKNOWN = "unknown"

# Refresh gates (seconds). The client-side gates inside eonnext.py and the
# coordinator intervals below intentionally pair up so each coordinator cycle
# maps to at most one API call per domain.
BALANCE_REFRESH_SECONDS = 900
TARIFF_REFRESH_SECONDS = 21600
RATES_REFRESH_SECONDS = 3600
PREPAY_REFRESH_SECONDS = 300
READINGS_INTERVAL_SECONDS = 3600

# Limits on what the user can configure via the options flow
MIN_BALANCE_REFRESH_MINUTES = 10
MIN_RATES_REFRESH_MINUTES = 30
MIN_PREPAY_REFRESH_MINUTES = 5

# Reading history kept for per-day usage estimates
READING_HISTORY_DAYS = 8

# Smart prepay sanity limits
PREPAY_STALE_AFTER_HOURS = 48
PREPAY_MAX_SNAPSHOT_MISSES = 24

# Defaults for user-configurable behaviour
DEFAULT_GAS_CALORIFIC_VALUE = 38.0
DEFAULT_LOW_CREDIT_PENCE = 200

# Options flow keys
OPTION_SHOW_BALANCE = "show_balance"
OPTION_SHOW_RATES = "show_rates"
OPTION_SHOW_USAGE = "show_usage"
OPTION_SHOW_PREPAY = "show_prepay"
OPTION_LOW_CREDIT_PENCE = "low_credit_pence"
OPTION_GAS_CALORIFIC_VALUE = "gas_calorific_value"
OPTION_BALANCE_MINUTES = "balance_refresh_minutes"
OPTION_RATES_MINUTES = "rates_refresh_minutes"
OPTION_PREPAY_MINUTES = "prepay_refresh_minutes"
