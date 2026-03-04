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
import yaml
from .util import get_moon_phase, get_sun_times
from .weatherstation import read_weather_station, _build_offline_row, _format_metric
from .allsky import read_allsky
import os

# ============================================================================
# Global State Variables
# ============================================================================

#: list: Rolling buffer of weather data records (max 4 days)
weather_data = []

# ============================================================================
# Server and Observatory Configuration Loading
# ============================================================================

# Load dashboard configuration from YAML
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)

# ============================================================================
# Dash Application Setup
# ============================================================================

# Initialize Dash
app = dash.Dash(__name__, prevent_initial_callbacks=True)
app.title = "OASI-Weather"

# 
app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>OASI-Weather</title>
        {%favicon%}
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
                src='https://github.com/GCP-ON/OASI-Weather/blob/master/src/assets/logo-impacton.jpg?raw=true',
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
            ], className="header-title"),
            # Loop status and last update on the right
            html.Div([
                html.Div(
                    id='loop-status',
                    className="loop-status-box",
                    children=[
                        html.Div(id='loop-active-indicator'),
                        html.Div(id='last-update-time')
                    ]
                )
            ])
        ], className="header"),

        # ----------- Main Row: Info, Satellite, All Sky ----------- #
        html.Div([
            # Info box (left column)
            html.Div([
                html.Div(id='info-box', className="info-box")
            ], className="info-box-container"),
            # Satellite and WeatherBug iframes (center column)
            html.Div([
                html.Iframe(
                    src=f"https://www.cptec.inpe.br/dsat/?product=true_color_ch13_dsa&product_opacity=1&date=202508051340&zoom=6&x=4560.0000&y=3153.5000&animate=true&t=350.00&options=false&legend=false",
                    className="inpe-iframe"
                ),
                html.Iframe(
                    src=f"https://lxapp.weatherbug.net/v2/lxapp_impl.html?lat={config['LATITUDE']}&lon={config['LONGITUDE']}&tv=1.8.1&nocache=1",
                    className="weatherbug-iframe"
                )
            ], className="inpe-container"),
            # All Sky image (right column)
            html.Div([
                html.Img(
                    id='all-sky-img',
                    src='...',
                    className='all-sky-img'
                ),
            ], className='all-sky-container'),
        ], className="main-row"),

        # ----------- Divider Line ----------- #
        html.Hr(className="hr-divider"),

        # ----------- Time Selector (above plots) ----------- #
        html.Div([
            html.Div([
                html.Label("Escala de tempo", className="time-selector-label"),
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

        # ----------- Interval for Updates ----------- #
        dcc.Interval(id='clock-interval', interval=config['UPDATE_INTERVAL_SECONDS'] * 1000, n_intervals=0),

        # ----------- Footer ----------- #
        html.Footer(
            "OASI-Weather © 2025 | Observatório Astronômico do Sertão de Itaparica",
            className="footer"
        )
    ]
)

# ============================================================================
# Dashboard Callbacks
# ============================================================================

@app.callback(
    Output('info-box', 'children'),
    Output('temperature-plot', 'figure'),
    Output('humidity-plot', 'figure'),
    Output('dew-point-plot', 'figure'),
    Output('pressure-plot', 'figure'),
    Output('wind-speed-plot', 'figure'),
    Output('wind-dir-plot', 'figure'),
    Output('loop-active-indicator', 'children'),
    Output('last-update-time', 'children'),
    Output('all-sky-img', 'src'),
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
        tuple: Contains 10 elements in order:
            - info_box (html.Div): Current conditions and location info panel
            - temp_fig (go.Figure): Temperature timeseries plot
            - hum_fig (go.Figure): Humidity timeseries plot
            - dew_fig (go.Figure): Dew point timeseries plot
            - pressure_fig (go.Figure): Pressure timeseries plot
            - wind_fig (go.Figure): Wind speed timeseries plot
            - dir_fig (go.Figure): Wind direction timeseries plot
            - status_indicator (html.Span): Status label with color
            - update_time (html.Span): Last update timestamp
            - all_sky_url (str): URL for all-sky camera image
    
    Raises:
        Exception: Caught internally. Connection failures result in offline mode.
    
    Note:
        Uses global variable `weather_data`.
    """
    global weather_data
    
    # Current timestamp
    now = datetime.datetime.now()
    
    # Initialize status indicators
    source_label = "Estação"
    loop_status = "Ativo"
    loop_color = "#5eb9d2"  # Blue for active

    # Live mode: attempt to read from weather station
    try:
        new_row = read_weather_station(config['WEATHER_STATION_CONFIG'])
        station_status = str(new_row.get('station_status', new_row.get('status', 'Ativo')))
        station_online = bool(new_row.get('station_online', True))
        loop_status = station_status
        if (not station_online) or station_status.lower() in {'offline', 'erro', 'error', 'falha'}:
            loop_color = "#d95252"
    except Exception:
        # Connection failed - switch to offline mode
        loop_status = "Offline"
        loop_color = "#d95252"  # Red for offline
        new_row = _build_offline_row(now)

    # Add new data to rolling buffer
    weather_data.append(new_row)
    
    # Keep only last 4 days of data to prevent memory bloat
    weather_data = [row for row in weather_data if row['date'] >= (now - datetime.timedelta(days=4))]

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

    # Get astronomical information
    sunrise, sunset = get_sun_times(config['LATITUDE'], config['LONGITUDE'])

    # Format last update time
    last_update = latest['date'].strftime('%d/%m/%Y %H:%M:%S')
    
    # Prepare wind rose data (use 0 for NaN to avoid display issues)
    wind_rose_speed = 0 if pd.isna(latest.get('wind_speed', np.nan)) else latest['wind_speed']
    wind_rose_dir = 0 if pd.isna(latest.get('wind_dir', np.nan)) else latest['wind_dir']

    info_box = html.Div([
        html.H4("Condições Atuais", className="color-location", style={'marginBottom': '10px', 'fontWeight': 'bold', 'fontSize': '15px', 'textAlign': 'center'}),
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
            html.Span(_format_metric(latest.get('wind_speed'), '.1f', 'km/h'), className="color-wind-speed")
        ]),
        html.P([
            "Direção do vento: ",
            html.Span(_format_metric(latest.get('wind_dir'), '.0f', '°'), className="color-wind-dir")
        ]),
        html.P([
            "Pressão: ",
            html.Span(_format_metric(latest.get('pressure'), '.1f', 'hPa'), className="color-location")
        ]),
        html.P([
            "Tensão bateria: ",
            html.Span(_format_metric(latest.get('battery_voltage'), '.2f', 'V'), className="color-location")
        ]),
        html.P([
            "Chuva (hora): ",
            html.Span(_format_metric(latest.get('rain_hour'), '.2f', 'mm/h'), className="color-location")
        ]),
        html.Div([
            dcc.Graph(
                id='wind-rose',
                figure=go.Figure(
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
                        polar=dict(
                            angularaxis=dict(
                                direction='clockwise',
                                rotation=90,
                                tickmode='array',
                                tickvals=[0, 45, 90, 135, 180, 225, 270, 315],
                                ticktext=['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'],
                                color='var(--color-location)'
                            ),
                            radialaxis=dict(
                                visible=False,
                                color='var(--color-location)'
                            )
                        ),
                        showlegend=False,
                        margin=dict(l=20, r=20, t=40, b=20),
                        height=220,
                        paper_bgcolor='rgba(0,0,0,0)',
                        plot_bgcolor='rgba(0,0,0,0)'
                    )
                ),
                config={'displayModeBar': False},
                style={'height': '220px'}
            )
        ], style={'marginTop': '8px', 'marginBottom': '8px'}),
        html.Hr(),
        html.H4("Localização", className="color-location", style={'marginBottom': '10px', 'fontWeight': 'bold', 'fontSize': '15px', 'textAlign': 'center'}),
        html.P([
            "Latitude: ",
            html.Span("8° 47' 32,1\" S", className="color-location") ########## -> fix latter
        ]),
        html.P([
            "Longitude: ",
            html.Span("38° 41' 18,7\" O", className="color-location") ########## -> fix latter
        ]),
        html.P([
            "Altitude: ",
            html.Span(f"{config['ALTITUDE']} m", className="color-location")
        ]),
        html.P([
            "Nascer do sol: ",
            html.Span(f"{sunrise}", className="color-sun")
        ]),
        html.P([
            "Pôr do sol: ",
            html.Span(f"{sunset}", className="color-sun")
        ]),
        html.P([
            "Fase da lua: ",
            html.Span(get_moon_phase(), className="color-moon")
        ])
    ])
    
    # Build all weather plots with consistent styling
    temp_fig = go.Figure(
        data=[go.Scatter(
            x=df['date'],
            y=df['temperature'],
            mode='lines',
            name='Temperatura',
            line={'color': '#ef5c42'}
        )],
        layout={
            'template': 'plotly_dark',
            'title': 'Temperatura (°C)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': '°C'},
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
        data=[go.Scatter(x=df['date'], 
                         y=df['wind_speed'], 
                         mode='lines', 
                         name='Velocidade do Vento',
                         line={'color': '#76d465'})],
        layout={
            'template': 'plotly_dark',
            'title': 'Velocidade do Vento (km/h)',
            'xaxis': {'title': 'Hora'},
            'yaxis': {'title': 'km/h'},
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
            'yaxis': {'title': '°'},
            'paper_bgcolor': 'rgba(0,0,0,0)',
            'plot_bgcolor': 'rgba(0,0,0,0)'
        }
    )

    all_sky_url = read_allsky(config['ALLSKY_CAMERA_CONFIG'])

    return (
        info_box,
        temp_fig,
        hum_fig,
        dew_fig,
        pressure_fig,
        wind_fig,
        dir_fig,
        html.Span(f"Status: {loop_status} ({source_label})", style={'color': loop_color}),
        html.Span([
            "Última atualização:",
            html.Br(),
            last_update
        ]),
        all_sky_url
    )

# ============================================================================
# Application Entry Point
# ============================================================================

if __name__ == '__main__':
    # Application entry point when run directly
    # Bind to all network interfaces to make accessible from other computers
    # Access from other devices at: http://192.168.1.88:<port>
    app.run(debug=True, host=config['SERVER_HOST'], port=config['SERVER_PORT'])

