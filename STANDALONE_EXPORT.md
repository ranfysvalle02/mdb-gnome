# Standalone Export Documentation

## Overview

This document describes the export process for experiments and best practices for re-importing modified exports back into the platform. Understanding this process helps prevent issues when iterating on exported experiments.

## Export Types

The platform supports three types of exports:

### 1. Standalone Export (`/api/package-standalone/{slug_id}`)

**Purpose**: Create a complete, self-contained FastAPI application that can run independently.

**Contents**:
- Platform files (database abstractions, wrappers)
- Generated `standalone_main.py` - Complete FastAPI application
- Database snapshots:
  - `db_config.json` - Experiment configuration
  - `db_collections.json` - All experiment data (up to 10,000 documents per collection)
- Experiment code:
  - `experiments/{slug}/` - All experiment files
  - `manifest.json`, `actor.py`, `__init__.py`
  - `requirements.txt`, `templates/`, `static/`
- Docker files (if docker export):
  - `Dockerfile`
  - `docker-compose.yml`
- Supporting files:
  - `requirements.txt` - Full dependency list including Ray
  - `README.md` - Deployment instructions
  - Platform abstraction files (`async_mongo_wrapper.py`, `experiment_db.py`, etc.)

**Use Case**: Deploy experiment as independent application.

### 2. Upload-Ready Export (`/api/package-upload-ready/{slug_id}`)

**Purpose**: Minimal export containing only experiment files, optimized for iterative development.

**Contents**:
- Only experiment files:
  - `manifest.json` (required)
  - `actor.py` (required)
  - `__init__.py` (required)
  - `requirements.txt` (optional)
  - `templates/` (optional)
  - `static/` (optional)
- Files at root level (no nested `experiments/{slug}/` structure)

**Use Case**: Edit experiment code and upload back to platform.

### 3. Docker Export (`/api/package-docker/{slug_id}`)

**Purpose**: Standalone export with Docker configuration for containerized deployments.

**Contents**: Same as standalone export, plus Docker-specific configurations.

## Export Process

### Step-by-Step Export Process

1. **Configuration Check**
   - Verifies experiment exists and is active
   - Checks authentication requirements if applicable

2. **Database Dump**
   - Extracts experiment configuration from `experiments_config` collection
   - Extracts all collections prefixed with `{slug_id}_`
   - Limits to 10,000 documents per collection (prevents accidental large exports)
   - Converts MongoDB ObjectIDs to strings for JSON serialization
   - Makes data JSON-serializable (handles dates, ObjectIDs, etc.)

3. **Code Collection**
   - Collects all files from `experiments/{slug}/` directory
   - Excludes: `__pycache__/`, `.DS_Store`, `*.pyc`, `.git/`, etc.
   - Fixes static file paths in HTML templates:
     - `static/` → `/experiments/{slug}/static/`
   - Includes templates if present
   - Includes static files if present

4. **Template Generation**
   - Generates `standalone_main.py` from Jinja2 template
   - Generates `README.md` with deployment instructions
   - Generates `requirements.txt` by merging:
     - Base requirements (FastAPI, Ray, Motor, etc.)
     - Experiment-specific requirements
   - Generates Docker files (if docker export)

5. **ZIP Creation**
   - Creates ZIP archive with all collected files
   - Uses ZIP_DEFLATED compression
   - Calculates SHA256 checksum for deduplication

6. **Storage**
   - If B2 (Backblaze) is configured:
     - Uploads to B2 with filename: `exports/{slug}_intelligent_export_{timestamp}.zip`
     - Generates presigned download URL (24-hour validity)
     - Reuses existing exports with matching checksum
   - If B2 not configured:
     - Saves to local `temp_exports/` directory
     - Generates download URL from local file

7. **Logging**
   - Logs export event to `export_logs` collection
   - Tracks: slug_id, export_type, user_email, file_size, checksum, B2 filename
   - Only logs if new export (not reused)

## Import/Re-Import Process

### Upload Endpoint (`/admin/api/upload-experiment`)

The upload process handles re-importing modified exports back into the platform.

### Step-by-Step Import Process

1. **ZIP Upload**
   - Receives ZIP file via POST request
   - Validates file is a valid ZIP archive

2. **Slug Detection**
   - Attempts to auto-detect slug from ZIP structure:
     - Looks for `experiments/{slug}/` containing required files
     - Looks for directories with both `actor.py` and `__init__.py`
   - Falls back to manual slug specification if needed

3. **Required Files Validation**
   - Validates ZIP contains:
     - `manifest.json` (required)
     - `actor.py` with `ExperimentActor` class (required)
     - `__init__.py` with `bp` (APIRouter) variable (required)

4. **Directory Cleanup**
   - Deletes existing `experiments/{slug}/` directory if present
   - Handles permission issues and locked files gracefully
   - Creates fresh directory

