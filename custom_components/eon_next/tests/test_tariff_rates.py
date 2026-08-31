"""Offline behavioural tests for tariff and unit-rate logic.

Run with:  python3 tests/test_tariff_rates.py
No network access is used - all GraphQL responses are stubbed.
"""
import asyncio
import datetime
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import aiohttp  # noqa
except ImportError:
    fake = types.ModuleType("aiohttp")
    fake.ContentTypeError = type("ContentTypeError", (Exception,), {})
    fake.ClientError = type("ClientError", (Exception,), {})
    class _Never:
        def __init__(self, *a, **k):
            raise AssertionError("real HTTP attempted in stub test")
    fake.ClientSession = _Never
    fake.ClientTimeout = lambda **k: k
    sys.modules["aiohttp"] = fake

from eonnext import EonNext, EnergyAccount  # noqa

FAILURES = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILURES.append(name)


def utc_stamp(hour, minute=0, day_offset=0):
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=day_offset)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")


tariff_payload = {
    "data": {"viewer": {"accounts": [
        {"number": "A-123", "properties": [
            {"electricityMeterPoints": [
                {"mpan": "1900000000001", "agreements": [
                    {"id": 42, "validFrom": "2024-11-05T00:00:00+00:00", "validTo": None,
                     "tariff": {"__typename": "DayNightTariff", "displayName": "Next Flex Smart PAYG",
                                "fullName": "Next Flex Smart PAYG Version", "productCode": "NEXT_FLEX_SMART_PAYG",
                                "tariffCode": "E-2R-NEXT_FLEX_SMART_PAYG-G",
                                "dayRate": 30.093, "nightRate": 14.7525, "standingCharge": 46.893}}
                ]}
            ],
            "gasMeterPoints": [
                {"mprn": "1234567890", "agreements": [
                    {"id": 43, "validFrom": "2024-11-05T00:00:00+00:00", "validTo": None,
                     "tariff": {"__typename": "GasTariffType", "displayName": "Next Flex Gas",
                                "productCode": "NEXT_FLEX_GAS", "tariffCode": "G-1R-NEXT_FLEX_GAS-G",
                                "unitRate": 6.852, "standingCharge": 31.412}}
                ]}
            ]}
        ]}
    ]}}
}

rate_payload = {
    "data": {"applicableRates": {"edges": [
        {"node": {"value": "30.09300", "validFrom": utc_stamp(7, 30, -1), "validTo": utc_stamp(23, 0, -1)}},
        {"node": {"value": "14.75250", "validFrom": utc_stamp(23, 0, -1), "validTo": utc_stamp(0, 30)}},
        {"node": {"value": "14.75250", "validFrom": utc_stamp(0, 30), "validTo": utc_stamp(7, 30)}},
        {"node": {"value": "30.09300", "validFrom": utc_stamp(7, 30), "validTo": utc_stamp(23, 0)}},
        {"node": {"value": "14.75250", "validFrom": utc_stamp(23, 0), "validTo": utc_stamp(0, 30, 1)}},
        {"node": {"value": "30.09300", "validFrom": utc_stamp(0, 30, 1), "validTo": utc_stamp(23, 0, 1)}},
    ]}}
}


class StubApi(EonNext):

    def __init__(self, responses):
        super().__init__()
        self.responses = responses
        self.calls = []

    async def _graphql_post(self, operation, query, variables={}, authenticated=True):
        self.calls.append(operation)
        if operation in self.responses:
            return self.responses[operation]
        raise AssertionError("unexpected operation " + operation)


class MeterReadingHistoryStubApi(StubApi):

    async def _graphql_post(self, operation, query, variables={}, authenticated=True):
        self.calls.append(operation)
        if operation == "MeterReadingHistory":
            return {"data": {"electricityMeterReadings": {"edges": [
                {"node": {"readAt": "2026-08-28T00:00:00+00:00",
                          "registers": [{"name": "Day", "value": 100.0}, {"name": "Night", "value": 50.0}]}},
                {"node": {"readAt": "2026-08-29T00:00:00+00:00",
                          "registers": [{"name": "Day", "value": 105.0}, {"name": "Night", "value": 52.0}]}},
            ]}}}
        return await super()._graphql_post(operation, query, variables, authenticated)


def check_usage_history_cost_path():
    """Cost path: both bands covered -> day/night basis; day band only ->
    standing-charge-only basis with night units NOT billed at day rate."""
    stub = MeterReadingHistoryStubApi({
        "AccountTariff": tariff_payload,
        "ApplicableRates": rate_payload,
        "ConsumptionEstimates": {"data": {"consumptionEstimates": {
            "low": {}, "medium": {}, "high": {}}}},
        "AccountBills": {"data": {"viewer": {"accounts": [{"number": "A-123", "bills": {"edges": []}}]}}},
    })
    acc = EnergyAccount(stub, "A-123")
    acc.electricity_mpan = "1900000000001"

    class _EMeter:
        meter_id = "8935356"

        def get_type(self):
            return "electricity"
    acc.meters.append(_EMeter())

    # Production order inside refresh_readings: tariff, rates, then usage
    asyncio.run(acc.refresh_tariff(force=True))
    asyncio.run(acc.refresh_rates(force=True))
    asyncio.run(acc.refresh_usage_history(force=True))
    latest = acc.get_latest_usage()
    check("usage latest day total", latest.get("total_kwh") == 7.0)
    check("usage day/night split", latest.get("day_kwh") == 5.0 and latest.get("night_kwh") == 2.0)
    # Stub rate windows are anchored to today, so they do not reach the
    # 08-29 register date: no unit cover at all, standing charge only:
    # 46.893 p standing only -> 0.47 GBP; night units must NOT be billed.
    check("usage cost standing-only", latest.get("cost_gbp") == 0.47)
    check("usage basis flagged", "standing charge only" in latest.get("cost_basis"))


