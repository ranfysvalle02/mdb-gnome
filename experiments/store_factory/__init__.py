"""
Store Factory Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette import status
from typing import Optional, Dict, Any
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Show store selection/creation page for a business type."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_selection.remote(business_type, context)
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
    tagline: Optional[str] = Form(None),
    about_text: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    hours: Optional[str] = Form(None),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle store creation."""
    form_data = {
        "name": name,
        "slug_id": slug_id,
        "tagline": tagline,
        "about_text": about_text,
        "address": address,
        "phone": phone,
        "hours": hours,
        "email": email,
        "password": password
    }
    
    try:
        result = await actor.create_store.remote(business_type, form_data)
        if result.get("success"):
            # Redirect to store home
            return RedirectResponse(
                url=f"/experiments/store_factory/{result['store_slug']}",
                status_code=status.HTTP_303_SEE_OTHER
            )
        else:
            # Redirect back with error (could use flash messages in future)
            return RedirectResponse(
                url=f"/experiments/store_factory/select/{business_type}?error={result.get('error', 'Unknown error')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for create_store: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to create store: {e}")


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

@bp.get("/{store_slug}/admin/slideshow", response_class=JSONResponse)
async def get_slideshow_images(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Get all slideshow images for a store."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        images = await actor.get_slideshow_images.remote(store_slug)
        return JSONResponse({"success": True, "images": images})
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


# --- Store Export/Download Routes ---

@bp.get("/{store_slug}/download")
async def download_store(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Download store as standalone Docker package (Dockerfile, docker-compose.yml, main.py, and data)."""
    try:
        from pathlib import Path
        import zipfile
        import io
        from config import BASE_DIR
        from export_helpers import make_intelligent_standalone_main_py
        from fastapi.templating import Jinja2Templates
        
        # Get store data from actor
        result = await actor.export_store_data.remote(store_slug)
        if not result.get("success"):
            return JSONResponse(result, status_code=404)
        
        export_data = result.get("data", {})
        store_name = export_data.get("export_metadata", {}).get("store_name", store_slug)
        store_slug_clean = export_data.get("export_metadata", {}).get("store_slug", store_slug)
        
        # Get experiment slug (store_factory)
        slug_id = getattr(request.state, "slug_id", "store_factory")
        experiment_path = BASE_DIR / "experiments" / slug_id
        source_dir = BASE_DIR
        
        # Create a minimal standalone main.py for just this store
        # This will be a simplified FastAPI app that only serves this one store
        store_data = export_data.get("store", {})
        business_type = export_data.get("export_metadata", {}).get("business_type", "generic-store")
        
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
        
        # Generate minimal standalone main.py for this store only
        standalone_main_source = f"""#!/usr/bin/env python3
# -*- coding: utf-8 -*-

\"\"\"
Standalone Store: {store_name}
This is a minimal standalone FastAPI application for just this store.
\"\"\"

import os
import sys
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from bson.objectid import ObjectId
from bson.errors import InvalidId

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
    # Get store from collections
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
        
        # Seed database
        await seed_database()
    except Exception as e:
        logger.error(f"MongoDB connection failed: {{e}}")
        raise

async def seed_database():
    if not mongo_db:
        return
    
    for collection_name, docs in DB_COLLECTIONS.items():
        try:
            collection = mongo_db[collection_name]
            existing = await collection.count_documents({{}})
            if existing == 0 and docs:
                logger.info(f"Seeding {{collection_name}} with {{len(docs)}} documents")
                for doc in docs:
                    if "_id" in doc and isinstance(doc["_id"], str):
                        try:
                            doc["_id"] = ObjectId(doc["_id"])
                        except:
                            pass
                await collection.insert_many(docs)
        except Exception as e:
            logger.error(f"Error seeding {{collection_name}}: {{e}}")

async def close_mongodb():
    global mongo_client
    if mongo_client:
        mongo_client.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_mongodb()
    yield
    await close_mongodb()

app = FastAPI(
    title=f"{{STORE_DATA.get('name', 'Store') if STORE_DATA else 'Store'}}",
    description="Standalone store application",
    lifespan=lifespan
)

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR)) if TEMPLATES_DIR.is_dir() else None

