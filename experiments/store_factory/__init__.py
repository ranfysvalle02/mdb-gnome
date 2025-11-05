"""
Store Factory Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette import status
from typing import Optional, Dict, Any, List
from pathlib import Path
import json
import datetime

from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()


async def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the Store Factory actor handle."""
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("Ray is globally unavailable, blocking actor handle request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ray service is unavailable. Check Ray cluster status."
        )
    
    slug_id = getattr(request.state, "slug_id", None)
    if not slug_id:
        logger.error("Server error: slug_id not found in request state.")
        raise HTTPException(500, "Server error: slug_id not found in request state.")
    
    actor_name = f"{slug_id}-actor"
    
    try:
        handle = ray.get_actor(actor_name, namespace="modular_labs")
        return handle
    except ValueError:
        logger.error(f"CRITICAL: Actor '{actor_name}' found no process running.")
        raise HTTPException(503, f"Experiment service '{actor_name}' is not running.")
    except Exception as e:
        logger.error(f"Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")


async def get_experiment_user_from_session(request: Request) -> Optional[Dict[str, Any]]:
    """
    Helper dependency to get experiment user from sub-auth session.
    
    StoreFactory uses experiment_users strategy (sub-auth only, no platform linking).
    This means users must log in directly within StoreFactory to access admin features.
    
    Usage in routes:
        @bp.get("/admin/dashboard")
        async def admin_dashboard(
            user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
        ):
            if not user:
                return RedirectResponse(url="/admin/login")
            # User is authenticated via sub-auth
    
    Returns:
        None if not authenticated
        Dict with user info (email, _id, etc.) if authenticated via sub-auth
    """
    try:
        from sub_auth import get_experiment_sub_user
        from core_deps import get_experiment_config, get_scoped_db
        
        slug_id = getattr(request.state, "slug_id", "store_factory")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        # Check if sub-auth is enabled
        sub_auth = config.get("sub_auth", {}) if config else {}
        if not sub_auth.get("enabled", False):
            return None
        
        db = await get_scoped_db(request)
        user = await get_experiment_sub_user(request, slug_id, db, config)
        return user
    except Exception as e:
        logger.error(f"Error getting experiment user from session: {e}", exc_info=True)
        return None


# --- Root Routes ---
@bp.get("/", response_class=HTMLResponse)
async def home(request: Request, actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)):
    """Default to business selection page."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_business_selection.remote(context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_business_selection: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/create", response_class=HTMLResponse)
async def create_store_flow(request: Request, actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)):
    """Business type selection page for creating a new store."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_business_selection.remote(context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_business_selection: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/select/{business_type}", response_class=HTMLResponse)
