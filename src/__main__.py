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
from flask import send_file, abort
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

#: WeatherDatabase: Persistent storage for weather readings
db = None

#: datetime: Timestamp of last database save
last_db_save = None

# Cached astronomical data to avoid external API call on every 1 Hz refresh
sun_times_cache_date = None
sunrise_cached = "N/D"
sunset_cached = "N/D"

# Async weather acquisition state (prevents 1 Hz UI callback from blocking).
station_executor = ThreadPoolExecutor(max_workers=1)
station_future = None
latest_station_row = None
latest_station_status = "Inicializando"
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

# Initialize database with yearly file pattern
db_pattern = config.get('DATABASE_PATH_PATTERN', 'weather_data_{year}.db')
db_dir = os.path.dirname(__file__)
db_path = get_yearly_db_path(db_pattern, db_dir)
db = WeatherDatabase(str(db_path))

# Load recent data from database on startup (bounded in-memory buffer)
startup_data = db.get_readings_since(minutes=MEMORY_RETENTION_DAYS * 24 * 60)
if not startup_data.empty:
    weather_data = startup_data.to_dict('records')
#    print(f"Loaded {len(weather_data)} readings from database at {db_path}")
else:
    print(f"Starting with empty database at {db_path}")

# ============================================================================
# Dash Application Setup
# ============================================================================

