
import datetime
import requests
import numpy as np
import pandas as pd


def generate_mock_data():
    r"""
    Gera dados meteorológicos simulados para um período de 4 dias.
    """
    np.random.seed(42)
    now = datetime.datetime.now()
    time_range = pd.date_range(end=now, periods=6*24*4, freq='10min')  # 4 days of data

    temperature = np.random.normal(15, 3, size=len(time_range))
    humidity = np.random.uniform(40, 90, size=len(time_range))
    dew_point = temperature - ((100 - humidity) / 5)
    wind_speed = np.random.uniform(0, 20, size=len(time_range))
    wind_dir = np.random.uniform(0, 360, size=len(time_range))
    pressure = np.random.normal(1013, 8, size=len(time_range))

    return pd.DataFrame({
                         'time': time_range,
                         'temperature': temperature,
                         'humidity': humidity,
                         'dew_point': dew_point,
                         'wind_speed': wind_speed,
                         'wind_dir': wind_dir,
                         'pressure': pressure
                        })


def get_sun_times(lat, lon):
    r"""
    Obtém os horários de nascer e pôr do sol hoje para as coordenadas fornecidas.
    """
    try:
        url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&formatted=0"
        resp = requests.get(url, timeout=5)
        if resp.ok:
            data = resp.json()
            sunrise_utc = data['results']['sunrise']
            sunset_utc = data['results']['sunset']
            sunrise_dt = datetime.datetime.fromisoformat(sunrise_utc.replace('Z', '+00:00'))
            sunset_dt = datetime.datetime.fromisoformat(sunset_utc.replace('Z', '+00:00'))
            sunrise_local = sunrise_dt.astimezone(datetime.timezone(datetime.timedelta(hours=-3)))
            sunset_local = sunset_dt.astimezone(datetime.timezone(datetime.timedelta(hours=-3)))
            return sunrise_local.strftime('%H:%M:%S'), sunset_local.strftime('%H:%M:%S')
        return "N/D", "N/D"
    except Exception:
        return "N/D", "N/D"

def get_moon_phase(date=None):
    """Retorna a fase da lua simplificada e fração iluminada para a data atual ou fornecida."""
    if date is None:
        date = datetime.datetime.now()
    year = date.year
    month = date.month
    day = date.day
    if month < 3:
        year -= 1
        month += 12
    month += 1
    c = 365.25 * year
    e = 30.6 * month
    jd = c + e + day - 694039.09  # data juliana
    jd /= 29.5305882              # ciclo lunar
    b = int(jd)                   # parte inteira
    jd -= b                       # parte fracionária
    phase_index = int(jd * 4)     # 0 a 3
    phase_frac = jd
    # Fases
    phases = ["Lua Nova",
              "Crescente",
              "Lua Cheia",
              "Minguante"]
    # Fração iluminada (0.0 a 1.0)
    illuminated = 0
    if phase_index == 0:  # Nova
        illuminated = phase_frac
    elif phase_index == 1:  # Crescente
        illuminated = 0.25 + 0.25 * (phase_frac * 4 - 1)
    elif phase_index == 2:  # Cheia
        illuminated = 0.5 + 0.5 * (phase_frac * 4 - 2)
    elif phase_index == 3:  # Minguante
        illuminated = 1 - (phase_frac * 4 - 3) * 0.25
    # Corrige limites
    illuminated = max(0, min(illuminated, 1))
    # Nome da fase
    phase_name = phases[phase_index % 4]
    return f"{phase_name} ({illuminated*100:.0f}%)"