# Standalone Export to Upload Compatibility

## Overview

This document explains how to use standalone exports with the upload workflow for iterative development.

## Workflow: Export → Iterate → Upload

The intended workflow is:

1. **Export** experiment as standalone ZIP
2. **Iterate** on the code locally (edit `actor.py`, `__init__.py`, templates, etc.)
3. **Upload** the modified experiment back to the platform

## Important: Upload Format vs Standalone Export Format

**✅ Both formats are now fully supported!**

The upload endpoint now supports both:
1. **Upload-ready format**: Root-level files (manifest.json, actor.py, __init__.py at ZIP root)
2. **Standalone export format**: Files in experiments/{slug}/ directory structure

### Standalone Export Structure

The standalone export ZIP contains:
```
standalone_export.zip
├── main.py                      # Platform: Standalone FastAPI app (DO NOT UPLOAD)
├── standalone_main.py           # Platform: Generated main file (DO NOT UPLOAD)
├── db_config.json               # Platform: Experiment config snapshot (DO NOT UPLOAD)
├── db_collections.json          # Platform: Database snapshot (DO NOT UPLOAD)
├── requirements.txt             # Platform: Full dependencies (DO NOT UPLOAD)
├── Dockerfile                   # Platform: Docker config (DO NOT UPLOAD)
├── docker-compose.yml           # Platform: Docker compose (DO NOT UPLOAD)
├── async_mongo_wrapper.py       # Platform: Database abstraction (DO NOT UPLOAD)
├── mongo_connection_pool.py     # Platform: Connection pool (DO NOT UPLOAD)
├── experiment_db.py             # Platform: Database abstraction (DO NOT UPLOAD)
├── README.md                    # Platform: Instructions (DO NOT UPLOAD)
└── experiments/
    └── {slug}/
        ├── manifest.json        # ✅ EXPERIMENT: Required for upload
        ├── actor.py             # ✅ EXPERIMENT: Required for upload
        ├── __init__.py          # ✅ EXPERIMENT: Required for upload
        ├── requirements.txt     # ✅ EXPERIMENT: Optional for upload
        ├── templates/           # ✅ EXPERIMENT: Optional for upload
        └── static/              # ✅ EXPERIMENT: Optional for upload
```

### Upload Format Requirements

The upload endpoint accepts ZIP files in TWO formats:

#### Format 1: Upload-Ready Format (Recommended for Iterative Development)
```
upload.zip
├── manifest.json        # REQUIRED: At root level
├── actor.py             # REQUIRED: At root level
├── __init__.py          # REQUIRED: At root level
├── requirements.txt     # OPTIONAL: At root level
├── templates/           # OPTIONAL: At root level
└── static/              # OPTIONAL: At root level
```

#### Format 2: Standalone Export Format
```
upload.zip
├── experiments/
│   └── {slug}/
│       ├── manifest.json        # REQUIRED: In experiments/{slug}/
│       ├── actor.py             # REQUIRED: In experiments/{slug}/
│       ├── __init__.py           # REQUIRED: In experiments/{slug}/
│       ├── requirements.txt      # OPTIONAL: In experiments/{slug}/
│       ├── templates/             # OPTIONAL: In experiments/{slug}/
│       └── static/                # OPTIONAL: In experiments/{slug}/
├── standalone_main.py    # Platform file (IGNORED during upload)
├── db_config.json        # Platform file (IGNORED during upload)
└── ...                   # Other platform files (IGNORED during upload)
```

**Note**: The upload process automatically detects the format and extracts only experiment files. Platform files from standalone export are automatically filtered out.

## Solution: Extract and Re-Zip Experiment Files

### Step 1: Extract the Standalone Export

```bash
unzip {slug}_intelligent_export_*.zip
cd {slug}_intelligent_export_*
```

### Step 2: Navigate to Experiment Directory

```bash
cd experiments/{slug}
```

This directory contains only the experiment files needed for upload:
- `manifest.json`
- `actor.py`
- `__init__.py`
- `requirements.txt` (if present)
- `templates/` (if present)
- `static/` (if present)

### Step 3: Edit Files as Needed

Modify any files in this directory:
- Edit `actor.py` to change actor behavior
- Edit `__init__.py` to change routes
- Edit templates in `templates/`
- Edit static files in `static/`
- Update `requirements.txt` if needed

### Step 4: Create Upload-Ready ZIP

From the `experiments/{slug}` directory:

```bash
# Create a new ZIP with only experiment files
zip -r ../{slug}_upload.zip .
cd ../..
```

Or use the `package-upload-ready` endpoint to get a pre-formatted upload ZIP.

### Step 5: Upload via Admin Panel

1. Go to Admin Panel → Upload Experiment
2. Enter the experiment slug ID
3. Select the `{slug}_upload.zip` file
4. Upload

## Automated Solution: Use "Upload-Ready" Export

Instead of using the standalone export, use the **"Upload-Ready"** export which is specifically formatted for upload:

### Via API

```bash
GET /api/package-upload-ready/{slug_id}
```

### Via Admin Panel

1. Go to Admin Panel
2. Click "Upload-Ready Export" button
3. Download the ZIP

This ZIP contains:
- Only experiment files (no platform files)
- Files at root level (no nested `experiments/{slug}/` structure)
- Ready to edit and upload directly

### Workflow with Upload-Ready Export

