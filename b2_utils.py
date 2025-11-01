"""Backblaze B2 utilities for presigned URLs and uploads."""
import logging
import asyncio
import io
from pathlib import Path
from typing import Union
from config import B2SDK_AVAILABLE, B2Error

logger = logging.getLogger(__name__)


def generate_presigned_download_url(b2_bucket, file_name: str, duration_seconds: int = 3600) -> str:
    """
    Generates a secure, time-limited HTTPS download URL using native B2 SDK.
    B2 SDK always returns HTTPS URLs, avoiding mixed content warnings.
    
    Args:
        b2_bucket: B2 bucket instance from B2Api
        file_name: Object key in the bucket
        duration_seconds: URL expiration time (default: 1 hour)
        
    Returns:
        HTTPS presigned download URL
    """
    if not B2SDK_AVAILABLE:
        raise RuntimeError("B2 SDK not available")
    
    try:
        # B2 SDK generates HTTPS URLs by default
        url = b2_bucket.get_download_url(file_name, duration_seconds)
        
        # Verify it's HTTPS (B2 SDK should always return HTTPS, but be defensive)
        if not url.startswith('https://'):
            logger.error(f"CRITICAL: B2 SDK returned non-HTTPS URL: {url[:100]}...")
            if url.startswith('http://'):
                url = url.replace('http://', 'https://', 1)
                logger.warning(f"Forced HTTPS on B2 presigned URL (was HTTP): {file_name}")
            else:
                raise ValueError(f"Invalid URL scheme from B2 SDK: {url[:50]}")
        
        logger.debug(f"Generated B2 presigned HTTPS URL for: {file_name}")
        return url
    except B2Error as e:
        logger.error(f"B2 SDK error generating presigned URL for '{file_name}': {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error generating B2 presigned URL: {e}", exc_info=True)
        raise


async def upload_export_to_b2(
    b2_bucket,
    zip_source: Union[io.BytesIO, Path],
    b2_filename: str
) -> str:
    """
    Uploads export ZIP to B2 storage.
    Accepts either BytesIO or Path.
    Returns the B2 file key/name.
    """
    if not B2SDK_AVAILABLE:
        raise RuntimeError("B2 SDK not available")
    
    try:
        # Get ZIP data
        if isinstance(zip_source, Path):
            zip_data = zip_source.read_bytes()
        else:
            zip_source.seek(0)
            zip_data = zip_source.getvalue()
        
        # Upload to B2 (offload to thread pool to avoid blocking event loop)
        await asyncio.to_thread(b2_bucket.upload_bytes, zip_data, b2_filename)
        logger.info(f"Uploaded export to B2: {b2_filename}")
        return b2_filename
    except B2Error as e:
        logger.error(f"B2 upload failed for '{b2_filename}': {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Failed to upload export to B2: {e}", exc_info=True)
        raise

