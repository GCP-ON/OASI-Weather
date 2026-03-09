"""OASI-Weather Dashboard Application.

This module implements the main web dashboard for the Observatório Astronômico
do Sertão de Itaparica (OASI) weather monitoring system. It provides real-time
visualization of meteorological data from a weather station via Modbus TCP,
along with all-sky camera imagery and external weather service integrations.

The dashboard displays:
    - Current weather conditions (temperature, humidity, pressure, wind, rain)
    - Historical weather plots (configurable time ranges)
    - Wind rose visualization
    - All-sky camera live view
    - Sun/moon information (sunrise, sunset, moon phase)
    - Embedded external services (INPE satellite, WeatherBug)

Modbus Communication:
    Weather station data is read via Modbus TCP protocol using register mappings
    defined in sigma.yaml.

Architecture:
    - Dash web framework for UI rendering and callbacks
    - Plotly for interactive charts
    - 10-second update interval for live data
    - 4-day rolling data buffer
    - Graceful degradation to offline mode on connection failure

Usage:
    Run dashboard:
        $ python -m src

Configuration:
    - config.yaml: Dashboard settings (location, time options)
    - sigma.yaml: Weather station Modbus register map (optional)
    - oculus.yaml: All-sky camera settings (optional)

Author: OASI Team
Date: 2025
"""

import dash
from dash import html, dcc, Input, Output
import plotly.graph_objs as go
import pandas as pd
import numpy as np
import datetime
from concurrent.futures import ThreadPoolExecutor
import yaml
from flask import send_file, abort, request
from .util import get_moon_phase, get_sun_times, get_moon_times
from .weatherstation import read_weather_station, _build_offline_row, _format_metric
from .allsky import read_allsky, get_camera_status
from .database import WeatherDatabase, get_yearly_db_path
import os

# ============================================================================
# Global State Variables
# ============================================================================

#: list: Rolling buffer of weather data records (max 4 days)
weather_data = []

#: list: High-frequency in-memory buffer used only for wind direction visuals
live_wind_data = []

#: WeatherDatabase: Persistent storage for weather readings
db = None

#: datetime: Timestamp of last database save
last_db_save = None

#: datetime: Timestamp of the last point successfully stored in the database
last_db_point_time = None

# Cached astronomical data to avoid external API call on every 1 Hz refresh
sun_times_cache_date = None
sunrise_cached = "N/D"
sunset_cached = "N/D"
sun_times_last_attempt_at = None

# Async weather acquisition state (prevents 1 Hz UI callback from blocking).
station_executor = ThreadPoolExecutor(max_workers=1)
station_future = None
latest_station_row = None
latest_station_status = "Desconectado"
latest_station_online = False
last_station_poll = None

# ============================================================================
# Server and Observatory Configuration Loading
# ============================================================================

# Load dashboard configuration from YAML
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)

MEMORY_RETENTION_DAYS = int(config.get('IN_MEMORY_RETENTION_DAYS', 2))
MAX_PLOT_POINTS = int(config.get('MAX_PLOT_POINTS', 4000))
WEATHER_FETCH_INTERVAL_SECONDS = float(config.get('WEATHER_FETCH_INTERVAL_SECONDS', 1))
MEMORY_MAX_ROWS = int(config.get('MEMORY_MAX_ROWS', 50000))
STARTUP_PRELOAD_LATEST_ONLY = bool(config.get('STARTUP_PRELOAD_LATEST_ONLY', False))
STARTUP_LOAD_MINUTES = int(config.get('STARTUP_LOAD_MINUTES', 1440))
STARTUP_MAX_ROWS = int(config.get('STARTUP_MAX_ROWS', 20000))
DROPDOWN_QUERY_MAX_ROWS = int(config.get('DROPDOWN_QUERY_MAX_ROWS', 80000))
PLOT_TARGET_POINTS = int(config.get('PLOT_TARGET_POINTS', 1200))
INPE_UPDATE_INTERVAL_SECONDS = int(config.get('INPE_UPDATE_INTERVAL_SECONDS', 600))

# Initialize database with yearly file pattern
db_pattern = config.get('DATABASE_PATH_PATTERN', 'weather_data_{year}.db')
db_dir = os.path.dirname(__file__)
db_path = get_yearly_db_path(db_pattern, db_dir)
db = WeatherDatabase(str(db_path))

# Load data on startup.
# Default behavior preloads recent history from DB to avoid empty startup plots.
if STARTUP_PRELOAD_LATEST_ONLY:
    latest_reading = db.get_latest_reading()
    startup_data = pd.DataFrame([latest_reading]) if latest_reading is not None else pd.DataFrame()
else:
    startup_data = db.get_readings_since(
        minutes=STARTUP_LOAD_MINUTES,
        max_rows=STARTUP_MAX_ROWS,
    )
    if startup_data.empty:
        latest_reading = db.get_latest_reading()
        if latest_reading is not None:
            startup_data = pd.DataFrame([latest_reading])

if not startup_data.empty:
    weather_data = startup_data.to_dict('records')
    if 'date' in startup_data.columns:
        last_db_point_time = pd.to_datetime(startup_data['date']).max()
#    print(f"Loaded {len(weather_data)} readings from database at {db_path}")
else:
    print(f"Starting with empty database at {db_path}")

# ============================================================================
# Dash Application Setup
# ============================================================================

# Initialize Dash
app = dash.Dash(__name__, prevent_initial_callbacks=False, update_title=None)
app.title = "OASI-Weather"


@app.server.after_request
def disable_dash_bundle_cache(response):
    """Prevent stale frontend bundles that can cause ChunkLoadError after restarts."""
    path = request.path or ""
    if path.startswith('/_dash-component-suites/') or path in {
        '/_dash-layout',
        '/_dash-dependencies',
        '/_dash-update-component',
    }:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.server.route('/allsky/latest.jpg')
def serve_allsky_latest():
    """Serve latest all-sky image from data directory (outside assets)."""
    latest_path = os.path.join(os.path.dirname(__file__), 'data', 'allsky', 'latest.jpg')
    if not os.path.exists(latest_path):
        abort(404)
    return send_file(latest_path, mimetype='image/jpeg', max_age=0, conditional=True)

