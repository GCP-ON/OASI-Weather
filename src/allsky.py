"""
All Sky Camera Communication Module (ASCOM Protocol)

Handles communication with the Starlight Express Oculus 3 all-sky camera
via ASCOM protocol. Only captures images during nighttime (after sunset).

Windows-only module using ASCOM drivers via win32com.
"""

import os
import threading
import yaml
import datetime
import logging
from pathlib import Path
import numpy as np

# Windows ASCOM support
try:
    import pythoncom
    import win32com.client
    ASCOM_AVAILABLE = True
except ImportError:
    pythoncom = None
    win32com = None
    ASCOM_AVAILABLE = False

# Image processing
try:
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    PIL_AVAILABLE = False

logger = logging.getLogger(__name__)

# Prevent overlapping capture attempts from periodic dashboard callbacks.
_capture_lock = threading.Lock()
_last_allsky_url = '/assets/logo-impacton.jpg'
_last_capture_started_at = None
_sun_times_cache_date = None
_sunrise_cached = None
_sunset_cached = None
_last_nighttime_warning_at = None


def _latest_assets_image_url():
    """Return latest saved all-sky URL with cache buster, if present."""
    latest_path = Path(__file__).parent / 'data' / 'allsky' / 'latest.jpg'
    if not latest_path.exists():
        return None

    try:
        cache_buster = int(latest_path.stat().st_mtime)
    except Exception:
        cache_buster = int(datetime.datetime.now().timestamp())
    return f"/allsky/latest.jpg?t={cache_buster}"


def _format_com_error(exc):
    """Return a concise COM error string with HRESULT when available."""
    hresult = getattr(exc, 'hresult', None)
    if hresult is None and getattr(exc, 'args', None):
        first = exc.args[0]
        if isinstance(first, int):
            hresult = first

    if hresult is not None:
        return f"HRESULT=0x{(hresult & 0xFFFFFFFF):08X}; {exc}"
    return str(exc)


def _get_candidate_device_ids(ascom_config):
    """Build ordered ASCOM device ID candidates from config."""
    configured_ids = []
    device_ids = ascom_config.get('device_ids', [])
    if isinstance(device_ids, list):
        configured_ids.extend([str(x).strip() for x in device_ids if str(x).strip()])

    single_device_id = str(ascom_config.get('device_id', '')).strip()
    if single_device_id:
        configured_ids.insert(0, single_device_id)

    # Preserve order while removing duplicates.
    seen = set()
    unique_ids = []
    for device_id in configured_ids:
        if device_id not in seen:
            unique_ids.append(device_id)
            seen.add(device_id)

    return unique_ids or ['ASCOM.SXCamera.Camera']


