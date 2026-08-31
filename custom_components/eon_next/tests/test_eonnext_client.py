"""Offline behavioural tests for the E.ON Next client.

Run with:  python3 tests/test_eonnext_client.py
No network access is used - all GraphQL responses are stubbed.
"""
import asyncio
import datetime
import os
import sys
import types

# Import the integration modules directly from the repo root
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

from eonnext import EonNext, EnergyAccount, GasMeter, ElectricityMeter  # noqa

FAILURES = []

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILURES.append(name)

accounts_payload = {
    "data": {"viewer": {"accounts": [
        {"number": "A-123", "balance": 12345},   # pence
        {"number": "B-456", "balance": None},
    ]}}
}

meters_payload = {
    "data": {"properties": [
        {
            "id": "p1", "postcode": "XX1 1AA",
            "electricityMeterPoints": [
                {"id": "ep1", "mpan": "1900000000001", "meters": [
                    {"id": "eid", "serialNumber": "E-SER", "activeTo": None,
                     "isTradPrepay": False,
                     "prepayLedgers": {
                         "creditLedger": {"currentBalance": 136},
                         "debtLedger": {"currentBalance": 0}
                     },
                     "smartDevices": [
                         {"__typename": "SmartMeterDeviceType", "deviceId": "DEV-1", "paymentMode": "PREPAY"}
                     ],
                     "registers": [{"id": "r1", "name": "1"}]}
                ]}
            ],
            "gasMeterPoints": [
                {"id": "gp1", "mprn": "1234567890", "meters": [
                    {"id": "gid", "serialNumber": "G-SER", "activeTo": None,
                     "isTradPrepay": False,
                     "prepayLedgers": None,
                     "smartDevices": [],
                     "registers": [{"id": "r2", "name": "1"}]}
                ]}
            ],
        }
    ]}
}

snapshot_payload = {
    "data": {"prepayBalanceSnapshot": {
        "asAt": "2026-08-31T00:00:00+00:00",
        "creditInPence": 1228,
        "debtInPence": 0,
        "emergencyCreditInPence": 1000,
        "meterBalanceInMillipence": 1227900,
        "__typename": "PrepayBalanceSnapshotType"
    }}
}

gas_readings_payload = {
    "data": {"readings": {"edges": [
        {"node": {"readAt": "2026-08-31T09:00:00+00:00", "registers": [{"name": "V", "value": "10"}]}}
    ]}}
}

elec_readings_payload = {
    "data": {"readings": {"edges": [
        {"node": {"readAt": "2026-08-30T10:00:00+00:00", "registers": [{"name": "N", "value": "12345.6"}]}}
    ]}}
}

fresh_accounts_payload = {
    "data": {"viewer": {"accounts": [
        {"number": "A-123", "balance": 500},    # pence -> 5.00
        {"number": "B-456", "balance": -150},   # pence -> -1.50
    ]}}
}

fresh_prepay_payload = {
    "data": {"properties": [
        {
            "id": "p1", "postcode": "XX1 1AA",
            "electricityMeterPoints": [
                {"id": "ep1", "mpan": "1900000000001", "meters": [
                    {"id": "eid", "serialNumber": "E-SER", "activeTo": None,
                     "isTradPrepay": False,
                     "prepayLedgers": {
                         "creditLedger": {"currentBalance": 1228},
                         "debtLedger": {"currentBalance": 75}
                     },
                     "smartDevices": [{"__typename": "SmartMeterDeviceType", "deviceId": "DEV-1", "paymentMode": "PREPAY"}],
                     "registers": []}
                ]}
            ],
            "gasMeterPoints": [],
        }
    ]}
}

class StubApi(EonNext):
    """Drive accounts/meters offline with canned GraphQL responses."""

    def __init__(self, responses):
        super().__init__()
        self.responses = responses
        self.calls = []

    async def _graphql_post(self, operation, query, variables={}, authenticated=True):
        self.calls.append(operation)
        if operation in self.responses:
            return self.responses[operation]
        raise AssertionError("unexpected operation " + operation)