# 
app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>OASI-Weather</title>
        {%favicon%}
        <link rel="icon" type="image/jpeg" href="/assets/logo-impacton_round.png">
        {%css%}
        <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
        <style>
            body {
                background-color: #000000 !important;
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

# ----------- Layout ----------- #
app.layout = html.Div(
    className="main-container",
    children=[
        # ----------- Header: Logo, Title, Status ----------- #
        html.Div([
            html.Div([
                # Logo on the left
                html.Img(
                    src='https://github.com/GCP-ON/OASI-Weather/blob/master/src/assets/logo-impacton_round.png?raw=true',
                    className="logo-img"
                ),
                # Title and subtitle in the center
                html.Div([
                    html.H1(
                        "Observatório Astronômico do Sertão de Itaparica",
                        className="header-title"
                    ),
                    html.H3(
                        "Estação Meteorológica e Câmera de Todo o Céu",
                        className="header-title"
                    ),
                    html.Div([
                        html.Span("📍︎", className="location-icon"),
                        html.Span("8° 47' 32,1\" S, 38° 41' 18,7\" O, 390 m", className="location-text")
                    ], className="header-location")
                ], className="header-title"),
                # Logo ON on the right
                html.Img(
                    src='/assets/logo-on_round.png',
                    className="logo-on-img"
                )
            ], className="header")
        ], className="header-bar"),

        # ----------- Status Row ----------- #
        html.Div([
            html.Div(
                id='loop-status',
                className="loop-status-box",
                children=[
                    html.Div(id='loop-active-indicator'),
                    html.Div(id='camera-status-indicator')
                ]
            )
        ], className="status-row"),

        # ----------- Main Row: 50/50 Split ----------- #
        html.Div([
            # Left half: 3-row grid
            html.Div([
                # Row 1: Weather conditions and wind rose
                html.Div([
                    html.Div(id='info-box', className="info-box")
                ], className="grid-card info-box-container"),
                html.Div([
                    dcc.Graph(
                        id='wind-rose-plot',
                        className='wind-plot-graph',
                        config={'displayModeBar': False, 'responsive': True}
                    )
                ], className="grid-card wind-card"),
                # Row 2: Astronomical information (spans both columns)
                html.Div([
                    html.Div(id='astro-info-box', className="astro-info-box")
                ], className="grid-card astro-card"),
                # Row 3: WeatherBug and INPE
                html.Div([
                    html.Iframe(
                        src=f"https://lxapp.weatherbug.net/v2/lxapp_impl.html?lat={config['LATITUDE']}&lon={config['LONGITUDE']}&tv=1.8.1&nocache=1",
                        className="weatherbug-iframe"
                    )
                ], className="grid-card iframe-card"),
                html.Div([
                    html.Iframe(
                        id='inpe-iframe',
                        src="https://www.cptec.inpe.br/dsat/?product=true_color_ch13_dsa&product_opacity=1&date=now&zoom=6&x=4501.7596&y=3177.0654&animate=true&t=350.00",
                        className="inpe-iframe"
                    )
                ], className="grid-card iframe-card")
            ], className="left-half left-grid"),
            # Right half: all-sky image
            html.Div([
                html.Div([
                    html.Div(
                        id='last-update-time',
                        className='allsky-timestamp'
                    ),
                    html.Img(
                        id='all-sky-img',
                        src='data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="1" height="1"%3E%3Crect width="1" height="1" fill="%23000000"/%3E%3C/svg%3E',
                        className='all-sky-img',
                        style={'filter': 'brightness(100%)'}
                    ),
                    html.Div([
                        html.Label('Brilho:', className='brightness-label'),
                        dcc.Slider(
                            id='brightness-slider',
                            min=50,
                            max=200,
                            step=5,
                            value=100,
                            marks={
                                50: {'label': '50%', 'style': {'color': '#eef4fa'}},
                                100: {'label': '100%', 'style': {'color': '#eef4fa'}},
                                150: {'label': '150%', 'style': {'color': '#eef4fa'}},
                                200: {'label': '200%', 'style': {'color': '#eef4fa'}},
                            },
                        )
                    ], className='brightness-control')
                ], className='all-sky-container')
            ], className='right-half'),
        ], className="main-row"),

        # ----------- Divider Line ----------- #
        html.Hr(className="hr-divider"),

        # ----------- Bottom Container: Time Selector + Plots ----------- #
        html.Div([
            # ----------- Time Selector (above plots) ----------- #
            html.Div([
                html.Div([
                    html.Label("Escala de tempo:", className="time-selector-label"),
                    dcc.Dropdown(
                        id='time-range-dropdown',
                        options=[
                            {'label': k, 'value': v} for k, v in config['TIME_OPTIONS'].items()
                        ],
                        value=60,
                        searchable=False,
                        clearable=False,
                        className="time-selector-dropdown"
                    )
                ], className="time-selector-box"),
            ], className="time-selector-container"),

            # ----------- Plots Row ----------- #
            html.Div([
                # Left column: temperature and pressure
                html.Div([
                    dcc.Graph(id='temperature-plot', className='plot-graph'),
                    dcc.Graph(id='pressure-plot', className='plot-graph'),
                ], className="plot-col"),
                # Center column: humidity and dew point
                html.Div([
                    dcc.Graph(id='humidity-plot', className='plot-graph'),
                    dcc.Graph(id='dew-point-plot', className='plot-graph'),
                ], className="plot-col"),
                # Right column: wind speed and wind direction
                html.Div([
                    dcc.Graph(id='wind-speed-plot', className='plot-graph'),
                    dcc.Graph(id='wind-dir-plot', className='plot-graph'),
                ], className="plot-col"),
            ], className="plots-row"),
            html.Div(
                id='plot-last-update',
                className='plot-last-update',
                children='Ultima atualizacao: --/--/---- --:--:--'
            ),
            html.Div(
                className='plot-update-frequency',
                children=f"Frequencia de atualizacao dos plots: a cada {int(config.get('DATABASE_SAVE_INTERVAL_SECONDS', 600))} s"
            ),
        ], className="bottom-container"),

        # ----------- Observation Condition Rules (Above Footer) ----------- #
        html.Div([
            html.H4("Condições de Observação", className='obs-criteria-title'),
            html.Div([
                html.Div([
                    html.Span('', className='obs-chip obs-chip-good'),
                    html.Span(
                        "Boas: Vento max (30 min) < 12 m/s e ponto de orvalho max (30 min) < temperatura minima (30 min) - 2°C."
                    )
                ], className='obs-criteria-item'),
                html.Div([
                    html.Span('', className='obs-chip obs-chip-medium'),
                    html.Span(
                        "Medias: Vento max (30 min) < 15 m/s, vento medio (30 min) < 12 m/s e ponto de orvalho max (30 min) < temperatura minima (30 min)."
                    )
                ], className='obs-criteria-item'),
                html.Div([
                    html.Span('', className='obs-chip obs-chip-bad'),
                    html.Span("Ruins: Qualquer condicao fora dos criterios acima.")
                ], className='obs-criteria-item'),
                html.Div([
                    html.Span('', className='obs-chip obs-chip-na'),
                    html.Span("N/D: Sem dados validos ou durante o periodo diurno.")
                ], className='obs-criteria-item'),
            ], className='obs-criteria-grid')
        ], className='obs-criteria-container'),

        # ----------- Interval for Updates ----------- #
        dcc.Interval(
            id='clock-interval',
            interval=max(1, int(config.get('UPDATE_INTERVAL_SECONDS', 2))) * 1000,
            n_intervals=0
        ),
        dcc.Interval(
            id='allsky-interval',
            interval=config.get('ALLSKY_UPDATE_INTERVAL_SECONDS', config['UPDATE_INTERVAL_SECONDS']) * 1000,
            n_intervals=0
        ),
        dcc.Interval(
            id='inpe-interval',
            interval=max(60, INPE_UPDATE_INTERVAL_SECONDS) * 1000,
            n_intervals=0
        ),

        # ----------- Footer ----------- #
        html.Footer(
            "OASI-Weather | Observatório Astronômico do Sertão de Itaparica",
            className="footer"
        )
    ]
)

# ============================================================================
# Dashboard Callbacks
# ============================================================================


def _get_cached_sun_times():
    """Return sunrise/sunset with lightweight daily cache.

    At 1 Hz UI updates, querying the sunrise API every callback can block
    dashboard rendering. This cache refreshes once per day.
    """
    global sun_times_cache_date, sunrise_cached, sunset_cached, sun_times_last_attempt_at
    now_local = datetime.datetime.now()
    today = now_local.date()
    retry_seconds = int(config.get('SUN_TIMES_RETRY_SECONDS', 300))

    needs_daily_refresh = (sun_times_cache_date != today)
    cache_invalid = (sunrise_cached in (None, 'N/D')) or (sunset_cached in (None, 'N/D'))
    retry_due = (
        sun_times_last_attempt_at is None
        or (now_local - sun_times_last_attempt_at).total_seconds() >= retry_seconds
    )

    if needs_daily_refresh or (cache_invalid and retry_due):
        sun_times_last_attempt_at = now_local
        sunrise_new, sunset_new = get_sun_times(config['LATITUDE'], config['LONGITUDE'])
        # Keep last valid values when API call fails temporarily.
        if sunrise_new != 'N/D' and sunset_new != 'N/D':
            sunrise_cached, sunset_cached = sunrise_new, sunset_new
        elif sunrise_cached in (None, 'N/D') or sunset_cached in (None, 'N/D'):
            sunrise_cached, sunset_cached = 'N/D', 'N/D'
        sun_times_cache_date = today
    return sunrise_cached or 'N/D', sunset_cached or 'N/D'


def _compute_daytime_state(sunrise, sunset):
    """Return True (day), False (night), or None (unknown)."""
    if sunrise == 'N/D' or sunset == 'N/D':
        return None

    def _parse_local_time(value):
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                return datetime.datetime.strptime(value, fmt).time()
            except ValueError:
                continue
        return None

    sunrise_time = _parse_local_time(sunrise)
    sunset_time = _parse_local_time(sunset)
    if sunrise_time is None or sunset_time is None:
        return None

    tz_local = datetime.timezone(datetime.timedelta(hours=-3))
    now_local = datetime.datetime.now(tz_local)
    sunrise_dt = datetime.datetime.combine(now_local.date(), sunrise_time, tzinfo=tz_local)
    sunset_dt = datetime.datetime.combine(now_local.date(), sunset_time, tzinfo=tz_local)

    if sunset_dt <= sunrise_dt:
        sunset_dt += datetime.timedelta(days=1)

    if now_local < sunrise_dt:
        sunrise_dt -= datetime.timedelta(days=1)
        sunset_dt -= datetime.timedelta(days=1)

    return sunrise_dt <= now_local <= sunset_dt


def _start_station_fetch(station_config_path):
    """Submit a non-blocking weather station read if worker is idle."""
    global station_future, last_station_poll
    if station_future is None:
        station_future = station_executor.submit(read_weather_station, station_config_path)
        last_station_poll = datetime.datetime.now()


def _downsample_dataframe(df, target_points):
    """Downsample to a near-constant point count for stable plot performance."""
    if df.empty:
        return df
    target = max(1, int(target_points))
    if len(df) <= target:
        return df
    idx = np.linspace(0, len(df) - 1, target, dtype=int)
    return df.iloc[idx].copy()


def _build_inpe_url(now=None):
    """Build INPE URL with UTC date rounded down to 10-minute boundary."""
    if now is None:
        now = datetime.datetime.utcnow()
    rounded = now.replace(second=0, microsecond=0, minute=(now.minute // 10) * 10)
    date_param = rounded.strftime('%Y%m%d%H%M')
    return (
        "https://www.cptec.inpe.br/dsat/?product=true_color_ch13_dsa"
        f"&product_opacity=1&date={date_param}&zoom=6&x=4501.7596&y=3177.0654"
        "&animate=true&t=350.00"
    )


def _is_daytime_now():
    """Return True when local time is between cached sunrise and sunset."""
    sunrise, sunset = _get_cached_sun_times()
    state = _compute_daytime_state(sunrise, sunset)
    return bool(state) if state is not None else False


def _format_wind_dir_with_cardinal(value):
    """Format wind direction as angle plus nearest cardinal point."""
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return 'N/D'

    if pd.isna(angle):
        return 'N/D'

    angle = angle % 360.0
    cardinals = ['N', 'NE', 'L', 'SE', 'S', 'SO', 'O', 'NO']
    idx = int((angle + 22.5) // 45) % len(cardinals)
    return f"{angle:.2f}° ({cardinals[idx]})"

@app.callback(
    [
        Output('info-box', 'children'),
        Output('astro-info-box', 'children'),
        Output('temperature-plot', 'figure'),
        Output('humidity-plot', 'figure'),
        Output('dew-point-plot', 'figure'),
        Output('pressure-plot', 'figure'),
        Output('wind-speed-plot', 'figure'),
        Output('wind-dir-plot', 'figure'),
        Output('wind-rose-plot', 'figure'),
        Output('plot-last-update', 'children'),
        Output('loop-active-indicator', 'children'),
        Output('camera-status-indicator', 'children'),
    ],
    [
        Input('time-range-dropdown', 'value'),
        Input('clock-interval', 'n_intervals'),
    ]
)
def update_dashboard(minutes, n_intervals):
    """Main callback function to update all dashboard components.
    
    This callback is triggered by the DB-aligned clock interval or when the
    time range dropdown is changed. It fetches new weather data, updates the
    rolling data buffer, filters data for the selected time range, and
    regenerates all UI components with the latest information.
    
    Data Flow:
        1. Fetch new data from weather station
        2. Append to rolling buffer (keeps last 4 days)
        3. Filter data for selected time range
        4. Build info box with current conditions
        5. Generate 6 weather plots
        6. Update status indicators
    
    Args:
        minutes (int): Time range in minutes selected by user (from dropdown).
        n_intervals (int): Number of interval ticks elapsed (triggers updates).
    
    Returns:
        tuple: Contains 8 elements in order:
            - info_box (html.Div): Current conditions and location info panel
            - temp_fig (go.Figure): Temperature timeseries plot
            - hum_fig (go.Figure): Humidity timeseries plot
            - dew_fig (go.Figure): Dew point timeseries plot
            - pressure_fig (go.Figure): Pressure timeseries plot
            - wind_fig (go.Figure): Wind speed timeseries plot
            - dir_fig (go.Figure): Wind direction timeseries plot
            - status_indicator (html.Span): Status label with color
    
    Raises:
        Exception: Caught internally. Connection failures result in offline mode.
    
    Note:
        Uses global variables `weather_data`, `db`, and `last_db_save`.
    """
    global weather_data, live_wind_data, db, last_db_save, last_db_point_time
    global station_future, latest_station_row, latest_station_status
    global latest_station_online, last_station_poll
    
    # Current timestamp
    now = datetime.datetime.now()
    triggered_id = None
    if hasattr(dash, 'ctx'):
        triggered_id = dash.ctx.triggered_id
    if triggered_id is None:
        # Compatibility fallback for older Dash versions.
        callback_ctx = dash.callback_context
        if callback_ctx.triggered:
            triggered_id = callback_ctx.triggered[0]['prop_id'].split('.')[0]
    is_time_range_change = triggered_id == 'time-range-dropdown'

    # Resolve station config path relative to the package when not absolute.
    station_config = config.get('WEATHER_STATION_CONFIG', 'sigma.yaml')
    if not os.path.isabs(station_config):
        station_config = os.path.join(os.path.dirname(__file__), station_config)

    # Keep acquisition always active, throttled by WEATHER_FETCH_INTERVAL_SECONDS.
    if station_future is None and (
        last_station_poll is None
        or (now - last_station_poll).total_seconds() >= WEATHER_FETCH_INTERVAL_SECONDS
    ):
        _start_station_fetch(station_config)

    if station_future is not None and station_future.done():
        try:
            fetched_row = station_future.result()
            latest_station_row = fetched_row
            latest_station_online = bool(fetched_row.get('station_online', True))
            latest_station_status = "Conectado" if latest_station_online else "Desconectado"
        except Exception:
            latest_station_row = None
            latest_station_status = "Desconectado"
            latest_station_online = False
        finally:
            station_future = None

    if latest_station_row is not None:
        new_row = dict(latest_station_row)
        new_row['date'] = now
        loop_status = "Conectado" if latest_station_online else "Desconectado"
        loop_color = "#5eb9d2" if latest_station_online else "#d95252"
    else:
        loop_status = "Desconectado"
        loop_color = "#d95252"
        new_row = _build_offline_row(now)

    # Keep a high-frequency buffer for wind direction/rose visuals (2s cadence).
    live_wind_data.append(new_row)
    live_cutoff = now - datetime.timedelta(minutes=30)
    live_wind_data = [row for row in live_wind_data if row['date'] >= live_cutoff]
    if len(live_wind_data) > 2000:
        live_wind_data = live_wind_data[-2000:]

    # Save to database periodically (every DATABASE_SAVE_INTERVAL_SECONDS).
    # Skip writes on dropdown-only trigger to keep interaction lightweight.
    save_interval = config.get('DATABASE_SAVE_INTERVAL_SECONDS', 600)
    if (not is_time_range_change) and (
        last_db_save is None or (now - last_db_save).total_seconds() >= save_interval
    ):
        try:
            inserted_id = db.insert_reading(new_row)
            if inserted_id is not None:
                last_db_save = now
                last_db_point_time = pd.to_datetime(new_row['date'])
                # Keep in-memory cache aligned with persisted points only.
                weather_data.append(new_row)
                weather_data = [
                    row for row in weather_data
                    if row['date'] >= (now - datetime.timedelta(days=MEMORY_RETENTION_DAYS))
                ]
                if len(weather_data) > MEMORY_MAX_ROWS:
                    weather_data = weather_data[-MEMORY_MAX_ROWS:]
        except Exception as e:
            print(f"Warning: Failed to save to database: {e}")

    # Build plot dataset from persisted DB points for time-scale consistency.
    db_range = db.get_readings_since(minutes=minutes, max_rows=DROPDOWN_QUERY_MAX_ROWS)
    if not db_range.empty:
        filtered = db_range.to_dict('records')
    else:
        cutoff = now - datetime.timedelta(minutes=minutes)
        filtered = [row for row in weather_data if row['date'] >= cutoff]

    # Fallback: use latest available data if no data in range
    if not filtered and weather_data:
        filtered = weather_data[-1:]
    
    # Fallback: use offline row if no data at all
    if not filtered:
        filtered = [_build_offline_row(now)]

    # Current conditions remain responsive even between DB save cycles.
    latest = new_row if new_row is not None else filtered[-1]

    # Convert persisted dataset to DataFrame for plotting
    df = pd.DataFrame(filtered)
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')

    # Get astronomical information from cache (refresh once/day)
    sunrise, sunset = _get_cached_sun_times()
    moonrise, moonset = get_moon_times(
        config['LATITUDE'],
        config['LONGITUDE'],
        sunrise_str=sunrise,
    )

    # Keep plots at a near-constant density across time scales.
    # This prevents very heavy traces when users select long time windows.
    plot_points_target = min(MAX_PLOT_POINTS, PLOT_TARGET_POINTS)
    df = _downsample_dataframe(df, plot_points_target)

    # Calculate observation conditions based on last 30 minutes.
    # Keep daytime suppression when known, but do not get stuck in N/D when
    # sunrise/sunset are temporarily unavailable.
    daytime_state = _compute_daytime_state(sunrise, sunset)
    is_daytime = (daytime_state is True)

    cutoff_30min = now - datetime.timedelta(minutes=30)

    def _row_has_valid_obs_metrics(row):
        """Return True when row has all metrics required for obs-condition scoring."""
        wind = row.get('wind_speed', np.nan)
        temp = row.get('temperature', np.nan)
        dew = row.get('dew_point', np.nan)
        return (not pd.isna(wind)) and (not pd.isna(temp)) and (not pd.isna(dew))

    # Prefer persisted DB points for observation-condition calculations.
    db_last_30 = db.get_readings_since(minutes=30, max_rows=5000)
    if not db_last_30.empty:
        last_30min = db_last_30.to_dict('records')
    else:
        last_30min = [row for row in live_wind_data if row['date'] >= cutoff_30min]

    # Keep 2s responsiveness by appending current reading if it is newer than DB window.
    if new_row.get('date') is not None and new_row['date'] >= cutoff_30min:
        has_wind = not pd.isna(new_row.get('wind_dir', np.nan)) and not pd.isna(new_row.get('wind_speed', np.nan))
        if has_wind:
            if not last_30min or pd.to_datetime(last_30min[-1].get('date')) != pd.to_datetime(new_row['date']):
                last_30min.append(new_row)

    # After sunset, keep observation status updated even if the last 30 minutes
    # still contain daytime-only data or sparse samples. In that case, fallback
    # to the newest valid persisted rows so status does not remain N/D.
    obs_rows = list(last_30min)
    if not is_daytime:
        valid_obs_rows = [row for row in obs_rows if _row_has_valid_obs_metrics(row)]
        if not valid_obs_rows:
            fallback_recent = db.get_readings_since(minutes=720, max_rows=3000)
            if not fallback_recent.empty:
                fallback_rows = fallback_recent.to_dict('records')
                valid_obs_rows = [row for row in fallback_rows if _row_has_valid_obs_metrics(row)]
            if valid_obs_rows:
                # Use a bounded recent sample to keep behavior stable.
                obs_rows = valid_obs_rows[-30:]

    obs_color = '#808080'  # Gray (N/D or daytime)
    obs_status = 'N/D'

    if (not is_daytime) and obs_rows:
        # Extract 30-minute window metrics, filtering out invalid values.
        wind_speeds = [row.get('wind_speed', np.nan) for row in obs_rows if not pd.isna(row.get('wind_speed', np.nan))]
        temperatures = [row.get('temperature', np.nan) for row in obs_rows if not pd.isna(row.get('temperature', np.nan))]
        dew_points = [row.get('dew_point', np.nan) for row in obs_rows if not pd.isna(row.get('dew_point', np.nan))]
        
        wind_speed_max = max(wind_speeds) if wind_speeds else np.nan
        wind_speed_avg = np.mean(wind_speeds) if wind_speeds else np.nan
        temp_min = min(temperatures) if temperatures else np.nan
        dew_point_max = max(dew_points) if dew_points else np.nan
        
        # Determine observation conditions color and status
        if (not pd.isna(wind_speed_max) and not pd.isna(wind_speed_avg)
                and not pd.isna(temp_min) and not pd.isna(dew_point_max)):
            # Green: stricter wind and dew-point margin.
            if wind_speed_max < 12 and dew_point_max < temp_min - 2:
                obs_color = '#2ecc71'  # Green
                obs_status = 'Boas'
            # Yellow: acceptable wind and dew point below minimum temperature.
            elif (wind_speed_max < 15 and 
                  wind_speed_avg < 12 and
                  dew_point_max < temp_min):
                obs_color = '#f1c40f'  # Yellow
                obs_status = 'Medias'
            # Red: otherwise
            else:
                obs_color = '#e74c3c'  # Red
                obs_status = 'Ruins'

    # Wind rose data is rebuilt every callback tick (2s) from the full
    # 30-minute DB window plus the live 2-second buffer overlay. This keeps
    # random sampling/top-5/latest responsive without losing recent history.
    rose_rows_db = db_last_30.to_dict('records') if not db_last_30.empty else []
    rose_rows_live = [row for row in live_wind_data if row['date'] >= cutoff_30min]
    rose_rows_by_ts = {}

    for row in rose_rows_db:
        ts = pd.to_datetime(row.get('date'), errors='coerce')
        if pd.isna(ts):
            continue
        rose_rows_by_ts[ts] = row

    for row in rose_rows_live:
        ts = pd.to_datetime(row.get('date'), errors='coerce')
        if pd.isna(ts):
            continue
        # Live rows overwrite same-timestamp DB rows.
        rose_rows_by_ts[ts] = row

    rose_rows = [rose_rows_by_ts[k] for k in sorted(rose_rows_by_ts.keys())]
    if not rose_rows and last_30min:
        rose_rows = list(last_30min)

    # Build 30-minute wind points for the wind rose.
    # Every refresh (2s), randomly resample the downsampled pool while always
    # keeping top-5 wind speeds and latest reading as dedicated overlay layers.
    wind_candidates = []
    for row in rose_rows:
        raw_dir = row.get('wind_dir', np.nan)
        raw_speed = row.get('wind_speed', np.nan)
        if pd.isna(raw_dir) or pd.isna(raw_speed):
            continue
        date_value = pd.to_datetime(row.get('date'), errors='coerce')
        if pd.isna(date_value):
            continue
        wind_candidates.append({
            'date': date_value,
            'dir': float(raw_dir) % 360.0,
            'speed': float(raw_speed),
        })

    wind_candidates = sorted(wind_candidates, key=lambda p: p['date'])
    sampled_points = []
    top5_points = []
    latest_point = None
    avg_dir_30min = None
    if wind_candidates:
        # Circular mean avoids invalid arithmetic around 0/360 boundary.
        dirs_rad = np.deg2rad(np.array([p['dir'] for p in wind_candidates], dtype=float))
        mean_sin = float(np.mean(np.sin(dirs_rad)))
        mean_cos = float(np.mean(np.cos(dirs_rad)))
        if (abs(mean_sin) > 1e-9) or (abs(mean_cos) > 1e-9):
            avg_dir_30min = (np.degrees(np.arctan2(mean_sin, mean_cos)) + 360.0) % 360.0

        # Keep a fixed-size random base cloud so points visibly reshuffle
        # every callback tick instead of often selecting almost all points.
        base_random_target = 80
        latest_index = len(wind_candidates) - 1
        speed_values = np.array([p['speed'] for p in wind_candidates], dtype=float)
        top_n = min(5, len(wind_candidates))
        top5_indices = np.argsort(speed_values)[-top_n:]

        preserve_indices = set(int(i) for i in top5_indices)
        preserve_indices.add(latest_index)

        all_indices = np.arange(len(wind_candidates), dtype=int)
        remaining_indices = [int(i) for i in all_indices if int(i) not in preserve_indices]
        sampled_slots = min(len(remaining_indices), max(0, base_random_target))

        sampled_indices = []
        if sampled_slots > 0 and remaining_indices:
            rng = np.random.default_rng(seed=int(n_intervals))
            sampled_indices = [
                int(i) for i in rng.choice(remaining_indices, size=sampled_slots, replace=False)
            ]

        sampled_points = [wind_candidates[i] for i in sorted(sampled_indices)]
        top5_points = [wind_candidates[i] for i in sorted(int(i) for i in top5_indices)]
        latest_point = wind_candidates[latest_index]

    info_box = html.Div([
        html.P([
            "Temperatura: ",
            html.Span(_format_metric(latest.get('temperature'), '.2f', '°C'), className="color-temp")
        ]),
        html.P([
            "Umidade: ",
            html.Span(_format_metric(latest.get('humidity'), '.2f', '%'), className="color-humidity")
        ]),
        html.P([
            "Ponto de orvalho: ",
            html.Span(_format_metric(latest.get('dew_point'), '.2f', '°C'), className="color-dew")
        ]),
        html.P([
            "Velocidade do vento: ",
            html.Span(_format_metric(latest.get('wind_speed'), '.2f', 'm/s'), className="color-wind-speed")
        ]),
        html.P([
            "Direção do vento: ",
            html.Span(_format_wind_dir_with_cardinal(latest.get('wind_dir')), className="color-wind-dir")
        ]),
        html.P([
            "Pressão: ",
            html.Span(_format_metric(latest.get('pressure'), '.2f', 'hPa'), className="color-location")
        ]),
        # html.P([
        #     "Tensão bateria: ",
        #     html.Span(_format_metric(latest.get('battery_voltage'), '.2f', 'V'), className="color-location")
        # ]),
        html.P([
            "Chuva (hora): ",
            html.Span(_format_metric(latest.get('rain_hour'), '.2f', 'mm/h'), className="color-location")
        ]),
        html.P([
            "Condições de Observação: ",
            html.Span(
                '',
                title=f'Condição: {obs_status}',
                style={
                    'display': 'inline-block',
                    'width': '28px',
                    'height': '28px',
                    'margin-left': '12px',
                    'border-radius': '6px',
                    'border': '2px solid #c0c0c0',
                    'backgroundColor': obs_color,
                    'verticalAlign': 'middle',
                }
            )
        ], style={
            'display': 'flex',
            'justifyContent': 'center',
            'alignItems': 'center',
            'fontWeight': 'bold',
            'textAlign': 'center',
        })
    ])
    
    # Astronomical information box
    astro_info_box = html.Div([
        html.Div([
            html.Div([
                html.P([
                    "Nascer do Sol: ",
                    html.Span(f"{sunrise}", className="color-sun")
                ]),
                html.P([
                    "Ocaso do Sol: ",
                    html.Span(f"{sunset}", className="color-sun")
                ])
            ], className="astro-col-left"),
            html.Div([
                html.P([
                    "Nascer da Lua: ",
                    html.Span(f"{moonrise}", className="color-moon-time")
                ]),
                html.P([
                    "Ocaso da Lua: ",
                    html.Span(f"{moonset}", className="color-moon-time")
                ])
            ], className="astro-col-right")
        ], className="astro-grid-top"),
        html.Div([
            html.P([
                "Fase da Lua: ",
                html.Span(get_moon_phase(), className="color-moon")
            ])
        ], className="astro-phase-bottom")
    ])
    
    sampled_dirs = np.array([p['dir'] for p in sampled_points], dtype=float)
    sampled_speeds = np.array([p['speed'] for p in sampled_points], dtype=float)
    top5_dirs = np.array([p['dir'] for p in top5_points], dtype=float)
    top5_speeds = np.array([p['speed'] for p in top5_points], dtype=float)

    wind_rose_data = [
        go.Barpolar(
            # Base random downsample from the last 30 minutes.
            r=np.ones_like(sampled_dirs),
            theta=sampled_dirs,
            width=np.full_like(sampled_dirs, 3.0),
            marker=dict(
                color=sampled_speeds,
                colorscale='Turbo',
                cmin=0,
                cmax=15,
                showscale=True,
                colorbar=dict(
                    title='Velocidade (m/s)',
                    orientation='h',
                    x=0.5,
                    xanchor='center',
                    y=-0.22,
                    yanchor='top',
                    len=0.9,
                    thickness=16,
                ),
            ),
            opacity=0.72,
            name='Amostra aleatória (30 min)',
            hovertemplate='Direção: %{theta:.0f}°<br>Velocidade: %{marker.color:.1f} m/s<extra></extra>',
        )
    ]

    if len(top5_dirs) > 0:
        wind_rose_data.append(
            go.Barpolar(
                # Rays for top-5 speeds.
                r=np.ones_like(top5_dirs),
                theta=top5_dirs,
                width=np.full_like(top5_dirs, 3.0),
                marker=dict(
                    color=top5_speeds,
                    colorscale='Turbo',
                    cmin=0,
                    cmax=15,
                    showscale=False,
                ),
                opacity=0.95,
                hoverinfo='skip',
                showlegend=False,
            )
        )
        wind_rose_data.append(
            go.Scatterpolar(
                # Top-5 wind speeds overlay (above base and its own rays).
                r=np.ones_like(top5_dirs),
                theta=top5_dirs,
                mode='markers',
                marker=dict(
                    size=12,
                    color='#ffe08a',
                    symbol='diamond',
                    line=dict(color='#1f1f1f', width=1.4),
                ),
                name='Top 5 velocidade',
                hovertemplate='Top 5<br>Direção: %{theta:.0f}°<br>Velocidade: %{customdata:.1f} m/s<extra></extra>',
                customdata=top5_speeds,
                showlegend=False,
            )
        )

    if avg_dir_30min is not None:
        wind_rose_data.append(
            go.Scatterpolar(
                # Soft glow base for a modern average-direction indicator.
                r=[0.0, 1.0],
                theta=[avg_dir_30min, avg_dir_30min],
                mode='lines',
                line=dict(color='rgba(255, 255, 255, 0.26)', width=10),
                hoverinfo='skip',
                showlegend=False,
            )
        )
        wind_rose_data.append(
            go.Scatterpolar(
                # Crisp foreground ray with endpoint marker.
                r=[0.0, 1.0],
                theta=[avg_dir_30min, avg_dir_30min],
                mode='lines+markers',
                line=dict(color='#ffffff', width=3),
                marker=dict(
                    size=[0, 11],
                    color=['rgba(0,0,0,0)', '#ffffff'],
                    symbol=['circle', 'circle'],
                    line=dict(color='#0f1115', width=1.6),
                ),
                name='Direção média (30 min)',
                hovertemplate='Direção média 30 min: %{theta:.0f}°<extra></extra>',
                showlegend=False,
            )
        )

    if latest_point is not None:
        wind_rose_data.append(
            go.Barpolar(
                # Ray for latest reading.
                r=[1.0],
                theta=[latest_point['dir']],
                width=[3.0],
                marker=dict(
                    color=[latest_point['speed']],
                    colorscale='Turbo',
                    cmin=0,
                    cmax=15,
                    showscale=False,
                ),
                opacity=0.98,
                hoverinfo='skip',
                showlegend=False,
            )
        )
        wind_rose_data.append(
            go.Scatterpolar(
                # Latest reading overlay, drawn last to stay on top.
                r=[1.0],
                theta=[latest_point['dir']],
                mode='markers',
                marker=dict(
                    size=15,
                    color='#ff4d4d',
                    symbol='star-diamond',
                    line=dict(color='#111111', width=1.5),
                ),
                name='Leitura mais recente',
                hovertemplate='Mais recente<br>Direção: %{theta:.0f}°<br>Velocidade: %{customdata:.1f} m/s<extra></extra>',
                customdata=[latest_point['speed']],
                showlegend=False,
            )
        )

    wind_rose_fig = go.Figure(
        data=wind_rose_data,
        layout=go.Layout(
            template='plotly_dark',
            title=dict(
                text='<b>Direção dos Ventos (Últimos 30 min)</b>',
                font=dict(size=14, color='#e0e0e0'),
                x=0.5,
                xanchor='center'
            ),
            polar=dict(
                angularaxis=dict(
                    direction='clockwise',
                    rotation=90,
                    tickmode='array',
                    tickvals=[0, 45, 90, 135, 180, 225, 270, 315],
                    ticktext=['N', 'NE', 'L', 'SE', 'S', 'SO', 'O', 'NO'],
                    color='var(--color-location)'
                ),
                radialaxis=dict(
                    range=[0, 1],
                    showticklabels=False,
                    ticks='',
                    showline=False,
                    color='var(--color-location)'
                )
            ),
            showlegend=False,
            margin=dict(l=40, r=40, t=50, b=88),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            height=None
        )
    )

    # Build all weather plots with consistent styling
    temp_fig = go.Figure(
        data=[
            go.Scatter(
                x=df['date'],
                y=df['temperature'],
                mode='lines',
                name='Temperatura',
                line={'color': '#ef5c42'}
            ),
            go.Scatter(
                x=df['date'],
                y=df['dew_point'],
                mode='lines',
                name='Ponto de Orvalho',
                line={'color': '#aa96e3'}
            )
        ],
        layout={
            'template': 'plotly_dark',
            'title': 'Temperatura e Ponto de Orvalho (°C)',
            # Preserve legend visibility toggles across interval refreshes.
            # Reset only when the selected time range changes.
            'uirevision': f'temp-dew-{int(minutes)}',
            'xaxis': {'title': 'Hora'},
            # Keep a top band free so the legend does not overlap plotted lines.
            'yaxis': {'title': '°C', 'domain': [0.0, 0.86]},
            'showlegend': True,
            'legend': {
                'x': 0.5,
                'y': 0.99,
                'xanchor': 'center',
                'yanchor': 'top',
                'bgcolor': 'rgba(0,0,0,0)',
                'borderwidth': 0,
                'orientation': 'h',
                'itemsizing': 'constant',
                'itemwidth': 30
            },
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )
    dew_fig = go.Figure(
        data=[go.Scatter(x=df['date'], 
                         y=df['dew_point'], 
                         mode='lines', 
                         name='Ponto de Orvalho',
                         line={'color': '#aa96e3'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Ponto de Orvalho (°C)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': '°C'},
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )
    hum_fig = go.Figure(
        data=[go.Scatter(x=df['date'], 
                         y=df['humidity'], 
                         mode='lines', 
                         name='Umidade',
                         line={'color': '#47b0d3'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Umidade (%)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': '%'},
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )
    pressure_fig = go.Figure(
        data=[go.Scatter(x=df['date'], 
                         y=df['pressure'], 
                         mode='lines', 
                         name='Pressão Atmosférica')],
        layout={
            'template': 'plotly_dark',
            'title': 'Pressão Atmosférica (hPa)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': 'hPa'},
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )
    wind_fig = go.Figure(
        data=[
            go.Scatter(
                x=df['date'],
                y=df['wind_speed'],
                mode='lines',
                name='Velocidade do Vento',
                line={'color': '#76d465'},
                showlegend=False
            ),
            go.Scatter(
                x=df['date'],
                y=np.full(len(df), 12.0),
                mode='lines',
                name='12 m/s',
                line={'color': '#f1c40f', 'width': 2, 'dash': 'dash'}
            ),
            go.Scatter(
                x=df['date'],
                y=np.full(len(df), 15.0),
                mode='lines',
                name='15 m/s',
                line={'color': '#e74c3c', 'width': 2, 'dash': 'dash'}
            )
        ],
        layout={
            'template': 'plotly_dark',
            'title': 'Velocidade do Vento (m/s)',
            # Preserve legend visibility toggles across interval refreshes.
            # Reset only when the selected time range changes.
            'uirevision': f'wind-speed-{int(minutes)}',
            'xaxis': {
                'title': 'Hora',
                'range': [now - datetime.timedelta(minutes=int(minutes)), now]
            },
            # Keep a top band free so the legend does not overlap plotted lines.
            'yaxis': {'title': 'm/s', 'domain': [0.0, 0.86]},
            'showlegend': True,
            'legend': {
                'x': 0.5,
                'y': 0.99,
                'xanchor': 'center',
                'yanchor': 'top',
                'bgcolor': 'rgba(0,0,0,0)',
                'borderwidth': 0,
                'orientation': 'h',
                'itemsizing': 'constant',
                'itemwidth': 30
            },
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )
    # Time-series wind direction plot remains a simple line chart,
    # consistent with the other bottom-container plots.
    df_dir_plot = df[['date', 'wind_dir']].copy()
    df_dir_plot['wind_dir'] = pd.to_numeric(df_dir_plot['wind_dir'], errors='coerce')

    dir_fig = go.Figure(
        data=[go.Scatter(x=df_dir_plot['date'], 
                         y=df_dir_plot['wind_dir'], 
                         mode='lines', 
                         name='Direção do Vento',
                         line={'color': '#7fd1b9'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Direção do Vento (°)',
            'xaxis': {
                'title': 'Hora',
                'range': [now - datetime.timedelta(minutes=int(minutes)), now]
            },
            'yaxis': {
                'title': 'Direção (°)',
                'range': [0, 360],
                'fixedrange': True,
                'tickmode': 'array',
                'tickvals': [0, 90, 180, 270, 360],
                'ticktext': ['N (0°)', 'L (90°)', 'S (180°)', 'O (270°)', '']
            },
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )

    camera_connected = get_camera_status()
    if not camera_connected and (not _is_daytime_now()):
        latest_allsky_path = os.path.join(os.path.dirname(__file__), 'data', 'allsky', 'latest.jpg')
        if os.path.exists(latest_allsky_path):
            try:
                image_age_seconds = (now - datetime.datetime.fromtimestamp(os.path.getmtime(latest_allsky_path))).total_seconds()
                fresh_window = max(180, int(config.get('ALLSKY_UPDATE_INTERVAL_SECONDS', 30)) * 4)
                if image_age_seconds <= fresh_window:
                    camera_connected = True
            except Exception:
                pass
    if last_db_point_time is not None:
        ts = pd.to_datetime(last_db_point_time).to_pydatetime()
        plot_update_text = f"Ultima atualizacao: {ts.strftime('%d/%m/%Y %H:%M:%S')}"
    else:
        plot_update_text = "Ultima atualizacao: N/D"

    return (
        info_box,
        astro_info_box,
        temp_fig,
        hum_fig,
        dew_fig,
        pressure_fig,
        wind_fig,
        dir_fig,
        wind_rose_fig,
        plot_update_text,
        html.Span(f"Estação Meteorológica: {loop_status}", className='status-indicator', style={'color': loop_color}),
        html.Span(
            f"Câmera de todo céu: {'Conectado' if camera_connected else 'Desconectado'}",
            className='status-indicator',
            style={'color': '#5eb9d2' if camera_connected else '#d95252'}
        )
    )


@app.callback(
    [
        Output('all-sky-img', 'src'),
        Output('last-update-time', 'children'),
    ],
    [
        Input('allsky-interval', 'n_intervals'),
    ]
)
def update_allsky_image(_n_intervals):
    """Update all-sky image independently and report image refresh time."""
    all_sky_url = read_allsky(config['ALLSKY_CAMERA_CONFIG'])
    if _is_daytime_now():
        return all_sky_url, html.Span("Última atualização: pausa diurna")

    image_update = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    return all_sky_url, html.Span(f"Última atualização: {image_update}")


@app.callback(
    [Output('inpe-iframe', 'src')],
    [Input('inpe-interval', 'n_intervals')]
)
def update_inpe_iframe(_n_intervals):
    """Refresh INPE iframe URL so the date query parameter stays updated."""
    return [_build_inpe_url()]


@app.callback(
    [Output('all-sky-img', 'style')],
    [Input('brightness-slider', 'value')]
)
def update_brightness(brightness_value):
    """Update all-sky image brightness based on slider value."""
    return [{'filter': f'brightness({brightness_value}%)'}]


# ============================================================================
# Application Entry Point
# ============================================================================

if __name__ == '__main__':
    # Application entry point when run directly
    # Bind to all network interfaces to make accessible from other computers
    # Access from other devices at: http://192.168.1.88:<port>
    debug_mode = bool(config.get('DASH_DEBUG', False))
    app.run(
        debug=debug_mode,
        host=config['SERVER_HOST'],
        port=config['SERVER_PORT'],
        use_reloader=debug_mode,
        dev_tools_hot_reload=debug_mode,
    )