5. **File Extraction**
   - Extracts all files from ZIP to `experiments/{slug}/`
   - Handles nested structures:
     - Flattens `experiments/{slug}/experiments/{slug}/` → `experiments/{slug}/`
   - **Important**: Currently extracts ALL files (does not filter platform files)

6. **B2 Storage** (if configured)
   - Uploads ZIP to B2 as `{slug}/runtime-{timestamp}.zip`
   - Generates presigned URL stored in experiment config (`runtime_s3_uri`)
   - Used by Ray for code distribution in clusters

7. **Registration**
   - Updates experiment configuration in database
   - Registers routes immediately (synchronous)
   - Starts Ray actors in background (asynchronous)
   - Reloads experiment configuration

8. **Background Reload**
   - Performs full experiment reload asynchronously:
     - Creates database indexes
     - Initializes Ray actors
     - Registers routes
   - Upload response returns immediately (non-blocking)

## Best Practices for Re-Import

### ⚠️ Critical: Platform Files vs Experiment Files

**Problem**: Standalone export includes platform files that should NOT be in the experiment directory.

**Platform Files** (DO NOT UPLOAD):
- `standalone_main.py`, `main.py`
- `db_config.json`, `db_collections.json`
- `async_mongo_wrapper.py`, `mongo_connection_pool.py`, `experiment_db.py`
- `Dockerfile`, `docker-compose.yml`
- Root-level `requirements.txt`, `README.md`

**Experiment Files** (UPLOAD THESE):
- `manifest.json`
- `actor.py`
- `__init__.py`
- `requirements.txt` (experiment-specific only)
- `templates/`
- `static/`

### Best Practice Workflow

#### Option 1: Use Upload-Ready Export (Recommended)

**For iterative development**, use the upload-ready export:

1. **Export**
   ```bash
   GET /api/package-upload-ready/{slug_id}
   ```
   This gives you a clean ZIP with only experiment files.

2. **Edit**
   - Extract ZIP or edit directly
   - Modify `actor.py`, `__init__.py`, templates, etc.
   - Update `requirements.txt` if needed

3. **Upload**
   - Upload ZIP directly via admin panel
   - No extraction needed - ZIP is ready to upload

**Advantages**:
- No platform files to worry about
- No extraction/re-zip needed
- Files at correct structure level
- Optimized for iterative workflow

#### Option 2: Extract from Standalone Export (Manual)

If you need to use standalone export, extract only experiment files:

1. **Extract Standalone Export**
   ```bash
   unzip {slug}_intelligent_export_*.zip
   cd {slug}_intelligent_export_*/
   ```

2. **Navigate to Experiment Directory**
   ```bash
   cd experiments/{slug}
   ```

3. **Verify Required Files**
   ```bash
   ls manifest.json actor.py __init__.py
   ```
   All three must exist.

4. **Edit Files**
   - Modify `actor.py`, `__init__.py`, templates, etc.
   - Update `requirements.txt` if needed

5. **Create Upload ZIP**
   ```bash
   # From experiments/{slug} directory
   zip -r ../../{slug}_upload.zip .
   ```

6. **Upload**
   - Upload `{slug}_upload.zip` via admin panel

### Pre-Upload Checklist

Before uploading a modified export, verify:

- [ ] ZIP contains `manifest.json` at root or in `experiments/{slug}/`
- [ ] ZIP contains `actor.py` with `ExperimentActor` class
- [ ] ZIP contains `__init__.py` with `bp` (APIRouter) variable
- [ ] No platform files in ZIP (`standalone_main.py`, `db_config.json`, etc.)
- [ ] Experiment-specific `requirements.txt` only includes experiment dependencies
- [ ] Static file paths in HTML are correct (`/experiments/{slug}/static/`)
- [ ] Template paths are correct
- [ ] Slug ID is correct and matches experiment

### Testing Before Upload

1. **Extract and Inspect**
   ```bash
   unzip {slug}_upload.zip -d test_extract/
   cd test_extract/
   tree -a  # or ls -R
   ```

2. **Verify Structure**
   - Check for platform files (should not be present)
   - Check for required files (must be present)
   - Check file paths are correct

3. **Validate Code**
   - Check `actor.py` has `ExperimentActor` class
   - Check `__init__.py` has `bp` variable
   - Check `manifest.json` is valid JSON

4. **Test Upload**
   - Upload to test environment first (if available)
   - Verify experiment loads correctly
   - Check logs for errors

## Common Issues and Solutions

### Issue 1: Platform Files in Experiment Directory

**Symptom**: After upload, experiment directory contains `standalone_main.py`, `db_config.json`, etc.

**Cause**: Uploaded standalone export ZIP directly without extracting experiment files.

**Solution**: 
- Use upload-ready export for iterative development
- Or extract only `experiments/{slug}/` files from standalone export

**Prevention**: Always verify ZIP contents before upload.

### Issue 2: Missing Required Files

**Symptom**: Upload fails with "manifest.json not found" or similar.