async def select_business(
    request: Request,
    business_type: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    error: Optional[str] = Query(None)
):
    """Show store selection/creation page for a business type."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_selection.remote(business_type, context, error)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_selection: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/create/{business_type}")
async def create_store_post(
    request: Request,
    business_type: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    name: str = Form(...),
    slug_id: str = Form(...),
    about_text: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    phone_display: Optional[str] = Form(None),
    hours: Optional[str] = Form(None),
    logo_url: Optional[str] = Form(None),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle store creation - generates zip download instead of creating in DB."""
    form_data = {
        "name": name,
        "slug_id": slug_id,
        "about_text": about_text,
        "address": address,
        "phone": phone,
        "phone_display": phone_display,
        "hours": hours,
        "logo_url": logo_url,
        "email": email,
        "password": password
    }
    
    try:
        # Get export data from form (no source store - create new)
        result = await actor.create_export_data_from_form.remote(business_type, form_data, source_store_slug=None)
        if not result.get("success"):
            return RedirectResponse(
                url=f"/experiments/store_factory/select/{business_type}?error={result.get('error', 'Unknown error')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
        
        # Generate zip download using the same logic as download_store
        return await _generate_store_zip(request, result.get("data", {}), actor)
    except Exception as e:
        logger.error(f"Actor call failed for create_store: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to create store: {e}")


@bp.post("/clone")
async def clone_store_post(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    source_store: str = Form(...),
    business_type: str = Form(...),
    name: str = Form(...),
    slug_id: str = Form(...),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle store cloning - generates zip download instead of creating in DB."""
    form_data = {
        "name": name,
        "slug_id": slug_id,
        "email": email,
        "password": password
    }
    
    try:
        # Get export data from form (cloning from source store)
        result = await actor.create_export_data_from_form.remote(business_type, form_data, source_store_slug=source_store)
        if not result.get("success"):
            return RedirectResponse(
                url=f"/experiments/store_factory/select/{business_type}?error={result.get('error', 'Unknown error')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
        
        # Generate zip download using the same logic as download_store
        return await _generate_store_zip(request, result.get("data", {}), actor)
    except Exception as e:
        logger.error(f"Actor call failed for clone_store: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to clone store: {e}")


@bp.get("/stores", response_class=HTMLResponse)
async def list_stores(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    business_type: Optional[str] = Query(None)
):
    """List all stores, optionally filtered by business type."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_list.remote(context, business_type)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_list: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


# --- Store Routes ---
@bp.get("/{store_slug}", response_class=HTMLResponse)
async def store_home(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display the store's homepage."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_home.remote(store_slug, context, user)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_home: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/{store_slug}/item/{item_id}", response_class=HTMLResponse)
async def item_details(
    request: Request,
    store_slug: str,
    item_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display details for a single item."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_item_details.remote(store_slug, item_id, context, user)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_item_details: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/{store_slug}/inquire/{item_id}")
async def submit_inquiry_post(
    request: Request,
    store_slug: str,
    item_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    customer_name: str = Form(...),
    customer_contact: str = Form(...),
    message: Optional[str] = Form(None)
):
    """Handle the submission of a new customer inquiry."""
    form_data = {
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "message": message
    }
    
    try:
        result = await actor.submit_inquiry.remote(store_slug, item_id, form_data)
        if result.get("success"):
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/item/{item_id}",
                status_code=status.HTTP_303_SEE_OTHER
            )
        else:
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/item/{item_id}?error={result.get('error', 'Unknown error')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for submit_inquiry: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to submit inquiry: {e}")


# --- Admin Routes ---
@bp.get("/{store_slug}/admin/login", response_class=HTMLResponse)
async def admin_login_get(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    error: Optional[str] = Query(None),
    email: Optional[str] = Query(None)
):
    """Display admin login page."""
    # Check if already authenticated
    user = await get_experiment_user_from_session(request)
    if user:
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}/admin/dashboard",
            status_code=status.HTTP_303_SEE_OTHER
        )
    
    # Render login template
    try:
        html = await actor.render_admin_login.remote(store_slug, {"url": str(request.url), "path": request.url.path}, error, email)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_admin_login: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/{store_slug}/admin/login")
async def admin_login_post(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle admin login using sub-authentication."""
    try:
        result = await actor.admin_login.remote(store_slug, email, password)
        if result.get("success"):
            # Use sub-authentication to create session
            from sub_auth import create_experiment_session
            from core_deps import get_experiment_config, get_scoped_db
            
            slug_id = getattr(request.state, "slug_id", "store_factory")
            config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
            db = await get_scoped_db(request)
            
            user_id = result.get("user_id")
            response = RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/admin/dashboard",
                status_code=status.HTTP_303_SEE_OTHER
            )
            
            # Create experiment session
            await create_experiment_session(request, slug_id, user_id, config, response)
            return response
        else:
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/admin/login?error={result.get('error', 'Invalid credentials')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for admin_login: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to process login: {e}")


@bp.post("/{store_slug}/admin/logout")
async def admin_logout(
    request: Request,
    store_slug: str
):
    """Handle admin logout - clears sub-auth session."""
    try:
        from sub_auth import get_experiment_sub_user
        from core_deps import get_experiment_config, get_scoped_db
        from fastapi.responses import Response
        
        slug_id = getattr(request.state, "slug_id", "store_factory")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        # Get session cookie name from config
        sub_auth = config.get("sub_auth", {}) if config else {}
        cookie_name = sub_auth.get("session_cookie_name", f"{slug_id}_session")
        
        # Create response that clears the cookie
        response = RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}",
            status_code=status.HTTP_303_SEE_OTHER
        )
        
        # Clear the session cookie
        response.delete_cookie(
            key=cookie_name,
            path="/",
            httponly=True,
            samesite="lax"
        )
        
        return response
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        # Still redirect even if logout fails
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}",
            status_code=status.HTTP_303_SEE_OTHER
        )


@bp.get("/{store_slug}/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display admin dashboard - requires sub-auth session."""
    # Check if user is authenticated via sub-auth
    if not user:
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}/admin/login",
            status_code=status.HTTP_303_SEE_OTHER
        )
    
    # User is authenticated via sub-auth, proceed with dashboard
    context = {"url": str(request.url), "path": request.url.path}
    try:
        store = await actor.get_store_by_slug.remote(store_slug)
        if not store:
            return HTMLResponse(f"<h1>404</h1><p>Store '{store_slug}' not found.</p>", status_code=404)
        
        # Get slideshow images for dashboard
        slideshow_images = await actor.get_slideshow_images.remote(store_slug)
        
        # Get SEO and PWA settings
        seo_pwa_result = await actor.get_seo_pwa_settings.remote(store_slug)
        seo_settings = seo_pwa_result.get("seo_settings", {}) if seo_pwa_result.get("success") else {}
        pwa_settings = seo_pwa_result.get("pwa_settings", {}) if seo_pwa_result.get("success") else {}
        
        # Helper function to escape HTML in values for security
        def escape_html_value(value):
            """Escape HTML special characters for safe display in templates."""
            if value is None:
                return ""
            value = str(value)
            return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;")
        
        # Escape HTML values for safe display
        seo_settings_escaped = {k: escape_html_value(v) for k, v in seo_settings.items()}
        pwa_settings_escaped = {k: escape_html_value(v) for k, v in pwa_settings.items()}
        
        # Simple HTML dashboard with slideshow management
        html = f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Admin Dashboard - {store.get('name', store_slug)}</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
        </head>
        <body class="bg-gray-900 text-white min-h-screen">
            <div class="container mx-auto px-6 py-10">
                <div class="flex justify-between items-center mb-8">
                    <h1 class="text-4xl font-bold">Admin Dashboard - {store.get('name', store_slug)}</h1>
                    <div class="flex items-center space-x-4">
                        <span class="text-gray-400">Logged in as: {user.get('email', 'Admin')}</span>
                        <form action="/experiments/store_factory/{store_slug}/admin/logout" method="POST" class="inline">
                            <button type="submit" class="px-4 py-2 bg-red-600 hover:bg-red-700 text-white font-bold rounded-lg">
                                Logout
                            </button>
                        </form>
                    </div>
                </div>
                
                <!-- Slideshow Management -->
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-8 mb-6">
                    <h2 class="text-2xl font-bold mb-6">
                        <i class="fas fa-images mr-2"></i>
                        Slideshow Management
                    </h2>
                    
                    <div class="mb-6">
                        <h3 class="text-xl font-semibold mb-4">Add New Slide</h3>
                        <form id="slideshow-add-form" class="space-y-4">
                            <div>
                                <label for="slide_image_url" class="block text-sm font-medium mb-2">Image URL</label>
                                <input type="text" id="slide_image_url" name="image_url" required
                                       placeholder="https://example.com/image.jpg"
                                       class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                <p class="text-sm text-gray-400 mt-2">Enter a URL to your image (supports Unsplash, Pexels, etc.)</p>
                            </div>
                            <div>
                                <label for="slide_caption" class="block text-sm font-medium mb-2">Caption (Optional)</label>
                                <input type="text" id="slide_caption" name="caption"
                                       placeholder="Enter caption text"
                                       class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                <p class="text-sm text-gray-400 mt-2">Caption will be displayed on the slide</p>
                            </div>
                            <button type="submit" class="px-6 py-2 bg-green-600 hover:bg-green-700 text-white font-bold rounded-lg">
                                <i class="fas fa-plus mr-2"></i>
                                Add Slide
                            </button>
                            <div id="slideshow-add-message" class="mt-4"></div>
                        </form>
                    </div>
                    
                    <div class="border-t border-gray-700 pt-6 mt-6">
                        <h3 class="text-xl font-semibold mb-4">Current Slides</h3>
                        <div id="slideshow-list" class="space-y-4">
                            <div class="text-gray-400 text-center py-8">Loading slides...</div>
                        </div>
                    </div>
                </div>
                
                <!-- Store Settings -->
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-8 mb-6">
                    <h2 class="text-2xl font-bold mb-6">
                        <i class="fas fa-cog mr-2"></i>
                        Store Settings
                    </h2>
                    
                    <div class="space-y-6">
                        <div>
                            <h3 class="text-xl font-semibold mb-4">Update Logo</h3>
                            <form id="logo-form" class="space-y-4">
                                <div>
                                    <label for="logo_url" class="block text-sm font-medium mb-2">Logo URL</label>
                                    <input type="text" id="logo_url" name="logo_url" 
                                           value="{store.get('logo_url', '/experiments/store_factory/static/img/logo.png')}"
                                           class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                    <p class="text-sm text-gray-400 mt-2">Enter a URL or path to your logo image. Default: /experiments/store_factory/static/img/logo.png</p>
                                </div>
                                <div id="logo-preview" class="mt-4">
                                    <p class="text-sm font-medium mb-2">Preview:</p>
                                    <img src="{store.get('logo_url', '/experiments/store_factory/static/img/logo.png')}" 
                                         alt="Logo Preview" 
                                         class="h-20 w-auto border border-gray-600 rounded">
                                </div>
                                <button type="submit" class="px-6 py-2 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg">
                                    Update Logo
                                </button>
                                <div id="logo-message" class="mt-4"></div>
                            </form>
                        </div>
                    </div>
                </div>
                
                <!-- SEO & PWA Settings -->
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-8 mb-6">
                    <h2 class="text-2xl font-bold mb-6">
                        <i class="fas fa-search mr-2"></i>
                        SEO & PWA Settings
                    </h2>
                    <p class="text-gray-400 mb-6">Manually edit SEO tags and PWA settings. These override AI-generated settings when exporting.</p>
                    
                    <div class="space-y-8">
                        <!-- SEO Settings -->
                        <div>
                            <div class="flex justify-between items-center mb-4">
                                <h3 class="text-xl font-semibold">
                                    <i class="fas fa-tags mr-2"></i>
                                    SEO Settings
                                </h3>
                                <button type="button" id="generate-seo-btn" class="px-4 py-2 bg-purple-600 hover:bg-purple-700 text-white font-bold rounded-lg">
                                    <i class="fas fa-magic mr-2"></i>
                                    Generate SEO Tags
                                </button>
                            </div>
                            <p class="text-sm text-gray-400 mb-4">Click "Generate SEO Tags" to automatically create optimized SEO metadata based on your store information. You can then edit and customize as needed.</p>
                            <form id="seo-form" class="space-y-4">
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <div>
                                        <label for="seo_title" class="block text-sm font-medium mb-2">Page Title (max 60 chars)</label>
                                        <input type="text" id="seo_title" name="seo_title" 
                                               value="{seo_settings_escaped.get('title', '')}"
                                               maxlength="60"
                                               class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        <p class="text-sm text-gray-400 mt-1">Appears in browser tabs and search results</p>
                                    </div>
                                    <div>
                                        <label for="seo_keywords" class="block text-sm font-medium mb-2">Keywords (comma-separated)</label>
                                        <input type="text" id="seo_keywords" name="seo_keywords" 
                                               value="{seo_settings_escaped.get('keywords', '')}"
                                               class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        <p class="text-sm text-gray-400 mt-1">Relevant keywords for search engines</p>
                                    </div>
                                </div>
                                <div>
                                    <label for="seo_description" class="block text-sm font-medium mb-2">Meta Description (max 160 chars)</label>
                                    <textarea id="seo_description" name="seo_description" 
                                              maxlength="160"
                                              rows="3"
                                              class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">{seo_settings_escaped.get('description', '')}</textarea>
                                    <p class="text-sm text-gray-400 mt-1">Appears in search engine results. <span id="seo_desc_count">0</span>/160 characters</p>
                                </div>
                                <div class="border-t border-gray-700 pt-4">
                                    <h4 class="text-lg font-semibold mb-3">Open Graph (Facebook/LinkedIn)</h4>
                                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                        <div>
                                            <label for="og_title" class="block text-sm font-medium mb-2">OG Title (max 60 chars)</label>
                                            <input type="text" id="og_title" name="og_title" 
                                                   value="{seo_settings_escaped.get('og_title', '')}"
                                                   maxlength="60"
                                                   class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        </div>
                                        <div>
                                            <label for="og_type" class="block text-sm font-medium mb-2">OG Type</label>
                                            <select id="og_type" name="og_type" 
                                                    class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                                <option value="website" {'selected' if seo_settings.get('og_type', 'website') == 'website' else ''}>Website</option>
                                                <option value="business.business" {'selected' if seo_settings.get('og_type') == 'business.business' else ''}>Business</option>
                                                <option value="product" {'selected' if seo_settings.get('og_type') == 'product' else ''}>Product</option>
                                            </select>
                                        </div>
                                    </div>
                                    <div class="mt-4">
                                        <label for="og_description" class="block text-sm font-medium mb-2">OG Description (max 160 chars)</label>
                                        <textarea id="og_description" name="og_description" 
                                                  maxlength="160"
                                                  rows="2"
                                                  class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">{seo_settings_escaped.get('og_description', '')}</textarea>
                                    </div>
                                </div>
                                <div class="border-t border-gray-700 pt-4">
                                    <h4 class="text-lg font-semibold mb-3">Twitter Card</h4>
                                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                        <div>
                                            <label for="twitter_card" class="block text-sm font-medium mb-2">Twitter Card Type</label>
                                            <select id="twitter_card" name="twitter_card" 
                                                    class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                                <option value="summary" {'selected' if seo_settings.get('twitter_card', 'summary_large_image') == 'summary' else ''}>Summary</option>
                                                <option value="summary_large_image" {'selected' if seo_settings.get('twitter_card', 'summary_large_image') == 'summary_large_image' else ''}>Summary Large Image</option>
                                            </select>
                                        </div>
                                        <div>
                                            <label for="twitter_title" class="block text-sm font-medium mb-2">Twitter Title (max 70 chars)</label>
                                            <input type="text" id="twitter_title" name="twitter_title" 
                                                   value="{seo_settings_escaped.get('twitter_title', '')}"
                                                   maxlength="70"
                                                   class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        </div>
                                    </div>
                                    <div class="mt-4">
                                        <label for="twitter_description" class="block text-sm font-medium mb-2">Twitter Description (max 200 chars)</label>
                                        <textarea id="twitter_description" name="twitter_description" 
                                                  maxlength="200"
                                                  rows="2"
                                                  class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">{seo_settings_escaped.get('twitter_description', '')}</textarea>
                                    </div>
                                </div>
                                <button type="submit" class="px-6 py-2 bg-green-600 hover:bg-green-700 text-white font-bold rounded-lg">
                                    <i class="fas fa-save mr-2"></i>
                                    Save SEO Settings
                                </button>
                                <div id="seo-message" class="mt-4"></div>
                            </form>
                        </div>
                        
                        <!-- PWA Settings -->
                        <div class="border-t border-gray-700 pt-6">
                            <h3 class="text-xl font-semibold mb-4">
                                <i class="fas fa-mobile-alt mr-2"></i>
                                PWA Settings
                            </h3>
                            <form id="pwa-form" class="space-y-4">
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <div>
                                        <label for="pwa_name" class="block text-sm font-medium mb-2">App Name</label>
                                        <input type="text" id="pwa_name" name="pwa_name" 
                                               value="{escape_html_value(pwa_settings.get('name', store.get('name', '')))}"
                                               class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        <p class="text-sm text-gray-400 mt-1">Full name when installed as PWA</p>
                                    </div>
                                    <div>
                                        <label for="pwa_short_name" class="block text-sm font-medium mb-2">Short Name (max 12 chars)</label>
                                        <input type="text" id="pwa_short_name" name="pwa_short_name" 
                                               value="{escape_html_value(pwa_settings.get('short_name', store.get('name', '')[:12]))}"
                                               maxlength="12"
                                               class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        <p class="text-sm text-gray-400 mt-1">Short name for app icon</p>
                                    </div>
                                </div>
                                <div>
                                    <label for="pwa_description" class="block text-sm font-medium mb-2">PWA Description</label>
                                    <textarea id="pwa_description" name="pwa_description" 
                                              rows="2"
                                              class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">{pwa_settings_escaped.get('description', '')}</textarea>
                                    <p class="text-sm text-gray-400 mt-1">Description for the Progressive Web App</p>
                                </div>
                                <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                                    <div>
                                        <label for="pwa_display" class="block text-sm font-medium mb-2">Display Mode</label>
                                        <select id="pwa_display" name="pwa_display" 
                                                class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                            <option value="standalone" {'selected' if pwa_settings.get('display', 'standalone') == 'standalone' else ''}>Standalone</option>
                                            <option value="fullscreen" {'selected' if pwa_settings.get('display') == 'fullscreen' else ''}>Fullscreen</option>
                                            <option value="minimal-ui" {'selected' if pwa_settings.get('display') == 'minimal-ui' else ''}>Minimal UI</option>
                                            <option value="browser" {'selected' if pwa_settings.get('display') == 'browser' else ''}>Browser</option>
                                        </select>
                                    </div>
                                    <div>
                                        <label for="pwa_orientation" class="block text-sm font-medium mb-2">Orientation</label>
                                        <select id="pwa_orientation" name="pwa_orientation" 
                                                class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                            <option value="portrait-primary" {'selected' if pwa_settings.get('orientation', 'portrait-primary') == 'portrait-primary' else ''}>Portrait</option>
                                            <option value="landscape-primary" {'selected' if pwa_settings.get('orientation') == 'landscape-primary' else ''}>Landscape</option>
                                            <option value="any" {'selected' if pwa_settings.get('orientation') == 'any' else ''}>Any</option>
                                        </select>
                                    </div>
                                    <div>
                                        <label for="pwa_theme_color" class="block text-sm font-medium mb-2">Theme Color</label>
                                        <input type="color" id="pwa_theme_color" name="pwa_theme_color" 
                                               value="{pwa_settings.get('theme_color', store.get('theme_primary', '#3b82f6'))}"
                                               class="w-full h-10 px-2 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                                    </div>
                                </div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <div>
                                        <label for="pwa_background_color" class="block text-sm font-medium mb-2">Background Color</label>
                                        <input type="color" id="pwa_background_color" name="pwa_background_color" 
                                               value="{pwa_settings.get('background_color', '#111827')}"
                                               class="w-full h-10 px-2 py-2 bg-gray-700 border border-gray-600 rounded-lg">
                                    </div>
                                    <div>
                                        <label for="pwa_start_url" class="block text-sm font-medium mb-2">Start URL</label>
                                        <input type="text" id="pwa_start_url" name="pwa_start_url" 
                                               value="{escape_html_value(pwa_settings.get('start_url', f'/{store_slug}'))}"
                                               class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                        <p class="text-sm text-gray-400 mt-1">URL when app opens</p>
                                    </div>
                                </div>
                                <button type="submit" class="px-6 py-2 bg-green-600 hover:bg-green-700 text-white font-bold rounded-lg">
                                    <i class="fas fa-save mr-2"></i>
                                    Save PWA Settings
                                </button>
                                <div id="pwa-message" class="mt-4"></div>
                            </form>
                        </div>
                    </div>
                </div>
                
                <div class="mt-6">
                    <a href="/experiments/store_factory/{store_slug}" class="text-blue-400 hover:underline">
                        <i class="fas fa-arrow-left mr-2"></i>
                        Back to Store
                    </a>
                </div>
            </div>
            
            <script>
                // Logo management
                document.getElementById('logo_url').addEventListener('input', function(e) {{
                    document.getElementById('logo-preview').querySelector('img').src = e.target.value;
                }});
                
                document.getElementById('logo-form').addEventListener('submit', async function(e) {{
                    e.preventDefault();
                    const logoUrl = document.getElementById('logo_url').value;
                    const messageDiv = document.getElementById('logo-message');
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/update-logo', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                            }},
                            body: 'logo_url=' + encodeURIComponent(logoUrl)
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success) {{
                            messageDiv.innerHTML = '<p class="text-green-400">' + result.message + '</p>';
                        }} else {{
                            messageDiv.innerHTML = '<p class="text-red-400">Error: ' + (result.error || 'Unknown error') + '</p>';
                        }}
                    }} catch (error) {{
                        messageDiv.innerHTML = '<p class="text-red-400">Error: ' + error.message + '</p>';
                    }}
                }});
                
                // Slideshow management
                let slideshowImages = [];
                
                async function loadSlideshow() {{
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/slideshow');
                        const result = await response.json();
                        if (result.success) {{
                            slideshowImages = result.images || [];
                            renderSlideshowList();
                        }}
                    }} catch (error) {{
                        console.error('Error loading slideshow:', error);
                        document.getElementById('slideshow-list').innerHTML = '<div class="text-red-400">Error loading slideshow images.</div>';
                    }}
                }}
                
                function renderSlideshowList() {{
                    const listDiv = document.getElementById('slideshow-list');
                    if (slideshowImages.length === 0) {{
                        listDiv.innerHTML = '<div class="text-gray-400 text-center py-8">No slideshow images yet. Add your first slide above!</div>';
                        return;
                    }}
                    
                    let html = '<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" id="slideshow-sortable">';
                    slideshowImages.forEach((img, index) => {{
                        const imgId = img._id?.$oid || img._id || '';
                        html += `
                            <div class="bg-gray-700 border border-gray-600 rounded-lg p-4 relative" data-id="${{imgId}}">
                                <div class="flex items-center justify-between mb-2">
                                    <span class="text-sm font-semibold text-gray-300">Slide ${{index + 1}}</span>
                                    <div class="flex gap-2">
                                        <button onclick="moveSlide(${{index}}, 'up')" ${{index === 0 ? 'disabled' : ''}} class="text-gray-400 hover:text-white ${{index === 0 ? 'opacity-50 cursor-not-allowed' : ''}}">
                                            <i class="fas fa-arrow-up"></i>
                                        </button>
                                        <button onclick="moveSlide(${{index}}, 'down')" ${{index === slideshowImages.length - 1 ? 'disabled' : ''}} class="text-gray-400 hover:text-white ${{index === slideshowImages.length - 1 ? 'opacity-50 cursor-not-allowed' : ''}}">
                                            <i class="fas fa-arrow-down"></i>
                                        </button>
                                        <button onclick="deleteSlide('${{imgId}}')" class="text-red-400 hover:text-red-300">
                                            <i class="fas fa-trash"></i>
                                        </button>
                                    </div>
                                </div>
                                <img src="${{img.image_url}}" alt="Slide" class="w-full h-32 object-cover rounded mb-2">
                                <p class="text-sm text-gray-400 truncate">${{(img.caption || 'No caption').replace(/'/g, "\\'")}}</p>
                            </div>
                        `;
                    }});
                    html += '</div>';
                    listDiv.innerHTML = html;
                }}
                
                async function moveSlide(index, direction) {{
                    if ((direction === 'up' && index === 0) || (direction === 'down' && index === slideshowImages.length - 1)) {{
                        return;
                    }}
                    
                    const newIndex = direction === 'up' ? index - 1 : index + 1;
                    const temp = slideshowImages[index];
                    slideshowImages[index] = slideshowImages[newIndex];
                    slideshowImages[newIndex] = temp;
                    
                    const imageOrders = slideshowImages.map(img => ({{
                        image_id: img._id?.$oid || img._id || ''
                    }}));
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/slideshow/update-order', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }},
                            body: JSON.stringify(imageOrders)
                        }});
                        
                        const result = await response.json();
                        if (result.success) {{
                            renderSlideshowList();
                        }} else {{
                            alert('Error: ' + (result.error || 'Unknown error'));
                            loadSlideshow(); // Reload on error
                        }}
                    }} catch (error) {{
                        alert('Error: ' + error.message);
                        loadSlideshow(); // Reload on error
                    }}
                }}
                
                async function deleteSlide(imageId) {{
                    if (!confirm('Are you sure you want to delete this slide?')) {{
                        return;
                    }}
                    
                    try {{
                        const response = await fetch(`/experiments/store_factory/{store_slug}/admin/slideshow/${{imageId}}/delete`, {{
                            method: 'POST'
                        }});
                        
                        const result = await response.json();
                        if (result.success) {{
                            loadSlideshow();
                        }} else {{
                            alert('Error: ' + (result.error || 'Unknown error'));
                        }}
                    }} catch (error) {{
                        alert('Error: ' + error.message);
                    }}
                }}
                
                document.getElementById('slideshow-add-form').addEventListener('submit', async function(e) {{
                    e.preventDefault();
                    const imageUrl = document.getElementById('slide_image_url').value;
                    const caption = document.getElementById('slide_caption').value;
                    const messageDiv = document.getElementById('slideshow-add-message');
                    
                    try {{
                        const formData = new URLSearchParams();
                        formData.append('image_url', imageUrl);
                        if (caption) {{
                            formData.append('caption', caption);
                        }}
                        
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/slideshow/add', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                            }},
                            body: formData
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success) {{
                            messageDiv.innerHTML = '<p class="text-green-400">' + result.message + '</p>';
                            document.getElementById('slideshow-add-form').reset();
                            loadSlideshow();
                        }} else {{
                            messageDiv.innerHTML = '<p class="text-red-400">Error: ' + (result.error || 'Unknown error') + '</p>';
                        }}
                    }} catch (error) {{
                        messageDiv.innerHTML = '<p class="text-red-400">Error: ' + error.message + '</p>';
                    }}
                }});
                
                // Load slideshow on page load
                loadSlideshow();
                
                // SEO form handling
                const seoDescTextarea = document.getElementById('seo_description');
                const seoDescCount = document.getElementById('seo_desc_count');
                if (seoDescTextarea && seoDescCount) {{
                    seoDescTextarea.addEventListener('input', function(e) {{
                        seoDescCount.textContent = e.target.value.length;
                    }});
                    seoDescCount.textContent = seoDescTextarea.value.length;
                }}
                
                // Generate SEO Tags button
                document.getElementById('generate-seo-btn')?.addEventListener('click', async function() {{
                    const btn = this;
                    const originalText = btn.innerHTML;
                    btn.disabled = true;
                    btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Generating...';
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/generate-seo-tags', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }}
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success && result.seo_tags) {{
                            // Populate form fields with generated tags
                            const tags = result.seo_tags;
                            if (tags.title) document.getElementById('seo_title').value = tags.title;
                            if (tags.description) {{
                                document.getElementById('seo_description').value = tags.description;
                                document.getElementById('seo_desc_count').textContent = tags.description.length;
                            }}
                            if (tags.keywords) document.getElementById('seo_keywords').value = tags.keywords;
                            if (tags.og_title) document.getElementById('og_title').value = tags.og_title;
                            if (tags.og_description) document.getElementById('og_description').value = tags.og_description;
                            if (tags.og_type) document.getElementById('og_type').value = tags.og_type;
                            if (tags.twitter_card) document.getElementById('twitter_card').value = tags.twitter_card;
                            if (tags.twitter_title) document.getElementById('twitter_title').value = tags.twitter_title;
                            if (tags.twitter_description) document.getElementById('twitter_description').value = tags.twitter_description;
                            
                            // Show success message
                            const messageDiv = document.getElementById('seo-message');
                            messageDiv.innerHTML = '<p class="text-green-400"><i class="fas fa-check-circle mr-2"></i>SEO tags generated successfully! Review and save when ready.</p>';
                            setTimeout(() => {{
                                messageDiv.innerHTML = '';
                            }}, 5000);
                        }} else {{
                            alert('Error generating SEO tags: ' + (result.error || 'Unknown error'));
                        }}
                    }} catch (error) {{
                        alert('Error generating SEO tags: ' + error.message);
                    }} finally {{
                        btn.disabled = false;
                        btn.innerHTML = originalText;
                    }}
                }});
                
                document.getElementById('seo-form')?.addEventListener('submit', async function(e) {{
                    e.preventDefault();
                    const messageDiv = document.getElementById('seo-message');
                    const formData = {{
                        title: document.getElementById('seo_title').value,
                        description: document.getElementById('seo_description').value,
                        keywords: document.getElementById('seo_keywords').value,
                        og_title: document.getElementById('og_title').value,
                        og_description: document.getElementById('og_description').value,
                        og_type: document.getElementById('og_type').value,
                        twitter_card: document.getElementById('twitter_card').value,
                        twitter_title: document.getElementById('twitter_title').value,
                        twitter_description: document.getElementById('twitter_description').value
                    }};
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/seo-pwa-settings', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }},
                            body: JSON.stringify({{ seo_settings: formData }})
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success) {{
                            messageDiv.innerHTML = '<p class="text-green-400">' + result.message + '</p>';
                        }} else {{
                            messageDiv.innerHTML = '<p class="text-red-400">Error: ' + (result.error || 'Unknown error') + '</p>';
                        }}
                    }} catch (error) {{
                        messageDiv.innerHTML = '<p class="text-red-400">Error: ' + error.message + '</p>';
                    }}
                }});
                
                // PWA form handling
                document.getElementById('pwa-form')?.addEventListener('submit', async function(e) {{
                    e.preventDefault();
                    const messageDiv = document.getElementById('pwa-message');
                    const formData = {{
                        name: document.getElementById('pwa_name').value,
                        short_name: document.getElementById('pwa_short_name').value,
                        description: document.getElementById('pwa_description').value,
                        display: document.getElementById('pwa_display').value,
                        orientation: document.getElementById('pwa_orientation').value,
                        theme_color: document.getElementById('pwa_theme_color').value,
                        background_color: document.getElementById('pwa_background_color').value,
                        start_url: document.getElementById('pwa_start_url').value
                    }};
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/seo-pwa-settings', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }},
                            body: JSON.stringify({{ pwa_settings: formData }})
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success) {{
                            messageDiv.innerHTML = '<p class="text-green-400">' + result.message + '</p>';
                        }} else {{
                            messageDiv.innerHTML = '<p class="text-red-400">Error: ' + (result.error || 'Unknown error') + '</p>';
                        }}
                    }} catch (error) {{
                        messageDiv.innerHTML = '<p class="text-red-400">Error: ' + error.message + '</p>';
                    }}
                }});
            </script>
        </body>
        </html>
        """
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for admin_dashboard: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/{store_slug}/admin/update-logo", response_class=JSONResponse)
async def update_logo_post(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    logo_url: str = Form(...),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Handle logo update."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.update_store_logo.remote(store_slug, logo_url)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_store_logo: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update logo: {e}"}, status_code=500)


# --- Slideshow Management Routes ---

@bp.get("/{store_slug}/admin/slideshow", response_class=HTMLResponse)
async def admin_slideshow(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display admin slideshow management page."""
    # Check if user is authenticated via sub-auth
    if not user:
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}/admin/login",
            status_code=status.HTTP_303_SEE_OTHER
        )
    
    try:
        store = await actor.get_store_by_slug.remote(store_slug)
        if not store:
            return HTMLResponse(f"<h1>404</h1><p>Store '{store_slug}' not found.</p>", status_code=404)
        
        # Load template using Jinja2
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        experiment_dir = Path(__file__).parent
        templates_dir = experiment_dir / "templates"
        templates = Jinja2Templates(directory=str(templates_dir))
        
        # Determine base path for URLs - use experiment prefix for main app context
        base_path = f"/experiments/store_factory/{store_slug}"
        
        context = {
            "request": request,
            "store": store,
            "user": user,
            "base_path": base_path
        }
        return templates.TemplateResponse("admin_slideshow.html", context)
    except Exception as e:
        logger.error(f"Actor call failed for admin_slideshow: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


def convert_objectid_for_json(obj):
    """Convert ObjectId and datetime objects to JSON-serializable types."""
    from bson.objectid import ObjectId
    from datetime import datetime
    
    if isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: convert_objectid_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_objectid_for_json(item) for item in obj]
    return obj

