# E.ON Next integration client for Home Assistant.
# Derivative work originally based on madmachinations/eon-next, with
# extensive changes: coordinator architecture, prepay snapshot support,
# tariff/rate windows, balance handling and error hardening.
# Original project: https://github.com/madmachinations/eon-next
#!/usr/bin/env python3

import asyncio
import aiohttp
import datetime
import logging

try:
    from .const import (
        GRAPHQL_URL,
        REQUEST_TIMEOUT_SECONDS,
        METER_TYPE_GAS,
        METER_TYPE_ELECTRIC,
        METER_TYPE_UNKNOWN,
        READING_HISTORY_DAYS,
        DEFAULT_GAS_CALORIFIC_VALUE,
        DEFAULT_LOW_CREDIT_PENCE,
    )
except ImportError:  # direct import for the offline test suite
    from const import (
        GRAPHQL_URL,
        REQUEST_TIMEOUT_SECONDS,
        METER_TYPE_GAS,
        METER_TYPE_ELECTRIC,
        METER_TYPE_UNKNOWN,
        READING_HISTORY_DAYS,
        DEFAULT_GAS_CALORIFIC_VALUE,
        DEFAULT_LOW_CREDIT_PENCE,
    )

_LOGGER = logging.getLogger(__name__)


class EonNextApiError(Exception):
    """Raised when the Eon Next API cannot be reached or returns an unusable response."""


