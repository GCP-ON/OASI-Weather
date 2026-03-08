
import datetime
import requests



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

def get_moon_times(lat, lon):
    """
    Obtém os horários aproximados de nascer e ocaso da lua hoje.
    Usa cálculo simplificado baseado na fase lunar.
    """
    try:
        # Get sun times as reference
        sunrise_str, sunset_str = get_sun_times(lat, lon)
        if sunrise_str == "N/D":
            return "N/D", "N/D"
        
        # Parse sun times
        sunrise_local = datetime.datetime.strptime(sunrise_str, '%H:%M:%S').time()
        
        # Get current moon phase angle
        date = datetime.datetime.now()
        j2000 = datetime.datetime(2000, 1, 1, 12, 0, 0)
        delta = (date - j2000).total_seconds() / 86400.0
        
        # Calculate moon's hour angle offset from sun
        L_moon = 218.316 + 13.176396 * delta
        L_sun = 280.466 + 0.9856474 * delta
        phase_angle = (L_moon - L_sun) % 360
        
        # Moon rises approximately 50 minutes later each day
        # Phase angle gives us position relative to sun
        hour_offset = (phase_angle / 360.0) * 24  # Hours after sun
        
        # Calculate moonrise (offset from sunrise)
        sunrise_minutes = sunrise_local.hour * 60 + sunrise_local.minute
        moonrise_minutes = (sunrise_minutes + hour_offset * 60) % 1440
        moonrise_hour = int(moonrise_minutes // 60)
        moonrise_minute = int(moonrise_minutes % 60)
        
        # Calculate moonset (offset from sunset, roughly 12 hours after moonrise)
        moonset_minutes = (moonrise_minutes + 12 * 60) % 1440
        moonset_hour = int(moonset_minutes // 60)
        moonset_minute = int(moonset_minutes % 60)
        
        return f"{moonrise_hour:02d}:{moonrise_minute:02d}:00", f"{moonset_hour:02d}:{moonset_minute:02d}:00"
    except Exception:
        return "N/D", "N/D"


def get_moon_phase(date=None):
    """Retorna a fase da lua e percentual correto de iluminação usando cálculo astronômico preciso."""
    import math
    if date is None:
        date = datetime.datetime.now()
    
    # J2000.0 epoch (January 1, 2000, 12:00 TT)
    j2000 = datetime.datetime(2000, 1, 1, 12, 0, 0)
    
    # Days since J2000
    delta = (date - j2000).total_seconds() / 86400.0
    
    # Mean longitude of the Sun (degrees)
    L = 280.466 + 0.9856474 * delta
    
    # Mean anomaly of the Sun (degrees)
    g = 357.528 + 0.9856003 * delta
    g_rad = math.radians(g % 360)
    
    # Ecliptic longitude of the Sun
    lambda_sun = L + 1.915 * math.sin(g_rad) + 0.020 * math.sin(2 * g_rad)
    
    # Mean longitude of the Moon (degrees)
    L_moon = 218.316 + 13.176396 * delta
    
    # Mean anomaly of the Moon (degrees)
    M_moon = 134.963 + 13.064993 * delta
    M_moon_rad = math.radians(M_moon % 360)
    
    # Ecliptic longitude of the Moon
    lambda_moon = L_moon + 6.289 * math.sin(M_moon_rad)
    
    # Phase angle (degrees): 0° = new moon, 180° = full moon
    phase_angle = (lambda_moon - lambda_sun) % 360
    phase_angle_rad = math.radians(phase_angle)
    
    # Illumination fraction
    illuminated = (1 - math.cos(phase_angle_rad)) / 2
    illuminated = max(0, min(illuminated, 1))  # Clamp to 0-1
    
    # Determine phase name based on phase angle
    if phase_angle < 22.5 or phase_angle >= 337.5:
        phase_name = "🌑 Lua Nova"
    elif phase_angle < 67.5:
        phase_name = "🌒 Crescente"
    elif phase_angle < 112.5:
        phase_name = "🌓 Quarto Crescente"
    elif phase_angle < 157.5:
        phase_name = "🌔 Gibosa Crescente"
    elif phase_angle < 202.5:
        phase_name = "🌕 Lua Cheia"
    elif phase_angle < 247.5:
        phase_name = "🌖 Gibosa Minguante"
    elif phase_angle < 292.5:
        phase_name = "🌗 Quarto Minguante"
    else:
        phase_name = "🌘 Minguante"
    
    return f"{phase_name} ({illuminated*100:.0f}%)"