@bp.get("/{store_slug}/admin/slideshow/api", response_class=JSONResponse)
async def get_slideshow_images(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Get all slideshow images for a store (API endpoint)."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        images = await actor.get_slideshow_images.remote(store_slug)
        # Convert ObjectIds to strings for JSON serialization
        images_json = convert_objectid_for_json(images)
        return JSONResponse({"success": True, "images": images_json})
    except Exception as e:
        logger.error(f"Actor call failed for get_slideshow_images: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to get slideshow images: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/add", response_class=JSONResponse)
async def add_slideshow_image(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    image_url: str = Form(...),
    caption: Optional[str] = Form(None),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Add a new slideshow image."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.add_slideshow_image.remote(store_slug, image_url, caption)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for add_slideshow_image: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to add slideshow image: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/{image_id}/delete", response_class=JSONResponse)
async def delete_slideshow_image(
    request: Request,
    store_slug: str,
    image_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Delete a slideshow image."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.delete_slideshow_image.remote(store_slug, image_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for delete_slideshow_image: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to delete slideshow image: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/update-order", response_class=JSONResponse)
async def update_slideshow_order(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Update the order of slideshow images."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        from fastapi import Body
        image_orders = await request.json()
        if not isinstance(image_orders, list):
            return JSONResponse({"success": False, "error": "Invalid request format"}, status_code=400)
        
        result = await actor.update_slideshow_order.remote(store_slug, image_orders)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_slideshow_order: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update slideshow order: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/{image_id}/update", response_class=JSONResponse)
async def update_slideshow_image(
    request: Request,
    store_slug: str,
    image_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    image_url: Optional[str] = Form(None),
    caption: Optional[str] = Form(None),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Update a slideshow image."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.update_slideshow_image.remote(store_slug, image_id, image_url, caption)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_slideshow_image: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update slideshow image: {e}"}, status_code=500)


# --- SEO & PWA Management Routes ---

@bp.get("/{store_slug}/admin/seo-pwa-settings", response_class=JSONResponse)
async def get_seo_pwa_settings(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Get SEO and PWA settings for a store."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.get_seo_pwa_settings.remote(store_slug)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for get_seo_pwa_settings: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to get SEO/PWA settings: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/seo-pwa-settings", response_class=JSONResponse)
async def update_seo_pwa_settings(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Update SEO and PWA settings for a store."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        data = await request.json()
        seo_settings = data.get("seo_settings")
        pwa_settings = data.get("pwa_settings")
        
        result = await actor.update_seo_pwa_settings.remote(store_slug, seo_settings, pwa_settings)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_seo_pwa_settings: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update SEO/PWA settings: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/generate-seo-tags", response_class=JSONResponse)
async def generate_seo_tags(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Generate SEO tags for a store using smart generation."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        # Get store data
        store = await actor.get_store_by_slug.remote(store_slug)
        if not store:
            return JSONResponse({"success": False, "error": "Store not found"}, status_code=404)
        
        # Get items for better SEO generation
        from core_deps import get_scoped_db
        
        items = []
        try:
            db = await get_scoped_db(request)
            if db:
                slug_id = getattr(request.state, "slug_id", "store_factory")
                items_collection = db[f"{slug_id}_items"]
                items_cursor = items_collection.find({"store_id": store["_id"]}).limit(10)
                items = await items_cursor.to_list(length=10)
        except Exception as e:
            logger.warning(f"Could not fetch items for SEO generation: {e}")
        
        # Generate SEO tags using smart generation
        business_type = store.get("business_type", "generic-store")
        seo_tags = await generate_seo_tags_with_openai(store, business_type, items)
        
        # Auto-save generated tags to store
        await actor.update_seo_pwa_settings.remote(store_slug, seo_settings=seo_tags, pwa_settings=None)
        
        return JSONResponse({
            "success": True,
            "message": "SEO tags generated successfully",
            "seo_tags": seo_tags
        })
    except Exception as e:
        logger.error(f"Error generating SEO tags: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Failed to generate SEO tags: {str(e)}"}, status_code=500)


# --- Store Export/Download Routes ---

async def generate_seo_tags_with_openai(store_data: Dict[str, Any], business_type: str, items: List[Dict[str, Any]] = None) -> Dict[str, str]:
    """Generate SEO tags using OpenAI API if available, otherwise use fallback."""
    import os
    import httpx
    import json
    
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    
    if not openai_api_key:
        logger.info("OPENAI_API_KEY not set, using fallback SEO tags")
        return generate_fallback_seo_tags(store_data, business_type, items)
    
    try:
        store_name = store_data.get("name", "Store")
        about_text = store_data.get("about_text", "")
        address = store_data.get("address", "")
        
        # Get business type label
        business_type_labels = {
            "restaurant": "Restaurant",
            "auto-sales": "Auto Sales",
            "auto-services": "Auto Services",
            "other-services": "Services",
            "generic-store": "Store",
            "lawyer-services": "Lawyer Services",
            "cleaning-services": "Cleaning Services"
        }
        business_label = business_type_labels.get(business_type, "Store")
        
        # Build product/item summary for SEO
        items_summary = ""
        if items and len(items) > 0:
            item_names = [item.get("name", "") for item in items[:5] if item.get("name")]
            if item_names:
                items_summary = f"Products/Services: {', '.join(item_names)}"
        
        prompt = f"""Generate comprehensive SEO metadata for a {business_type} business website.

Business Name: {store_name}
Business Type: {business_label}
About: {about_text}
Address: {address}
{items_summary}

Focus on:
- Business type: {business_type} ({business_label})
- Store name and location
- Products/services offered
- Local SEO optimization

Generate SEO tags in JSON format with these keys:
- title: Page title (max 60 characters, include business name and type)
- description: Meta description (max 160 characters, compelling and keyword-rich, include location if available)
- keywords: Comma-separated keywords (10-15 relevant keywords specific to {business_type} business)
- og_title: Open Graph title (max 60 characters)
- og_description: Open Graph description (max 160 characters)
- og_type: Open Graph type (usually "website" or "business.business")
- twitter_card: Twitter card type (usually "summary_large_image")
- twitter_title: Twitter title (max 70 characters)
- twitter_description: Twitter description (max 200 characters)

Make keywords specific to {business_type} business type. Include location if address is provided.

Return ONLY valid JSON, no markdown, no code blocks."""

        headers = {
            "Authorization": f"Bearer {openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "system", "content": "You are an SEO expert. Generate SEO metadata in valid JSON format only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 300,
        }
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            
            # Try to parse JSON from response
            # Remove markdown code blocks if present
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            content = content.strip()
            
            seo_data = json.loads(content)
            logger.info(f"Generated SEO tags using OpenAI for {store_name}")
            return seo_data
            
    except Exception as e:
        logger.warning(f"Failed to generate SEO tags with OpenAI: {e}, using fallback")
        return generate_fallback_seo_tags(store_data, business_type, items)

def generate_fallback_seo_tags(store_data: Dict[str, Any], business_type: str, items: List[Dict[str, Any]] = None) -> Dict[str, str]:
    """Generate fallback SEO tags without OpenAI - optimized for store type with comprehensive defaults per business type."""
    store_name = store_data.get("name", "Store")
    about_text = store_data.get("about_text", "")[:100]  # Truncate
    address = store_data.get("address", "")
    
    # Business type labels for SEO
    business_type_labels = {
        "restaurant": "Restaurant",
        "auto-sales": "Auto Sales",
        "auto-services": "Auto Services",
        "other-services": "Services",
        "generic-store": "Store",
        "lawyer-services": "Lawyer Services",
        "cleaning-services": "Cleaning Services"
    }
    business_label = business_type_labels.get(business_type, "Store")
    
    # Comprehensive default keywords per business type
    business_keywords = {
        "restaurant": "restaurant, food, dining, menu, delivery, takeout, local restaurant, cuisine, dining out, eat, meal, food delivery, restaurant menu, local food, restaurant near me",
        "auto-sales": "cars, vehicles, auto sales, car dealership, used cars, car buying, automotive, vehicle inventory, pre-owned cars, car lot, auto dealer, car sales, vehicles for sale",
        "auto-services": "auto repair, car service, vehicle maintenance, automotive service, car repair, auto shop, mechanic, car maintenance, auto mechanic, vehicle repair, car service center",
        "other-services": "services, professional services, local business, service provider, business services, professional help, service company",
        "generic-store": "store, shop, products, local business, retail, shopping, buy, purchase, online store, local shop, products for sale, retail store",
        "lawyer-services": "lawyer, attorney, legal services, law firm, legal consultation, legal advice, lawyer services, legal help, legal representation, attorney services, legal counsel",
        "cleaning-services": "cleaning services, house cleaning, commercial cleaning, janitorial services, professional cleaning, maid service, deep cleaning, cleaning company, residential cleaning"
    }
    
    # Default titles per business type (will be customized with store name)
    default_title_templates = {
        "restaurant": f"{store_name} - Restaurant & Dining",
        "auto-sales": f"{store_name} - Auto Sales & Dealership",
        "auto-services": f"{store_name} - Auto Repair & Service",
        "other-services": f"{store_name} - Professional Services",
        "generic-store": f"{store_name} - Store & Shopping",
        "lawyer-services": f"{store_name} - Legal Services & Law Firm",
        "cleaning-services": f"{store_name} - Professional Cleaning Services"
    }
    
    # Default descriptions per business type (will be customized with store info)
    # Note: Descriptions are built to stay within 160 chars after adding location
    default_description_templates = {
        "restaurant": f"Visit {store_name} for delicious food and great dining. {about_text[:70] if about_text and len(about_text) > 0 else 'Quality restaurant offering fresh meals.'}",
        "auto-sales": f"{store_name} offers quality vehicles at great prices. {about_text[:70] if about_text and len(about_text) > 0 else 'Browse our inventory of cars today.'}",
        "auto-services": f"Professional auto repair and maintenance at {store_name}. {about_text[:70] if about_text and len(about_text) > 0 else 'Expert mechanics providing quality service.'}",
        "other-services": f"Professional services at {store_name}. {about_text[:100] if about_text and len(about_text) > 0 else 'Quality service provider helping you.'}",
        "generic-store": f"Shop at {store_name} for quality products. {about_text[:100] if about_text and len(about_text) > 0 else 'Browse our selection of products.'}",
        "lawyer-services": f"Legal services and representation at {store_name}. {about_text[:70] if about_text and len(about_text) > 0 else 'Experienced attorneys providing consultation.'}",
        "cleaning-services": f"Professional cleaning services from {store_name}. {about_text[:70] if about_text and len(about_text) > 0 else 'Quality cleaning for residential and commercial.'}"
    }
    
    # Get base keywords
    keywords = business_keywords.get(business_type, "store, shop, local business")
    
    # Add store name and location to keywords
    keywords = f"{store_name}, {keywords}"
    if address:
        location_keywords = address.split(',')[-1].strip() if ',' in address else address.split()[-1] if address else ""
        if location_keywords:
            keywords = f"{keywords}, {location_keywords}"
    
    # Add product/service keywords from items if available
    if items and len(items) > 0:
        item_keywords = []
        for item in items[:5]:
            item_name = item.get("name", "")
            if item_name:
                # Extract key terms from item names
                words = item_name.lower().split()
                item_keywords.extend([w for w in words if len(w) > 3][:2])  # Take meaningful words
        if item_keywords:
            unique_keywords = list(set(item_keywords))[:3]  # Limit to 3 unique keywords
            keywords = f"{keywords}, {', '.join(unique_keywords)}"
    
    # Build title using business-type specific template (max 60 chars)
    title = default_title_templates.get(business_type, f"{store_name} - {business_label}")
    if len(title) > 60:
        # Try to preserve store name
        if len(store_name) <= 45:
            # Shorten the suffix
            suffix_map = {
                "restaurant": " - Restaurant",
                "auto-sales": " - Auto Sales",
                "auto-services": " - Auto Service",
                "other-services": " - Services",
                "generic-store": " - Store",
                "lawyer-services": " - Law Firm",
                "cleaning-services": " - Cleaning"
            }
            suffix = suffix_map.get(business_type, f" - {business_label}")
            title = f"{store_name}{suffix}"[:60]
        else:
            title = store_name[:60]
    
    # Build description using business-type specific template (max 160 chars)
    description = default_description_templates.get(business_type, f"Visit {store_name} for quality {business_type.replace('-', ' ')} products and services.")
    
    # Add location if available
    if address:
        location = address.split(',')[-1].strip() if ',' in address else address
        location_text = f" Located in {location}."
        if len(description) + len(location_text) <= 160:
            description = f"{description}{location_text}"
        else:
            # Truncate description to fit location
            max_desc_len = 160 - len(location_text)
            if max_desc_len > 0:
                description = f"{description[:max_desc_len]}{location_text}"
    
    # Ensure description doesn't exceed 160 chars
    if len(description) > 160:
        description = description[:157] + "..."
    
    # Build OG title (can be same as title or slightly different)
    og_title = title
    if len(og_title) > 60:
        og_title = og_title[:57] + "..."
    
    # Build OG description (can be same as description or slightly expanded)
    og_description = description
    if len(og_description) > 160:
        og_description = og_description[:157] + "..."
    
    # Determine OG type based on business type
    og_type = "business.business" if business_type in ["restaurant", "auto-sales", "auto-services", "lawyer-services", "cleaning-services"] else "website"
    
    return {
        "title": title,
        "description": description,
        "keywords": keywords,
        "og_title": og_title,
        "og_description": og_description,
        "og_type": og_type,
        "twitter_card": "summary_large_image",
        "twitter_title": og_title[:70],
        "twitter_description": og_description[:200]
    }

async def _generate_store_zip(
    request: Request,
    export_data: Dict[str, Any],
    actor: "ray.actor.ActorHandle",
    store_slug: Optional[str] = None
) -> Response:
    """Helper function to generate store zip from export_data."""
    try:
        from pathlib import Path
        import zipfile
        import io
        from config import BASE_DIR
        from export_helpers import make_intelligent_standalone_main_py
        from fastapi.templating import Jinja2Templates
        
        store_name = export_data.get("export_metadata", {}).get("store_name", "store")
        store_slug_clean = export_data.get("export_metadata", {}).get("store_slug", store_slug or "store")
        store_data = export_data.get("store", {})
        business_type = export_data.get("export_metadata", {}).get("business_type", "generic-store")
        
        # Update logo URL for standalone store (remove experiments path)
        if store_data.get("logo_url", "").startswith("/experiments/store_factory/"):
            store_data["logo_url"] = store_data["logo_url"].replace("/experiments/store_factory/", "/")
        elif not store_data.get("logo_url"):
            store_data["logo_url"] = "/static/img/logo.png"
        
        # Get SEO and PWA settings from store (manual settings take priority)
        manual_seo_settings = store_data.get("seo_settings", {})
        manual_pwa_settings = store_data.get("pwa_settings", {})
        
        # If we have a store_slug, try getting from actor method (for existing stores)
        if store_slug:
            seo_pwa_result = await actor.get_seo_pwa_settings.remote(store_slug)
            if seo_pwa_result.get("success"):
                if not manual_seo_settings:
                    manual_seo_settings = seo_pwa_result.get("seo_settings", {})
                if not manual_pwa_settings:
                    manual_pwa_settings = seo_pwa_result.get("pwa_settings", {})
        
        # Generate SEO tags - use manual settings if available, otherwise generate with OpenAI
        if manual_seo_settings and manual_seo_settings.get("title"):
            seo_tags = {
                "title": manual_seo_settings.get("title", ""),
                "description": manual_seo_settings.get("description", ""),
                "keywords": manual_seo_settings.get("keywords", ""),
                "og_title": manual_seo_settings.get("og_title", manual_seo_settings.get("title", "")),
                "og_description": manual_seo_settings.get("og_description", manual_seo_settings.get("description", "")),
                "og_type": manual_seo_settings.get("og_type", "website"),
                "twitter_card": manual_seo_settings.get("twitter_card", "summary_large_image"),
                "twitter_title": manual_seo_settings.get("twitter_title", manual_seo_settings.get("title", "")),
                "twitter_description": manual_seo_settings.get("twitter_description", manual_seo_settings.get("description", ""))
            }
            logger.info(f"Using manual SEO settings for {store_name}")
        else:
            # Generate SEO tags (with OpenAI if available) - include items for better SEO
            items = export_data.get("items", [])
            seo_tags = await generate_seo_tags_with_openai(store_data, business_type, items)
        
        # Get experiment slug (store_factory)
        slug_id = getattr(request.state, "slug_id", "store_factory")
        experiment_path = BASE_DIR / "experiments" / slug_id
        
        # Read the store_factory actor to get business types
        actor_file = experiment_path / "actor.py"
        business_types_code = ""
        if actor_file.is_file():
            with open(actor_file, "r") as f:
                content = f.read()
                # Extract BUSINESS_TYPES dict
                start_idx = content.find("BUSINESS_TYPES = {")
                if start_idx != -1:
                    bracket_count = 0
                    end_idx = start_idx
                    for i, char in enumerate(content[start_idx:], start_idx):
                        if char == "{":
                            bracket_count += 1
                        elif char == "}":
                            bracket_count -= 1
                            if bracket_count == 0:
                                end_idx = i + 1
                                break
                    business_types_code = content[start_idx:end_idx]
        
        # Call the implementation with all required parameters
        return await _generate_store_zip_impl(
            request, 
            export_data, 
            business_type, 
            store_name, 
            store_slug_clean, 
            seo_tags, 
            business_types_code, 
            experiment_path,
            slug_id,
            store_data,
            manual_pwa_settings
        )
    except Exception as e:
        logger.error(f"Failed to generate store zip: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to generate store zip: {str(e)}")


async def _generate_store_zip_impl(
    request: Request,
    export_data: Dict[str, Any],
    business_type: str,
    store_name: str,
    store_slug_clean: str,
    seo_tags: Dict[str, str],
    business_types_code: str,
    experiment_path: Path,
    slug_id: str,
    store_data: Dict[str, Any],
    manual_pwa_settings: Dict[str, Any]
) -> Response:
    """Internal implementation of zip generation."""
    try:
        import zipfile
        import io
        import json
        import re
        import datetime
        import os
        import fnmatch
        from config import BASE_DIR
        
        # Helper to escape HTML
        def escape_html(text):
            if not text:
                return ""
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;")
        
        # Prepare db_config.json
        db_data = {
            "slug": slug_id,
            "name": store_name,
            "description": f"Standalone store: {store_name}",
            "status": "active",
            "managed_indexes": {
                "stores": [{"name": "stores_slug_id_index", "type": "regular", "keys": {"slug_id": 1}, "options": {"unique": True}}],
                "items": [{"name": "items_item_code_store_id_index", "type": "regular", "keys": [["item_code", 1], ["store_id", 1]], "options": {"unique": True}}],
                "specials": [{"name": "specials_store_id_date_created_index", "type": "regular", "keys": [["store_id", 1], ["date_created", -1]]}],
                "slideshow": [{"name": "slideshow_store_id_order_index", "type": "regular", "keys": [["store_id", 1], ["order", 1]]}]
            }
        }
        
        # Prepare db_collections.json
        collections_data = {
            f"{slug_id}_stores": [export_data.get("store", {})],
            f"{slug_id}_items": export_data.get("items", []),
            f"{slug_id}_specials": export_data.get("specials", []),
            f"{slug_id}_slideshow": export_data.get("slideshow_images", []),
            f"{slug_id}_users": export_data.get("users", [])
        }
        
        # Generate standalone main.py (simple FastAPI app without Ray)
        standalone_main_source = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Standalone Store: {store_name}
This is a standalone FastAPI application for this store.
Graduated from StoreFactory - fully independent and ready to deploy.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from bson.objectid import ObjectId
from bson.errors import InvalidId
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
import secrets

# Load environment variables from .env file
load_dotenv()

# Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("standalone")

# Paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Environment
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/")
DB_NAME = os.getenv("DB_NAME", "labs_db")
PORT = int(os.getenv("PORT", "8000"))

# Admin credentials (configure via environment variables)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Load data
def _load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

DB_CONFIG = _load_json(BASE_DIR / "db_config.json", {{}})
DB_COLLECTIONS = _load_json(BASE_DIR / "db_collections.json", {{}})

# Store data (pre-loaded from export)
STORE_SLUG = "{store_slug_clean}"
STORE_DATA = None
if DB_COLLECTIONS:
    stores_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_stores"
    if stores_key in DB_COLLECTIONS and DB_COLLECTIONS[stores_key]:
        STORE_DATA = DB_COLLECTIONS[stores_key][0]

# Business types (minimal - just what we need)
{business_types_code if business_types_code else "BUSINESS_TYPES = {}"}
BUSINESS_CONFIG = BUSINESS_TYPES.get("{business_type}", BUSINESS_TYPES.get('generic-store', {{}}))

# MongoDB
mongo_client: Optional[AsyncIOMotorClient] = None
mongo_db: Optional[AsyncIOMotorDatabase] = None

async def connect_mongodb():
    global mongo_client, mongo_db
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        await mongo_client.admin.command("ping")
        mongo_db = mongo_client[DB_NAME]
        logger.info(f"MongoDB connected: {{DB_NAME}}")
        await seed_database()
    except Exception as e:
        logger.error(f"MongoDB connection failed: {{e}}")
        raise

async def seed_database():
    if mongo_db is None:
        return
    
    # First, seed stores to get store_id
    store_id = None
    stores_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_stores"
    if stores_key in DB_COLLECTIONS and DB_COLLECTIONS[stores_key]:
        stores_collection = mongo_db[stores_key]
        existing = await stores_collection.count_documents({{}})
        if existing == 0 and DB_COLLECTIONS[stores_key]:
            store_doc = DB_COLLECTIONS[stores_key][0].copy()
            if "_id" in store_doc and isinstance(store_doc["_id"], str):
                try:
                    store_doc["_id"] = ObjectId(store_doc["_id"])
                except:
                    pass
            result = await stores_collection.insert_one(store_doc)
            store_id = result.inserted_id
            logger.info(f"Seeded {{stores_key}} with store_id: {{store_id}}")
        elif existing > 0:
            store = await stores_collection.find_one({{}})
            if store:
                store_id = store.get("_id")
    
    # Now seed other collections
    for collection_name, docs in DB_COLLECTIONS.items():
        if collection_name == stores_key:
            continue  # Already seeded
        try:
            collection = mongo_db[collection_name]
            existing = await collection.count_documents({{}})
            if existing == 0 and docs:
                logger.info(f"Seeding {{collection_name}} with {{len(docs)}} documents")
                for doc in docs.copy():
                    if "_id" in doc and isinstance(doc["_id"], str):
                        try:
                            doc["_id"] = ObjectId(doc["_id"])
                        except:
                            pass
                    if "store_id" in doc:
                        if isinstance(doc["store_id"], str):
                            try:
                                doc["store_id"] = ObjectId(doc["store_id"])
                            except:
                                if store_id:
                                    doc["store_id"] = store_id
                        elif doc["store_id"] is None and store_id:
                            doc["store_id"] = store_id
                    elif store_id and (collection_name.endswith("_items") or collection_name.endswith("_specials") or collection_name.endswith("_slideshow") or collection_name.endswith("_users")):
                        # Set store_id if missing and we have one
                        doc["store_id"] = store_id
                await collection.insert_many(docs)
                logger.info(f"Successfully seeded {{collection_name}}")
        except Exception as e:
            logger.error(f"Error seeding {{collection_name}}: {{e}}")

async def close_mongodb():
    global mongo_client
    if mongo_client is not None:
        mongo_client.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_mongodb()
    yield
    await close_mongodb()

app = FastAPI(
    title=f"{{{{STORE_DATA.get('name', 'Store') if STORE_DATA else 'Store'}}}}",
    description="Standalone store application - graduated from StoreFactory",
    lifespan=lifespan
)

# Session middleware
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_urlsafe(32)))

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR)) if TEMPLATES_DIR.is_dir() else None