# Static files
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Helper to get store data
async def get_store():
    if not mongo_db or not STORE_DATA:
        return None
    
    stores_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_stores"
    collection = mongo_db[stores_key]
    store = await collection.find_one({{"slug_id": STORE_SLUG}})
    
    if store and not store.get('logo_url'):
        store['logo_url'] = "/static/img/logo.png"
    
    return store

# Helper to get items
async def get_items(store_id):
    if not mongo_db:
        return []
    
    items_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_items"
    collection = mongo_db[items_key]
    items = await collection.find({{"store_id": ObjectId(store_id) if isinstance(store_id, str) else store_id}}).sort("date_added", -1).to_list(length=None)
    return items

# Helper to get specials
async def get_specials(store_id):
    if not mongo_db:
        return []
    
    specials_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_specials"
    collection = mongo_db[specials_key]
    specials = await collection.find({{"store_id": ObjectId(store_id) if isinstance(store_id, str) else store_id}}).sort("date_created", -1).limit(3).to_list(length=None)
    return specials

# Helper to get slideshow
async def get_slideshow(store_id):
    if not mongo_db:
        return []
    
    slideshow_key = f"{{DB_CONFIG.get('slug', 'store_factory')}}_slideshow"
    collection = mongo_db[slideshow_key]
    slideshow = await collection.find({{"store_id": ObjectId(store_id) if isinstance(store_id, str) else store_id}}).sort("order", 1).to_list(length=None)
    return slideshow

# Routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url=f"/{STORE_SLUG}", status_code=302)

@app.get("/{STORE_SLUG}", response_class=HTMLResponse)
async def store_home(request: Request):
    store = await get_store()
    if not store:
        return HTMLResponse("<h1>Store not found</h1>", status_code=404)
    
    items = await get_items(store['_id'])
    specials = await get_specials(store['_id'])
    slideshow = await get_slideshow(store['_id'])
    
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

