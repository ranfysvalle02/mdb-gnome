"""Export and ZIP creation helpers for experiment packages."""
import os
import sys
import json
import asyncio
import shutil
import zipfile
import fnmatch
import io
import re
import hashlib
import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import BASE_DIR, EXPERIMENTS_DIR, TEMPLATES_DIR, EXPORTS_TEMP_DIR
from utils import (
    read_file_async,
    write_file_async,
    calculate_dir_size_sync,
    make_json_serializable,
)
from b2_utils import upload_export_to_b2, generate_presigned_download_url

logger = logging.getLogger(__name__)

# Master requirements (loaded at module level)
def _parse_requirements_file_sync(req_path: Path) -> List[str]:
    """Synchronous version for module-level initialization."""
    if not req_path.is_file():
        return []
    lines = []
    try:
        with req_path.open("r", encoding="utf-8") as rf:
            for raw_line in rf:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                lines.append(line)
    except Exception as e:
        logger.error(f"Failed to read requirements file '{req_path}': {e}")
        return []
    return lines


MASTER_REQUIREMENTS = _parse_requirements_file_sync(BASE_DIR / "requirements.txt")
if MASTER_REQUIREMENTS:
    logger.info(f"Master environment requirements loaded ({len(MASTER_REQUIREMENTS)} lines).")
else:
    logger.info("No top-level requirements.txt found or empty.")


def _extract_pkgname(line: str) -> str:
    """Extract package name from a requirements line."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return ""
    if 'pkg_resources' in sys.modules:
        try:
            import pkg_resources
            req = pkg_resources.Requirement.parse(line)
            return req.name.lower()
        except Exception:
            pass
    match = re.match(r"[-e\s]*([\w\-\._]+)", line)
    if match:
        return match.group(1).lower()
    return line.lower()


def _parse_requirements_from_string(content: str) -> List[str]:
    """Parse requirements from a string."""
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def parse_requirements_from_string(content: str) -> List[str]:
    """Parse requirements from a string (public wrapper)."""
    return _parse_requirements_from_string(content)


async def estimate_export_size(
    db_data: Dict[str, Any],
    db_collections: Dict[str, List[Dict[str, Any]]],
    source_dir: Path,
    slug_id: str
) -> int:
    """Estimate the size of the export in bytes."""
    size = 0
    
    # Estimate size of JSON data
    try:
        size += len(json.dumps(db_data, indent=2).encode('utf-8'))
        size += len(json.dumps(db_collections, indent=2).encode('utf-8'))
    except Exception:
        pass
    
    # Estimate size of experiment files (offloaded to thread pool)
    experiment_path = source_dir / "experiments" / slug_id
    if experiment_path.is_dir():
        size += await asyncio.to_thread(calculate_dir_size_sync, experiment_path)
    
    return size


def should_use_disk_streaming(estimated_size: int, max_size_mb: int = 100) -> bool:
    """Determine if disk streaming should be used based on estimated size."""
    max_size_bytes = max_size_mb * 1024 * 1024
    return estimated_size > max_size_bytes


def calculate_export_checksum(zip_source: io.BytesIO | Path) -> str:
    """Calculate SHA256 checksum of the export ZIP."""
    try:
        if isinstance(zip_source, Path):
            zip_data = zip_source.read_bytes()
        else:
            zip_source.seek(0)
            zip_data = zip_source.getvalue()
        
        checksum = hashlib.sha256(zip_data).hexdigest()
        logger.debug(f"Calculated export checksum: {checksum[:16]}...")
        return checksum
    except Exception as e:
        logger.error(f"Failed to calculate export checksum: {e}", exc_info=True)
        raise


async def find_existing_export_by_checksum(
    db,
    checksum: str,
    slug_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Find an existing export with the same checksum that is not invalidated."""
    try:
        query = {
            "checksum": checksum,
            "invalidated": {"$ne": True}
        }
        if slug_id:
            query["slug_id"] = slug_id
        
        existing_export = await db.export_logs.find_one(query, sort=[("created_at", -1)])
        
        if existing_export:
            logger.info(f"Found existing export with matching checksum: {existing_export.get('_id')}")
            return existing_export
        
        return None
    except Exception as e:
        logger.warning(f"Error checking for existing export by checksum: {e}", exc_info=True)
        return None