# Static files
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Helper to get store data
async def get_store():
    if mongo_db is None or not STORE_DATA:
        return None
    stores_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_stores"
    collection = mongo_db[stores_key]
    store = await collection.find_one({{"slug_id": STORE_SLUG}})
    if store and not store.get('logo_url'):
        store['logo_url'] = "/static/img/logo.png"
    return store

# Helper to get items
async def get_items(store_id):
    if mongo_db is None:
        return []
    items_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_items"
    collection = mongo_db[items_key]
    store_obj_id = ObjectId(store_id) if isinstance(store_id, str) else store_id
    items = await collection.find({{
        "store_id": store_obj_id,
        "status": {{"$ne": "Sold"}}
    }}).sort("date_added", -1).limit(12).to_list(length=None)
    return items

# Helper to get specials
async def get_specials(store_id):
    if mongo_db is None:
        return []
    specials_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_specials"
    collection = mongo_db[specials_key]
    specials = await collection.find({{"store_id": ObjectId(store_id) if isinstance(store_id, str) else store_id}}).sort("date_created", -1).limit(3).to_list(length=None)
    return specials

# Helper to get slideshow
async def get_slideshow(store_id):
    if mongo_db is None:
        return []
    slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
    collection = mongo_db[slideshow_key]
    slideshow = await collection.find({{"store_id": ObjectId(store_id) if isinstance(store_id, str) else store_id}}).sort("order", 1).to_list(length=None)
    return slideshow

