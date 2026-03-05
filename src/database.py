"""Database module for persistent weather data storage.

This module provides SQLite-based storage for weather station readings,
enabling long-term data retention and efficient time-series queries.

Architecture:
    - SQLite database with indexed timestamp column
    - Automatic table creation on first use
    - Pandas DataFrame integration for queries
    - Thread-safe operations for concurrent access

Database Schema:
    weather_readings table:
        - id: Auto-incrementing primary key
        - timestamp: Reading datetime (indexed)
        - temperature: Temperature in °C
        - humidity: Relative humidity in %
        - dew_point: Dew point in °C
        - wind_speed: Wind speed in km/h
        - wind_dir: Wind direction in degrees
        - pressure: Barometric pressure in hPa
        - battery_voltage: Station battery voltage in V
        - source_voltage: Station power source voltage in V
        - rain_min: Rain accumulation per minute in mm
        - rain_hour: Rain accumulation per hour in mm
        - rain_day: Rain accumulation per day in mm
        - rain_total: Total rain accumulation in mm
        - station_status: Status string ('Ativo', 'Offline', etc.)
        - station_online: Boolean connection status

Usage:
    from .database import WeatherDatabase
    
    db = WeatherDatabase('weather_data.db')
    db.insert_reading(reading_dict)
    df = db.get_readings_since(minutes=60)
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def get_yearly_db_path(base_pattern='weather_data_{year}.db', base_dir=None):
    """Generate yearly database path with current year.
    
    Args:
        base_pattern (str): Database filename pattern with {year} placeholder.
            Defaults to 'weather_data_{year}.db'.
        base_dir (str or Path): Directory for database files. If None, uses
            current directory.
    
    Returns:
        Path: Full path to yearly database file.
    
    Example:
        >>> path = get_yearly_db_path('weather_{year}.db', './data')
        >>> str(path)
        './data/weather_2026.db'
    """
    year = datetime.now().year
    filename = base_pattern.format(year=year)
    
    if base_dir:
        return Path(base_dir) / filename
    return Path(filename)


class WeatherDatabase:
    """SQLite database manager for weather station data."""
    
    def __init__(self, db_path='weather_data.db'):
        """Initialize database connection and create tables if needed.
        
        Args:
            db_path (str): Path to SQLite database file. Will be created if
                it doesn't exist. Defaults to 'weather_data.db' in current dir.
        """
        self.db_path = Path(db_path)
        self._init_database()
        logger.info(f"Weather database initialized at {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections.
        
        Yields:
            sqlite3.Connection: Database connection with row factory set.
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            conn.close()
    
    def _init_database(self):
        """Create database tables and indexes if they don't exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Create weather_readings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weather_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    temperature REAL,
                    humidity REAL,
                    dew_point REAL,
                    wind_speed REAL,
                    wind_dir REAL,
                    pressure REAL,
                    battery_voltage REAL,
                    source_voltage REAL,
                    rain_min REAL,
                    rain_hour REAL,
                    rain_day REAL,
                    rain_total REAL,
                    station_status TEXT,
                    station_online BOOLEAN
                )
            """)
            
            # Create index on timestamp for fast time-range queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp 
                ON weather_readings(timestamp)
            """)
            
            logger.info("Database schema initialized")
    
    def insert_reading(self, reading):
        """Insert a single weather reading into the database.
        
        Args:
            reading (dict): Weather data dictionary with keys:
                - date (datetime): Timestamp of the reading
                - temperature, humidity, dew_point, etc.
        
        Returns:
            int: Row ID of inserted record, or None on error.
        
        Example:
            >>> db.insert_reading({
            ...     'date': datetime.now(),
            ...     'temperature': 25.3,
            ...     'humidity': 65.2,
            ...     'pressure': 1013.2
            ... })
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # Convert NaN to None for SQL NULL
                def clean_value(val):
                    if val is None or (isinstance(val, float) and np.isnan(val)):
                        return None
                    return val
                
                cursor.execute("""
                    INSERT INTO weather_readings (
                        timestamp, temperature, humidity, dew_point,
                        wind_speed, wind_dir, pressure, battery_voltage,
                        source_voltage, rain_min, rain_hour, rain_day,
                        rain_total, station_status, station_online
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    reading.get('date'),
                    clean_value(reading.get('temperature')),
                    clean_value(reading.get('humidity')),
                    clean_value(reading.get('dew_point')),
                    clean_value(reading.get('wind_speed')),
                    clean_value(reading.get('wind_dir')),
                    clean_value(reading.get('pressure')),
                    clean_value(reading.get('battery_voltage')),
                    clean_value(reading.get('source_voltage')),
                    clean_value(reading.get('rain_min')),
                    clean_value(reading.get('rain_hour')),
                    clean_value(reading.get('rain_day')),
                    clean_value(reading.get('rain_total')),
                    reading.get('station_status', 'Ativo'),
                    reading.get('station_online', True)
                ))
                
                return cursor.lastrowid
                
        except Exception as e:
            logger.error(f"Failed to insert reading: {e}")
            return None
    
    def insert_readings_bulk(self, readings):
        """Insert multiple weather readings efficiently.
        
        Args:
            readings (list): List of weather data dictionaries.
        
        Returns:
            int: Number of records successfully inserted.
        """
        inserted_count = 0
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                for reading in readings:
                    def clean_value(val):
                        if val is None or (isinstance(val, float) and np.isnan(val)):
                            return None
                        return val
                    
                    cursor.execute("""
                        INSERT INTO weather_readings (
                            timestamp, temperature, humidity, dew_point,
                            wind_speed, wind_dir, pressure, battery_voltage,
                            source_voltage, rain_min, rain_hour, rain_day,
                            rain_total, station_status, station_online
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        reading.get('date'),
                        clean_value(reading.get('temperature')),
                        clean_value(reading.get('humidity')),
                        clean_value(reading.get('dew_point')),
                        clean_value(reading.get('wind_speed')),
                        clean_value(reading.get('wind_dir')),
                        clean_value(reading.get('pressure')),
                        clean_value(reading.get('battery_voltage')),
                        clean_value(reading.get('source_voltage')),
                        clean_value(reading.get('rain_min')),
                        clean_value(reading.get('rain_hour')),
                        clean_value(reading.get('rain_day')),
                        clean_value(reading.get('rain_total')),
                        reading.get('station_status', 'Ativo'),
                        reading.get('station_online', True)
                    ))
                    inserted_count += 1
                    
        except Exception as e:
            logger.error(f"Failed to bulk insert readings: {e}")
        
        logger.info(f"Bulk inserted {inserted_count} readings")
        return inserted_count
    
    def get_readings_since(self, minutes=60):
        """Retrieve readings from the last N minutes.
        
        Args:
            minutes (int): Number of minutes to look back from now.
        
        Returns:
            pandas.DataFrame: Weather readings with 'date' as datetime column.
        """
        try:
            cutoff = datetime.now() - timedelta(minutes=minutes)
            
            with self._get_connection() as conn:
                query = """
                    SELECT 
                        timestamp as date,
                        temperature, humidity, dew_point,
                        wind_speed, wind_dir, pressure,
                        battery_voltage, source_voltage,
                        rain_min, rain_hour, rain_day, rain_total,
                        station_status, station_online
                    FROM weather_readings
                    WHERE timestamp >= ?
                    ORDER BY timestamp ASC
                """
                
                df = pd.read_sql_query(query, conn, params=(cutoff,))
                
                # Convert timestamp column to datetime
                if not df.empty:
                    df['date'] = pd.to_datetime(df['date'])
                
                return df
                
        except Exception as e:
            logger.error(f"Failed to query readings: {e}")
            return pd.DataFrame()
    
    def get_readings_between(self, start_datetime, end_datetime):
        """Retrieve readings between two datetime objects.
        
        Args:
            start_datetime (datetime): Start of time range (inclusive).
            end_datetime (datetime): End of time range (inclusive).
        
        Returns:
            pandas.DataFrame: Weather readings in the specified range.
        """
        try:
            with self._get_connection() as conn:
                query = """
                    SELECT 
                        timestamp as date,
                        temperature, humidity, dew_point,
                        wind_speed, wind_dir, pressure,
                        battery_voltage, source_voltage,
                        rain_min, rain_hour, rain_day, rain_total,
                        station_status, station_online
                    FROM weather_readings
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                """
                
                df = pd.read_sql_query(
                    query, conn, 
                    params=(start_datetime, end_datetime)
                )
                
                if not df.empty:
                    df['date'] = pd.to_datetime(df['date'])
                
                return df
                
        except Exception as e:
            logger.error(f"Failed to query readings between dates: {e}")
            return pd.DataFrame()
    
    def get_latest_reading(self):
        """Get the most recent weather reading.
        
        Returns:
            dict: Latest weather reading as dictionary, or None if no data.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        timestamp as date,
                        temperature, humidity, dew_point,
                        wind_speed, wind_dir, pressure,
                        battery_voltage, source_voltage,
                        rain_min, rain_hour, rain_day, rain_total,
                        station_status, station_online
                    FROM weather_readings
                    ORDER BY timestamp DESC
                    LIMIT 1
                """)
                
                row = cursor.fetchone()
                if row:
                    result = dict(row)
                    result['date'] = pd.to_datetime(result['date'])
                    return result
                return None
                
        except Exception as e:
            logger.error(f"Failed to get latest reading: {e}")
            return None
    
    def get_record_count(self):
        """Get total number of stored readings.
        
        Returns:
            int: Total count of weather readings in database.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM weather_readings")
                return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to get record count: {e}")
            return 0
    
    def delete_old_readings(self, days=30):
        """Delete readings older than specified days.
        
        Args:
            days (int): Delete readings older than this many days.
        
        Returns:
            int: Number of records deleted.
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)
            
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM weather_readings WHERE timestamp < ?",
                    (cutoff,)
                )
                deleted = cursor.rowcount
                
            logger.info(f"Deleted {deleted} readings older than {days} days")
            return deleted
            
        except Exception as e:
            logger.error(f"Failed to delete old readings: {e}")
            return 0
    
    def vacuum(self):
        """Optimize database file size after deletions.
        
        This reclaims disk space after deleting records. Should be run
        periodically if you regularly delete old data.
        """
        try:
            with self._get_connection() as conn:
                conn.execute("VACUUM")
            logger.info("Database vacuumed successfully")
        except Exception as e:
            logger.error(f"Failed to vacuum database: {e}")
    
    def get_statistics(self, days=7):
        """Get statistical summary of recent weather data.
        
        Args:
            days (int): Number of days to analyze.
        
        Returns:
            dict: Statistical summary with min, max, avg for each metric.
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)
            
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        COUNT(*) as record_count,
                        MIN(temperature) as temp_min,
                        MAX(temperature) as temp_max,
                        AVG(temperature) as temp_avg,
                        MIN(humidity) as humidity_min,
                        MAX(humidity) as humidity_max,
                        AVG(humidity) as humidity_avg,
                        MIN(pressure) as pressure_min,
                        MAX(pressure) as pressure_max,
                        AVG(pressure) as pressure_avg,
                        MAX(wind_speed) as wind_max,
                        AVG(wind_speed) as wind_avg,
                        SUM(rain_total) as total_rain
                    FROM weather_readings
                    WHERE timestamp >= ?
                """, (cutoff,))
                
                row = cursor.fetchone()
                return dict(row) if row else {}
                
        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            return {}
