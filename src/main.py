import dash
from dash import html, dcc, Input, Output
import plotly.graph_objs as go
import pandas as pd
import numpy as np
import datetime
import requests
import math
import yaml

from .util import generate_mock_data, get_moon_phase, get_sun_times
import os

# Inicializa com dados mock
mock_data = generate_mock_data()

# ----------- Constants ----------- #

config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)

TIME_OPTIONS = config['TIME_OPTIONS']
LATITUDE = config['LATITUDE']
LONGITUDE = config['LONGITUDE']
ALTITUDE = config['ALTITUDE']

# ----------- Dash App Initialization ----------- #
app = dash.Dash(__name__, prevent_initial_callbacks=True)
app.title = "OASI-Weather"
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
                src='https://tse3.mm.bing.net/th/id/OIP.dkPjsWzf2yJfoMq3ziBVsgHaH2?pid=Api',
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
                    src="https://www.cptec.inpe.br/dsat/?product=true_color_ch13_dsa&product_opacity=1&date=202508051340&zoom=6&x=4560.0000&y=3153.5000&animate=true&t=350.00&options=false&legend=false",
                    className="inpe-iframe"
                ),
                html.Iframe(
                    src="https://lxapp.weatherbug.net/v2/lxapp_impl.html?lat=-8.79225&lon=-38.68853&tv=1.8.1&nocache=1",
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
                        {'label': k, 'value': v} for k, v in TIME_OPTIONS.items()
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
            ], className="plot-col-center"),
            # Right column: wind speed and wind direction
            html.Div([
                dcc.Graph(id='wind-speed-plot', className='plot-graph'),
                dcc.Graph(id='wind-dir-plot', className='plot-graph'),
            ], className="plot-col"),
        ], className="plots-row"),

        # ----------- Interval for Updates ----------- #
        dcc.Interval(id='clock-interval', interval=10000, n_intervals=0),

        # ----------- Footer ----------- #
        html.Footer(
            "OASI-Weather © 2025 | Observatório Astronômico do Sertão de Itaparica",
            className="footer"
        )
    ]
)

# ----------- Callbacks ----------- #
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
    Output('all-sky-img', 'src'),  # Adicione este output
    Input('time-range-dropdown', 'value'),
    Input('clock-interval', 'n_intervals')
)