@app.get("/{STORE_SLUG}/item/{{item_id}}", response_class=HTMLResponse)
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
"""
        
        # Prepare db_config.json (minimal config for this store only)
        db_data = {
            "slug": slug_id,  # Keep slug for collection naming
            "name": store_name,  # Store name, not "Store Factory"
            "description": f"Standalone store: {store_name}",
            "status": "active",
            "managed_indexes": {
                "stores": [{"name": "stores_slug_id_index", "type": "regular", "keys": {"slug_id": 1}, "options": {"unique": True}}],
                "items": [{"name": "items_item_code_store_id_index", "type": "regular", "keys": [["item_code", 1], ["store_id", 1]], "options": {"unique": True}}],
                "specials": [{"name": "specials_store_id_date_created_index", "type": "regular", "keys": [["store_id", 1], ["date_created", -1]]}],
                "slideshow": [{"name": "slideshow_store_id_order_index", "type": "regular", "keys": [["store_id", 1], ["order", 1]]}]
            }
        }
        
        # Prepare db_collections.json (store data in collection format)
        # Collection names need to be prefixed with experiment slug
        collections_data = {
            f"{slug_id}_stores": [export_data.get("store", {})],
            f"{slug_id}_items": export_data.get("items", []),
            f"{slug_id}_specials": export_data.get("specials", []),
            f"{slug_id}_slideshow": export_data.get("slideshow_images", [])
        }
        
        # Generate requirements
        local_reqs_path = experiment_path / "requirements.txt"
        base_requirements = [
            "fastapi",
            "uvicorn[standard]",
            "motor>=3.0.0",
            "pymongo==4.15.3",
            "python-multipart",
            "jinja2",
            "ray[default]>=2.9.0",
        ]
        
        all_requirements = base_requirements.copy()
        if local_reqs_path.is_file():
            with open(local_reqs_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        all_requirements.append(line)
        
        # Minimal requirements (no Ray needed for simple store)
        minimal_requirements = [
            "fastapi",
            "uvicorn[standard]",
            "motor>=3.0.0",
            "pymongo==4.15.3",
            "python-multipart",
            "jinja2",
        ]
        requirements_content = "\n".join(minimal_requirements)
        
        # Generate Dockerfile (minimal - no Ray needed for simple store)
        def create_dockerfile() -> str:
            dockerfile_lines = [
                "# --- Stage 1: Build dependencies ---",
                "FROM python:3.10-slim-bookworm as builder",
                "WORKDIR /app",
                "",
                "# Install build deps",
                "RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*",
                "",
                "# Create requirements file"
            ]
            
            for req in minimal_requirements:
                escaped_req = req.replace("'", "'\"'\"'")
                dockerfile_lines.append(f"RUN echo '{escaped_req}' >> /tmp/requirements.txt")
            
            dockerfile_lines.extend([
                "",
                "# Install dependencies",
                "RUN python -m venv /opt/venv && \\",
                "    . /opt/venv/bin/activate && \\",
                "    pip install --upgrade pip && \\",
                "    pip install -r /tmp/requirements.txt",
                "",
                "# --- Stage 2: Final image ---",
                "FROM python:3.10-slim-bookworm",
                "WORKDIR /app",
                "",
                "# Copy venv from builder",
                "COPY --from=builder /opt/venv /opt/venv",
                'ENV PATH="/opt/venv/bin:$PATH"',
                "",
                "# Copy templates and static files",
                "COPY templates /app/templates",
                "COPY static /app/static",
                "",
                "# Copy configuration files",
                "COPY db_config.json /app/db_config.json",
                "COPY db_collections.json /app/db_collections.json",
                "",
                "# Copy standalone main application",
                "COPY main.py /app/main.py",
                "",
                "# Create non-root user",
                "RUN addgroup --system app && adduser --system --group app",
                "RUN chown -R app:app /app",
                "USER app",
                "",
                "ARG APP_PORT=8000",
                "ENV PORT=$APP_PORT",
                "",
                "EXPOSE ${APP_PORT}",
                "",
                "CMD python main.py",
            ])
            
            return "\n".join(dockerfile_lines)
        
        dockerfile_content = create_dockerfile()
        
        # Generate docker-compose.yml
        sanitized_slug = slug_id.lstrip("_").replace("_", "-")
        if sanitized_slug and sanitized_slug[0].isdigit():
            sanitized_slug = f"app-{sanitized_slug}"
        if not sanitized_slug or sanitized_slug.startswith("-"):
            sanitized_slug = f"app-{slug_id.lstrip('_').replace('_', '-')}" or "app-store-factory"
        
        docker_compose_content = f"""services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {sanitized_slug}-app
    platform: linux/arm64
    ports:
      - "8000:8000"
    environment:
      - MONGO_URI=mongodb://mongo:27017/
      - DB_NAME=labs_db
      - PORT=8000
      - LOG_LEVEL=INFO
    volumes:
      - .:/app
    depends_on:
      mongo:
        condition: service_healthy
    restart: unless-stopped

  mongo:
    image: mongodb/mongodb-atlas-local:latest
    container_name: {sanitized_slug}-mongo
    platform: linux/arm64
    ports:
      - "27017:27017"
    volumes:
      - mongo-data:/data/db
    healthcheck:
      test: echo 'db.runCommand("ping").ok' | mongosh localhost:27017/test --quiet
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  mongo-data:
"""
        
        # Generate README
        safe_store_name = store_name.replace(' ', '_')
        business_type = export_data.get('export_metadata', {}).get('business_type', 'generic-store')
        export_date = export_data.get('export_metadata', {}).get('exported_at', datetime.datetime.now().isoformat())
        
        readme_content = f"""# Standalone Store Export: {store_name}

