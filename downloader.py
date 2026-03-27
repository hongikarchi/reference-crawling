"""Image downloading with retry and streaming."""

import os

import config
from utils import logger, RateLimiter


def download_image(url, local_path, session, rate_limiter):
    """Download a single image to local_path. Returns file size on success, None on failure."""
    rate_limiter.wait()

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT, stream=True)

            if resp.status_code == 404:
                logger.warning(f"Image 404: {url}")
                return None

            resp.raise_for_status()

            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            file_size = os.path.getsize(local_path)
            if file_size == 0:
                os.remove(local_path)
                logger.warning(f"Empty image file: {url}")
                return None

            return file_size

        except Exception as e:
            if attempt < config.RETRY_MAX_ATTEMPTS:
                logger.warning(f"Image download error {url}: {e}, retry {attempt}/{config.RETRY_MAX_ATTEMPTS}")
                import time
                time.sleep(config.RETRY_BACKOFF_FACTOR ** attempt)
            else:
                logger.error(f"Failed to download image after {config.RETRY_MAX_ATTEMPTS} attempts: {url}")
                if os.path.exists(local_path):
                    os.remove(local_path)
                return None

    return None
