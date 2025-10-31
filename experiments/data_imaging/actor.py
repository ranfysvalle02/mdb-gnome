# File: /app/experiments/data_imaging/actor.py

# import ray  <-- REMOVED
import logging
import json
import pathlib
from typing import List, Dict, Any

# --- NO HEAVY IMPORTS HERE ---
# All heavy modules (motor, jinja2, engine, ScopedMongoWrapper)
# will be imported *inside* the actor's __init__ method.
import ray

# Actor-local paths
experiment_dir = pathlib.Path(__file__).parent
templates_dir = experiment_dir / "templates"

logger = logging.getLogger(__name__)


@ray.remote
class ExperimentActor:
    """
    This is the "Headless Server". It runs in a separate, isolated
    Ray worker process with all the heavy dependencies.
    main.py is responsible for decorating and launching this class.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: list[str]):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        self.vector_index_name = f"{write_scope}_workout_vector_index" # Pre-calculate the prefixed index name
        
        logger.info(f"[{write_scope}-Actor] Initializing...")

        # --- THIS IS THE FIX ---
        # All heavy dependencies are imported *here*, inside the
        # isolated Ray worker process, not at the top level.
        try:
            # Import local engine module
            from . import engine
            self.engine = engine
            
            # Import other heavy dependencies
            from fastapi.templating import Jinja2Templates
            import motor.motor_asyncio
            from async_mongo_wrapper import ScopedMongoWrapper 
            from pymongo.errors import OperationFailure, DuplicateKeyError

            # Store modules/classes on the instance for later use
            self.Jinja2Templates = Jinja2Templates
            self.motor_asyncio = motor.motor_asyncio
            self.ScopedMongoWrapper = ScopedMongoWrapper
            self.OperationFailure = OperationFailure
            self.DuplicateKeyError = DuplicateKeyError
            
            logger.info(f"[{write_scope}-Actor] Successfully lazy-loaded heavy dependencies.")

        except ImportError as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to lazy-load dependencies: {e}")
            # Set all attributes to None so _check_ready fails cleanly
            self.engine = None
            self.Jinja2Templates = None
            self.motor_asyncio = None
            self.ScopedMongoWrapper = None
            self.OperationFailure = None
            self.DuplicateKeyError = None
        # ---------------------
        
        # Actor-local Template Engine
        if not templates_dir.is_dir():
            logger.error(f"[{write_scope}-Actor] Template dir not found at {templates_dir}")
            self.templates = None
        elif self.Jinja2Templates: # Check if import succeeded
            self.templates = self.Jinja2Templates(directory=str(templates_dir))
            logger.info(f"[{write_scope}-Actor] Templates loaded from {templates_dir}")
        else:
            self.templates = None # Import failed, set to None

        # Actor-local, persistent DB Client and Scoped Wrapper
        try:
            # Check if DB-related imports succeeded
            if not self.motor_asyncio or not self.ScopedMongoWrapper:
                raise ImportError("DB modules (motor, ScopedMongoWrapper) not loaded.")
                
            self.client = self.motor_asyncio.AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
            self.real_db = self.client[db_name]
            self.db = self.ScopedMongoWrapper(
                real_db=self.real_db,
                read_scopes=read_scopes,
                write_scope=write_scope
            )
            logger.info(f"[{write_scope}-Actor] DB connection and scope wrapper created.")
        except Exception as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to init DB: {e}")
            self.client = None
            self.real_db = None
            self.db = None

    def __del__(self):
        """Close the client when the actor is destroyed."""
        if hasattr(self, 'client') and self.client:
            self.client.close()
            logger.info(f"[{self.write_scope}-Actor] DB connection closed.")

    def _check_ready(self):
        """Internal helper to check if init succeeded."""
        if not self.db or not self.templates or not self.engine or not self.OperationFailure:
            # Log the specific failure
            if not self.engine:
                logger.error(f"[{self.write_scope}-Actor] Call failed: 'engine' module not loaded.")
            if not self.templates:
                logger.error(f"[{self.write_scope}-Actor] Call failed: 'templates' not loaded.")
            if not self.db:
                logger.error(f"[{self.write_scope}-Actor] Call failed: 'db' connection not loaded.")
            if not self.OperationFailure:
                 logger.error(f"[{self.write_scope}-Actor] Call failed: 'pymongo.errors' not loaded.")
            raise RuntimeError("Actor is not initialized correctly. Check logs for import errors.")

    # --- Method 1: Replaces show_gallery ---
    async def render_gallery_page(self, request_context: dict) -> str:
        self._check_ready()
        try:
            # We use the scoped wrapper (self.db) which prefixes 'workouts' automatically
            docs = await self.db.workouts.find({}).sort("_id", 1).to_list(200)
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] DB error in render_gallery_page: {e}")
            docs = []

        snippet_list = []
        for d in docs:
            arrays = self.engine.generate_workout_viz_arrays(d, size=8)
            b64_img = self.engine.encode_png_b64(arrays["rgb_combined"], (128, 128))
            suffix = d["_id"].split("_")[-1]
            snippet_list.append(f"""
              <div class="collection-item">
                <a href="./workout/{suffix}">
                  <img src="data:image/png;base64,{b64_img}" alt="Workout {suffix}">
                  <p>Workout #{suffix}</p>
                </a>
              </div>
            """)

        if not snippet_list:
            snippet_list = ["<p>No workouts present. Click 'Generate' to create some!</p>"]

        response = self.templates.TemplateResponse(
            "index.html",
            {"request": request_context, "collection_images_html": "".join(snippet_list)},
        )
        return response.body.decode("utf-8")

    # --- Method 2: Replaces show_detail ---
    async def render_detail_page(self, workout_id: int, request_context: dict) -> str:
        self._check_ready()
        
        doc_id = f"workout_rad_{workout_id}"
        doc = await self.db.workouts.find_one({"_id": doc_id})
        if not doc:
            return f"<h1>404 - Not Found</h1><p>No workout with id {doc_id}</p>"

        # Check if AI summary is pending
        summary_is_pending = (
            self.engine.PLACEHOLDER_CLASSIFICATION in doc.get("ai_classification", "") or
            self.engine.PLACEHOLDER_SUMMARY in doc.get("ai_summary", "")
        )

        # kNN pipeline (Atlas vectorSearch)
        neighbors_html = "<p>Vector data is missing, so no neighbors found.</p>"
        neighbors = []
        if isinstance(doc.get("workout_vector"), list) and len(doc["workout_vector"]) == 192:
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": self.vector_index_name, # Use the prefixed index name
                        "path": "workout_vector",
                        "queryVector": doc["workout_vector"],
                        "filter": {"_id": {"$ne": doc_id}},
                        "numCandidates": 50,
                        "limit": 3,
                    }
                },
                {"$project": {"_id": 1, "score": {"$meta": "vectorSearchScore"}, "workout_type": 1, "session_tag": 1, "ai_classification": 1}}
            ]
            try:
                # self.db.workouts.aggregate will use the scoped collection
                cur = self.db.workouts.aggregate(pipeline)
                neighbors = await cur.to_list(None)
                if neighbors:
                    items = []
                    for n in neighbors:
                        sid = n["_id"].split("_")[-1]
                        context_span = f"Type: {n.get('workout_type','?')}"
                        if n.get("session_tag"): context_span += f" | Tag: {n['session_tag']}"
                        if n.get("ai_classification") != self.engine.PLACEHOLDER_CLASSIFICATION:
                            context_span += f" | Pattern: {n['ai_classification']}"
                        items.append(f'<li><a href="/experiments/data_imaging/workout/{sid}">Workout #{sid}</a> <span>({context_span})</span><br>Similarity Score: {n["score"]:.4f}</li>')
                    neighbors_html = "".join(items)
                else:
                    neighbors_html = "<p>No neighbors found (maybe only 1 doc in DB?).</p>"
            except self.OperationFailure as oe:
                err_msg = oe.details.get('errmsg', str(oe))
                logger.error(f"[{self.write_scope}-Actor] VectorSearch error: {err_msg}")
                neighbors_html = f"<p><b>VectorSearch DB Error:</b> {err_msg}<br><small>Is index '{self.vector_index_name}' active?</small></p>"
            except Exception as e:
                logger.error(f"[{self.write_scope}-Actor] Unexpected vector search error: {e}")
                neighbors_html = f"<p><b>Unexpected vector search error:</b> {e}</p>"
        
        ephemeral_prompt = doc.get("llm_analysis_prompt", self.engine.PLACEHOLDER_PROMPT)
        ai_class = doc.get("ai_classification", self.engine.PLACEHOLDER_CLASSIFICATION)
        ai_sum = doc.get("ai_summary", self.engine.PLACEHOLDER_SUMMARY)

        if summary_is_pending:
            ephemeral_class, ephemeral_prompt = self.engine.analyze_time_series_features(doc, neighbors)
            ai_class = ephemeral_class
        else:
            ephemeral_prompt = doc.get("llm_analysis_prompt", self.engine.PLACEHOLDER_PROMPT)

        # Construct images:
        arrays = self.engine.generate_workout_viz_arrays(doc, size=8)
        b64_combined = self.engine.encode_png_b64(arrays["rgb_combined"], (256,256))
        b64_chart_hr = self.engine.generate_chart_base64(arrays["raw_hr"], "#FF6868")
        b64_chart_cal = self.engine.generate_chart_base64(arrays["raw_cal"], "#00ED64")
        b64_chart_speed = self.engine.generate_chart_base64(arrays["raw_speed"], "#58AEFF")
        b64_r = self.engine.encode_png_b64(arrays["channel_r_2d"], (128,128), tint_color=(255,0,0))
        b64_g = self.engine.encode_png_b64(arrays["channel_g_2d"], (128,128), tint_color=(0,255,0))
        b64_b = self.engine.encode_png_b64(arrays["channel_b_2d"], (128,128), tint_color=(0,0,255))

        # Make doc copy safe for JSON
        doc_copy = dict(doc)
        if isinstance(doc_copy.get("workout_vector"), list):
            vec_len = len(doc_copy["workout_vector"])
            short_vec = doc_copy["workout_vector"][:5]
            doc_copy["workout_vector"] = f"[{short_vec[0]:.2f}... {vec_len - 1} more elements]"
        doc_json = json.dumps(doc_copy, indent=2, default=str)

        # ... (other HTML snippets) ...
        gear_used_html = ""
        if doc.get("gear_used"):
             gear_used_html = "<ul>"
             for g in doc.get("gear_used", []):
                 gear_used_html += f"<li>{json.dumps(g)}</li>"
             gear_used_html += "</ul>"
        
        if summary_is_pending:
            ai_analysis_button_html = f"""
              <form id="analyzeForm" action="/experiments/data_imaging/workout/{workout_id}/analyze" method="POST" style="margin:0;">
                <button type="submit" id="analyzeBtn" class="control-btn" style="background-color:var(--accent-blue);color:white;">
                  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"
                       fill="currentColor" viewBox="0 0 16 16"
                       style="vertical-align:-2px;margin-right:5px;">
                    <path d="M8 15A7 7 0 1 0 8 1a7 7 0 0 0
                             0 14zm-5.467 4.14C7.02 12.637 7.558
                             13 8 13c.448 0 .89-.37 1.341-.758.384-.33
                             1.164-.98 1.956-1.579.529-.396.958-.87
                             1.253-1.412.308-.567.452-1.217.452-1.921
                             0-.663-.122-1.284-.367-1.841-.247-.568
                             -.62-1.11-1.12-1.583-.497-.47-1.127-.866
                             -1.87-1.171C9.697 5.093 8.87 4.75
                             8 4.75c-.878 0-1.688.354-2.457.784
                             -.735.41-1.353.94-1.854 1.572
                             -.497.625-.873 1.342-1.124
                             2.144-.25.808-.372 1.68
                             -.372 2.616 0 .666.126
                             1.298.375 1.879.248.568.618
                             1.107 1.582.497.47
                             1.127.865 1.87 1.171z"/>
                    <path fill-rule="evenodd"
                          d="M8 15A7 7 0 1 0
                             8 1a7 7 0 0
                             0 0 14zM8
                             2A6 6 0
                             1 1 8 14 6
                             6 0 0 1 8 2z"/>
                  </svg>
                  Generate AI Summary
                </button>
              </form>
            """
        else:
            ai_analysis_button_html = '<span style="color: var(--atlas-green); font-weight:600;">Analysis Complete</span>'

        context = {
            "request": request_context,
            "workout_id": workout_id,
            "b64_combined": b64_combined,
            "b64_chart_hr": b64_chart_hr,
            "b64_chart_cal": b64_chart_cal,
            "b64_chart_speed": b64_chart_speed,
            "b64_r": b64_r, "b64_g": b64_g, "b64_b": b64_b,
            "json_data_pretty": doc_json,
            "ai_neighbors_html": neighbors_html,
            "ai_classification": ai_class,
            "ai_summary": ai_sum,
            "llm_analysis_prompt": ephemeral_prompt,
            "ai_analysis_button_html": ai_analysis_button_html,
            "norm_bounds_hr": f"{self.engine.NORM_BOUNDS['heart_rate'][0]}-{self.engine.NORM_BOUNDS['heart_rate'][1]}bpm",
            "norm_bounds_cal": f"{self.engine.NORM_BOUNDS['calories_per_min'][0]}-{self.engine.NORM_BOUNDS['calories_per_min'][1]}/min",
            "norm_bounds_speed": f"{self.engine.NORM_BOUNDS['speed_kph'][0]}-{self.engine.NORM_BOUNDS['speed_kph'][1]}kph",
            "workout_type": doc.get("workout_type", "N/A"),
            "session_tag": doc.get("session_tag", "N/A"),
            "gear_used_html": gear_used_html,
            "sets_reps_html": "", 
            "cycling_html": "", 
            "yoga_html": "",
            "post_session_notes_html": f"<code>{json.dumps(doc.get('post_session_notes', {}))}</code>",
            "vector_index_name": self.vector_index_name,
        }
        
        response = self.templates.TemplateResponse("detail.html", context)
        return response.body.decode("utf-8")

    # --- Method 3: Replaces analyze_workout ---
    async def run_analysis(self, workout_id: int) -> bool:
        self._check_ready()
        
        doc_id = f"workout_rad_{workout_id}"
        doc = await self.db.workouts.find_one({"_id": doc_id})
        if not doc:
            logger.error(f"[{self.write_scope}-Actor] run_analysis: Doc {doc_id} not found.")
            return False

        neighbors = []
        if "workout_vector" in doc and isinstance(doc["workout_vector"], list):
            pipeline = [
                {"$vectorSearch": {"index": self.vector_index_name, "path": "workout_vector", "queryVector": doc["workout_vector"], "numCandidates": 50, "limit": 3, "filter": {"_id": {"$ne": doc_id}}}},
                {"$project": {"_id":1, "score":{"$meta":"vectorSearchScore"}, "workout_type":1, "session_tag":1, "ai_classification":1}}
            ]
            try:
                cur = self.db.workouts.aggregate(pipeline)
                neighbors = await cur.to_list(None)
            except Exception as e:
                logger.error(f"[{self.write_scope}-Actor] Vector search error during final analysis: {e}")

        final_class, final_prompt = self.engine.analyze_time_series_features(doc, neighbors)
        summary = await self.engine.call_openai_api(final_prompt)

        await self.db.workouts.update_one(
            {"_id": doc_id},
            {"$set": {"ai_classification": final_class, "ai_summary": summary, "llm_analysis_prompt": final_prompt}}
        )
        logger.info(f"[{self.write_scope}-Actor] Analysis complete for {doc_id}.")
        return True

    # --- Method 4: generate_one (Now uses actor's scoped DB) ---
    async def generate_one(self) -> int:
        self._check_ready()

        # This pipeline now correctly runs on the *scoped* collection
        pipeline = [
            {"$match": {"_id": {"$regex": "^workout_rad_\\d+$"}}},
            {"$project": {"num": {"$toInt": {"$arrayElemAt": [{"$split": ["$_id","_"]}, -1]}}}},
            {"$group": {"_id": None, "max_id": {"$max":"$num"}}},
        ]
        result_list = await self.db.workouts.aggregate(pipeline).to_list(1)
        max_id = result_list[0]["max_id"] if result_list and 'max_id' in result_list[0] else 0
        new_suffix = max_id + 1

        for attempt in range(5):
            doc = self.engine.create_synthetic_apple_watch_data(new_suffix)
            feature_vec = self.engine.get_feature_vector(doc)
            doc["workout_vector"] = feature_vec.tolist()
            try:
                # This insert now correctly uses the scoped DB wrapper
                await self.db.workouts.insert_one(doc)
                logger.info(f"[{self.write_scope}-Actor] Inserted new doc {doc['_id']}")
                return new_suffix # Return the successful ID
            except self.DuplicateKeyError:
                logger.warning(f"[{self.write_scope}-Actor] Collision on doc {doc['_id']}. Retrying...")
                new_suffix += 1
        
        logger.error(f"[{self.write_scope}-Actor] Could not generate new doc after 5 collisions.")
        raise Exception("Actor could not generate new doc after multiple collisions.")

    # --- Method 5: Replaces clear_all ---
    async def clear_all(self) -> dict:
        self._check_ready()
        result = await self.db.workouts.delete_many({})
        logger.info(f"[{self.write_scope}-Actor] Cleared {result.deleted_count} documents.")
        return {"deleted_count": result.deleted_count}