This is a standalone Docker package for the store **{store_name}**.

## Quick Start

1. **Extract the ZIP file**
   ```bash
   unzip {safe_store_name}_{store_slug_clean}_*.zip
   cd {safe_store_name}_{store_slug_clean}_*
   ```

2. **Start with Docker Compose**
   ```bash
   docker-compose up
   ```

3. **Access the store**
   - Open http://localhost:8000 in your browser
   - The store will be available at http://localhost:8000/{store_slug_clean}

## What's Included

This ZIP contains ONLY the files needed for **{store_name}**:

- **main.py** - Minimal FastAPI application (serves only this store)
- **Dockerfile** - Docker container configuration
- **docker-compose.yml** - Docker Compose setup with MongoDB
- **db_config.json** - Configuration for this store only
- **db_collections.json** - Data for this store only (items, specials, slideshow)
- **templates/** - Only store_home.html and item_details.html templates
- **static/** - Static files (images, CSS, JS) for this store
- **requirements.txt** - Minimal Python dependencies (FastAPI, MongoDB, Jinja2)

**This is NOT the full Store Factory platform - it's just this one store!**

## Store Information

- **Store Name**: {store_name}
- **Store Slug**: {store_slug_clean}
- **Business Type**: {business_type}
- **Export Date**: {export_date}

## Running Without Docker

If you prefer to run without Docker:

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Start MongoDB (or use MongoDB Atlas)

3. Set environment variables:
   ```bash
   export MONGO_URI="mongodb://localhost:27017/"
   export DB_NAME="labs_db"
   export PORT=8000
   ```

4. Run the application:
   ```bash
   python main.py
   ```

## Notes

- The store data is pre-seeded in the database
- MongoDB Atlas Local is used for local development
- This is a minimal standalone package - only this store, no store factory features
- All store data (items, specials, slideshow images) for this store is included

Enjoy your standalone store!
"""
        
        # Create ZIP file
        zip_buffer = io.BytesIO()
        EXCLUSION_PATTERNS = ["__pycache__", ".DS_Store", "*.pyc", "*.tmp", ".git", ".idea", ".vscode"]
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Only include templates and static files needed for this store
            import os
            import fnmatch
            
            # Include only store_home.html and item_details.html templates
            templates_dir = experiment_path / "templates"
            if templates_dir.is_dir():
                for template_file in ["store_home.html", "item_details.html"]:
                    template_path = templates_dir / template_file
                    if template_path.is_file():
                        zf.write(template_path, f"templates/{template_file}")
            
            # Include static files (logo, JS, CSS)
            static_dir = experiment_path / "static"
            if static_dir.is_dir():
                for root, dirs, files in os.walk(static_dir):
                    # Skip excluded directories
                    dirs[:] = [d for d in dirs if d not in EXCLUSION_PATTERNS]
                    
                    for file_name in files:
                        if any(fnmatch.fnmatch(file_name, p) for p in EXCLUSION_PATTERNS):
                            continue
                        
                        file_path = Path(root) / file_name
                        try:
                            arcname = f"static/{file_path.relative_to(static_dir)}"
                            zf.write(file_path, arcname)
                        except Exception as e:
                            logger.warning(f"Failed to include {file_path}: {e}")
            
            # Add generated files
            zf.writestr("Dockerfile", dockerfile_content)
            zf.writestr("docker-compose.yml", docker_compose_content)
            zf.writestr("db_config.json", json.dumps(db_data, indent=2))
            zf.writestr("db_collections.json", json.dumps(collections_data, indent=2))
            zf.writestr("main.py", standalone_main_source)
            zf.writestr("requirements.txt", requirements_content)
            zf.writestr("README.md", readme_content)
        
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
        logger.error(f"Failed to create standalone export: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Failed to export store: {e}"}, status_code=500)

