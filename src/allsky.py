"""
All Sky Camera Communication Module

Handles communication with the All Sky camera via HTTP REST API/MJPEG streams.
Follows the same pattern as weatherstation.py for consistency.
"""

import os
import yaml
import datetime

try:
    import requests
    from PIL import Image
    from io import BytesIO
except ImportError:
    requests = None
    Image = None
    BytesIO = None


def read_oculus_config(config_path):
    """
    Load All Sky camera configuration from YAML file.
    
    Args:
        config_path: Path to oculus.yaml configuration file
        
    Returns:
        Dictionary with camera configuration
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config


def get_camera_host(config):
    """
    Get camera host from config or environment variable.
    
    Args:
        config: Configuration dictionary from oculus.yaml
        
    Returns:
        Camera host IP address
    """
    return (
        os.getenv("ALLSKY_CAMERA_HOST")
        or config.get("camera", {}).get("host")
        or "192.168.100.100"
    )


def get_camera_stream_url(config):
    """Build the full stream URL from configuration."""
    host = get_camera_host(config)
    port = config.get("camera", {}).get("port", 8080)
    stream_path = config.get("camera", {}).get("stream_url", "/mjpg/video.mjpg")
    return f"http://{host}:{port}{stream_path}"


def read_camera_frame_once(config_path):
    """
    Capture a single frame from the All Sky camera.
    
    Args:
        config_path: Path to oculus.yaml configuration file
        
    Returns:
        Dictionary with frame metadata and status, or None on error
        
    Raises:
        ImportError: If requests or PIL are not installed
        ConnectionError: If unable to connect to camera
    """
    if requests is None or Image is None:
        raise ImportError("requests and Pillow are required for camera capture")
    
    config = read_oculus_config(config_path)
    stream_url = get_camera_stream_url(config)
    timeout = float(config.get("camera", {}).get("timeout", 5))
    retry_count = int(config.get("camera", {}).get("retry_count", 3))
    
    last_error = None
    for attempt in range(retry_count):
        try:
            response = requests.get(stream_url, timeout=timeout, stream=True)
            if response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                return {
                    "timestamp": datetime.datetime.now(),
                    "status": "success",
                    "resolution": img.size,
                    "format": img.format,
                    "url": stream_url,
                    "frame_data": response.content,
                }
            else:
                last_error = f"HTTP {response.status_code}"
        except Exception as e:
            last_error = str(e)
        
        if attempt < retry_count - 1:
            try:
                import time
                time.sleep(0.5)
            except Exception:
                pass
    
    raise ConnectionError(
        f"Unable to capture frame from {stream_url} after {retry_count} attempts: {last_error}"
    )


def get_camera_health(config_path):
    """
    Check if the All Sky camera is reachable and healthy.
    
    Args:
        config_path: Path to oculus.yaml configuration file
        
    Returns:
        Dictionary with health status information
    """
    try:
        result = read_camera_frame_once(config_path)
        return {
            "online": True,
            "status": "OK",
            "timestamp": result["timestamp"],
            "resolution": result.get("resolution"),
        }
    except Exception as e:
        return {
            "online": False,
            "status": f"Offline: {str(e)[:50]}",
            "timestamp": datetime.datetime.now(),
            "resolution": None,
        }


def generate_mock_allsky_frame():
    """
    Generate mock all sky frame data for testing/development.
    
    Returns:
        Dictionary simulating frame capture response
    """
    return {
        "timestamp": datetime.datetime.now(),
        "status": "mock",
        "resolution": (1920, 1080),
        "format": "JPEG",
        "url": "mock://allsky-frame",
        "frame_data": None,  # Would contain actual image bytes in production
    }