# -------- Update Dashboard -------- #
def update_dashboard(minutes, n_intervals):
    # Simula novos dados a cada atualização
    global mock_data
    if n_intervals > 0:
        now = datetime.datetime.now()
        new_row = {
            'time': now,
            'temperature': np.random.normal(15, 3),
            'humidity': np.random.uniform(40, 90),
            'dew_point': np.random.normal(15, 3) - ((100 - np.random.uniform(40, 90)) / 5),
            'wind_speed': np.random.uniform(0, 20),
            'wind_dir': np.random.uniform(0, 360),
            'pressure': np.random.normal(1013, 8)
        }
        mock_data = pd.concat([mock_data, pd.DataFrame([new_row])], ignore_index=True)
        # Mantém apenas os últimos 4 dias
        mock_data = mock_data[mock_data['time'] >= (now - datetime.timedelta(days=4))]

    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
    filtered = mock_data[mock_data['time'] >= cutoff]

    latest = filtered.iloc[-1]
    sunrise, sunset = get_sun_times(LATITUDE, LONGITUDE)

    # Status/last update
    loop_status = "Ativo" if n_intervals > 0 else "Inativo"
    loop_color = "#5eb9d2" if n_intervals > 0 else "#d95252"
    last_update = latest['time'].strftime('%d/%m/%Y %H:%M:%S')

    info_box = html.Div([
        html.H4("Condições Atuais", style={'marginBottom': '10px', 'color': "#ffffff", 'fontWeight': 'bold', 'fontSize': '15px', 'textAlign': 'center'}),
        html.P([
            "Temperatura: ",
            html.Span(f"{latest['temperature']:.1f} °C", style={'color': "#ef5c42", 'fontWeight': 'bold'})
        ]),
        html.P([
            "Umidade: ",
            html.Span(f"{latest['humidity']:.1f} %", style={'color': "#47b0d3", 'fontWeight': 'bold'})
        ]),
        html.P([
            "Ponto de orvalho: ",
            html.Span(f"{latest['dew_point']:.1f} °C", style={'color': "#aa96e3", 'fontWeight': 'bold'})
        ]),
        html.P([
            "Velocidade do vento: ",
            html.Span(f"{latest['wind_speed']:.1f} km/h", style={'color': "#76d465", 'fontWeight': 'bold'})
        ]),
        html.P([
            "Direção do vento: ",
            html.Span(f"{latest['wind_dir']:.0f}°", style={'color': '#7fd1b9', 'fontWeight': 'bold'})
        ]),
        # Rosa dos ventos
        html.Div([
            dcc.Graph(
                id='wind-rose',
                figure=go.Figure(
                    data=[
                        go.Barpolar(
                            r=[latest['wind_speed']],
                            theta=[latest['wind_dir']],
                            marker=dict(color='#7fd1b9'),
                            width=[30],  # largura do setor
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
                                color='#e0e0e0'
                            ),
                            radialaxis=dict(
                                visible=False,  # <--- Remove os ticks e números do centro
                                color='#e0e0e0'
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
        html.H4("Localização", style={'marginBottom': '10px', 'color': "#ffffff", 'fontWeight': 'bold', 'fontSize': '15px', 'textAlign': 'center'}),
        html.P([
            "Latitude: ",
            html.Span("8° 47' 32,1\" S", style={'color': "#ffffff", 'fontWeight': 'bold'})
        ]),
        html.P([
            "Longitude: ",
            html.Span("38° 41' 18,7\" O", style={'color': '#ffffff', 'fontWeight': 'bold'})
        ]),
        html.P([
            "Altitude: ",
            html.Span(f"{ALTITUDE} m", style={'color': '#ffffff', 'fontWeight': 'bold'})
        ]),
        html.P([
            "Nascer do sol: ",
            html.Span(f"{sunrise}", style={'color': "#f3dc76", 'fontWeight': 'bold'})
        ]),
        html.P([
            "Pôr do sol: ",
            html.Span(f"{sunset}", style={'color': '#f3dc76', 'fontWeight': 'bold'})
        ]),
        html.P([
            "Fase da lua: ",
            html.Span(get_moon_phase(), style={'color': "#cececc", 'fontWeight': 'bold'})
        ])
    ])
    # Plots
    temp_fig = go.Figure(
        data=[go.Scatter(
            x=filtered['time'],
            y=filtered['temperature'],
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
        data=[go.Scatter(x=filtered['time'], 
                         y=filtered['dew_point'], 
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
        data=[go.Scatter(x=filtered['time'], 
                         y=filtered['humidity'], 
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
        data=[go.Scatter(x=filtered['time'], 
                         y=filtered['pressure'], 
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
        data=[go.Scatter(x=filtered['time'], 
                         y=filtered['wind_speed'], 
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
        data=[go.Scatter(x=filtered['time'], 
                         y=filtered['wind_dir'], 
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

    # Atualiza a imagem All Sky (simulação: sempre o mesmo URL)
    all_sky_url = update_allsky_image()

    return (
        info_box,
        temp_fig,
        hum_fig,
        dew_fig,
        pressure_fig,
        wind_fig,
        dir_fig,
        html.Span(f"Status: {loop_status}", style={'color': loop_color}),
        html.Span([
            "Última atualização:",
            html.Br(),
            last_update
        ]),
        all_sky_url
    )


def update_allsky_image():
    # Lógica para atualizar a imagem do céu
    return 'https://tse4.mm.bing.net/th/id/OIP.88LnC-aFnoEce7DoolPx8wHaG6?pid=Api'


# ----------- Main ----------- #
if __name__ == '__main__':
    app.run(debug=True, port=8051)
    # app.run(debug=False, port=8051)

