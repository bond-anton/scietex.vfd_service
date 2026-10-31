# scietex.vfd_service

Scietex microservice daemon for variable frequency drive (VFD) monitoring and
control.

The service drives one or more VFDs through the Scietex Modbus gateway. Each
drive is handled by a driver from
[`scietex.hal.vfd`](https://github.com/bond-anton/scietex.hal.vfd), selected by
entry point name in the service configuration. The service polls each drive on
its own long-lived task, publishes readings to a per-drive Valkey stream, and
exposes control tasks (`vfd:start`, `vfd:stop`, `vfd:set-frequency`,
`vfd:clear-error`) on the data lane.

## Supported drives

| Driver entry point | Drive | Temperature reading |
| --- | --- | --- |
| `intek_spe_b_modbus` | Intek SPE-B | none (overheat is a fault code) |
| `vesper_e5_8200_modbus` | Vesper E5-8200 | none (overheat is a fault code) |
| `vesper_e4_8400_modbus` | Vesper E4-8400 / E4-P8402 | power-module temperature (U1-46) |

The gateway descriptors (`intek_spe_b`, `vesper_e5_8200`, `vesper_e4_8400`)
live under the `scietex.modbus.drivers` group and are registered with the
gateway, not with this service. The `_modbus` suffix distinguishes the HAL
driver entry points from the gateway descriptors.

## Configuration

The service reads `vfd/vfd.yml` under the framework config directory
(`SCIETEX_CONFIG_DIR`, default `/config` in the container). The file is created
with defaults on first start.

```yaml
gateway:
  host: 127.0.0.1
  port: 15020
  timeout: 1.0
drives:
  - label: pump
    driver: intek_spe_b_modbus
    unit_id: 1
    poll_interval: 1.0
    # max_frequency_hz: 50.0
failure_threshold: 3
stream_maxlen: 10000
```

- `gateway` — Modbus/TCP endpoint of the Scietex Modbus gateway.
- `drives[].label` — unique label; the registry key and the stream key segment.
- `drives[].driver` — HAL driver entry point name (see the table above).
- `drives[].unit_id` — Modbus unit id the gateway routes to the drive. This is
  the drive's own configured address (`Pd.02` on SPE-B, `09-00` on E5-8200,
  `H5-01` on E4-8400).
- `drives[].poll_interval` — seconds between reads.
- `drives[].max_frequency_hz` — optional. Only meaningful for a drive whose
  protocol does not expose its configured maximum output frequency; the driver
  uses it to derive `frequency_percent` and to bound `set_frequency`. Omit it to
  keep the driver's own default (SPE-B reads `P0.10` from the drive, E4-8400
  reads `E1-04`; E5-8200 falls back to 50 Hz).
- `failure_threshold` — consecutive failed reads before a drive is reported as
  disconnected. One dropped frame on an RS485 segment must not flip the status.
- `stream_maxlen` — entries retained per drive's readings stream.

A configuration change at runtime is **restart-required**: the drive registry is
not live-rebuilt.

## Tasks

Control lane:

- `monitoring:start` / `monitoring:stop` — payload `{"label": "pump"}` or empty
  for every drive.

Data lane:

- `vfd:read` — payload `{"label": "pump"}`. Returns every parameter field plus
  the decoded fault (`error_code`, `error_message`).
- `vfd:start` — `{"label": "pump", "forward": true, "slow": false}`.
- `vfd:stop` — `{"label": "pump", "freewheel": false}`.
- `vfd:set-frequency` — `{"label": "pump", "frequency_hz": 42.5}`.
- `vfd:clear-error` — `{"label": "pump", "comm": false}`.

Every control task is gated on the drive's declared capabilities, so an
unsupported command is rejected as `UNSUPPORTED` before the driver is called.
`vfd:read` is ungated.

## Valkey keys

- Readings stream: `scietex:{service}:{instance_id}:vfd:{label}:readings`
  with fields `ts`, `frequency_hz`, `frequency_percent`, `output_current`,
  `output_voltage`, `output_power`, `dc_bus_voltage`, `heatsink_temperature_c`,
  `motor_temperature_c`, `state`, `total_run_hours`, `total_power_on_hours`. A
  field the drive does not report is written as an empty value, which is
  distinguishable from a reported `0.0`.
- Telemetry: `scietex:{service}:{instance_id}:telemetry`, pushed every 2 s with
  a 6 s TTL.

## Working hours

`total_run_hours` and `total_power_on_hours` are the drive's own counters when
it keeps them, otherwise the worker's accumulation. The telemetry entry carries
`hours_source` to say which: `"drive"`, `"accumulated"`, or `null` when neither
is available yet.

The accumulation credits the elapsed time between two successful readings,
capped at `MAX_ACCUMULATION_GAP` (60 s) so a worker that was stopped does not
credit its downtime to the drive. The powered-on counter is credited on every
reading; the run counter only while the drive reports a running state. It is
in-memory: a worker restart resets it, which is why a drive that reports its own
counters is always preferred.

## Running

```bash
start-vfd-service --conf-dir /etc/scietex
```

Flags: `--conf-dir` (auto-resolved when omitted), `--service-name` (default
`$SCIETEX_SERVICE_NAME` or `VFD`), `--logging-level` (default
`$SCIETEX_LOGGING_LEVEL` or `INFO`). It runs in the foreground until `SIGINT` or
`SIGTERM`.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install -e ".[dev,test,lint]" --python .venv/bin/python
tox -e format,lint,type,test
```

## Container

```bash
./build_image.sh [--latest]
```

Builds and pushes `registry.buro-nts.ru/scietex-vfd-service:<version>` for
`linux/amd64` and `linux/arm64`. The image installs the released wheel from
PyPI, so publish the version first.

## Deployment prerequisites

The gateway speaks 8-N-1 on its serial port. Each drive must be configured to
match before the gateway can talk to it:

- **Intek SPE-B** — `Pd.01` → `3` (8-N-1), `Pd.05` → `1` (standard Modbus),
  `Pd.02` → the unit id used in `vfd.yml`.
- **Vesper E5-8200** — `09-01` → `0` (RTU), `09-03` → `0` (1 stop bit),
  `09-04` → `0` (no parity), `09-05` → `0` (8 data bits), `09-00` → the unit id.
- **Vesper E4-8400** — `H5-08` → `0` (RTU), `H5-09` → `0` (1 stop bit),
  `H5-10` → `0` (8 data bits), `H5-03` → `0` (no parity), `H5-01` → the unit id.

The drive must also accept its setpoint from the communication channel
(`P0.03` = `9` on SPE-B, `b1-01`/`b1-02` = `2` on E4-8400). See the HAL
protocol docs for the full register maps.