# Meter discovery + prepay ledgers + smart device ids in one round trip.
# On smart prepay accounts the canonical balance shown in the E.ON Next app
# comes from prepayBalanceSnapshot(deviceId), keyed by the smart device id
# hanging off the meter - so we collect both ledgers and device ids here.
SELECTOR_QUERY = (
    "query getAccountMeterSelector($accountNumber: String!, $showInactive: Boolean!) {\n"
    "  properties(accountNumber: $accountNumber) {\n"
    "    id\n"
    "    postcode\n"
    "    electricityMeterPoints {\n"
    "      id\n"
    "      mpan\n"
    "      meters(includeInactive: $showInactive) {\n"
    "        id\n"
    "        serialNumber\n"
    "        activeTo\n"
    "        isTradPrepay\n"
    "        prepayLedgers {\n"
    "          creditLedger {\n"
    "            currentBalance\n"
    "          }\n"
    "          debtLedger {\n"
    "            currentBalance\n"
    "          }\n"
    "        }\n"
    "        smartDevices {\n"
    "          __typename\n"
    "          ... on SmartMeterDeviceType {\n"
    "            deviceId\n"
    "            paymentMode\n"
    "          }\n"
    "        }\n"
    "        registers {\n"
    "          id\n"
    "          name\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "    gasMeterPoints {\n"
    "      id\n"
    "      mprn\n"
    "      meters(includeInactive: $showInactive) {\n"
    "        id\n"
    "        serialNumber\n"
    "        activeTo\n"
    "        isTradPrepay\n"
    "        prepayLedgers {\n"
    "          creditLedger {\n"
    "            currentBalance\n"
    "          }\n"
    "          debtLedger {\n"
    "            currentBalance\n"
    "          }\n"
    "        }\n"
    "        smartDevices {\n"
    "          __typename\n"
    "          ... on SmartMeterDeviceType {\n"
    "            deviceId\n"
    "            paymentMode\n"
    "          }\n"
    "        }\n"
    "        registers {\n"
    "          id\n"
    "          name\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)

# This is the exact query the E.ON Next dashboard issues for the balance gauge.
PREPAY_SNAPSHOT_QUERY = (
    "query balanceForDevice($deviceId: String!) {\n"
    "  prepayBalanceSnapshot(deviceId: $deviceId) {\n"
    "    asAt\n"
    "    creditInPence\n"
    "    debtInPence\n"
    "    emergencyCreditInPence\n"
    "    meterBalanceInMillipence\n"
    "    __typename\n"
    "  }\n"
    "}"
)


# Live unit rates for the meter point, with the exact validity windows.
# Resolving the window containing "now" gives the price the Energy
# dashboard needs for cost tracking.
RATES_QUERY = (
    "query ApplicableRates($accountNumber: String!, $mpxn: String!, $startAt: DateTime!, $endAt: DateTime!) {\n"
    "  applicableRates(accountNumber: $accountNumber, mpxn: $mpxn, startAt: $startAt, endAt: $endAt, first: 50) {\n"
    "    edges {\n"
    "      node {\n"
    "        value\n"
    "        validFrom\n"
    "        validTo\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)

# Tariff details for the meter point's current agreement. The union must be
# decomposed with fragments on the concrete tariff types.
TARIFF_QUERY = (
    "query AccountTariff {\n"
    "  viewer {\n"
    "    accounts {\n"
    "      ... on AccountType {\n"
    "        number\n"
    "        properties {\n"
    "          electricityMeterPoints {\n"
    "            mpan\n"
    "            agreements {\n"
    "              id\n"
    "              validFrom\n"
    "              validTo\n"
    "              tariff {\n"
    "                __typename\n"
    "                ... on DayNightTariff { displayName fullName productCode tariffCode dayRate nightRate standingCharge }\n"
    "                ... on StandardTariff { displayName fullName productCode tariffCode unitRate standingCharge }\n"
    "                ... on PrepayTariff { displayName productCode tariffCode unitRate standingCharge }\n"
    "              }\n"
    "            }\n"
    "          }\n"
    "          gasMeterPoints {\n"
    "            mprn\n"
    "            agreements {\n"
    "              id\n"
    "              validFrom\n"
    "              validTo\n"
    "              tariff {\n"
    "                __typename\n"
    "                ... on GasTariffType { displayName fullName productCode tariffCode unitRate standingCharge }\n"
    "              }\n"
    "            }\n"
    "          }\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)


class EonNext:

    def __init__(self):
        self.username = ""
        self.password = ""
        self._session = None
        self._auth_lock = asyncio.Lock()
        self._last_balance_fetch = None
        self.__reset_authentation()
        self.__reset_accounts()
    

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and self._session.closed == False:
            await self._session.close()
    

    def _json_contains_key_chain(self, data: dict, key_chain: list) -> bool:
        for key in key_chain:
            if key in data:
                data = data[key]
            else:
                return False
        return True
    

    def __current_timestamp(self) -> int:
        now = datetime.datetime.now()
        return int(datetime.datetime.timestamp(now))


    def __reset_authentation(self):
        self.auth = {
            "issued": None,
            "token": {
                "token": None,
                "expires": None
            },
            "refresh": {
                "token": None,
                "expires": None
            }
        }
    
    def __store_authentication(self, kraken_token: dict):
        self.auth = {
            "issued": kraken_token['payload']['iat'],
            "token": {
                "token": kraken_token['token'],
                "expires": kraken_token['payload']['exp']
            },
            "refresh": {
                "token": kraken_token['refreshToken'],
                "expires": kraken_token['refreshExpiresIn']
            }
        }
    

    def __auth_token_is_valid(self) -> bool:
        if self.auth['token']['token'] == None:
            return False
        
        if self.auth['token']['expires'] <= self.__current_timestamp():
            return False
        
        return True
    

    def __refresh_token_is_valid(self) -> bool:
        if self.auth['refresh']['token'] == None:
            return False
        
        if self.auth['refresh']['expires'] <= self.__current_timestamp():
            return False
        
        return True
    

    async def __auth_token(self) -> str:
        async with self._auth_lock:
            return await self.__auth_token_locked()
    
    async def __auth_token_locked(self) -> str:
        if self.__auth_token_is_valid() == False:
            if self.__refresh_token_is_valid() == True:
                await self.__login_with_refresh_token()
            elif self.username != "" and self.password != "":
                await self.login_with_username_and_password(self.username, self.password, False)
        
        if self.__auth_token_is_valid() == False:
            raise EonNextApiError("Unable to authenticate")

        return self.auth['token']['token']
    

    async def _graphql_post(self, operation: str, query: str, variables: dict={}, authenticated: bool = True) -> dict:
        use_headers = {}

        if authenticated == True:
            use_headers['authorization'] = "JWT " + await self.__auth_token()

        if self._session is None or self._session.closed == True:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS))

        try:
            async with self._session.post(
                GRAPHQL_URL,
                json={"operationName": operation, "variables": variables, "query": query},
                headers=use_headers
            ) as response:
                if response.status != 200:
                    _LOGGER.debug("%s returned HTTP %s", operation, response.status)
                    raise EonNextApiError(operation + " returned HTTP " + str(response.status))

                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise EonNextApiError(operation + " returned a malformed response") from exc
        except aiohttp.ClientError as exc:
            raise EonNextApiError(operation + " request failed: " + str(exc)) from exc
    

    async def login_with_username_and_password(self, username: str, password: str, initialise: bool = True) -> bool:
        self.username = username
        self.password = password
        
        result = await self._graphql_post(
            "loginEmailAuthentication",
            "mutation loginEmailAuthentication($input: ObtainJSONWebTokenInput!) {obtainKrakenToken(input: $input) {    payload    refreshExpiresIn    refreshToken    token    __typename}}",
            {
                "input": {
                    "email": self.username,
                    "password": self.password
                }
            },
            False
        )

        if self._json_contains_key_chain(result, ["data", "obtainKrakenToken", "token"]) == True:
            self.__store_authentication(result['data']['obtainKrakenToken'])
            if initialise == True:
                await self.__init_accounts()
            return True
        else:
            self.__reset_authentation()
            return False
    

    async def login_with_refresh_token(self, token: str) -> bool:
        self.auth['refresh']['token'] = token
        return await self.__login_with_refresh_token(True)
    

    async def __login_with_refresh_token(self, initialise: bool = False) -> bool:
        result = await self._graphql_post(
            "refreshToken",
            "mutation refreshToken($input: ObtainJSONWebTokenInput!) {  obtainKrakenToken(input: $input) {    payload    refreshExpiresIn    refreshToken    token    __typename  }}",
            {
                "input": {
                    "refreshToken": self.auth['refresh']['token']
                }
            },
            False
        )

        if self._json_contains_key_chain(result, ["data", "obtainKrakenToken", "token"]) == True:
            self.__store_authentication(result['data']['obtainKrakenToken'])
            if initialise == True:
                await self.__init_accounts()
            return True
        else:
            self.__reset_authentation()
            return False
    

    def __reset_accounts(self):
        self.accounts = []
    

    async def __get_account_numbers(self) -> list:
        result = await self._graphql_post(
            "headerGetLoggedInUser",
            "query headerGetLoggedInUser {\n  viewer {\n    accounts {\n      ... on AccountType {\n        applications(first: 1) {\n          edges {\n            node {\n              isMigrated\n              migrationSource\n              __typename\n            }\n            __typename\n          }\n          __typename\n        }\n        balance\n        id\n        number\n        __typename\n      }\n      __typename\n    }\n    id\n    preferredName\n    __typename\n  }\n}\n"
        )
        
        if self._json_contains_key_chain(result, ["data", "viewer", "accounts"]) == False:
            raise EonNextApiError("Unable to load energy accounts")

        found = []
        for account_entry in result['data']['viewer']['accounts']:
            found.append({
                "number": account_entry['number'],
                "balance": account_entry.get('balance')
            })

        return found
    

    async def _fetch_meter_properties(self, account_number: str, show_inactive: bool = False) -> list:
        """Fetch the meter structure (prepay ledgers + smart device ids) for an account."""
        result = await self._graphql_post(
            "getAccountMeterSelector",
            SELECTOR_QUERY,
            {
                "accountNumber": account_number,
                "showInactive": show_inactive
            }
        )

        if self._json_contains_key_chain(result, ["data", "properties"]) == False:
            raise EonNextApiError("Unable to load energy meters for account " + account_number)

        return result['data']['properties']
    

    async def fetch_applicable_rates(self, account_number: str, mpxn: str, start_at: str, end_at: str) -> list:
        """Live unit-rate windows for a meter point: [{value, validFrom, validTo}].
        value is a decimal string in pence per kWh."""
        result = await self._graphql_post(
            "ApplicableRates",
            RATES_QUERY,
            {
                "accountNumber": account_number,
                "mpxn": mpxn,
                "startAt": start_at,
                "endAt": end_at
            }
        )

        if self._json_contains_key_chain(result, ["data", "applicableRates", "edges"]) == False:
            _LOGGER.debug("No applicable rates for %s", mpxn)
            return []

        windows = []
        for edge in result['data']['applicableRates']['edges']:
            node = edge.get('node') or {}
            if node.get('value') == None or node.get('validFrom') == None or node.get('validTo') == None:
                continue
            windows.append({
                "pence": float(node['value']),
                "from": node['validFrom'],
                "to": node['validTo']
            })

        windows.sort(key=lambda w: w["from"])
        return windows
    

    async def fetch_account_tariff(self, account_number: str) -> dict:
        """Tariff + agreement details for the electricity and gas points of an
        account. Returns {"electricity": {...}, "gas": {...}} or partial."""
        result = await self._graphql_post("AccountTariff", TARIFF_QUERY)

        if self._json_contains_key_chain(result, ["data", "viewer", "accounts"]) == False:
            _LOGGER.debug("Unable to load tariff details")
            return {}

        out = {}
        for account in result['data']['viewer']['accounts']:
            if account.get('number') != account_number:
                continue
            for property in (account.get('properties') or []):
                for point in (property.get('electricityMeterPoints') or []):
                    out["electricity"] = self.__tariff_from_point(point, "mpan")
                for point in (property.get('gasMeterPoints') or []):
                    out["gas"] = self.__tariff_from_point(point, "mprn")
        
        return out
    

    def __tariff_from_point(self, point: dict, id_key: str) -> dict:
        """Extract the current agreement's tariff fields from a meter point."""
        info = {id_key: point.get(id_key)}

        agreements = point.get('agreements') or []

        current = None
        for agreement in agreements:
            if agreement.get('validTo') == None:
                current = agreement
                break
        if current == None and len(agreements) > 0:
            current = agreements[0]

        if current == None:
            return info

        info["agreement_id"] = current.get('id')
        info["agreement_from"] = current.get('validFrom')
        info["agreement_to"] = current.get('validTo')

        tariff = current.get('tariff') or {}
        for field in ("__typename", "displayName", "fullName", "productCode", "tariffCode",
                      "unitRate", "standingCharge", "dayRate", "nightRate"):
            if tariff.get(field) != None:
                info[field] = tariff.get(field)
        
        return info
    

    async def fetch_prepay_balance_snapshot(self, device_id: str):
        """The dashboard's own balance query. Returns the snapshot dict or None.
        creditInPence/debtInPence are pence; asAt is the snapshot timestamp."""
        result = await self._graphql_post(
            "balanceForDevice",
            PREPAY_SNAPSHOT_QUERY,
            {
                "deviceId": device_id
            }
        )

        if self._json_contains_key_chain(result, ["data", "prepayBalanceSnapshot"]) == False:
            _LOGGER.debug("No prepay snapshot for device %s", device_id)
            return None

        return result['data']['prepayBalanceSnapshot']
    

    async def __init_accounts(self):
        self.__reset_accounts()

        for account_entry in await self.__get_account_numbers():

            account = EnergyAccount(self, account_entry['number'])
            account.set_balance(account_entry.get('balance'))
            await account._load_meters()

            self.accounts.append(account)
    

    BALANCE_REFRESH_SECONDS = 900

    async def refresh_account_balances(self, force: bool = False) -> None:
        """Re-fetch the account ledger balance for every loaded account. The
        15-minute gate is skipped when forced (coordinator-driven cycles)."""
        if len(self.accounts) == 0:
            return

        now = datetime.datetime.now()

        if force == False and self._last_balance_fetch != None:
            elapsed = (now - self._last_balance_fetch).total_seconds()
            if elapsed < self.BALANCE_REFRESH_SECONDS:
                return

        balances_by_number = {}
        for account_entry in await self.__get_account_numbers():
            balances_by_number[account_entry['number']] = account_entry.get('balance')

        _LOGGER.debug("Fetched account balances: %s", balances_by_number)

        for account in self.accounts:
            if account.account_number in balances_by_number:
                account.set_balance(balances_by_number[account.account_number])

        self._last_balance_fetch = now



class EnergyAccount:

    def __init__(self, api: EonNext, account_number: str):
        self.api = api
        self.account_number = account_number
        self.balance = None
        self.meters = []
        self._last_prepay_fetch = None

        self.electricity_mpan = None
        self.gas_mprn = None
        self.tariff = {}
        self.rate_windows = []
        self._last_tariff_fetch = None
        self._last_rates_fetch = None

        self.gas_calorific_value = DEFAULT_GAS_CALORIFIC_VALUE
        self.low_credit_pence = DEFAULT_LOW_CREDIT_PENCE
    

    def apply_options(self, options: dict) -> None:
        """Apply user-configurable options (options flow values)."""
        if options == None:
            return
        
        if options.get("gas_calorific_value") != None:
            try:
                self.gas_calorific_value = float(options["gas_calorific_value"])
            except (TypeError, ValueError):
                pass
        
        if options.get("low_credit_pence") != None:
            try:
                self.low_credit_pence = int(options["low_credit_pence"])
            except (TypeError, ValueError):
                pass
    

    def set_balance(self, balance) -> None:
        self.balance = balance


    def get_balance_amount(self):
        """Best-effort account balance in pounds, or None.
        Kraken ledger balances are integers in pence."""
        if self.balance == None or isinstance(self.balance, bool) or self.balance == "":
            return None
        
        if isinstance(self.balance, dict):
            for key in ("formatted", "amount", "value"):
                value = self.balance.get(key)
                if value != None:
                    parsed = self.__amount_from_value(value)
                    if parsed != None:
                        return parsed
            return None
        
        return self.__amount_from_value(self.balance)
    

    async def refresh_balance(self, force: bool = False) -> None:
        """Re-fetch this account's ledger balance via the API."""
        await self.api.refresh_account_balances(force=force)
    

    async def refresh_readings(self, force: bool = False) -> None:
        """Refresh every meter's cumulative register. Meters gate themselves
        to one real fetch per day (after 7am) via _should_update()."""
        for meter in self.meters:
            if force == True:
                await meter._update()
                meter.last_updated = datetime.datetime.now()
            else:
                await meter.update()
    

    def __amount_from_value(self, value):
        """Convert an API value into pounds. Integers are pence (the Kraken
        convention for ledger balances); floats and strings are treated as
        already-formatted pounds."""
        if isinstance(value, bool) or value == None:
            return None
        
        if isinstance(value, int):
            return round(value / 100.0, 2)
        
        if isinstance(value, float):
            return round(value, 2)
        
        if isinstance(value, str):
            cleaned = ""
            for char in value:
                if char.isdigit() or char in ["-", ".", ","]:
                    cleaned = cleaned + char
            
            cleaned = cleaned.replace(",", "")
            if cleaned in ["", "-", ".", "-."]:
                return None
            
            try:
                return round(float(cleaned), 2)
            except ValueError:
                return None
        
        return None
    

    async def _load_meters(self):
        await self.refresh_prepay_balances(force=True)

    PREPAY_REFRESH_SECONDS = 300
    TARIFF_REFRESH_SECONDS = 21600
    RATES_REFRESH_SECONDS = 3600

    async def refresh_tariff(self, force: bool = False) -> None:
        """Fetch this account's tariff details, at most every 6 hours."""
        now = datetime.datetime.now()

        if force == False and self._last_tariff_fetch != None:
            elapsed = (now - self._last_tariff_fetch).total_seconds()
            if elapsed < self.TARIFF_REFRESH_SECONDS:
                return

        self.tariff = await self.api.fetch_account_tariff(self.account_number)
        self._last_tariff_fetch = now
    

    async def refresh_rates(self, force: bool = False) -> None:
        """Fetch the live unit-rate windows for the electricity point, at most
        every hour. Windows are UTC ISO strings with identical formats, so
        ISO string ordering is a safe comparison key."""
        if self.electricity_mpan == None:
            return

        now = datetime.datetime.now()

        if force == False and self._last_rates_fetch != None:
            elapsed = (now - self._last_rates_fetch).total_seconds()
            if elapsed < self.RATES_REFRESH_SECONDS:
                return

        tz = datetime.timezone.utc
        day_start = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00+00:00")
        day_end = (now + datetime.timedelta(days=2)).strftime("%Y-%m-%dT00:00:00+00:00")

        self.rate_windows = await self.api.fetch_applicable_rates(
            self.account_number, self.electricity_mpan, day_start, day_end)

        if len(self.rate_windows) > 0:
            self._last_rates_fetch = now
    

    def get_blended_rate_pence_for_date(self, target_date):
        """Duration-weighted average unit rate (pence/kWh) across the windows
        intersecting a UTC calendar day. None when no window data covers it.
        Used for estimated daily cost on prepay/day-night tariffs."""
        if len(self.rate_windows) == 0 or target_date == None:
            return None
        
        day_start = datetime.datetime(target_date.year, target_date.month, target_date.day, tzinfo=datetime.timezone.utc)
        day_end = day_start + datetime.timedelta(days=1)

        weighted = 0.0
        hours = 0.0

        for window in self.rate_windows:
            try:
                wf = datetime.datetime.fromisoformat(str(window["from"]))
                wt = None
                if window.get("to") != None:
                    wt = datetime.datetime.fromisoformat(str(window["to"]))
            except ValueError:
                continue
            
            if wf.tzinfo == None:
                wf = wf.replace(tzinfo=datetime.timezone.utc)
            if wt != None and wt.tzinfo == None:
                wt = wt.replace(tzinfo=datetime.timezone.utc)
            
            start = max(wf, day_start)
            end = day_end
            if wt != None:
                end = min(wt, day_end)
            
            if end <= start:
                continue
            
            span = (end - start).total_seconds() / 3600.0
            weighted += window["pence"] * span
            hours += span
        
        if hours == 0:
            return None
        
        return round(weighted / hours, 4)
    

    def get_merged_rate_windows(self):
        """Rate windows merged into contiguous same-rate blocks suitable for
        calendar events: [{"pence", "from", "to"}]."""
        if len(self.rate_windows) == 0:
            return []

        merged = []
        for window in self.rate_windows:
            if len(merged) > 0 and merged[-1]["pence"] == window["pence"] and merged[-1]["to"] == window["from"]:
                merged[-1]["to"] = window["to"]
            else:
                merged.append({
                    "pence": window["pence"],
                    "from": window["from"],
                    "to": window["to"]
                })
        
        return merged
    

    def get_current_rate_info(self):
        """The rate window containing "now": the contiguous same-rate block
        around it (start/end when the rate changes), the next change, and
        today's min/max/average. Follows the shape popularised by the
        Octopus Energy integration's rate_information helper.

        Returns {} when no window covers the current instant."""
        if len(self.rate_windows) == 0:
            return {}

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        current = None
        for window in self.rate_windows:
            if window["from"] <= now_iso and (window["to"] == None or now_iso < window["to"]):
                current = window
                break

        if current == None:
            return {}

        # Grow the block outwards from the current window across adjacent
        # windows with the same rate value (e.g. the actual day/night block).
        block_from = current["from"]
        block_to = current["to"]
        changed = True
        while changed == True:
            changed = False
            for window in self.rate_windows:
                if window["pence"] != current["pence"]:
                    continue
                
                if window["to"] != None and window["to"] == block_from:
                    block_from = window["from"]
                    changed = True
                
                if window["from"] == block_to:
                    block_to = window["to"]
                    changed = True

        upcoming = []
        for window in self.rate_windows:
            if block_to == None or window["from"] >= block_to:
                upcoming.append(window)
        upcoming.sort(key=lambda w: w["from"])

        info = {
            "pence": current["pence"],
            "from": current["from"],
            "to": current["to"],
            "block_from": block_from,
            "block_to": block_to,
        }

        if len(upcoming) > 0:
            info["next_pence"] = upcoming[0]["pence"]
            info["next_from"] = upcoming[0]["from"]

        # Spread across the current UTC day
        day_start = now_dt.strftime("%Y-%m-%dT00:00:00+00:00")
        day_end = (now_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00+00:00")

        values_today = []
        for window in self.rate_windows:
            if window["from"] < day_end and (window["to"] == None or window["to"] > day_start):
                values_today.append(window["pence"])

        if len(values_today) > 0:
            info["min_pence_today"] = min(values_today)
            info["max_pence_today"] = max(values_today)
            info["average_pence_today"] = round(sum(values_today) / len(values_today), 4)

        return info
    

    async def refresh_prepay_balances(self, force: bool = False) -> None:
        """Refresh meter configs (ledgers + device ids) and each smart device's
        prepay balance snapshot - the same data the E.ON Next dashboard shows.
        Throttled to at most every 5 minutes unless forced."""
        now = datetime.datetime.now()

        if force == False and self._last_prepay_fetch != None:
            elapsed = (now - self._last_prepay_fetch).total_seconds()
            if elapsed < self.PREPAY_REFRESH_SECONDS:
                return

        properties = await self.api._fetch_meter_properties(self.account_number, False)

        configs_by_id = {}
        for property in properties:
            for point in list(property['electricityMeterPoints']) + list(property['gasMeterPoints']):
                for meter_config in point['meters']:
                    configs_by_id[meter_config['id']] = meter_config

        if self.meters == []:
            # First load: construct the meters from the configs
            for property in properties:
                for point in property['electricityMeterPoints']:
                    if point.get('mpan') != None and self.electricity_mpan == None:
                        self.electricity_mpan = point['mpan']
                    for meter_config in point['meters']:
                        self.meters.append(ElectricityMeter(self, meter_config))
                for point in property['gasMeterPoints']:
                    if point.get('mprn') != None and self.gas_mprn == None:
                        self.gas_mprn = point['mprn']
                    for meter_config in point['meters']:
                        self.meters.append(GasMeter(self, meter_config))

        for meter in self.meters:
            config = configs_by_id.get(meter.meter_id)
            if config != None:
                meter.apply_meter_config(config)

            # The dashboard's canonical balance: snapshot per smart device
            for device_id in meter.device_ids:
                snapshot = await self.api.fetch_prepay_balance_snapshot(device_id)
                if snapshot != None:
                    meter.apply_prepay_snapshot(snapshot)
                    break

        self._last_prepay_fetch = now



class EnergyMeter:

    def __init__(self, account: EnergyAccount, meter_config: dict):
        self.account = account
        self.api = account.api

        self.last_updated = None

        self.type = METER_TYPE_UNKNOWN
        self.meter_id = meter_config['id']
        self.serial = meter_config['serialNumber']

        self.prepay_credit_pence = None
        self.prepay_debt_pence = None

        # Dashboard snapshot values (prepayBalanceSnapshot)
        self._had_prepay_snapshot = False
        self.snapshot_credit_pence = None
        self.snapshot_debt_pence = None
        self.snapshot_emergency_pence = None
        self.snapshot_meter_balance_pence = None
        self.snapshot_as_at = None

        # Smart devices (SmartMeterDeviceType) bound to this meter
        self.device_ids = []
        self.payment_mode = None

        self.latest_reading = None
        self.latest_reading_date = None

        # Recent daily register snapshots: [{"date", "reading"}] oldest first
        self.reading_history = []

        self.apply_meter_config(meter_config)
    

    def apply_meter_config(self, meter_config: dict) -> None:
        """Apply a meter selector config: prepay ledgers + smart device ids."""
        self.is_trad_prepay = meter_config.get('isTradPrepay') == True

        prepay = meter_config.get('prepayLedgers')
        if prepay != None:
            credit = prepay.get('creditLedger')
            debt = prepay.get('debtLedger')
            
            if credit != None and credit.get('currentBalance') != None:
                self.prepay_credit_pence = credit.get('currentBalance')
            
            if debt != None and debt.get('currentBalance') != None:
                self.prepay_debt_pence = debt.get('currentBalance')
        
        for device in (meter_config.get('smartDevices') or []):
            if device.get('__typename') != "SmartMeterDeviceType":
                continue
            if device.get('deviceId') != None and device['deviceId'] not in self.device_ids:
                self.device_ids.append(device['deviceId'])
                if device.get('paymentMode') != None:
                    self.payment_mode = device.get('paymentMode')
    

    def apply_prepay_snapshot(self, snapshot: dict) -> None:
        """Store prepayBalanceSnapshot fields (pence; asAt ISO timestamp)."""
        if snapshot.get('creditInPence') != None:
            self.snapshot_credit_pence = snapshot.get('creditInPence')
        
        if snapshot.get('debtInPence') != None:
            self.snapshot_debt_pence = snapshot.get('debtInPence')
        
        if snapshot.get('emergencyCreditInPence') != None:
            self.snapshot_emergency_pence = snapshot.get('emergencyCreditInPence')
        
        millipence = snapshot.get('meterBalanceInMillipence')
        if millipence != None:
            self.snapshot_meter_balance_pence = round(millipence / 1000.0)
        
        if snapshot.get('asAt') != None:
            self.snapshot_as_at = snapshot.get('asAt')
            self._had_prepay_snapshot = True
    

    def has_prepay_snapshot(self) -> bool:
        return self.snapshot_as_at != None
    

    def has_prepay_snapshot_had_value(self) -> bool:
        """True when this meter has returned a balance snapshot at least once
        (differentiates never-reported from stopped-reporting)."""
        return self._had_prepay_snapshot == True
    

    def is_prepay(self) -> bool:
        return self.prepay_credit_pence != None or self.prepay_debt_pence != None or len(self.device_ids) > 0
    

    def is_ready_for_topup(self) -> bool:
        return self.prepay_credit_pence != None or self.snapshot_credit_pence != None
    

    def get_prepay_credit_gbp(self):
        """Credit in pounds: dashboard snapshot when available, otherwise the
        live credit ledger."""
        if self.snapshot_credit_pence != None:
            return round(self.snapshot_credit_pence / 100.0, 2)
        if self.prepay_credit_pence != None:
            return round(self.prepay_credit_pence / 100.0, 2)
        return None
    

    def get_prepay_debt_gbp(self):
        """Debt in pounds: snapshot when available, otherwise the debt ledger."""
        if self.snapshot_debt_pence != None:
            return round(self.snapshot_debt_pence / 100.0, 2)
        if self.prepay_debt_pence != None:
            return round(self.prepay_debt_pence / 100.0, 2)
        return None
    

    def get_prepay_credit_source(self) -> str:
        """Which source the current credit value came from."""
        if self.snapshot_credit_pence != None:
            return "snapshot"
        if self.prepay_credit_pence != None:
            return "ledger"
        return "none"
    

    def get_prepay_emergency_credit_gbp(self):
        if self.snapshot_emergency_pence == None:
            return None
        return round(self.snapshot_emergency_pence / 100.0, 2)
    

    def get_prepay_meter_balance_gbp(self):
        if self.snapshot_meter_balance_pence == None:
            return None
        return round(self.snapshot_meter_balance_pence / 100.0, 2)
    

    def get_type(self) -> str:
        return self.type
    

    def get_serial(self) -> str:
        return self.serial
    

    def _should_update(self) -> bool:
        if self.last_updated == None:
            return True
        
        now = datetime.datetime.now()
        if now.strftime("%d") != self.last_updated.strftime("%d"):
            if now.hour >= 7:
                return True
        
        return False


    def _convert_datetime_str_to_date(self, datetime_str: str) -> datetime.date:
        date_chunks = str(datetime_str.split("T")[0]).split("-")
        return datetime.date(int(date_chunks[0]), int(date_chunks[1]), int(date_chunks[2]))
    

    async def _update(self):
        pass


    async def update(self):
        if self._should_update() == True:
            await self._update()
    

    def _record_reading_history(self) -> None:
        """Keep a short history of daily register values for usage estimates."""
        if self.latest_reading == None or self.latest_reading_date == None:
            return
        
        entry = {"date": self.latest_reading_date, "reading": self.latest_reading}
        
        for existing in self.reading_history:
            if existing["date"] == entry["date"]:
                existing["reading"] = entry["reading"]
                return
        
        self.reading_history.append(entry)
        self.reading_history.sort(key=lambda e: e["date"])
        
        if len(self.reading_history) > READING_HISTORY_DAYS:
            self.reading_history = self.reading_history[-READING_HISTORY_DAYS:]
    

    def get_usage_for_date(self, target_date):
        """Consumption (register delta) during a calendar date: the reading
        on that date minus the reading on the latest earlier date, whichever
        list position each happens to occupy."""
        target_entry = None
        prior_entry = None
        
        for entry in self.reading_history:
            if entry["date"] == target_date:
                if target_entry == None:
                    target_entry = entry
            elif entry["date"] < target_date:
                if prior_entry == None or entry["date"] > prior_entry["date"]:
                    prior_entry = entry
        
        if target_entry == None or prior_entry == None:
            return None
        
        delta = target_entry["reading"] - prior_entry["reading"]
        return round(delta, 3) if delta >= 0 else None
    

    def get_usage_previous_day(self):
        """Consumption for the most recent day in history (by date, not by
        list position)."""
        latest_entry = self.get_latest_history_entry()
        if latest_entry == None:
            return None
        
        return self.get_usage_for_date(latest_entry["date"])
    

    def get_latest_history_entry(self):
        """The history entry with the greatest date, or None when empty."""
        latest_entry = None
        
        for entry in self.reading_history:
            if latest_entry == None or entry["date"] > latest_entry["date"]:
                latest_entry = entry
        
        return latest_entry
    

    async def has_reading(self) -> bool:
        await self.update()
        if self.latest_reading != None:
            return True
        return False


    async def get_latest_reading(self) -> int:
        await self.update()
        return self.latest_reading


    async def get_latest_reading_date(self) -> datetime.date:
        await self.update()
        return self.latest_reading_date



class ElectricityMeter(EnergyMeter):

    def __init__(self, account: EnergyAccount, meter_config: dict):
        super().__init__(account, meter_config)
        self.type = METER_TYPE_ELECTRIC
    

    async def _update(self):
        result = await self.api._graphql_post(
            "meterReadingsHistoryTableElectricityReadings",
            "query meterReadingsHistoryTableElectricityReadings($accountNumber: String!, $cursor: String, $meterId: String!) {\n  readings: electricityMeterReadings(\n    accountNumber: $accountNumber\n    after: $cursor\n    first: 12\n    meterId: $meterId\n  ) {\n    edges {\n      ...MeterReadingsHistoryTableElectricityMeterReadingConnectionTypeEdge\n      __typename\n    }\n    pageInfo {\n      endCursor\n      hasNextPage\n      __typename\n    }\n    __typename\n  }\n}\n\nfragment MeterReadingsHistoryTableElectricityMeterReadingConnectionTypeEdge on ElectricityMeterReadingConnectionTypeEdge {\n  node {\n    id\n    readAt\n    readingSource\n    registers {\n      name\n      value\n      __typename\n    }\n    source\n    __typename\n  }\n  __typename\n}\n",
            {
                "accountNumber": self.account.account_number,
                "cursor": "",
                "meterId": self.meter_id
            }
        )

        if self.api._json_contains_key_chain(result, ["data", "readings"]) == False:
            raise EonNextApiError("Unable to load readings for meter " + self.serial)

        readings = result['data']['readings']['edges']
        if len(readings) > 0:
            self.latest_reading = float(readings[0]['node']['registers'][0]['value'])
            self.latest_reading_date = self._convert_datetime_str_to_date(readings[0]['node']['readAt'])
            self.last_updated = datetime.datetime.now()
            self._record_reading_history()



class GasMeter(EnergyMeter):

    def __init__(self, account: EnergyAccount, meter_config: dict):
        super().__init__(account, meter_config)
        self.type = METER_TYPE_GAS
    

    async def _update(self):
        result = await self.api._graphql_post(
            "meterReadingsHistoryTableGasReadings",
            "query meterReadingsHistoryTableGasReadings($accountNumber: String!, $cursor: String, $meterId: String!) {\n  readings: gasMeterReadings(\n    accountNumber: $accountNumber\n    after: $cursor\n    first: 12\n    meterId: $meterId\n  ) {\n    edges {\n      ...MeterReadingsHistoryTableGasMeterReadingConnectionTypeEdge\n      __typename\n    }\n    pageInfo {\n      endCursor\n      hasNextPage\n      __typename\n    }\n    __typename\n  }\n}\n\nfragment MeterReadingsHistoryTableGasMeterReadingConnectionTypeEdge on GasMeterReadingConnectionTypeEdge {\n  node {\n    id\n    readAt\n    readingSource\n    registers {\n      name\n      value\n      __typename\n    }\n    source\n    __typename\n  }\n  __typename\n}\n",
            {
                "accountNumber": self.account.account_number,
                "cursor": "",
                "meterId": self.meter_id
            }
        )

        if self.api._json_contains_key_chain(result, ["data", "readings"]) == False:
            raise EonNextApiError("Unable to load readings for meter " + self.serial)

        readings = result['data']['readings']['edges']
        if len(readings) > 0:
            self.latest_reading = float(readings[0]['node']['registers'][0]['value'])
            self.latest_reading_date = self._convert_datetime_str_to_date(readings[0]['node']['readAt'])
            self.last_updated = datetime.datetime.now()
            self._record_reading_history()
    

    async def get_latest_reading_kwh(self) -> float:
        m3 = await self.get_latest_reading()
        gas_caloric_value = 38

        kwh = m3 * 1.02264
        kwh = kwh * self.account.gas_calorific_value
        kwh = kwh / 3.6

        return round(kwh, 2)