**Cause**: Required files missing or at wrong path in ZIP.

**Solution**:
- Ensure `manifest.json`, `actor.py`, `__init__.py` are in ZIP root or `experiments/{slug}/`
- Check file names are correct (case-sensitive)

**Prevention**: Use upload-ready export or verify ZIP structure.

### Issue 3: Import Errors After Upload

**Symptom**: Experiment loads but routes don't work or actors fail.

**Cause**: 
- Code changes broke compatibility
- Missing dependencies in `requirements.txt`
- Template/static file path issues

**Solution**:
- Check logs for specific errors
- Verify `requirements.txt` includes all dependencies
- Verify static file paths use `/experiments/{slug}/static/`
- Test code changes locally before upload

**Prevention**: Test changes incrementally, test locally first.

### Issue 4: Duplicate Export Storage

**Symptom**: Multiple exports with same content stored separately.

**Cause**: Export checksum deduplication not working.

**Solution**:
- System automatically reuses exports with matching checksum
- If issue persists, check B2 configuration and logs

**Prevention**: System handles this automatically via checksum matching.

### Issue 5: Static Files Not Loading

**Symptom**: Images, CSS, JS files return 404 after upload.

**Cause**: Static file paths not updated in templates.

**Solution**:
- Ensure templates use `/experiments/{slug}/static/` paths
- Export process automatically fixes paths in HTML templates
- For manual edits, update paths manually

**Prevention**: Use export process to ensure paths are correct.

## Export Checksum Deduplication

The export system uses SHA256 checksums to avoid duplicate storage:

1. **Checksum Calculation**: Calculated from entire ZIP content
2. **Duplicate Detection**: Checks `export_logs` collection for matching checksum
3. **Reuse Logic**: If matching export found:
   - Reuses B2 file (if available)
   - Generates new presigned URL (24-hour validity)
   - Skips duplicate log entry
4. **Benefits**: 
   - Reduces storage costs
   - Faster export response (no upload needed)
   - Preserves export history

**Note**: Checksums are per-slug, so same content for different experiments are separate.

## B2 Storage Integration

If Backblaze B2 is configured, exports are stored in B2:

- **Standalone Exports**: `exports/{slug}_intelligent_export_{timestamp}.zip`
- **Runtime ZIPs**: `{slug}/runtime-{timestamp}.zip`
- **Presigned URLs**: 24-hour validity for downloads
- **Deduplication**: Matching checksums reuse existing files

### B2 Configuration

Required environment variables:
- `B2_ENDPOINT_URL`
- `B2_BUCKET_NAME`
- `B2_ACCESS_KEY_ID`
- `B2_SECRET_ACCESS_KEY`

If B2 not configured, exports stored locally in `temp_exports/` directory.

## File Size Limits

- **Collection Limits**: 10,000 documents per collection (prevents accidental large exports)
- **Export Size Estimation**: System estimates export size before creation
- **Disk Streaming**: Large exports (>100MB) use disk streaming for memory efficiency
- **Storage Limits**: Subject to B2 bucket limits or local disk space

## Recommendations Summary

### For Iterative Development

✅ **DO**: Use upload-ready export (`/api/package-upload-ready/{slug_id}`)
- Clean ZIP with only experiment files
- Ready to edit and upload directly
- No platform file extraction needed

❌ **DON'T**: Upload standalone export directly
- Contains platform files that shouldn't be in experiment directory
- Requires manual extraction

### For Standalone Deployment

✅ **DO**: Use standalone export (`/api/package-standalone/{slug_id}`)
- Complete, self-contained application
- Includes all dependencies and database snapshots
- Ready for independent deployment

❌ **DON'T**: Try to run standalone export code in platform
- Platform files are for standalone use only
- Use upload-ready export for platform development

### General Best Practices

1. **Test Locally First**: Test code changes before upload
2. **Verify ZIP Contents**: Check what's in ZIP before upload
3. **Incremental Changes**: Make small, testable changes
4. **Check Logs**: Review logs after upload for errors
5. **Backup Before Changes**: Export current version before making changes
6. **Use Version Control**: Track experiment code changes in Git
7. **Document Changes**: Note what changed and why

## Related Documentation

- [UPLOAD_COMPATIBILITY.md](./UPLOAD_COMPATIBILITY.md) - Detailed upload compatibility guide
- [NOTES.md](./NOTES.md) - Implementation notes for standalone exports
- [README.md](./README.md) - Platform overview and usage

## Summary

- **Export Process**: Creates ZIP with experiment code, database snapshots, and platform files
- **Import Process**: Extracts ZIP, validates files, registers experiment
- **Best Practice**: Use upload-ready export for iterative development
- **Critical**: Never upload standalone export directly (extract experiment files first)
- **Verification**: Always check ZIP contents before upload

Understanding the export/import process and following best practices prevents issues and enables smooth iterative development workflows.
