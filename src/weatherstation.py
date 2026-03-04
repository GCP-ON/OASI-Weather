import datetime
import os
import struct
import numpy as np
import pandas as pd
import yaml
from enum import Enum

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    ModbusTcpClient = None

class WindDirection(Enum):
    N = 0
    NNE = 1
    NE = 2
    ENE = 3
    E = 4
    ESE = 5
    SE = 6
    SSE = 7
    S = 8
    SSW = 9
    SW = 10
    WSW = 11
    W = 12
    WNW = 13
    NW = 14
    NNW = 15


# ============================================================================
# Helper Functions
# ============================================================================

def _build_mock_row(now):
    """Generate a single mock weather data record.
    
    Creates synthetic weather data for testing and development purposes.
    Values are randomly generated within reasonable meteorological ranges.
    
    Args:
        now (datetime.datetime): Timestamp for this data record.
    
    Returns:
        dict: Weather data record with the following keys:
            - date: Timestamp of the record
            - temperature: Temperature in °C (normal distribution, mean=15, std=3)
            - humidity: Relative humidity in % (uniform 40-90)
            - dew_point: Dew point in °C (calculated from temp and humidity)
            - wind_speed: Wind speed in km/h (uniform 0-20)
            - wind_dir: Wind direction in degrees (uniform 0-360)
            - pressure: Barometric pressure in hPa (normal, mean=1013, std=8)
            - battery_voltage: NaN (not simulated in mock)
            - source_voltage: NaN (not simulated in mock)
            - rain_min: NaN (not simulated in mock)
            - rain_hour: NaN (not simulated in mock)
            - rain_day: NaN (not simulated in mock)
            - rain_total: NaN (not simulated in mock)
    """
    humidity = np.random.uniform(40, 90)
    temperature = np.random.normal(15, 3)
    return {
        'date': now,
        'temperature': temperature,
        'humidity': humidity,
        'dew_point': temperature - ((100 - humidity) / 5),
        'wind_speed': np.random.uniform(0, 20),
        'wind_dir': np.random.uniform(0, 360),
        'pressure': np.random.normal(1013, 8),
        'battery_voltage': np.nan,
        'source_voltage': np.nan,
        'rain_min': np.nan,
        'rain_hour': np.nan,
        'rain_day': np.nan,
        'rain_total': np.nan,
    }


def _build_offline_row(now):
    """Generate an offline/no-data weather record.
    
    Creates a data record with all values set to NaN, used when the weather
    station is offline or unreachable. This ensures the dashboard displays
    "-" for all metrics instead of showing stale or invalid data.
    
    Args:
        now (datetime.datetime): Timestamp for this record.
    
    Returns:
        dict: Weather data record with all metric values set to np.nan.
    """
    return {
        'date': now,
        'temperature': np.nan,
        'humidity': np.nan,
        'dew_point': np.nan,
        'wind_speed': np.nan,
        'wind_dir': np.nan,
        'pressure': np.nan,
        # 'battery_voltage': np.nan,
        # 'source_voltage': np.nan,
        'rain_min': np.nan,
        'rain_hour': np.nan,
        'rain_day': np.nan,
        'rain_total': np.nan,
    }


def _format_metric(value, fmt, unit):
    """Format a weather metric value for display.
    
    Formats numeric weather values with appropriate precision and units.
    Returns "-" for missing/invalid data (None or NaN) to indicate
    unavailable measurements.
    
    Args:
        value (float or None): The numeric value to format.
        fmt (str): Python format specification (e.g., '.1f', '.2f', '.0f').
        unit (str): The unit string to append (e.g., '°C', 'hPa', 'km/h').
    
    Returns:
        str: Formatted string "value unit" or "-" if value is missing.
    
    Examples:
        >>> _format_metric(23.4567, '.1f', '°C')
        '23.5 °C'
        >>> _format_metric(np.nan, '.1f', '°C')
        '-'
        >>> _format_metric(1013.25, '.0f', 'hPa')
        '1013 hPa'
    """
    if value is None or pd.isna(value):
        return '-'
    return f"{value:{fmt}} {unit}".strip()


def generate_mock_data():
    """Generate mock weather data for 4 days (10-min intervals)."""
    np.random.seed(42)
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    records = []
    
    for i in range(6 * 24 * 4):  # 4 days, 10 min each
        dt = now - datetime.timedelta(minutes=10 * (6 * 24 * 4 - i - 1))
        record = {
            'date': dt,
            'temperature': np.random.normal(25, 3),
            'humidity': np.random.uniform(40, 90),
            'wind_speed': np.random.uniform(0, 20),
            'wind_dir': np.random.uniform(0, 360),
            'pressure': np.random.normal(1013, 8),
            # 'battery_voltage': np.random.normal(12.5, 0.5),
            # 'source_voltage': np.random.normal(24, 1),
            'rain_min': np.random.uniform(0, 0.5),
            'rain_hour': np.random.uniform(0, 5),
            'rain_day': np.random.uniform(0, 20),
            'rain_total': np.random.uniform(100, 500),
        }
        # Calculate dew point from temperature and humidity
        record['dew_point'] = record['temperature'] - ((100 - record['humidity']) / 5)
        records.append(record)
    
    return records


