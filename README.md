# OASI-Weather

OASI-Weather is a Python dashboard used at the Observatorio Astronomico do Sertao de Itaparica (OASI) to monitor local weather and all-sky camera images in near real time.

It is designed for observatory operation support, mainly to:

- visualize current meteorological conditions;
- track short and medium weather history;
- evaluate observation conditions from wind/temperature/dew-point rules;
- display latest all-sky frame (ASCOM camera);
- persist weather data in yearly SQLite databases.

## What this codebase does

- Reads weather station registers over Modbus TCP using `sigma.yaml` mapping.
- Displays a Dash/Plotly web UI with live metrics and historical plots.
- Stores measurements to SQLite (`weather_data_{year}.db`) through `database.py`.
- Integrates sunrise/sunset and moon calculations for night/day behavior.
- Captures and serves all-sky images through ASCOM (`allsky.py` + `oculus.yaml`).

## Project structure

- `src/__main__.py`: dashboard app, callbacks, plotting, and app server.
- `src/weatherstation.py`: Modbus data acquisition and metric formatting helpers.
- `src/allsky.py`: all-sky camera capture pipeline and image serving path logic.
- `src/database.py`: SQLite persistence and time-range query methods.
- `src/util.py`: sun/moon helper functions.
- `src/config.yaml`: dashboard and runtime defaults.
- `src/sigma.yaml`: weather station host/register map.
- `src/oculus.yaml`: camera, capture, and processing settings.

## Requirements

- Python 3.12+
- Windows if ASCOM camera integration is needed (`pywin32` + ASCOM drivers)
- Network access to weather station host configured in `src/sigma.yaml`

## Installation

From repository root:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

Alternative (if you use `uv`):

```bash
uv sync
```

## Run

From repository root:

```bash
python -m src
```

The dashboard binds to host/port from `src/config.yaml` (default `0.0.0.0:8051`).

## Default parameters

Main defaults from `src/config.yaml`:

- `SERVER_HOST`: `0.0.0.0`
- `SERVER_PORT`: `8051`
- `UPDATE_INTERVAL_SECONDS`: `2`
- `ALLSKY_UPDATE_INTERVAL_SECONDS`: `30`
- `INPE_UPDATE_INTERVAL_SECONDS`: `600`
- `WEATHER_FETCH_INTERVAL_SECONDS`: `1`
- `DATABASE_SAVE_INTERVAL_SECONDS`: `30`
- `DATABASE_PATH_PATTERN`: `weather_data_{year}.db`
- `IN_MEMORY_RETENTION_DAYS`: `7`
- `MAX_PLOT_POINTS`: `4000`
- `PLOT_TARGET_POINTS`: `1200`
- `MEMORY_MAX_ROWS`: `50000`
- `STARTUP_PRELOAD_LATEST_ONLY`: `false`
- `STARTUP_LOAD_MINUTES`: `1440`
- `STARTUP_MAX_ROWS`: `20000`
- `DROPDOWN_QUERY_MAX_ROWS`: `80000`

Weather station defaults from `src/sigma.yaml`:

- `host`: `192.168.1.56` (replace with your real station IP)
- `service_port`: `502`
- `function_code`: `3`

All-sky defaults from `src/oculus.yaml`:

- `camera.exposure_time`: `30.0` seconds
- `camera.min_capture_interval_seconds`: `90`
- `camera.capture_timeout_seconds`: `180`
- `camera.apply_binning`: `false`
- `camera.apply_gain`: `false`
- `camera.apply_cooling`: `false`
- `schedule.capture_after_sunset_offset`: `0` minutes
- `schedule.capture_before_sunrise_offset`: `0` minutes
- `storage.keep_latest_only`: `true`
- `storage.save_path`: `data/allsky`

## Notes for operation

- Observation condition indicator is computed mainly from last 30 minutes of wind/temperature/dew-point.
- If weather station is unreachable, dashboard keeps running in offline mode with `N/D` style values.
- All-sky capture runs only during nighttime according to sunrise/sunset logic and schedule offsets.

## Data and generated files

Generated runtime files are intentionally excluded by `.gitignore`, including:

- Python cache folders (`__pycache__/`)
- build artifacts (`*.egg-info`, `build/`, `dist/`)
- yearly database files (`src/weather_data_*.db`)
- runtime logs such as `startup.log`
- captured all-sky images under `src/data/allsky/`