# Helper to get current user from session
async def get_current_user(request: Request):
    """Get current logged-in user from session - supports both database users and env admin."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    
    # Check if it's the environment variable admin
    if user_id == "admin":
        admin_email = request.session.get("admin_email") or ADMIN_EMAIL
        return {{
            "_id": "admin",
            "email": admin_email,
            "is_admin": True
        }}
    
    # Otherwise, get from database
    if mongo_db is None:
        return None
    users_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_users"
    collection = mongo_db[users_key]
    try:
        user = await collection.find_one({{"_id": ObjectId(user_id) if isinstance(user_id, str) else user_id}})
        return user
    except:
        return None

# Helper to verify password (support plain text for demo and bcrypt)
def verify_password(stored_password: str, provided_password: str) -> bool:
    """Verify password - supports both plain text and bcrypt."""
    if stored_password == provided_password:
        return True
    try:
        import bcrypt
        if stored_password.startswith("$2b$") or stored_password.startswith("$2a$"):
            return bcrypt.checkpw(provided_password.encode(), stored_password.encode())
    except:
        pass
        return False

# Routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url=f"/{{STORE_SLUG}}", status_code=302)

@app.get("/{{STORE_SLUG}}", response_class=HTMLResponse)
async def store_home(request: Request):
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    store_id = store['_id']
    items = await get_items(store_id)
    specials = await get_specials(store_id)
    slideshow = await get_slideshow(store_id)
    store['specials'] = specials
    store['slideshow_images'] = slideshow
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    context = {{
        "request": request,
        "store": store,
        "items": items[:12],
        "business_config": BUSINESS_CONFIG,
        "user": None,
        "now": datetime.utcnow()
    }}
    return templates.TemplateResponse("store_home.html", context)

@app.get("/{{STORE_SLUG}}/item/{{{{item_id}}}}", response_class=HTMLResponse)
async def item_details(request: Request, item_id: str):
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    try:
        item_obj_id = ObjectId(item_id)
    except InvalidId:
        return HTMLResponse("<h1>Invalid item ID</h1>", status_code=400)
    items = await get_items(store['_id'])
    item = next((i for i in items if i['_id'] == item_obj_id), None)
    if not item:
        return HTMLResponse("<h1>Item not found</h1>", status_code=404)
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    context = {{
        "request": request,
        "store": store,
        "item": item,
        "business_config": BUSINESS_CONFIG,
        "user": None,
        "now": datetime.utcnow()
    }}
    return templates.TemplateResponse("item_details.html", context)

@app.post("/{{STORE_SLUG}}/inquire/{{{{item_id}}}}")
async def submit_inquiry(request: Request, item_id: str, customer_name: str = Form(...), customer_contact: str = Form(...), message: Optional[str] = Form(None)):
    store = await get_store()
    if not store:
        return RedirectResponse(url=f"/{{STORE_SLUG}}", status_code=302)
    try:
        item_obj_id = ObjectId(item_id)
    except InvalidId:
        return RedirectResponse(url=f"/{{STORE_SLUG}}", status_code=302)
    items = await get_items(store['_id'])
    item = next((i for i in items if i['_id'] == item_obj_id), None)
    if not item:
        return RedirectResponse(url=f"/{{STORE_SLUG}}", status_code=302)
    if mongo_db is not None:
        inquiries_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_inquiries"
        collection = mongo_db[inquiries_key]
        await collection.insert_one({{
            "item_id": item_obj_id,
            "item_name": item.get('name', ''),
            "customer_name": customer_name,
            "customer_contact": customer_contact,
            "message": message or "",
            "date_submitted": datetime.utcnow(),
            "store_id": store['_id'],
            "status": "New",
            "archived": False,
            "notes": ""
        }})
    return RedirectResponse(url=f"/{{STORE_SLUG}}/item/{{{{item_id}}}}", status_code=303)

# Admin Routes
@app.get("/{{STORE_SLUG}}/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, error: Optional[str] = None, email: Optional[str] = None):
    """Display admin login page."""
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/dashboard", status_code=303)
    
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    
    context = {{
        "request": request,
        "store_slug": STORE_SLUG,
        "error": error,
        "email": email
    }}
    return templates.TemplateResponse("admin_login.html", context)

@app.post("/{{STORE_SLUG}}/admin/login")
async def admin_login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    """Handle admin login - supports both database users and environment variable admin."""
    store = await get_store()
    if not store:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login?error=Store+not+found", status_code=303)
    
    # First check environment variables (ADMIN_EMAIL and ADMIN_PASSWORD)
    if ADMIN_EMAIL and ADMIN_PASSWORD:
        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            # Create a temporary user session for admin
            request.session["user_id"] = "admin"
            request.session["store_id"] = str(store['_id']) if store else ""
            request.session["admin_email"] = ADMIN_EMAIL
            return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/dashboard", status_code=303)
    
    # Fall back to database users
    if mongo_db is None:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login?error=Database+not+available", status_code=303)
    
    users_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_users"
    collection = mongo_db[users_key]
    user = await collection.find_one({{
        "email": email,
        "store_id": store['_id']
    }})
    
    if not user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login?error=Invalid+credentials&email={{email}}", status_code=303)
    
    stored_password = user.get("password") or user.get("password_hash", "")
    if not stored_password or not verify_password(stored_password, password):
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login?error=Invalid+credentials&email={{email}}", status_code=303)
    
    # Set session
    request.session["user_id"] = str(user['_id'])
    request.session["store_id"] = str(store['_id'])
    return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/dashboard", status_code=303)

@app.post("/{{STORE_SLUG}}/admin/logout")
async def admin_logout(request: Request):
    """Handle admin logout."""
    request.session.clear()
    return RedirectResponse(url=f"/{{STORE_SLUG}}", status_code=303)

@app.get("/{{STORE_SLUG}}/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Display admin dashboard."""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login", status_code=303)
    
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    
    # Get store stats
    store_id = store['_id']
    items_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_items"
    specials_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_specials"
    slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
    inquiries_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_inquiries"
    
    items_count = 0
    specials_count = 0
    slideshow_count = 0
    inquiries_count = 0
    
    if mongo_db is not None:
        try:
            items_collection = mongo_db[items_key]
            items_count = await items_collection.count_documents({{"store_id": store_id}})
            
            specials_collection = mongo_db[specials_key]
            specials_count = await specials_collection.count_documents({{"store_id": store_id}})
            
            slideshow_collection = mongo_db[slideshow_key]
            slideshow_count = await slideshow_collection.count_documents({{"store_id": store_id}})
            
            inquiries_collection = mongo_db[inquiries_key]
            inquiries_count = await inquiries_collection.count_documents({{"store_id": store_id, "archived": False}})
        except:
            pass
    
    context = {{
        "request": request,
        "store": store,
        "user": user,
        "items_count": items_count,
        "specials_count": specials_count,
        "slideshow_count": slideshow_count,
        "inquiries_count": inquiries_count,
        "business_config": BUSINESS_CONFIG
    }}
    return templates.TemplateResponse("admin_dashboard.html", context)