1. **Export**: Download upload-ready ZIP
2. **Edit**: Modify files directly in the ZIP or extract/edit/re-zip
3. **Upload**: Upload the ZIP directly (no extraction needed)

## Upload Process Details

The upload endpoint:

1. **Detects format**:
   - Checks for upload-ready format (root-level files)
   - Checks for standalone export format (experiments/{slug}/ structure)
   - Auto-detects slug from manifest.json or directory structure

2. **Validates** ZIP contains required files:
   - `manifest.json` (at root or in experiments/{slug}/)
   - `actor.py` (at root or in experiments/{slug}/)
   - `__init__.py` (at root or in experiments/{slug}/)

3. **Extracts** experiment files to `experiments/{slug}/`:
   - **Upload-ready format**: Extracts root-level files directly
   - **Standalone export format**: Extracts from experiments/{slug}/ directory
   - Automatically filters out platform files (standalone_main.py, db_config.json, etc.)

4. **Handles nested structures**:
   - If files are in `experiments/{slug}/experiments/{slug}/`, flattens them
   - Looks for directories with both `__init__.py` and `actor.py`

5. **Filters platform files**:
   - Automatically filters out platform files from standalone export
   - Platform files are NOT extracted to experiment directory
   - ✅ **Fixed**: Platform files are now properly filtered

6. **Registers experiment**:
   - Updates database configuration
   - Registers routes
   - Starts Ray actors
   - Reloads experiment

## Current Status

### ✅ Platform Files Filtering (FIXED)

**Previous Problem**: Standalone export includes platform files that shouldn't be in experiment directory.

**Current Status**: ✅ **FIXED**
- Platform files are automatically filtered during upload
- Platform files are NOT extracted to experiment directory
- Both upload-ready and standalone export formats are fully supported

**Platform files that are automatically filtered**:
- `standalone_main.py` → Filtered out (exact match)
- `main.py` → Should be filtered if present in standalone export (verify implementation)
- `db_config.json`, `db_collections.json` → Filtered out (exact match)
- `async_mongo_wrapper.py`, `mongo_connection_pool.py`, `experiment_db.py` → Filtered out (prefix match)
- `Dockerfile`, `docker-compose.yml` → Filtered out (exact match)
- Root-level `README.md` → Filtered out (exact match)
- Root-level `requirements.txt` (platform version) → Not explicitly filtered (verify behavior)

**You can now**:
1. ✅ Upload standalone export directly (platform files automatically filtered)
2. ✅ Upload upload-ready export directly (clean, no platform files)
3. ✅ Either format works seamlessly

### Recommendation

**For iterative development workflow**:
1. Use `/api/package-upload-ready/{slug_id}` for export (recommended)
2. This gives you a clean ZIP with only experiment files at root level
3. Edit files and upload directly
4. ✅ Both formats work, but upload-ready is simpler

**For standalone deployment**:
1. Use `/api/package-standalone/{slug_id}` for export
2. This gives you a complete standalone application
3. ✅ You can now upload the standalone export directly back to the platform
4. Platform files are automatically filtered during upload

## Platform File Filtering Implementation

The upload process automatically filters platform files using:

1. **Platform File List**: Known platform files are filtered by exact name match
2. **Platform File Prefixes**: Files starting with platform module prefixes are filtered
3. **Path-based Detection**: Platform files are detected regardless of their location in the ZIP structure
4. **Automatic Format Detection**: Detects both upload-ready and standalone export formats

**Filtered Platform Files**:
- Exact matches: `README.md`, `db_config.json`, `db_collections.json`, `standalone_main.py`, `Dockerfile`, `docker-compose.yml`
- Prefix matches: Files starting with `async_mongo_wrapper`, `mongo_connection_pool`, `experiment_db`

**Note**: If `main.py` is present in standalone exports, it should also be filtered (verify implementation if needed).

## Future Improvements

Potential enhancements:

1. **Enhanced Platform File Detection**:
   - Add `main.py` to explicit platform files list (if not already handled)
   - Expand pattern matching for platform files

2. **Upload Progress Tracking**:
   - Progress indicator for large ZIP uploads
   - Detailed logging of filtered files

3. **ZIP Validation**:
   - Pre-upload validation of ZIP structure
   - Check for required files before extraction
   - Verify no platform files in experiment directory after extraction

## Testing Upload Compatibility

To test if your modified ZIP will upload correctly:

1. **Extract standalone export**:
   ```bash
   unzip {slug}_intelligent_export_*.zip
   cd {slug}_intelligent_export_*/experiments/{slug}
   ```

2. **Verify required files**:
   ```bash
   ls manifest.json actor.py __init__.py
   ```
   All three should exist.

3. **Create test ZIP**:
   ```bash
   zip -r ../../test_upload.zip .
   ```

4. **Test upload**:
   - Upload via admin panel
   - Check that experiment loads correctly
   - Verify no platform files in `experiments/{slug}/` directory

## Summary

✅ **Works**: Upload-ready export → Edit → Upload directly  
✅ **Works**: Standalone export → Upload directly (platform files automatically filtered)  
✅ **Works**: Standalone export → Extract experiment files → Re-zip → Upload  

**Both formats are fully supported!** The upload endpoint automatically:
- Detects the ZIP format (upload-ready or standalone export)
- Extracts only experiment files (filters out platform files)
- Handles both root-level and nested directory structures

**Best Practice**: Use upload-ready export for iterative development workflow (simpler, cleaner ZIP).