async def save_export_locally(zip_source: io.BytesIO | Path, filename: str) -> Path:
    """Save export ZIP to local temp directory."""
    export_file = EXPORTS_TEMP_DIR / filename
    try:
        if isinstance(zip_source, Path):
            if zip_source != export_file:
                shutil.copy2(zip_source, export_file)
                zip_source.unlink()
            logger.debug(f"Export already on disk: {export_file}")
        else:
            zip_source.seek(0)
            zip_data = zip_source.getvalue()
            await write_file_async(export_file, zip_data)
            logger.debug(f"Saved export to local temp: {export_file}")
        return export_file
    except Exception as e:
        logger.error(f"Failed to save export locally: {e}", exc_info=True)
        raise


async def cleanup_local_export_file(file_path: Path | None):
    """Remove a local export file after successful B2 upload."""
    if not file_path:
        return
    
    try:
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            logger.debug(f"Cleaned up local export file after B2 upload: {file_path.name}")
    except Exception as e:
        logger.warning(f"Failed to cleanup local export file {file_path.name}: {e}")


async def cleanup_old_exports(max_age_hours: int = 24):
    """Remove export files older than max_age_hours from temp directory."""
    try:
        cutoff_time = datetime.datetime.now().timestamp() - (max_age_hours * 3600)
        
        files_to_delete = []
        for export_file in EXPORTS_TEMP_DIR.glob("*.zip"):
            try:
                if export_file.stat().st_mtime < cutoff_time:
                    files_to_delete.append(export_file)
            except Exception as e:
                logger.warning(f"Failed to check export file {export_file.name}: {e}")
        
        async def _delete_file_safe(file_path: Path) -> Tuple[Path, bool]:
            """Delete a single file, returning (path, success)."""
            try:
                file_path.unlink()
                logger.debug(f"Cleaned up old export: {file_path.name}")
                return file_path, True
            except Exception as e:
                logger.warning(f"Failed to delete old export {file_path.name}: {e}")
                return file_path, False
        
        if files_to_delete:
            delete_tasks = [_delete_file_safe(f) for f in files_to_delete]
            results = await asyncio.gather(*delete_tasks, return_exceptions=True)
            
            deleted_count = sum(1 for r in results if not isinstance(r, Exception) and r[1])
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count}/{len(files_to_delete)} old export file(s) in parallel operation")
    except Exception as e:
        logger.error(f"Error during export cleanup: {e}", exc_info=True)


async def log_export(
    db,
    slug_id: str,
    export_type: str,
    user_email: str,
    local_file_path: Optional[str] = None,
    file_size: Optional[int] = None,
    b2_file_name: Optional[str] = None,
    invalidated: bool = False,
    checksum: Optional[str] = None
):
    """Log an export event to the database for tracking purposes."""
    try:
        export_log = {
            "slug_id": slug_id,
            "export_type": export_type,
            "user_email": user_email,
            "local_file_path": local_file_path,
            "file_size": file_size,
            "b2_file_name": b2_file_name,
            "invalidated": invalidated,
            "checksum": checksum,
            "created_at": datetime.datetime.utcnow(),
        }
        result = await db.export_logs.insert_one(export_log)
        logger.debug(f"Logged export: {slug_id} ({export_type}) by {user_email}, ID: {result.inserted_id}")
        return result.inserted_id
    except Exception as e:
        logger.error(f"Failed to log export to database: {e}", exc_info=True)
        return None


def parse_requirements_file_sync(req_path: Path) -> List[str]:
    """Synchronous version for module-level initialization."""
    return _parse_requirements_file_sync(req_path)