# Initialize Dash
app = dash.Dash(__name__, prevent_initial_callbacks=True, update_title=None)
app.title = "OASI-Weather"


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
        ], className="header"),

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
                        src="https://www.cptec.inpe.br/dsat/?product=true_color_ch13_dsa&product_opacity=1&date=202508051340&zoom=6&x=4560.0000&y=3153.5000&animate=true&t=350.00&options=false&legend=false",
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
                            marks={50: '50%', 100: '100%', 150: '150%', 200: '200%'},
                            tooltip={'placement': 'bottom', 'always_visible': False}
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
        ], className="bottom-container"),

        # ----------- Interval for Updates ----------- #
        dcc.Interval(id='clock-interval', interval=config['UPDATE_INTERVAL_SECONDS'] * 1000, n_intervals=0),
        dcc.Interval(
            id='allsky-interval',
            interval=config.get('ALLSKY_UPDATE_INTERVAL_SECONDS', config['UPDATE_INTERVAL_SECONDS']) * 1000,
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
    global sun_times_cache_date, sunrise_cached, sunset_cached
    today = datetime.date.today()
    if sun_times_cache_date != today:
        sunrise_cached, sunset_cached = get_sun_times(config['LATITUDE'], config['LONGITUDE'])
        sun_times_cache_date = today
    return sunrise_cached, sunset_cached


def _start_station_fetch(station_config_path):
    """Submit a non-blocking weather station read if worker is idle."""
    global station_future, last_station_poll
    if station_future is None:
        station_future = station_executor.submit(read_weather_station, station_config_path)
        last_station_poll = datetime.datetime.now()

@app.callback(
    Output('info-box', 'children'),
    Output('astro-info-box', 'children'),
    Output('temperature-plot', 'figure'),
    Output('humidity-plot', 'figure'),
    Output('dew-point-plot', 'figure'),
    Output('pressure-plot', 'figure'),
    Output('wind-speed-plot', 'figure'),
    Output('wind-dir-plot', 'figure'),
    Output('wind-rose-plot', 'figure'),
    Output('loop-active-indicator', 'children'),
    Output('camera-status-indicator', 'children'),
    Input('time-range-dropdown', 'value'),
    Input('clock-interval', 'n_intervals')
)
def update_dashboard(minutes, n_intervals):
    """Main callback function to update all dashboard components.
    
    This callback is triggered every 10 seconds (clock-interval) or when the
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
        n_intervals (int): Number of 10-second intervals elapsed (triggers updates).
    
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
    global weather_data, db, last_db_save
    global station_future, latest_station_row, latest_station_status
    global latest_station_online, last_station_poll
    
    # Current timestamp
    now = datetime.datetime.now()
    
    # Resolve station config path relative to the package when not absolute
    # Resolve station config path relative to the package when not absolute
    station_config = config.get('WEATHER_STATION_CONFIG', 'sigma.yaml')
    if not os.path.isabs(station_config):
        station_config = os.path.join(os.path.dirname(__file__), station_config)

    # Kick off or collect async weather read without blocking this callback.
    if station_future is None and (
        last_station_poll is None
        or (now - last_station_poll).total_seconds() >= WEATHER_FETCH_INTERVAL_SECONDS
    ):
        _start_station_fetch(station_config)

    if station_future is not None and station_future.done():
        try:
            fetched_row = station_future.result()
            latest_station_row = fetched_row
            latest_station_status = str(fetched_row.get('station_status', fetched_row.get('status', 'Ativo')))
            latest_station_online = bool(fetched_row.get('station_online', True))
        except Exception:
            latest_station_row = None
            latest_station_status = "Offline"
            latest_station_online = False
        finally:
            station_future = None

    if latest_station_row is not None:
        new_row = dict(latest_station_row)
        new_row['date'] = now
        loop_status = latest_station_status
        loop_color = "#5eb9d2" if latest_station_online else "#d95252"
    else:
        loop_status = "Offline"
        loop_color = "#d95252"
        new_row = _build_offline_row(now)

    # Add new data to rolling buffer
    weather_data.append(new_row)
    
    # Keep bounded data in memory to prevent RAM growth with 1 Hz updates.
    weather_data = [
        row for row in weather_data
        if row['date'] >= (now - datetime.timedelta(days=MEMORY_RETENTION_DAYS))
    ]
    
    # Save to database periodically (every DATABASE_SAVE_INTERVAL_SECONDS)
    save_interval = config.get('DATABASE_SAVE_INTERVAL_SECONDS', 600)
    if last_db_save is None or (now - last_db_save).total_seconds() >= save_interval:
        try:
            db.insert_reading(new_row)
            last_db_save = now
        except Exception as e:
            print(f"Warning: Failed to save to database: {e}")

    # Filter data for selected time range
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
    filtered = [row for row in weather_data if row['date'] >= cutoff]
    
    # Fallback: use latest available data if no data in range
    if not filtered and weather_data:
        filtered = weather_data[-1:]
    
    # Fallback: use offline row if no data at all
    if not filtered:
        filtered = [_build_offline_row(now)]

    # Extract latest record and convert to DataFrame for plotting
    latest = filtered[-1]
    df = pd.DataFrame(filtered)

    # Get astronomical information from cache (refresh once/day)
    sunrise, sunset = _get_cached_sun_times()
    moonrise, moonset = get_moon_times(config['LATITUDE'], config['LONGITUDE'])

    # Prepare wind rose data (use 0 for NaN to avoid display issues)
    wind_rose_speed = 0 if pd.isna(latest.get('wind_speed', np.nan)) else latest['wind_speed']
    wind_rose_dir = 0 if pd.isna(latest.get('wind_dir', np.nan)) else latest['wind_dir']

    # Reduce rendered points to keep browser responsive with high-frequency data.
    if len(df) > MAX_PLOT_POINTS:
        idx = np.linspace(0, len(df) - 1, MAX_PLOT_POINTS, dtype=int)
        df = df.iloc[idx].copy()

    # Calculate observation conditions based on last 30 minutes
    cutoff_30min = now - datetime.timedelta(minutes=30)
    last_30min = [row for row in weather_data if row['date'] >= cutoff_30min]
    
    if last_30min:
        # Extract wind speeds and temperatures from last 30 min, filtering out NaN values
        wind_speeds = [row.get('wind_speed', np.nan) for row in last_30min if not pd.isna(row.get('wind_speed', np.nan))]
        temperatures = [row.get('temperature', np.nan) for row in last_30min if not pd.isna(row.get('temperature', np.nan))]
        
        wind_speed_max = max(wind_speeds) if wind_speeds else np.nan
        wind_speed_avg = np.mean(wind_speeds) if wind_speeds else np.nan
        temp_min = min(temperatures) if temperatures else np.nan
        dew_point = latest.get('dew_point', np.nan)
        
        # Determine observation conditions color and status
        if (not pd.isna(wind_speed_max) and not pd.isna(temp_min) and not pd.isna(dew_point)):
            # Green: wind_speed_max < 12 AND dew_point < temp_min
            if wind_speed_max < 12 and dew_point < temp_min:
                obs_color = '#5eb9d2'  # Green/Blue
                obs_status = 'Boas'
            # Orange: wind_speed_max < 15 AND wind_speed_avg < 12 AND dew_point < temp_min - 2
            elif (wind_speed_max < 15 and 
                  not pd.isna(wind_speed_avg) and wind_speed_avg < 12 and 
                  dew_point < temp_min - 2):
                obs_color = '#f39c12'  # Orange
                obs_status = 'Regulares'
            # Red: otherwise
            else:
                obs_color = '#d95252'  # Red
                obs_status = 'Ruins'
        else:
            obs_color = '#808080'  # Gray
            obs_status = 'N/D'
    else:
        obs_color = '#808080'  # Gray
        obs_status = 'N/D'

    info_box = html.Div([
        html.P([
            "Temperatura: ",
            html.Span(_format_metric(latest.get('temperature'), '.1f', '°C'), className="color-temp")
        ]),
        html.P([
            "Umidade: ",
            html.Span(_format_metric(latest.get('humidity'), '.1f', '%'), className="color-humidity")
        ]),
        html.P([
            "Ponto de orvalho: ",
            html.Span(_format_metric(latest.get('dew_point'), '.1f', '°C'), className="color-dew")
        ]),
        html.P([
            "Velocidade do vento: ",
            html.Span(_format_metric(latest.get('wind_speed'), '.1f', 'm/s'), className="color-wind-speed")
        ]),
        html.P([
            "Direção do vento: ",
            html.Span(_format_metric(latest.get('wind_dir'), '.0f', '°'), className="color-wind-dir")
        ]),
        html.P([
            "Pressão: ",
            html.Span(_format_metric(latest.get('pressure'), '.1f', 'hPa'), className="color-location")
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
            html.Span(obs_status, style={'color': obs_color, 'font-weight': 'bold'})
        ])
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
    
    wind_rose_fig = go.Figure(
        data=[
            go.Barpolar(
                r=[wind_rose_speed],
                theta=[wind_rose_dir],
                marker=dict(color='var(--color-wind-dir)'),
                width=[30],
                name='Direção Atual'
            )
        ],
        layout=go.Layout(
            template='plotly_dark',
            title=dict(
                text='<b>Direção dos Ventos</b>',
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
                    visible=False,
                    color='var(--color-location)'
                )
            ),
            showlegend=False,
            margin=dict(l=40, r=40, t=50, b=40),
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
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': '°C'},
            'showlegend': True,
            'legend': {
                'x': 0.98,
                'y': 0.98,
                'xanchor': 'right',
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
    rain_fig = go.Figure(
        data=[go.Scatter(x=df['date'], 
                         y=df['rain_hour'], 
                         mode='lines', 
                         name='Chuva',
                         line={'color': '#5eb9d2'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Chuva (mm/h)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': 'mm/h'},
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
        data=[go.Scatter(x=df['date'], 
                         y=df['wind_speed'], 
                         mode='lines', 
                         name='Velocidade do Vento',
                         line={'color': '#76d465'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Velocidade do Vento (m/s)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': 'm/s'},
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )
    dir_fig = go.Figure(
        data=[go.Scatter(x=df['date'], 
                         y=df['wind_dir'], 
                         mode='lines', 
                         name='Direção do Vento',
                         line={'color': '#7fd1b9'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Direção do Vento (°)',
            'xaxis': {'title': 'Hora'},
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

    return (
        info_box,
        astro_info_box,
        temp_fig,
        hum_fig,
        rain_fig,
        pressure_fig,
        wind_fig,
        dir_fig,
        wind_rose_fig,
        html.Span(f"Estação Meteorológica: {loop_status}", className='status-indicator', style={'color': loop_color}),
        html.Span(
            f"Câmera de todo céu: {'Ativo' if get_camera_status() else 'Offline'}",
            className='status-indicator',
            style={'color': '#5eb9d2' if get_camera_status() else '#d95252'}
        )
    )


@app.callback(
    Output('all-sky-img', 'src'),
    Output('last-update-time', 'children'),
    Input('allsky-interval', 'n_intervals')
)
def update_allsky_image(_n_intervals):
    """Update all-sky image independently and report image refresh time."""
    all_sky_url = read_allsky(config['ALLSKY_CAMERA_CONFIG'])
    image_update = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    return all_sky_url, html.Span(f"Última atualização: {image_update}")


@app.callback(
    Output('all-sky-img', 'style'),
    Input('brightness-slider', 'value')
)
def update_brightness(brightness_value):
    """Update all-sky image brightness based on slider value."""
    return {'filter': f'brightness({brightness_value}%)'}


# ============================================================================
# Application Entry Point
# ============================================================================

if __name__ == '__main__':
    # Application entry point when run directly
    # Bind to all network interfaces to make accessible from other computers
    # Access from other devices at: http://192.168.1.88:<port>
    app.run(debug=True, host=config['SERVER_HOST'], port=config['SERVER_PORT'])

