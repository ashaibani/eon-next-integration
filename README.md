# E.ON Next Integration for Home Assistant

A Home Assistant custom component for the E.ON Next (UK energy supplier)
customer API: meter readings, smart prepay balances, tariff and unit-rate
sensors, an Energy-dashboard-ready setup, and a rates calendar.

> ## Credits
>
> This project is a derivative of
> **[madmachinations/eon-next](https://github.com/madmachinations/eon-next)** -
> the original E.ON Next integration, which worked out the GraphQL operations,
> the authentication flow and the account/meter model. Without that reverse
> engineering this integration would not exist. **Thank you.**
>
> This fork has since diverged substantially (see *What changed from the
> original* below), but all credit for the ground work is theirs.

An independent project. Not affiliated with, endorsed by, or connected to
E.ON Next Energy Limited; "E.ON Next" is their trademark and is used here
solely to describe interoperability.

## What it provides

**Per meter**
- Cumulative register sensor (kWh / m) usable as the Energy dashboard
  "grid import" / "gas consumption" source (`total_increasing`)
- Reading-date sensor
- Gas register converted to kWh (calorific value configurable)

**Smart prepay accounts** (verified against the live API)
- Prepay credit / debt / emergency credit sensors - using the same
  `prepayBalanceSnapshot` query the E.ON Next app itself uses, so the figure
  matches the app
- "Prepay updated" freshness sensor (the meter's own `asAt` timestamp)
- "Prepay low credit" binary sensor with a configurable threshold

**Tariff and rates**
- Current unit-rate sensor in GBP/kWh (follows day/night windows) - usable as
  the Energy dashboard's "entity with current price"
- Next-rate sensor for "cheap window starts" automations
- Standing charge and tariff name sensors
- Unit-rate calendar (merged same-rate windows)
- "Night rate active" binary sensor

**Estimates and orchestration**
- Daily usage estimate (register delta)
- Daily cost estimate (duration-weighted blended rate)
- `eon_next.refresh` service to force-refresh everything (next to useless
  after a top-up otherwise)
- Options flow: entity visibility toggles, refresh intervals, low-credit
  threshold, gas calorific value
- Home Assistant repair issues when a prepay meter stops reporting

## Requirements

- Home Assistant 2025.1+ (developed on 2026.8)
- An E.ON Next online-account login (the same email/password you use on
  eonnext.com)
- A smart-prepay-capable meter for the prepay sensors (credit sensors appear
  automatically when the account exposes them)

## Installation

### HACS (recommended)

1. HACS = three-dot menu -> Custom repositories (or add this URL when asked):
   `https://github.com/ashaibani/eon-next-integration`
2. Category: **Integration**
3. Install **E.ON Next Integration**
4. **Restart Home Assistant**

### Manual

Copy `custom_components/eon_next/` into your HA config's `custom_components/`
folder and restart Home Assistant.

## Setup

Settings -> Devices & Services -> Add Integration -> **E.ON Next** -> enter
your email and password. Everything else is automatic.

Bad credentials show "Authentication failed"; API problems show "Failed to
connect" and retry with backoff.

## Energy dashboard setup

- **Grid import**: your `<serial> Electricity` sensor
- **Cost tracking**: *Use an entity with current price* -> your
  `<account> Electricity Rate` sensor
- Notes: no export data exists in the API; costs exclude the standing charge
  (it has its own sensor); the register updates about once a day, so Energy
  shows one daily increment rather than a live curve. A power-monitoring plug
  (e.g. Tasmota) fills the live-power slot nicely.

## Options

Configure via the integration's Configure button:

| Option | Default | Purpose |
|---|---|---|
| Show tariff/rate sensors | on | Hides the rate fleet if you only want meters |
| Show balance sensor | on | Account ledger balance |
| Show usage estimates | on | Daily usage and cost sensors |
| Show prepay sensors | on | Credit/debt/emergency/updated |
| Low credit threshold | 200 pence | The low-credit binary sensor's trigger level |
| Gas calorific value | 38.0 | m-to-kWh conversion accuracy (check your bill; 37.5-39.5 typical) |
| Balance refresh | 15 min (min 10) | Account ledger cycle |
| Rate refresh | 60 min (min 30) | Unit-rate windows cycle |
| Prepay refresh | 5 min (min 5) | Prepay balances + meter snapshot cycle |

## What changed from the original

- **Architecture**: `DataUpdateCoordinator` per data domain (balance, tariff,
  rates, prepay, readings); sensors are coordinator observers with no API
  calls of their own, so failures mark entities unavailable instead of
  erroring per-sensor
- **Prepay support**: meter `prepayLedgers` plus the app's
  `prepayBalanceSnapshot(deviceId)` call (credit/debt/emergency, with the
  meter's own `asAt` freshness)
- **Tariff/rate windows** via `applicableRates`, with current/next rate
  resolution, contiguous same-rate blocks and today's min/average/max
- **Balance handling**: pence integers converted to GBP; account, rates and
  prepay refreshes independently tuned and throttled
- **Error hardening**: typed `EonNextApiError`, aiohttp session reuse,
  timeouts, auth lock, re-login after refresh-token expiry
- **Entities**: binary sensors, calendar platform, diagnostic categories,
  repair issues, options flow, refresh service
- **Tests**: offline stub-based suites in `tests/`

## Troubleshooting

- **400 Bad Request behind a reverse proxy**: trust your proxy
  (`http: use_x_forwarded_for` + `trusted_proxies` in `configuration.yaml`)
- **Prepay credit differs from the app**: the app shows the meter snapshot;
  the integration prefers the same snapshot. The credit sensor's
  `source`/`ledger_credit_pence` attributes expose both retailer-ledger and
  meter values (they legitimately differ until the meter reports)
- **No data / stale**: enable debug logging for the integration, then open an
  issue with the logs (redact your email)

## Unofficial API caveat

E.ON Next publishes **no public developer API**. This integration talks to
the same unpublished GraphQL endpoint its app and website use. It works today
and can break without notice if E.ON Next changes their platform. Please be
reasonable with request rates - hence the throttles and coordinator intervals.

## Running the tests

```bash
python3 custom_components/eon_next/tests/test_eonnext_client.py
python3 custom_components/eon_next/tests/test_tariff_rates.py
```

Both are offline stub-based suites - no network access, no credentials.

## Acknowledgements

- [madmachinations/eon-next](https://github.com/madmachinations/eon-next) -
  the original integration (see Credits above; that project carries no
  licence file, so none of its code is republished here unmodified - it is
  acknowledged as inspiration and origin for the API surface)
- The [Home Assistant](https://www.home-assistant.io) and
  [Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)
  integration communities for the coordinator and Energy-dashboard patterns

## License

MIT - see [LICENSE](LICENSE).