def _decode_float32(registers):
    if len(registers) != 2:
        raise ValueError("float32 requires exactly 2 registers")
    raw = struct.pack(">HH", int(registers[0]) & 0xFFFF, int(registers[1]) & 0xFFFF)
    return float(struct.unpack(">f", raw)[0])


def _read_register_value(client, register_address, data_type="float32", unit_id=1, function_code=3):
    address = int(register_address) - 1
    if data_type.lower() == "float32":
        count = 2
    else:
        count = 1

    if function_code == 3:
        response = client.read_holding_registers(address=address, count=count, device_id=unit_id)
    elif function_code == 4:
        response = client.read_input_registers(address=address, count=count, device_id=unit_id)
    else:
        raise ValueError(f"Unsupported function_code: {function_code}")

    if response.isError():
        raise RuntimeError(f"Modbus read error at register {register_address}: {response}")

    registers = response.registers
    if data_type.lower() == "float32":
        return _decode_float32(registers)
    return float(registers[0])


def read_weather_station(station_config_path):
    with open(station_config_path, "r", encoding="utf-8") as file_handle:
        weather_config = yaml.safe_load(file_handle)

    station_host = (
        os.getenv("WEATHER_STATION_HOST")
        or weather_config.get("host")
        or weather_config.get("ip")
    )
    if not station_host:
        raise ValueError(
            "Station host is not configured. Set WEATHER_STATION_HOST or add host in sigma.yaml"
        )

    station_port = int(os.getenv("WEATHER_STATION_PORT", weather_config.get("service_port", 502)))
    station_unit = int(os.getenv("WEATHER_STATION_UNIT_ID", weather_config.get("unit_id", 1)))
    function_code = int(weather_config.get("function_code", 3))
    timeout_seconds = float(os.getenv("WEATHER_STATION_TIMEOUT", "2.0"))

    register_map = weather_config.get("registers", {})

    client = ModbusTcpClient(host=station_host, port=station_port, timeout=timeout_seconds)
    if not client.connect():
        raise ConnectionError(f"Unable to connect to weather station at {station_host}:{station_port}")

    try:
        values = {}
        for tag_name, metadata in register_map.items():
            reg = metadata.get("register")
            data_type = metadata.get("data_type", "float32")
            if reg is None:
                continue
            values[tag_name] = _read_register_value(
                client=client,
                register_address=reg,
                data_type=data_type,
                unit_id=station_unit,
                function_code=function_code,
            )
    finally:
        client.close()

    temperature = values.get("SSTRH_TEMPERATURA")
    humidity = values.get("SSTRH_UMIDADE")
    pressure = values.get("BAROMETRO")
    wind_speed_ms = values.get("VEL_VENTO")
    wind_dir = values.get("DIR_VENTO")

    if humidity is not None and temperature is not None:
        dew_point = float(temperature - ((100.0 - humidity) / 5.0))
    else:
        dew_point = None

    return {
        "date": datetime.datetime.now(),
        "temperature": float(temperature) if temperature is not None else np.nan,
        "humidity": float(humidity) if humidity is not None else np.nan,
        "dew_point": dew_point if dew_point is not None else np.nan,
        "wind_speed": float(wind_speed_ms) * 3.6 if wind_speed_ms is not None else np.nan,
        "wind_dir": float(wind_dir) if wind_dir is not None else np.nan,
        "pressure": float(pressure) if pressure is not None else np.nan,
        # "battery_voltage": float(values.get("TENSAO_BATERIA")) if values.get("TENSAO_BATERIA") is not None else np.nan,
        # "source_voltage": float(values.get("TENSAO_FONTE")) if values.get("TENSAO_FONTE") is not None else np.nan,
        "rain_min": float(values.get("CHUVA_ACUMULADA_MIN")) if values.get("CHUVA_ACUMULADA_MIN") is not None else np.nan,
        "rain_hour": float(values.get("CHUVA_ACUMULADA_HORA")) if values.get("CHUVA_ACUMULADA_HORA") is not None else np.nan,
        "rain_day": float(values.get("CHUVA_ACUMULADA_DIA")) if values.get("CHUVA_ACUMULADA_DIA") is not None else np.nan,
        "rain_total": float(values.get("CHUVA_ACUMULADA_TOTAL")) if values.get("CHUVA_ACUMULADA_TOTAL") is not None else np.nan,
    }