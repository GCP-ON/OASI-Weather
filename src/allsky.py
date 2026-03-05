"""
All Sky Camera Communication Module (ASCOM Protocol)

Handles communication with the Starlight Express Oculus 3 all-sky camera
via ASCOM protocol. Only captures images during nighttime (after sunset).

Windows-only module using ASCOM drivers via win32com.
"""

import os
import yaml
import datetime
import logging
from pathlib import Path
import numpy as np

# Windows ASCOM support
try:
    import win32com.client
    ASCOM_AVAILABLE = True
except ImportError:
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


def _is_nighttime(config):
    """Check if it's currently nighttime (after sunset, before sunrise).
    
    Args:
        config (dict): Configuration dictionary with sunset/sunrise offsets.
    
    Returns:
        bool: True if nighttime, False if daytime.
    """
    try:
        from .util import get_sun_times
        
        latitude = config.get('metadata', {}).get('latitude', -8.79225)
        longitude = config.get('metadata', {}).get('longitude', -38.68853)
        
        sunrise_str, sunset_str = get_sun_times(latitude, longitude)
        now = datetime.datetime.now()
        
        # Parse sunset/sunrise times (format: "HH:MM")
        sunset_time = datetime.datetime.strptime(sunset_str, "%H:%M").time()
        sunrise_time = datetime.datetime.strptime(sunrise_str, "%H:%M").time()
        current_time = now.time()
        
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
        logger.error(f"Error checking nighttime: {e}")
        # Default to allowing capture if check fails
        return True


def _connect_ascom_camera(device_id, timeout=10):
    """Connect to ASCOM camera driver.
    
    Args:
        device_id (str): ASCOM device identifier (e.g., "ASCOM.SXCamera.Camera").
        timeout (int): Connection timeout in seconds.
    
    Returns:
        object: Connected ASCOM camera object, or None on failure.
    """
    if not ASCOM_AVAILABLE:
        logger.error("ASCOM not available - win32com not installed")
        return None
    
    try:
        camera = win32com.client.Dispatch(device_id)
        camera.Connected = True
        
        # Wait for connection
        import time
        start = time.time()
        while not camera.Connected and (time.time() - start) < timeout:
            time.sleep(0.5)
        
        if camera.Connected:
            logger.info(f"Connected to ASCOM camera: {camera.Name}")
            return camera
        else:
            logger.error("Camera connection timeout")
            return None
            
    except Exception as e:
        logger.error(f"Failed to connect to ASCOM camera: {e}")
        return None


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
        
        # Set binning if supported
        if hasattr(camera, 'BinX') and hasattr(camera, 'BinY'):
            camera.BinX = binning
            camera.BinY = binning
        
        # Set gain if supported
        if hasattr(camera, 'Gain'):
            gain = cam_config.get('gain', 100)
            camera.Gain = gain
        
        # Set temperature if cooling enabled
        cooling = cam_config.get('cooling_enabled', False)
        if cooling and hasattr(camera, 'SetCCDTemperature'):
            target_temp = cam_config.get('target_temperature', -10)
            camera.SetCCDTemperature = target_temp
            camera.CoolerOn = True
        
        # Start exposure
        logger.info(f"Starting {exposure}s exposure...")
        camera.StartExposure(exposure, True)  # True = light frame
        
        # Wait for exposure to complete
        import time
        while not camera.ImageReady:
            time.sleep(0.5)
        
        # Get image data
        image_array = camera.ImageArray
        
        # Convert to numpy array
        if isinstance(image_array, (list, tuple)):
            # ASCOM returns as 2D array or variant array
            img_data = np.array(image_array, dtype=np.uint16)
        else:
            img_data = np.array(image_array)
        
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
        
        # Resize if configured
        width = proc_config.get('resize_width')
        height = proc_config.get('resize_height')
        if width and height:
            pil_img = pil_img.resize((width, height), Image.Resampling.LANCZOS)
        
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
        
        # Generate filename
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        pattern = storage.get('filename_pattern', 'allsky_{timestamp}.jpg')
        filename = pattern.format(timestamp=timestamp)
        filepath = save_dir / filename
        
        # Save image
        quality = cam_config.get('quality', 90)
        pil_img.save(filepath, 'JPEG', quality=quality)
        
        # Clean up old images if keep_latest_only
        if storage.get('keep_latest_only', True):
            for old_file in save_dir.glob('allsky_*.jpg'):
                if old_file != filepath:
                    old_file.unlink()
        
        logger.info(f"Image saved: {filepath}")
        
        # Return relative path for web serving
        rel_path = filepath.relative_to(Path(__file__).parent.parent)
        return str(rel_path)
        
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
        return daytime_img
    
    # Check if ASCOM is available
    if not ASCOM_AVAILABLE:
        logger.warning("ASCOM not available - using placeholder")
        return config.get('error_handling', {}).get(
            'placeholder_image',
            '/assets/error_placeholder.jpg'
        )
    
    camera = None
    try:
        # Connect to camera
        ascom_config = config.get('ascom', {})
        device_id = ascom_config.get('device_id', 'ASCOM.SXCamera.Camera')
        timeout = ascom_config.get('connect_timeout', 10)
        
        camera = _connect_ascom_camera(device_id, timeout)
        if not camera:
            raise ConnectionError("Failed to connect to camera")
        
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
        return f'/{img_path}'
        
    except Exception as e:
        logger.error(f"All-sky capture error: {e}")
        # Return placeholder on error
        error_config = config.get('error_handling', {})
        if error_config.get('use_placeholder_on_error', True):
            return error_config.get(
                'placeholder_image',
                '/assets/error_placeholder.jpg'
            )
        return '/assets/error_placeholder.jpg'
        
    finally:
        # Disconnect camera
        if camera:
            try:
                camera.Connected = False
            except:
                pass