# Admin Management Routes
@app.get("/{{STORE_SLUG}}/admin/items", response_class=HTMLResponse)
async def admin_items(request: Request):
    """Display admin items management page."""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login", status_code=303)
    
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    
    store_id = store['_id']
    items_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_items"
    items = []
    if mongo_db is not None:
        try:
            items_collection = mongo_db[items_key]
            items = await items_collection.find({{"store_id": store_id}}).sort("date_added", -1).to_list(length=None)
        except:
            pass
    
    context = {{
        "request": request,
        "store": store,
        "user": user,
        "items": items,
        "business_config": BUSINESS_CONFIG
    }}
    return templates.TemplateResponse("admin_items.html", context)

@app.get("/{{STORE_SLUG}}/admin/specials", response_class=HTMLResponse)
async def admin_specials(request: Request):
    """Display admin specials management page."""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login", status_code=303)
    
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    
    store_id = store['_id']
    specials_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_specials"
    specials = []
    if mongo_db is not None:
        try:
            specials_collection = mongo_db[specials_key]
            specials = await specials_collection.find({{"store_id": store_id}}).sort("date_created", -1).to_list(length=None)
        except:
            pass
    
    context = {{
        "request": request,
        "store": store,
        "user": user,
        "specials": specials,
        "business_config": BUSINESS_CONFIG
    }}
    return templates.TemplateResponse("admin_specials.html", context)

@app.get("/{{STORE_SLUG}}/admin/inquiries", response_class=HTMLResponse)
async def admin_inquiries(request: Request):
    """Display admin inquiries page."""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login", status_code=303)
    
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    
    store_id = store['_id']
    inquiries_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_inquiries"
    inquiries = []
    if mongo_db is not None:
        try:
            inquiries_collection = mongo_db[inquiries_key]
            inquiries = await inquiries_collection.find({{"store_id": store_id, "archived": False}}).sort("date_submitted", -1).to_list(length=None)
        except:
            pass
    
    context = {{
        "request": request,
        "store": store,
        "user": user,
        "inquiries": inquiries,
        "business_config": BUSINESS_CONFIG
    }}
    return templates.TemplateResponse("admin_inquiries.html", context)

@app.post("/{{STORE_SLUG}}/admin/update-store", response_class=JSONResponse)
async def update_store(request: Request):
    """Update store information."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({{"success": False, "error": "Not authenticated"}}, status_code=401)
    
    store = await get_store()
    if not store or mongo_db is None:
        return JSONResponse({{"success": False, "error": "Store not found"}}, status_code=404)
    
    try:
        data = await request.json()
        stores_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_stores"
        collection = mongo_db[stores_key]
        
        update_data = {{}}
        allowed_fields = ['name', 'email', 'phone', 'phone_display', 'address', 'about_text']
        for field in allowed_fields:
            if field in data:
                update_data[field] = data[field]
        
        if update_data:
            await collection.update_one(
                {{"_id": store['_id']}},
                {{"$set": update_data}}
            )
            return JSONResponse({{"success": True, "message": "Store information updated successfully"}})
        else:
            return JSONResponse({{"success": False, "error": "No valid fields to update"}}, status_code=400)
    except Exception as e:
        logger.error(f"Error updating store: {{e}}", exc_info=True)
        return JSONResponse({{"success": False, "error": str(e)}}, status_code=500)

# Slideshow Management Routes
@app.get("/{{STORE_SLUG}}/admin/slideshow", response_class=HTMLResponse)
async def admin_slideshow(request: Request):
    """Display admin slideshow management page."""
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url=f"/{{STORE_SLUG}}/admin/login", status_code=303)
    
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    
    if not templates:
        return HTMLResponse("<h1>Templates not available</h1>", status_code=500)
    
    context = {{
        "request": request,
        "store": store,
        "user": user
    }}
    return templates.TemplateResponse("admin_slideshow.html", context)

def convert_objectid_for_json(obj):
    """Convert ObjectId and datetime objects to JSON-serializable types."""
    if isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {{k: convert_objectid_for_json(v) for k, v in obj.items()}}
    elif isinstance(obj, list):
        return [convert_objectid_for_json(item) for item in obj]
    return obj

@app.get("/{{STORE_SLUG}}/admin/slideshow/api", response_class=JSONResponse)
async def get_slideshow_images_api(request: Request):
    """Get all slideshow images for a store (API endpoint)."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({{"success": False, "error": "Unauthorized"}}, status_code=401)
    
    store = await get_store()
    if not store or mongo_db is None:
        return JSONResponse({{"success": False, "error": "Store not found"}}, status_code=404)
    
    try:
        store_id = store['_id']
        slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
        collection = mongo_db[slideshow_key]
        images = await collection.find({{"store_id": store_id}}).sort("order", 1).to_list(length=None)
        # Convert ObjectIds to strings for JSON serialization
        images_json = convert_objectid_for_json(images)
        return JSONResponse({{"success": True, "images": images_json}})
    except Exception as e:
        logger.error(f"Error getting slideshow images: {{e}}", exc_info=True)
        return JSONResponse({{"success": False, "error": str(e)}}, status_code=500)