async def main():
    stub = StubApi({
        "AccountTariff": tariff_payload,
        "ApplicableRates": rate_payload,
        "ConsumptionEstimates": {"data": {"consumptionEstimates": {
            "low": {"elecAnnualConsumptionStandard": 1600, "elecAnnualConsumptionDay": 1102, "elecAnnualConsumptionNight": 798, "gasAnnualConsumption": 6000},
            "medium": {"elecAnnualConsumptionStandard": 2500, "elecAnnualConsumptionDay": 1972, "elecAnnualConsumptionNight": 1428, "gasAnnualConsumption": 9500},
            "high": {"elecAnnualConsumptionStandard": 3800, "elecAnnualConsumptionDay": 3538, "elecAnnualConsumptionNight": 2562, "gasAnnualConsumption": 14000}}}},
        "AccountBills": {"data": {"viewer": {"accounts": [
            {"number": "A-123", "bills": {"edges": [
                {"node": {"__typename": "PeriodBasedDocumentType", "id": "doc1", "fromDate": "2025-10-01", "toDate": "2026-08-27", "issuedDate": "2026-08-31"}}
            ]}}]}}},
    })
    acc = EnergyAccount(stub, "A-123")
    acc.electricity_mpan = "1900000000001"
    acc.gas_mprn = "1234567890"

    await acc.refresh_tariff()
    elec_t = acc.tariff.get("electricity") or {}
    gas_t = acc.tariff.get("gas") or {}
    check("elec tariff name", elec_t.get("displayName") == "Next Flex Smart PAYG")
    check("elec tariff mpan", elec_t.get("mpan") == "1900000000001")
    check("elec tariff standing pence", elec_t.get("standingCharge") == 46.893)
    check("elec tariff day/night", elec_t.get("dayRate") == 30.093 and elec_t.get("nightRate") == 14.7525)
    check("gas tariff unit rate", gas_t.get("unitRate") == 6.852)

    calls_before = len(stub.calls)
    await acc.refresh_tariff()
    check("tariff throttle holds", len(stub.calls) == calls_before)

    await acc.refresh_rates()
    check("rate windows loaded", len(acc.rate_windows) == 6)
    check("rate windows sorted", acc.rate_windows[0]["from"] < acc.rate_windows[-1]["from"])
    check("rate pence parsed as float", isinstance(acc.rate_windows[0]["pence"], float))

    info = acc.get_current_rate_info()
    check("current rate resolved", info != {})
    if info != {}:
        check("current rate is day or night", info["pence"] in (30.093, 14.7525))
        check("current window bounds present", info.get("from") != None and info.get("to") != None)
        check("next rate present", info.get("next_pence") != None)
        check("block starts at or before window", info.get("block_from") <= info.get("from"))
        check("block ends at or after window", info.get("block_to") != None and info.get("block_to") >= info.get("to"))
        check("min today known", info.get("min_pence_today") in (30.093, 14.7525))
        check("max today known", info.get("max_pence_today") in (30.093, 14.7525))
        check("min <= avg <= max", info.get("min_pence_today") <= info.get("average_pence_today") <= info.get("max_pence_today"))

    calls_before = len(stub.calls)
    await acc.refresh_rates()
    check("rates throttle holds", len(stub.calls) == calls_before)

    # Merged windows: adjacent same-rate windows collapse for the calendar
    merged = acc.get_merged_rate_windows()
    check("merged windows fewer or equal", len(merged) <= len(acc.rate_windows))
    if len(merged) > 0:
        check("merge preserves fields", all(k in merged[0] for k in ("pence", "from", "to")))

    # Duration-weighted blended rate for a covered day (today must be covered)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    blended = acc.get_blended_rate_pence_for_date(today)
    check("blended rate resolved for covered day",
          blended != None and 14.7525 <= blended <= 30.093)
    check("blended rate absent for uncovered day",
          acc.get_blended_rate_pence_for_date(today + datetime.timedelta(days=30)) is None)

    check("standing charge GBP/day", round(elec_t["standingCharge"] / 100.0, 4) == 0.4689)
    check("day rate GBP/kWh", round(elec_t["dayRate"] / 100.0, 4) == 0.3009)
    check("gas rate GBP/kWh", round(gas_t["unitRate"] / 100.0, 4) == 0.0685)

    acc2 = EnergyAccount(stub, "B-456")
    await acc2.refresh_rates()
    check("no mpan guard", acc2.rate_windows == [])

check_usage_history_cost_path()
asyncio.run(main())

print("---")
print("FAILED:", FAILURES if FAILURES else "none")
sys.exit(1 if FAILURES else 0)