def _parse_time_string(time_str):
    """Parse a time string accepting HH:MM or HH:MM:SS."""
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(time_str, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time format: '{time_str}'")


def _set_optional_camera_property(camera, prop_name, value):
    """Set optional ASCOM property and continue when unsupported.

    ASCOM drivers may expose a property name but still reject read/write.
    We first consult capability flags when available to avoid noisy COM errors.
    """
    capability_map = {
        'Gain': 'CanSetGain',
        'SetCCDTemperature': 'CanSetCCDTemperature',
        'CoolerOn': 'CanSetCCDTemperature',
    }

    capability_name = capability_map.get(prop_name)
    if capability_name:
        try:
            capability = bool(getattr(camera, capability_name))
            if not capability:
                logger.info(
                    "Skipping optional camera property '%s': %s=False",
                    prop_name,
                    capability_name,
                )
                return False
        except Exception as e:
            logger.info(
                "Skipping optional camera property '%s': unable to read %s (%s)",
                prop_name,
                capability_name,
                _format_com_error(e),
            )
            return False

    try:
        setattr(camera, prop_name, value)
        return True
    except Exception as e:
        logger.info(
            "Skipping optional camera property '%s'=%s: %s",
            prop_name,
            value,
            _format_com_error(e)
        )
        return False


def _call_optional_camera_method(camera, method_name, *args):
    """Call optional ASCOM method and ignore unsupported-method failures."""
    try:
        method = getattr(camera, method_name)
    except Exception as e:
        logger.info(
            "Skipping optional camera method '%s': %s",
            method_name,
            _format_com_error(e),
        )
        return False

    try:
        method(*args)
        return True
    except Exception as e:
        logger.info(
            "Skipping optional camera method '%s': %s",
            method_name,
            _format_com_error(e),
        )
        return False


def _normalize_dashboard_path(path_value, fallback='/assets/logo-impacton.jpg'):
    """Normalize configured image path into a web path usable by Dash."""
    if not path_value:
        return fallback

    normalized = str(path_value).replace('\\', '/').strip()
    if normalized.startswith('/'):
        return normalized

    # Convert common config style `assets/foo.jpg` into `/assets/foo.jpg`.
    if normalized.startswith('assets/'):
        return f"/{normalized}"

    return f"/{normalized}"


def _is_nighttime(config):
    """Check if it's currently nighttime (after sunset, before sunrise).
    
    Args:
        config (dict): Configuration dictionary with sunset/sunrise offsets.
    
    Returns:
        bool: True if nighttime, False if daytime.
    """
    global _sun_times_cache_date, _sunrise_cached, _sunset_cached
    global _last_nighttime_warning_at

    def _warn_once_per_minute(message):
        global _last_nighttime_warning_at
        now_local = datetime.datetime.now()
        if (
            _last_nighttime_warning_at is None
            or (now_local - _last_nighttime_warning_at).total_seconds() >= 60
        ):
            logger.warning(message)
            _last_nighttime_warning_at = now_local

    try:
        from .util import get_sun_times
        
        latitude = config.get('metadata', {}).get('latitude', -8.79225)
        longitude = config.get('metadata', {}).get('longitude', -38.68853)
        
        sunrise_str, sunset_str = get_sun_times(latitude, longitude)
        now = datetime.datetime.now()

        if sunrise_str == "N/D" or sunset_str == "N/D":
            # Reuse last successful values so transient API failures do not break schedule.
            if _sunrise_cached and _sunset_cached:
                sunrise_str, sunset_str = _sunrise_cached, _sunset_cached
                _warn_once_per_minute(
                    "Sunrise/sunset service returned N/D; using cached values"
                )
            else:
                _warn_once_per_minute(
                    "Sunrise/sunset service returned N/D and no cache is available; allowing capture"
                )
                return True

        # Parse times from util.get_sun_times (HH:MM:SS) while supporting HH:MM.
        sunset_time = _parse_time_string(sunset_str)
        sunrise_time = _parse_time_string(sunrise_str)

        # Update daily cache after successful parse.
        _sun_times_cache_date = now.date()
        _sunrise_cached = sunrise_str
        _sunset_cached = sunset_str
        
        # Apply offsets from config
        schedule = config.get('schedule', {})
        sunset_offset = schedule.get('capture_after_sunset_offset', 0)
        sunrise_offset = schedule.get('capture_before_sunrise_offset', 0)
        
        # Adjust times with offsets
        sunset_dt = datetime.datetime.combine(now.date(), sunset_time)
        sunset_dt += datetime.timedelta(minutes=sunset_offset)
        
        sunrise_dt = datetime.datetime.combine(now.date(), sunrise_time)
        if sunrise_time < sunset_time:  # Sunrise is next day
            sunrise_dt += datetime.timedelta(days=1)
        sunrise_dt -= datetime.timedelta(minutes=sunrise_offset)
        
        # Check if current time is in night window
        if now >= sunset_dt and now <= sunrise_dt:
            return True
        return False
        
    except Exception as e:
        _warn_once_per_minute(f"Error checking nighttime: {e}")
        # Default to allowing capture if check fails
        return True


def _connect_ascom_camera(device_ids, timeout=10, use_chooser_on_failure=False):
    """Connect to ASCOM camera driver.
    
    Args:
        device_ids (list[str]): Ordered ASCOM ProgID candidates.
        timeout (int): Connection timeout in seconds.
        use_chooser_on_failure (bool): If True, opens ASCOM chooser dialog
            when all configured ProgIDs fail.
    
    Returns:
        tuple: (camera_or_none, selected_device_id_or_none, error_summary_or_none).
    """
    if not ASCOM_AVAILABLE:
        logger.error("ASCOM not available - win32com not installed")
        return None, None, "win32com is not installed"

    if pythoncom is None:
        logger.error("ASCOM not available - pythoncom not installed")
        return None, None, "pythoncom is not installed"

    errors = []

    def _attempt_connect(device_id):
        camera_obj = win32com.client.Dispatch(device_id)
        camera_obj.Connected = True

        # Wait for connection
        import time
        start = time.time()
        while not camera_obj.Connected and (time.time() - start) < timeout:
            time.sleep(0.5)

        if not camera_obj.Connected:
            raise TimeoutError(f"Camera connection timeout after {timeout}s")
        return camera_obj

    for device_id in device_ids:
        try:
            camera = _attempt_connect(device_id)
            logger.info(f"Connected to ASCOM camera using '{device_id}': {camera.Name}")
            return camera, device_id, None
        except Exception as e:
            details = _format_com_error(e)
            errors.append(f"{device_id} -> {details}")
            logger.error(f"Failed to connect to ASCOM camera ({device_id}): {details}")

    if use_chooser_on_failure:
        try:
            chooser = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
            chooser.DeviceType = "Camera"
            selected = chooser.Choose(None)
            if selected:
                camera = _attempt_connect(selected)
                logger.info(f"Connected to ASCOM camera using chooser selection '{selected}'")
                return camera, selected, None
            errors.append("ASCOM chooser returned no selection")
        except Exception as e:
            errors.append(f"ASCOM chooser failed -> {_format_com_error(e)}")

    return None, None, "; ".join(errors) if errors else "No ASCOM device IDs configured"


def _capture_image(camera, config):
    """Capture image from ASCOM camera with configured settings.
    
    Args:
        camera: Connected ASCOM camera object.
        config (dict): Camera configuration dictionary.
    
    Returns:
        numpy.ndarray: Captured image array, or None on failure.
    """
    try:
        cam_config = config.get('camera', {})
        
        # Set camera parameters
        exposure = cam_config.get('exposure_time', 30.0)
        binning = cam_config.get('binning', 1)
        capture_timeout = float(cam_config.get('capture_timeout_seconds', max(120.0, float(exposure) + 60.0)))

        apply_binning = bool(cam_config.get('apply_binning', False))
        apply_gain = bool(cam_config.get('apply_gain', False))
        apply_cooling = bool(cam_config.get('apply_cooling', False))

        logger.info(
            "Capture settings: exposure=%ss, binning=%s (apply=%s), gain=%s (apply=%s), cooling_enabled=%s (apply=%s)",
            exposure,
            binning,
            apply_binning,
            cam_config.get('gain'),
            apply_gain,
            cam_config.get('cooling_enabled', False),
            apply_cooling,
        )

        # Reset any stale in-progress state before starting a new exposure.
        _call_optional_camera_method(camera, 'AbortExposure')

        # Optional camera properties can break some drivers; keep disabled by default.
        if apply_binning:
            _set_optional_camera_property(camera, 'BinX', binning)
            _set_optional_camera_property(camera, 'BinY', binning)

        gain = cam_config.get('gain')
        if apply_gain and gain is not None:
            _set_optional_camera_property(camera, 'Gain', gain)

        # Set temperature if cooling is configured and explicitly enabled
        cooling = cam_config.get('cooling_enabled', False)
        if apply_cooling and cooling:
            target_temp = cam_config.get('target_temperature', -10)
            _set_optional_camera_property(camera, 'SetCCDTemperature', target_temp)
            _set_optional_camera_property(camera, 'CoolerOn', True)
        
        # Start exposure
        logger.info(f"Starting {exposure}s exposure...")
        try:
            camera.StartExposure(exposure, True)  # True = light frame
        except Exception as e:
            logger.error(f"StartExposure failed: {_format_com_error(e)}")
            return None
        
        # Wait for exposure to complete
        import time
        start = time.time()
        while True:
            try:
                image_ready = bool(camera.ImageReady)
            except Exception as e:
                logger.error(f"ImageReady failed: {_format_com_error(e)}")
                return None

            if image_ready:
                break

            if (time.time() - start) > capture_timeout:
                logger.error(f"Exposure timeout after {capture_timeout:.1f}s")
                _call_optional_camera_method(camera, 'AbortExposure')
                return None

            time.sleep(0.5)
        
        # Get image data
        image_array = camera.ImageArray
        
        # Convert to numpy array
        if isinstance(image_array, (list, tuple)):
            # ASCOM returns as 2D array or variant array
            img_data = np.array(image_array, dtype=np.uint16)
        else:
            img_data = np.array(image_array)

        # Some ASCOM drivers expose ImageArray as [x, y] instead of [y, x].
        # Use reported camera dimensions to detect and correct swapped axes.
        if img_data.ndim == 2:
            try:
                cam_x = int(getattr(camera, 'CameraXSize'))
                cam_y = int(getattr(camera, 'CameraYSize'))
                if img_data.shape == (cam_x, cam_y):
                    img_data = np.transpose(img_data)
                    logger.info(
                        "Transposed ImageArray to match (height,width): raw=%s corrected=%s expected=(%s,%s)",
                        (cam_x, cam_y),
                        img_data.shape,
                        cam_y,
                        cam_x,
                    )
            except Exception:
                # If camera dimension properties are unavailable, keep original array.
                pass
        
        logger.info(f"Image captured: {img_data.shape}")
        return img_data
        
    except Exception as e:
        logger.error(f"Image capture failed: {e}")
        return None


def _process_image(img_array, config):
    """Process captured image (stretch, resize, watermark).
    
    Args:
        img_array (numpy.ndarray): Raw image array from camera.
        config (dict): Processing configuration.
    
    Returns:
        PIL.Image: Processed image, or None on failure.
    """
    if not PIL_AVAILABLE:
        logger.warning("PIL not available - cannot process image")
        return None
    
    try:
        proc_config = config.get('processing', {})
        
        # Normalize to 8-bit
        img_normalized = ((img_array - img_array.min()) / 
                          (img_array.max() - img_array.min()) * 255).astype(np.uint8)
        
        # Convert to PIL Image (handle grayscale)
        pil_img = Image.fromarray(img_normalized, mode='L')
        
        # Convert to RGB for processing
        pil_img = pil_img.convert('RGB')
        
        # Auto-stretch for better visibility
        if proc_config.get('auto_stretch', True):
            enhancer = ImageEnhance.Contrast(pil_img)
            pil_img = enhancer.enhance(1.5)
        
        # Resize if configured. Preserve aspect ratio by default to avoid distortion.
        width = proc_config.get('resize_width')
        height = proc_config.get('resize_height')
        preserve_aspect = bool(proc_config.get('preserve_aspect_ratio', True))
        if width or height:
            src_w, src_h = pil_img.size
            target_w = int(width) if width else None
            target_h = int(height) if height else None

            if preserve_aspect:
                if target_w and target_h:
                    scale = min(target_w / src_w, target_h / src_h)
                elif target_w:
                    scale = target_w / src_w
                else:
                    scale = target_h / src_h

                new_w = max(1, int(round(src_w * scale)))
                new_h = max(1, int(round(src_h * scale)))
                pil_img = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            elif target_w and target_h:
                pil_img = pil_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
        
        # Add watermark
        if proc_config.get('watermark', True):
            draw = ImageDraw.Draw(pil_img)
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            text = proc_config.get('watermark_text', 'OASI - {timestamp}')
            text = text.format(timestamp=timestamp)
            
            # Position watermark
            position = proc_config.get('watermark_position', 'bottom-right')
            img_width, img_height = pil_img.size
            
            try:
                font = ImageFont.truetype("arial.ttf", 20)
            except:
                font = ImageFont.load_default()
            
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            
            if 'bottom' in position:
                y = img_height - text_height - 10
            else:
                y = 10
            
            if 'right' in position:
                x = img_width - text_width - 10
            else:
                x = 10
            
            # Draw text with shadow for visibility
            draw.text((x+1, y+1), text, fill=(0, 0, 0), font=font)
            draw.text((x, y), text, fill=(255, 255, 255), font=font)
        
        return pil_img
        
    except Exception as e:
        logger.error(f"Image processing failed: {e}")
        return None


def _save_image(pil_img, config):
    """Save processed image to disk.
    
    Args:
        pil_img (PIL.Image): Processed image.
        config (dict): Storage configuration.
    
    Returns:
        str: Path to saved image file (relative to src/).
    """
    try:
        storage = config.get('storage', {})
        cam_config = config.get('camera', {})
        
        # Prepare save directory
        save_dir = Path(__file__).parent / storage.get('save_path', 'data/allsky')
        save_dir.mkdir(parents=True, exist_ok=True)
        
        # Save image
        quality = cam_config.get('quality', 90)
        keep_latest_only = bool(storage.get('keep_latest_only', True))
        latest_path = save_dir / 'latest.jpg'

        if keep_latest_only:
            # Overwrite a single file to keep disk usage bounded.
            pil_img.save(latest_path, 'JPEG', quality=quality)

            # Remove any legacy timestamped captures from previous configurations.
            for old_file in save_dir.glob('allsky_*.jpg'):
                try:
                    old_file.unlink()
                except Exception:
                    pass

            logger.info(f"Image saved (latest-only): {latest_path}")
        else:
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            pattern = storage.get('filename_pattern', 'allsky_{timestamp}.jpg')
            filename = pattern.format(timestamp=timestamp)
            filepath = save_dir / filename
            pil_img.save(filepath, 'JPEG', quality=quality)
            pil_img.save(latest_path, 'JPEG', quality=quality)
            logger.info(f"Image saved: {filepath}")

        return 'allsky/latest.jpg'
        
    except Exception as e:
        logger.error(f"Failed to save image: {e}")
        return None


def read_allsky(allsky_config_path):
    """Capture and return all-sky camera image URL.
    
    Main entry point for all-sky camera integration. Only captures images
    during nighttime (after sunset). Returns placeholder during day or on error.
    
    Args:
        allsky_config_path (str): Path to oculus.yaml configuration file.
    
    Returns:
        str: URL/path to all-sky image for dashboard display.
    """
    # Load configuration
    try:
        if not os.path.isabs(allsky_config_path):
            allsky_config_path = os.path.join(
                os.path.dirname(__file__), 
                allsky_config_path
            )

        logger.info("All-sky runtime paths: module=%s config=%s", __file__, allsky_config_path)
        
        with open(allsky_config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return '/assets/error_placeholder.jpg'
    
    # Check if it's nighttime
    if not _is_nighttime(config):
        logger.info("Daytime - skipping image capture")
        daytime_img = config.get('schedule', {}).get(
            'daytime_placeholder', 
            '/assets/daytime_placeholder.jpg'
        )
        return _normalize_dashboard_path(daytime_img)
    
    # Check if ASCOM is available
    if not ASCOM_AVAILABLE:
        logger.warning("ASCOM not available - using placeholder")
        return _normalize_dashboard_path(config.get('error_handling', {}).get(
            'placeholder_image',
            '/assets/error_placeholder.jpg'
        ))

    global _last_allsky_url, _last_capture_started_at

    camera_cfg = config.get('camera', {})
    min_interval_seconds = float(camera_cfg.get('min_capture_interval_seconds', 60))
    now = datetime.datetime.now()
    if _last_capture_started_at is not None:
        elapsed = (now - _last_capture_started_at).total_seconds()
        if elapsed < min_interval_seconds:
            latest_url = _latest_assets_image_url()
            if latest_url:
                _last_allsky_url = latest_url
            logger.info(
                "Skipping new capture (%.1fs < min %.1fs); reusing latest image",
                elapsed,
                min_interval_seconds,
            )
            return _last_allsky_url

    if not _capture_lock.acquire(blocking=False):
        latest_url = _latest_assets_image_url()
        if latest_url:
            _last_allsky_url = latest_url
        logger.info("All-sky capture already in progress; reusing latest image")
        return _last_allsky_url
    
    camera = None
    selected_device_id = None
    com_initialized = False
    try:
        if pythoncom is None:
            raise RuntimeError("pythoncom is not available in this environment")

        # Dash callbacks may run in worker threads where COM is not initialized.
        pythoncom.CoInitialize()
        com_initialized = True
        _last_capture_started_at = datetime.datetime.now()

        # Connect to camera
        ascom_config = config.get('ascom', {})
        device_ids = _get_candidate_device_ids(ascom_config)
        timeout = ascom_config.get('connect_timeout', 10)
        use_chooser = bool(ascom_config.get('use_chooser_on_failure', False))

        camera, selected_device_id, connect_error = _connect_ascom_camera(
            device_ids,
            timeout,
            use_chooser_on_failure=use_chooser
        )
        if not camera:
            raise ConnectionError(
                "Failed to connect to ASCOM camera. "
                f"Tried: {device_ids}. "
                f"Details: {connect_error}."
            )
        
        # Capture image
        img_array = _capture_image(camera, config)
        if img_array is None:
            raise RuntimeError("Failed to capture image")
        
        # Process image
        pil_img = _process_image(img_array, config)
        if pil_img is None:
            raise RuntimeError("Failed to process image")
        
        # Save image
        img_path = _save_image(pil_img, config)
        if img_path is None:
            raise RuntimeError("Failed to save image")
        
        # Return path for dashboard
        latest_url = _latest_assets_image_url() or f'/{img_path}'
        _last_allsky_url = latest_url
        return latest_url
        
    except Exception as e:
        logger.error(f"All-sky capture error: {e}")
        latest_url = _latest_assets_image_url()
        if latest_url:
            _last_allsky_url = latest_url
            return latest_url
        # Return placeholder on error
        error_config = config.get('error_handling', {})
        if error_config.get('use_placeholder_on_error', True):
            return _normalize_dashboard_path(error_config.get(
                'placeholder_image',
                '/assets/error_placeholder.jpg'
            ))
        return '/assets/error_placeholder.jpg'
        
    finally:
        # Disconnect camera
        if camera:
            try:
                camera.Connected = False
            except:
                pass
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        _capture_lock.release()