@app.post("/{{STORE_SLUG}}/admin/slideshow/add", response_class=JSONResponse)
async def add_slideshow_image(request: Request, image_url: str = Form(...), caption: Optional[str] = Form(None)):
    """Add a new slideshow image."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({{"success": False, "error": "Unauthorized"}}, status_code=401)
    
    store = await get_store()
    if not store or mongo_db is None:
        return JSONResponse({{"success": False, "error": "Store not found"}}, status_code=404)
    
    try:
        store_id = store['_id']
        slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
        collection = mongo_db[slideshow_key]
        
        # Get the next order number
        max_order_doc = await collection.find({{"store_id": store_id}}).sort("order", -1).limit(1).to_list(length=1)
        next_order = (max_order_doc[0]['order'] + 1) if max_order_doc else 1
        
        slideshow_image = {{
            "store_id": store_id,
            "image_url": image_url,
            "caption": caption or "",
            "order": next_order,
            "date_added": datetime.utcnow()
        }}
        
        result = await collection.insert_one(slideshow_image)
        return JSONResponse({{"success": True, "message": "Slideshow image added successfully.", "image_id": str(result.inserted_id)}})
    except Exception as e:
        logger.error(f"Error adding slideshow image: {{e}}", exc_info=True)
        return JSONResponse({{"success": False, "error": str(e)}}, status_code=500)

@app.post("/{{STORE_SLUG}}/admin/slideshow/{{{{image_id}}}}/delete", response_class=JSONResponse)
async def delete_slideshow_image(request: Request, image_id: str):
    """Delete a slideshow image."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({{"success": False, "error": "Unauthorized"}}, status_code=401)
    
    store = await get_store()
    if not store or mongo_db is None:
        return JSONResponse({{"success": False, "error": "Store not found"}}, status_code=404)
    
    try:
        image_obj_id = ObjectId(image_id)
    except InvalidId:
        return JSONResponse({{"success": False, "error": "Invalid image ID."}}, status_code=400)
    
    try:
        store_id = store['_id']
        slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
        collection = mongo_db[slideshow_key]
        
        result = await collection.delete_one({{"_id": image_obj_id, "store_id": store_id}})
        if result.deleted_count > 0:
            # Reorder remaining images
            slideshow_images = await collection.find({{"store_id": store_id}}).sort("order", 1).to_list(length=None)
            for idx, img in enumerate(slideshow_images, start=1):
                await collection.update_one({{"_id": img['_id']}}, {{"$set": {{"order": idx}}}})
            return JSONResponse({{"success": True, "message": "Slideshow image deleted successfully."}})
        else:
            return JSONResponse({{"success": False, "error": "Image not found or already deleted."}}, status_code=404)
    except Exception as e:
        logger.error(f"Error deleting slideshow image: {{e}}", exc_info=True)
        return JSONResponse({{"success": False, "error": str(e)}}, status_code=500)

@app.post("/{{STORE_SLUG}}/admin/slideshow/update-order", response_class=JSONResponse)
async def update_slideshow_order(request: Request):
    """Update the order of slideshow images."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({{"success": False, "error": "Unauthorized"}}, status_code=401)
    
    store = await get_store()
    if not store or mongo_db is None:
        return JSONResponse({{"success": False, "error": "Store not found"}}, status_code=404)
    
    try:
        image_orders = await request.json()
        if not isinstance(image_orders, list):
            return JSONResponse({{"success": False, "error": "Invalid request format"}}, status_code=400)
        
        store_id = store['_id']
        slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
        collection = mongo_db[slideshow_key]
        
        for idx, img_order in enumerate(image_orders, start=1):
            try:
                image_obj_id = ObjectId(img_order['image_id'])
                await collection.update_one(
                    {{"_id": image_obj_id, "store_id": store_id}},
                    {{"$set": {{"order": idx}}}}
                )
            except (InvalidId, KeyError) as e:
                logger.warning(f"Invalid image ID in order update: {{e}}")
                continue
        
        return JSONResponse({{"success": True, "message": "Slideshow order updated successfully."}})
    except Exception as e:
        logger.error(f"Error updating slideshow order: {{e}}", exc_info=True)
        return JSONResponse({{"success": False, "error": str(e)}}, status_code=500)

@app.post("/{{STORE_SLUG}}/admin/slideshow/{{{{image_id}}}}/update", response_class=JSONResponse)
async def update_slideshow_image(request: Request, image_id: str, image_url: Optional[str] = Form(None), caption: Optional[str] = Form(None)):
    """Update a slideshow image."""
    user = await get_current_user(request)
    if not user:
        return JSONResponse({{"success": False, "error": "Unauthorized"}}, status_code=401)
    
    store = await get_store()
    if not store or mongo_db is None:
        return JSONResponse({{"success": False, "error": "Store not found"}}, status_code=404)
    
    try:
        image_obj_id = ObjectId(image_id)
    except InvalidId:
        return JSONResponse({{"success": False, "error": "Invalid image ID."}}, status_code=400)
    
    update_data = {{}}
    if image_url is not None:
        update_data["image_url"] = image_url
    if caption is not None:
        update_data["caption"] = caption
    
    if not update_data:
        return JSONResponse({{"success": False, "error": "No fields to update."}}, status_code=400)
    
    try:
        store_id = store['_id']
        slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
        collection = mongo_db[slideshow_key]
        
        result = await collection.update_one(
            {{"_id": image_obj_id, "store_id": store_id}},
            {{"$set": update_data}}
        )
        if result.modified_count > 0:
            return JSONResponse({{"success": True, "message": "Slideshow image updated successfully."}})
        else:
            return JSONResponse({{"success": False, "error": "Image not found or no changes made."}}, status_code=404)
    except Exception as e:
        logger.error(f"Error updating slideshow image: {{e}}", exc_info=True)
        return JSONResponse({{"success": False, "error": str(e)}}, status_code=500)

# PWA Routes
@app.get("/manifest.json", response_class=JSONResponse)
async def pwa_manifest(request: Request):
    manifest_path = BASE_DIR / "manifest.json"
    if manifest_path.is_file():
        with open(manifest_path, "r") as f:
            return json.load(f)
    return {{"error": "Manifest not found"}}

@app.get("/service-worker.js", response_class=Response)
async def service_worker(request: Request):
    sw_path = BASE_DIR / "service-worker.js"
    if sw_path.is_file():
        return Response(content=sw_path.read_text(encoding="utf-8"), media_type="application/javascript")
    return Response(content="// Service worker not found", media_type="application/javascript", status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
'''
        
        # Generate requirements.txt (no Ray)
        requirements_content = """fastapi
uvicorn[standard]
motor>=3.0.0
pymongo==4.15.3
python-multipart
jinja2
itsdangerous
python-dotenv
"""
        
        # Generate Dockerfile
        dockerfile_content = f"""# Standalone Store Dockerfile
# Multi-stage build for optimized image size

# --- Stage 1: Build dependencies ---
FROM python:3.10-slim-bookworm as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt /tmp/requirements.txt

# Create virtual environment and install dependencies
RUN python -m venv /opt/venv && \\
    . /opt/venv/bin/activate && \\
    pip install --upgrade pip && \\
    pip install --no-cache-dir -r /tmp/requirements.txt

# --- Stage 2: Final image ---
FROM python:3.10-slim-bookworm

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

# Copy application files
COPY templates /app/templates
COPY static /app/static
COPY db_config.json /app/db_config.json
COPY db_collections.json /app/db_collections.json
COPY main.py /app/main.py
COPY manifest.json /app/manifest.json
COPY service-worker.js /app/service-worker.js

# Create non-root user for security
RUN addgroup --system app && \\
    adduser --system --group app && \\
    chown -R app:app /app

# Switch to non-root user
USER app

# Expose port
ARG APP_PORT=8000
ENV PORT=$APP_PORT
EXPOSE ${{APP_PORT}}

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \\
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${{PORT}}/')" || exit 1

# Run the application
CMD ["python", "main.py"]
"""
        
        # Generate docker-compose.yml
        sanitized_slug = slug_id.lstrip("_").replace("_", "-")
        if sanitized_slug and sanitized_slug[0].isdigit():
            sanitized_slug = f"app-{sanitized_slug}"
        if not sanitized_slug or sanitized_slug.startswith("-"):
            sanitized_slug = f"app-{slug_id.lstrip('_').replace('_', '-')}" or "app-store-factory"
        
        docker_compose_content = f"""# Standalone Store Docker Compose
version: '3.8'

services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
      args:
        APP_PORT: 8000
    container_name: {sanitized_slug}-app
    platform: linux/arm64
    ports:
      - "8000:8000"
    environment:
      - MONGO_URI=mongodb://mongo:27017/
      - DB_NAME=labs_db
      - PORT=8000
      - LOG_LEVEL=INFO
      - PYTHONUNBUFFERED=1
      - ADMIN_EMAIL=admin@example.com
      - ADMIN_PASSWORD=password123
    volumes:
      - ./static:/app/static:ro
      - ./templates:/app/templates:ro
    depends_on:
      mongo:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  mongo:
    image: mongodb/mongodb-atlas-local:latest
    container_name: {sanitized_slug}-mongo
    platform: linux/arm64
    ports:
      - "27017:27017"
    volumes:
      - mongo-data:/data/db
    environment:
      - MONGODB_INITDB_DATABASE=labs_db
    healthcheck:
      test: echo 'db.runCommand("ping").ok' | mongosh localhost:27017/test --quiet
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    restart: unless-stopped

volumes:
  mongo-data:
    driver: local
"""
        
        # Generate README.md
        readme_content = f"""# Standalone Store: {store_name}

This is a **standalone store application** that has graduated from StoreFactory.

## Quick Start

1. **Extract the ZIP file**
   ```bash
   unzip {store_name.replace(' ', '_')}_{store_slug_clean}_*.zip
   cd {store_name.replace(' ', '_')}_{store_slug_clean}_*
   ```

2. **Configure environment variables** (optional but recommended)
   ```bash
   cp .env.example .env
   # Edit .env and set your ADMIN_EMAIL and ADMIN_PASSWORD
   ```

3. **Start with Docker Compose**
   ```bash
   docker-compose up
   ```

4. **Access the store**
   - Open http://localhost:8000 in your browser
   - The store will be available at http://localhost:8000/{store_slug_clean}
   - Admin login: Use the credentials from your .env file (ADMIN_EMAIL and ADMIN_PASSWORD)

## Environment Variables

The application supports the following environment variables:

- `ADMIN_EMAIL` - Email address for admin login (default: from .env or empty)
- `ADMIN_PASSWORD` - Password for admin login (default: from .env or empty)
- `MONGO_URI` - MongoDB connection string (default: mongodb://mongo:27017/)
- `DB_NAME` - Database name (default: labs_db)
- `PORT` - Application port (default: 8000)
- `LOG_LEVEL` - Logging level (default: INFO)

If `ADMIN_EMAIL` and `ADMIN_PASSWORD` are set, you can use these credentials to log in to the admin dashboard. Otherwise, the app will use users from the database.

## What's Included

This ZIP contains everything needed to run **{store_name}** as a standalone application:
- **main.py** - Standalone FastAPI application (no Ray, no StoreFactory dependencies)
- **requirements.txt** - Python dependencies (FastAPI, Motor, Jinja2 - no Ray!)
- **db_config.json** - Store configuration
- **db_collections.json** - Store data (items, specials, slideshow, inquiries)
- **Dockerfile** - Multi-stage Docker container configuration
- **docker-compose.yml** - Complete Docker Compose setup with MongoDB
- **templates/** - HTML templates (store_home.html, item_details.html) with PWA and SEO support
- **static/** - Static files (images, CSS, JS)
- **manifest.json** - PWA manifest for installable app
- **service-worker.js** - Service worker for offline support and caching
- **.env.example** - Example environment variables file

## Store Information

- **Store Name**: {store_name}
- **Store Slug**: {store_slug_clean}
- **Business Type**: {business_type}
- **Export Date**: {datetime.datetime.now().isoformat()}

Enjoy your standalone store! 🎉
"""
        
        # Generate PWA manifest.json
        theme_color = store_data.get('theme_primary', '#3b82f6')
        pwa_manifest_content = {
            "name": manual_pwa_settings.get("name", store_name) if manual_pwa_settings else store_name,
            "short_name": manual_pwa_settings.get("short_name", store_name[:12] if len(store_name) > 12 else store_name) if manual_pwa_settings else store_name[:12] if len(store_name) > 12 else store_name,
            "description": manual_pwa_settings.get("description", seo_tags.get("description", f"{store_name} - Progressive Web App")) if manual_pwa_settings else seo_tags.get("description", f"{store_name} - Progressive Web App"),
            "start_url": manual_pwa_settings.get("start_url", f"/{store_slug_clean}") if manual_pwa_settings else f"/{store_slug_clean}",
            "display": manual_pwa_settings.get("display", "standalone") if manual_pwa_settings else "standalone",
            "background_color": manual_pwa_settings.get("background_color", "#111827") if manual_pwa_settings else "#111827",
            "theme_color": manual_pwa_settings.get("theme_color", theme_color) if manual_pwa_settings else theme_color,
            "orientation": manual_pwa_settings.get("orientation", "portrait-primary") if manual_pwa_settings else "portrait-primary",
            "icons": manual_pwa_settings.get("icons", [
                {"src": "/static/img/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/static/img/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
            ]) if manual_pwa_settings else [
                {"src": "/static/img/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/static/img/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
            ],
            "categories": manual_pwa_settings.get("categories", ["business", "shopping"]) if manual_pwa_settings else ["business", "shopping"],
            "screenshots": manual_pwa_settings.get("screenshots", []) if manual_pwa_settings else []
        }
        
        # Generate service worker
        service_worker_content = """// Progressive Web App Service Worker
const CACHE_NAME = 'store-pwa-v1';
const urlsToCache = [
  '/',
  '/static/css/style.css',
  '/static/js/navigation.js',
  '/static/img/logo.png',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        console.log('Service Worker: Caching files');
        return cache.addAll(urlsToCache);
      })
      .catch((error) => {
        console.error('Service Worker: Cache failed', error);
      })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            console.log('Service Worker: Deleting old cache', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  return self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }
  if (!event.request.url.startsWith(self.location.origin)) {
    return;
  }
  event.respondWith(
    caches.match(event.request)
      .then((response) => {
        return response || fetch(event.request).then((response) => {
          if (!response || response.status !== 200 || response.type !== 'basic') {
            return response;
          }
          const responseToCache = response.clone();
          caches.open(CACHE_NAME)
            .then((cache) => {
              cache.put(event.request, responseToCache);
            });
          return response;
        });
      })
      .catch(() => {
        return caches.match('/');
      })
  );
});
"""
        
        # Helper to process templates (remove StoreFactory references, add PWA/SEO)
        def process_template_for_pwa(content: str, store_slug: str, seo_tags: Dict[str, str], pwa_manifest_path: str, store_data: Dict[str, Any], store_name: str) -> str:
            # Replace /experiments/store_factory/ paths with relative paths
            content = content.replace(f"/experiments/store_factory/{store_slug}", f"/{store_slug}")
            content = content.replace("/experiments/store_factory/", "/")
            content = content.replace("/experiments/store_factory/static/img/logo.png", "/static/img/logo.png")
            content = re.sub(r"logo_url\s+or\s+['\"]/experiments/store_factory/static/img/logo\.png['\"]", "logo_url or '/static/img/logo.png'", content)
            content = content.replace("Powered by StoreFactory", "")
            content = re.sub(r'<a[^>]*href=["\'][^"\']*/download["\'][^>]*>.*?</a>', '', content, flags=re.DOTALL)
            # Fix admin template paths - ensure they use the store slug
            content = re.sub(r'/experiments/store_factory/([^/]+)/admin/', rf'/{store_slug}/admin/', content)
            
            # Add PWA and SEO meta tags to <head>
            escaped_description = escape_html(seo_tags.get('description', ''))
            escaped_keywords = escape_html(seo_tags.get('keywords', ''))
            escaped_og_title = escape_html(seo_tags.get('og_title', ''))
            escaped_og_description = escape_html(seo_tags.get('og_description', ''))
            escaped_twitter_title = escape_html(seo_tags.get('twitter_title', ''))
            escaped_twitter_description = escape_html(seo_tags.get('twitter_description', ''))
            escaped_store_name = escape_html(store_name)
            theme_color = store_data.get('theme_primary', '#3b82f6') if store_data else '#3b82f6'
            
            pwa_seo_tags = f"""
    <!-- SEO Meta Tags -->
    <meta name="description" content="{escaped_description}">
    <meta name="keywords" content="{escaped_keywords}">
    
    <!-- Open Graph / Facebook -->
    <meta property="og:type" content="{seo_tags.get('og_type', 'website')}">
    <meta property="og:title" content="{escaped_og_title}">
    <meta property="og:description" content="{escaped_og_description}">
    <meta property="og:image" content="/static/img/icon-512.png">
    
    <!-- Twitter -->
    <meta name="twitter:card" content="{seo_tags.get('twitter_card', 'summary_large_image')}">
    <meta name="twitter:title" content="{escaped_twitter_title}">
    <meta name="twitter:description" content="{escaped_twitter_description}">
    <meta name="twitter:image" content="/static/img/icon-512.png">
    
    <!-- PWA Meta Tags -->
    <meta name="theme-color" content="{theme_color}">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="{escaped_store_name}">
    
    <!-- PWA Manifest -->
    <link rel="manifest" href="{pwa_manifest_path}">
    
    <!-- PWA Icons -->
    <link rel="icon" type="image/png" sizes="192x192" href="/static/img/icon-192.png">
    <link rel="icon" type="image/png" sizes="512x512" href="/static/img/icon-512.png">
    <link rel="apple-touch-icon" href="/static/img/icon-192.png">
"""
            
            # Update title tag with SEO title if present
            if seo_tags.get('title'):
                escaped_title = escape_html(seo_tags.get('title', ''))
                content = re.sub(r'<title>.*?</title>', f'<title>{escaped_title}</title>', content, flags=re.DOTALL)
            
            # Insert PWA/SEO tags after <head> or before </head>
            if "<head>" in content:
                content = content.replace("<head>", f"<head>{pwa_seo_tags}", 1)
            elif "</head>" in content:
                content = content.replace("</head>", f"{pwa_seo_tags}</head>", 1)
            
            # Add service worker registration script before </body>
            # Use {% raw %} to prevent Jinja2 from interpreting the JavaScript
            sw_script = """
    <script>
        {% raw %}
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/service-worker.js')
                    .then((registration) => {
                        console.log('Service Worker registered:', registration.scope);
                    })
                    .catch((error) => {
                        console.log('Service Worker registration failed:', error);
                    });
            });
        }
        {% endraw %}
    </script>
"""
            if "</body>" in content:
                content = content.replace("</body>", f"{sw_script}</body>", 1)
            
            return content
        
        # Create ZIP file
        zip_buffer = io.BytesIO()
        EXCLUSION_PATTERNS = ["__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"]
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Include templates - process to remove StoreFactory references and add PWA/SEO
            templates_dir = experiment_path / "templates"
            pwa_manifest_path = "/manifest.json"
            if templates_dir.is_dir():
                # Customer-facing templates
                for template_file in ["store_home.html", "item_details.html"]:
                    template_path = templates_dir / template_file
                    if template_path.is_file():
                        template_content = template_path.read_text(encoding="utf-8")
                        template_content = process_template_for_pwa(template_content, store_slug_clean, seo_tags, pwa_manifest_path, store_data, store_name)
                        zf.writestr(f"templates/{template_file}", template_content)
                
                # Admin templates (no processing needed, just copy)
                for template_file in ["admin_login.html", "admin_dashboard.html", "admin_items.html", "admin_specials.html", "admin_inquiries.html", "admin_slideshow.html"]:
                    template_path = templates_dir / template_file
                    if template_path.is_file():
                        template_content = template_path.read_text(encoding="utf-8")
                        # Fix admin template paths to use store slug
                        template_content = re.sub(r'/experiments/store_factory/([^/]+)/admin/', rf'/{store_slug_clean}/admin/', template_content)
                        template_content = re.sub(r'/experiments/store_factory/([^/]+)"', rf'/{store_slug_clean}"', template_content)
                        zf.writestr(f"templates/{template_file}", template_content)
            
            # Include static files
            static_dir = experiment_path / "static"
            if static_dir.is_dir():
                for root, dirs, files in os.walk(static_dir):
                    dirs[:] = [d for d in dirs if d not in EXCLUSION_PATTERNS]
                    for file_name in files:
                        if file_name.startswith('.') or any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
                            continue
                        file_path = Path(root) / file_name
                        try:
                            arcname = f"static/{file_path.relative_to(static_dir)}"
                            zf.write(file_path, arcname)
                        except Exception as e:
                            logger.warning(f"Failed to include {file_path}: {e}")
            
            # Add generated files
            zf.writestr("main.py", standalone_main_source)
            zf.writestr("requirements.txt", requirements_content)
            zf.writestr("db_config.json", json.dumps(db_data, indent=2))
            zf.writestr("db_collections.json", json.dumps(collections_data, indent=2))
            zf.writestr("manifest.json", json.dumps(pwa_manifest_content, indent=2))
            zf.writestr("service-worker.js", service_worker_content)
            zf.writestr("Dockerfile", dockerfile_content)
            zf.writestr("docker-compose.yml", docker_compose_content)
            zf.writestr("README.md", readme_content)
            zf.writestr(".dockerignore", "# Docker ignore\n__pycache__/\n*.pyc\n.DS_Store\n.git/\n.idea\n.vscode/\n")
            zf.writestr(".gitignore", "# Git ignore\n__pycache__/\n*.pyc\n.DS_Store\n.env\n.venv/\n*.log\n")
            zf.writestr(".env.example", """# Environment Variables for Standalone Store
# Copy this file to .env and set your values

# Admin Credentials (used for admin login)
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=your-secure-password-here

# MongoDB Configuration
MONGO_URI=mongodb://mongo:27017/
DB_NAME=labs_db

# Application Configuration
PORT=8000
LOG_LEVEL=INFO

# Session Secret (auto-generated if not set)
# SESSION_SECRET=
""")
        
        zip_buffer.seek(0)
        
        # Create filename
        safe_name = "".join(c for c in store_name if c.isalnum() or c in (' ', '-', '_')).rstrip().replace(' ', '_')
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_name}_{store_slug_clean}_{timestamp}.zip"
        
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error(f"Failed to generate store zip: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Failed to export store: {e}"}, status_code=500)


@bp.get("/{store_slug}/download")
async def download_store(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Download store as standalone Docker package with PWA and SEO support."""
    try:
        # Get store data from actor
        result = await actor.export_store_data.remote(store_slug)
        if not result.get("success"):
            return JSONResponse(result, status_code=404)
        
        export_data = result.get("data", {})
        
        # Generate zip using helper
        return await _generate_store_zip(request, export_data, actor, store_slug=store_slug)
    except Exception as e:
        logger.error(f"Failed to download store: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Failed to export store: {e}"}, status_code=500)
