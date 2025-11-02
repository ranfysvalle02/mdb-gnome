# Standalone Export to Upload Compatibility

## Overview

This document explains how to use standalone exports with the upload workflow for iterative development.

## Workflow: Export → Iterate → Upload

The intended workflow is:

1. **Export** experiment as standalone ZIP
2. **Iterate** on the code locally (edit `actor.py`, `__init__.py`, templates, etc.)
3. **Upload** the modified experiment back to the platform

## Important: Upload Format vs Standalone Export Format

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

The upload endpoint expects a ZIP with:
```
upload.zip
├── manifest.json        # REQUIRED: Must be in root or in experiments/{slug}/
├── actor.py             # REQUIRED: Must be in root or in experiments/{slug}/
├── __init__.py          # REQUIRED: Must be in root or in experiments/{slug}/
├── requirements.txt     # OPTIONAL: Experiment-specific dependencies only
├── templates/           # OPTIONAL: If present
└── static/              # OPTIONAL: If present
```

**Critical**: The upload process extracts ALL files from the ZIP. Platform files from standalone export should NOT be uploaded.

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

1. **Validates** ZIP contains required files:
   - `manifest.json`
   - `actor.py`
   - `__init__.py`

2. **Extracts** all files to `experiments/{slug}/`

3. **Handles nested structures**:
   - If files are in `experiments/{slug}/experiments/{slug}/`, flattens them
   - Looks for directories with both `__init__.py` and `actor.py`

4. **Filters files**:
   - Currently does NOT filter platform files
   - **Issue**: Platform files from standalone export will be extracted to experiment directory

5. **Registers experiment**:
   - Updates database configuration
   - Registers routes
   - Starts Ray actors
   - Reloads experiment

## Current Limitations

### Platform Files in Standalone Export

**Problem**: Standalone export includes platform files that shouldn't be in experiment directory:
- `main.py`, `standalone_main.py` → Should not be in experiment
- `db_config.json`, `db_collections.json` → Should not be in experiment
- `async_mongo_wrapper.py`, etc. → Should not be in experiment

**Impact**: If standalone export ZIP is uploaded directly:
- Platform files will be extracted to `experiments/{slug}/`
- These files will be ignored by the platform (not used)
- They won't cause errors but are unnecessary

**Solution**: 
1. Use upload-ready export instead (recommended)
2. Or extract and re-zip only experiment files (manual)

### Recommendation

**For iterative development workflow**:
1. Use `/api/package-upload-ready/{slug_id}` for export
2. This gives you a clean ZIP with only experiment files
3. Edit files and upload directly

**For standalone deployment**:
1. Use `/api/package-standalone/{slug_id}` for export
2. This gives you a complete standalone application
3. Extract experiment files if you need to upload them back

## Future Improvements

Potential enhancements:

1. **Filter platform files in upload**:
   - Upload process could ignore known platform files
   - Whitelist approach: only extract known experiment file types

2. **Smart ZIP detection**:
   - Detect standalone export format
   - Automatically extract only experiment files

3. **Unified export format**:
   - Create export that works for both standalone AND upload
   - Include flag to indicate export type

4. **Extract helper script**:
   - Provide script to extract experiment files from standalone export
   - Automatically create upload-ready ZIP

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

✅ **Works**: Upload-ready export → Edit → Upload  
✅ **Works**: Standalone export → Extract experiment files → Re-zip → Upload  
⚠️ **Not Recommended**: Standalone export → Upload directly (includes platform files)

**Best Practice**: Use upload-ready export for iterative development workflow.