async def main():
    stub = StubApi({
        "headerGetLoggedInUser": accounts_payload,
        "getAccountMeterSelector": meters_payload,
        "balanceForDevice": snapshot_payload,
    })
    api = stub

    await api._EonNext__init_accounts()
    check("two accounts loaded", len(api.accounts) == 2)
    check("meter types", sorted(m.get_type() for m in api.accounts[0].meters) == ["electricity", "gas"])
    check("mpan captured", api.accounts[0].electricity_mpan == "1900000000001")
    check("mprn captured", api.accounts[0].gas_mprn == "1234567890")
    check("pence account balance converted", api.accounts[0].get_balance_amount() == 123.45)

    elec = api.accounts[0].meters[0]
    gas = api.accounts[0].meters[1]

    check("elec device ids discovered", elec.device_ids == ["DEV-1"])
    check("elec payment mode", elec.payment_mode == "PREPAY")
    check("gas has no devices", gas.device_ids == [])

    check("snapshot credit 1228 pence", elec.snapshot_credit_pence == 1228)
    check("credit GBP matches dashboard", elec.get_prepay_credit_gbp() == 12.28)
    check("credit source is snapshot", elec.get_prepay_credit_source() == "snapshot")
    check("emergency credit GBP", elec.get_prepay_emergency_credit_gbp() == 10.0)
    check("meter balance from millipence", elec.get_prepay_meter_balance_gbp() == 12.28)
    check("had snapshot flag", elec.has_prepay_snapshot_had_value() == True)
    check("asAt stored", elec.snapshot_as_at == "2026-08-31T00:00:00+00:00")
    check("gas not prepay", gas.is_prepay() == False)

    # Snapshot fetched per device-bearing meter (stub serves both accounts)
    check("snapshot fetched per device meter", stub.calls.count("balanceForDevice") == 2)

    # Throttles
    await api.accounts[0].refresh_prepay_balances()
    prepay_calls = stub.calls.count("getAccountMeterSelector") + stub.calls.count("balanceForDevice")
    await api.accounts[0].refresh_prepay_balances()
    check("prepay refresh throttle", (stub.calls.count("getAccountMeterSelector") + stub.calls.count("balanceForDevice")) == prepay_calls)

    # Force bypasses the throttle (coordinator-driven cycles rely on this)
    await api.accounts[0].refresh_prepay_balances(force=True)
    check("force bypasses throttle", (stub.calls.count("getAccountMeterSelector") + stub.calls.count("balanceForDevice")) > prepay_calls)

    # Balance parser edge cases (int = pence, float/str = pounds)
    acc = EnergyAccount(api, "C-789")
    acc.set_balance(12.5);          check("float balance as pounds", acc.get_balance_amount() == 12.5)
    acc.set_balance(500);           check("int pence 500 -> 5.0", acc.get_balance_amount() == 5.0)
    acc.set_balance(-150);          check("negative pence", acc.get_balance_amount() == -1.5)
    acc.set_balance(True);          check("bool rejected", acc.get_balance_amount() is None)
    acc.set_balance("");            check("empty rejected", acc.get_balance_amount() is None)
    acc.set_balance({"amount": 700}); check("dict amount int pence", acc.get_balance_amount() == 7.0)
    acc.set_balance("£-4.20");      check("signed string", acc.get_balance_amount() == -4.2)
    acc.set_balance("n/a");         check("junk string", acc.get_balance_amount() is None)

    # Reading precision preserved (Energy dashboard accuracy)
    elec_stub = StubApi({"meterReadingsHistoryTableElectricityReadings": elec_readings_payload})
    em = ElectricityMeter(EnergyAccount(elec_stub, "A-123"),
                          {"id": "eid", "serialNumber": "E-SER", "isTradPrepay": False, "prepayLedgers": None})
    await em._update()
    check("electric reading float", em.latest_reading == 12345.6)

    gas_stub = StubApi({"meterReadingsHistoryTableGasReadings": gas_readings_payload})
    gm = GasMeter(EnergyAccount(gas_stub, "A-123"),
                  {"id": "gid", "serialNumber": "G-SER", "isTradPrepay": False, "prepayLedgers": None})
    check("gas reading fetched", await gm.get_latest_reading() == 10)
    check("gas m3->kWh (CV 38)", await gm.get_latest_reading_kwh() == 107.95)

    # Reading history + usage estimation
    check("history recorded", len(gm.reading_history) == 1)
    check("usage needs previous day", gm.get_usage_previous_day() is None)
    gm.reading_history.append({"date": datetime.date(2026, 8, 30), "reading": 9.5})
    check("usage delta", gm.get_usage_previous_day() == 0.5)

    # Fresh coordinator-style cycle applies new snapshot + ledgers
    stub.responses = {"headerGetLoggedInUser": fresh_accounts_payload,
                      "getAccountMeterSelector": fresh_prepay_payload,
                      "balanceForDevice": snapshot_payload}
    api.accounts[0]._last_prepay_fetch = None
    await api.accounts[0].refresh_prepay_balances(force=True)
    check("ledger value refreshed", elec.prepay_credit_pence == 1228)
    check("debt ledger refreshed", elec.prepay_debt_pence == 75)

    # Options plumbing
    api.accounts[0].apply_options({"gas_calorific_value": 39.5, "low_credit_pence": 150})
    check("options CV", api.accounts[0].gas_calorific_value == 39.5)
    check("options threshold", api.accounts[0].low_credit_pence == 150)

asyncio.run(main())

print("---")
print("FAILED:", FAILURES if FAILURES else "none")
sys.exit(1 if FAILURES else 0)