async def dump_db_to_json(db, slug_id: str) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    """Dump experiment database to JSON format."""
    config_doc = await db.experiments_config.find_one({"slug": slug_id})
    if not config_doc:
        raise ValueError(f"No experiment config found for slug '{slug_id}'")
    config_data = dict(config_doc)
    if "_id" in config_data:
        config_data["_id"] = str(config_data["_id"])
    
    config_data = make_json_serializable(config_data)

    sub_collections = []
    all_coll_names = await db.list_collection_names()
    for cname in all_coll_names:
        if cname.startswith(f"{slug_id}_"):
            sub_collections.append(cname)

    async def _dump_single_collection(coll_name: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Dump a single collection's documents."""
        docs_list = []
        try:
            cursor = db[coll_name].find().limit(10000)
            async for doc in cursor:
                doc_dict = dict(doc)
                if "_id" in doc_dict:
                    doc_dict["_id"] = str(doc_dict["_id"])
                doc_dict = make_json_serializable(doc_dict)
                docs_list.append(doc_dict)
        except Exception as e:
            logger.error(f"Error dumping collection '{coll_name}': {e}", exc_info=True)
            docs_list = []
        return coll_name, docs_list

    dump_tasks = [_dump_single_collection(coll_name) for coll_name in sub_collections]
    results = await asyncio.gather(*dump_tasks, return_exceptions=True)
    
    collections_data: Dict[str, List[Dict[str, Any]]] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Error in collection dump task: {result}", exc_info=True)
            continue
        coll_name, docs_list = result
        collections_data[coll_name] = docs_list

    return config_data, collections_data


# Template generation functions - templates should be passed as parameter
def make_intelligent_standalone_main_py(slug_id: str, templates) -> str:
    """Generate intelligent standalone main.py using real MongoDB."""
    if not templates:
        raise RuntimeError("Jinja2 templates object is not initialized.")
    template = templates.get_template("standalone_main_intelligent.py.jinja2")
    standalone_main_source = template.render(slug_id=slug_id)
    return standalone_main_source


def fix_static_paths(html_content: str, slug_id: str) -> str:
    """Rewrite references to 'static/' => '/experiments/<slug>/static/'."""
    replaced = html_content
    
    pattern_src_href = re.compile(r'(src|href)=([\'"])static/')
    def replacer_src_href(match):
        attr = match.group(1)
        quote_char = match.group(2)
        return f'{attr}={quote_char}/experiments/{slug_id}/static/'
    
    replaced = pattern_src_href.sub(replacer_src_href, replaced)
    
    pattern_url = re.compile(r'url\(\s*[\'"]?\s*static/')
    replaced = pattern_url.sub(f'url("/experiments/{slug_id}/static/', replaced)
    return replaced


def create_zip_to_disk(
    slug_id: str,
    source_dir: Path,
    db_data: Dict[str, Any],
    db_collections: Dict[str, List[Dict[str, Any]]],
    zip_path: Path,
    experiment_path: Path,
    templates_dir: Path | None,
    standalone_main_source: str,
    requirements_content: str,
    readme_content: str,
    include_templates: bool = False,
    include_docker_files: bool = False,
    dockerfile_content: str | None = None,
    docker_compose_content: str | None = None,
    main_file_name: str = "standalone_main.py"
) -> Path:
    """Create a ZIP archive directly on disk for large exports."""
    EXCLUSION_PATTERNS = [
        "__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"
    ]
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        if experiment_path.is_dir():
            logger.debug(f"Including experiment code from: {experiment_path}")
            for folder_name, _, file_names in os.walk(experiment_path):
                if Path(folder_name).name in EXCLUSION_PATTERNS:
                    continue
                
                for file_name in file_names:
                    if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
                        continue
                    
                    file_path = Path(folder_name) / file_name
                    try:
                        arcname = str(file_path.relative_to(experiment_path))
                    except ValueError:
                        logger.error(f"Failed to get relative path for {file_path}")
                        continue
                    
                    if file_path.suffix in (".html", ".htm"):
                        original_html = file_path.read_text(encoding="utf-8")
                        fixed_html = fix_static_paths(original_html, slug_id)
                        zf.writestr(arcname, fixed_html)
                    else:
                        zf.write(file_path, arcname)
        
        if include_templates and templates_dir and templates_dir.is_dir():
            for folder_name, _, file_names in os.walk(templates_dir):
                if Path(folder_name).name in EXCLUSION_PATTERNS:
                    continue
                for file_name in file_names:
                    if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
                        continue
                    file_path = Path(folder_name) / file_name
                    try:
                        arcname = f"templates/{file_path.relative_to(templates_dir)}"
                    except ValueError:
                        continue
                    zf.write(file_path, arcname)
        
        experiments_init = source_dir / "experiments" / "__init__.py"
        if experiments_init.is_file():
            zf.write(experiments_init, "experiments/__init__.py")
        
        if include_docker_files:
            mongo_wrapper = source_dir / "async_mongo_wrapper.py"
            if mongo_wrapper.is_file():
                zf.write(mongo_wrapper, "async_mongo_wrapper.py")
            
            mongo_pool = source_dir / "mongo_connection_pool.py"
            if mongo_pool.is_file():
                zf.write(mongo_pool, "mongo_connection_pool.py")
            
            experiment_db = source_dir / "experiment_db.py"
            if experiment_db.is_file():
                zf.write(experiment_db, "experiment_db.py")
            
            if dockerfile_content:
                zf.writestr("Dockerfile", dockerfile_content)
            if docker_compose_content:
                zf.writestr("docker-compose.yml", docker_compose_content)
        
        zf.writestr("db_config.json", json.dumps(db_data, indent=2))
        zf.writestr("db_collections.json", json.dumps(db_collections, indent=2))
        zf.writestr(main_file_name, standalone_main_source)
        zf.writestr("README.md", readme_content)
        
        if requirements_content:
            zf.writestr("requirements.txt", requirements_content)
    
    return zip_path

