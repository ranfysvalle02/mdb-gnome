# File: /app/experiments/data_imaging/actor.py

import logging
import json
import pathlib
import asyncio
from typing import List, Dict, Any
import os
import io
import base64
from datetime import datetime, timezone
import ray

# --- NOTE: ALL HEAVY IMPORTS ARE GONE FROM THE TOP LEVEL ---
# (numpy, matplotlib, httpx, PIL)

# Actor-local paths
experiment_dir = pathlib.Path(__file__).parent
templates_dir = experiment_dir / "templates"

logger = logging.getLogger(__name__)

# --- Constants are still fine at the top level ---
PLACEHOLDER_CLASSIFICATION = "Pending Analysis"
PLACEHOLDER_SUMMARY = "Click 'Generate AI Summary' to analyze"
PLACEHOLDER_PROMPT = "(not yet generated)"
AVAILABLE_METRICS = [
    "heart_rate",
    "calories_per_min",
    "speed_kph",
    "power",
    "cadence"
]
NORM_BOUNDS = {
    "heart_rate": (50, 200),
    "calories_per_min": (0, 20),
    "speed_kph": (0, 15),
    "power": (0, 400),
    "cadence": (0, 120),
}


@ray.remote
class ExperimentActor:
    """
    This is the "Headless Server". It runs in a separate, isolated
    Ray worker process with all the heavy dependencies.
    main.py is responsible for decorating and launching this class.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: list[str]):
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        
        # Critical fix: Ensure write_scope is in read_scopes so we can read what we write!
        # Without this, count_documents({}) will return 0 even if documents exist!
        if write_scope not in read_scopes:
            logger.warning(f"[{write_scope}-Actor] ⚠️ WARNING: write_scope '{write_scope}' not in read_scopes {read_scopes}! "
                         f"Adding it to prevent data visibility issues.")
            self.read_scopes = list(read_scopes) + [write_scope]
        
        self.vector_index_name = f"{write_scope}_workout_vector_index"
        
        # Lazy-load heavy dependencies (experiment-specific)
        try:
            import httpx
            import numpy
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot
            from PIL import Image
            from fastapi.templating import Jinja2Templates
            from pymongo.errors import OperationFailure
            
            self.httpx = httpx
            self.np = numpy
            self.plt = matplotlib.pyplot
            self.Image = Image
            self.OperationFailure = OperationFailure
            
            if templates_dir.is_dir():
                self.templates = Jinja2Templates(directory=str(templates_dir))
            else:
                self.templates = None
                logger.warning(f"[{write_scope}-Actor] Template dir not found at {templates_dir}")
            
            logger.info(f"[{write_scope}-Actor] Successfully loaded heavy dependencies.")
        except ImportError as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to load dependencies: {e}", exc_info=True)
            self.httpx = None
            self.np = None
            self.plt = None
            self.Image = None
            self.OperationFailure = None
            self.templates = None
        
        # Database initialization (follows pattern from other experiments)
        try:
            from experiment_db import create_actor_database
            # Use self.read_scopes which may have been updated to include write_scope
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                self.read_scopes  # Use updated read_scopes that includes write_scope
            )
            logger.info(
                f"[{write_scope}-Actor] initialized with write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None

    # ============================================================================
    # Engine module code (Now as private methods, using self.np, self.plt etc.)
    # ============================================================================

    async def _call_openai_api(self, prompt: str) -> str:
        """Calls the OpenAI Chat Completion endpoint."""
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
        if not OPENAI_API_KEY:
            logger.error("OpenAI API key not set (OPENAI_API_KEY). Returning error message.")
            return "ERROR: OpenAI API key (OPENAI_API_KEY) is not set in the server environment."

        system_prompt = (
            "You are a professional Workout Radiologist. Your job is to synthesize the provided data "
            "into a concise, qualitative summary (max 3 sentences)."
        )
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 100,
        }
        
        try:
            # Use self.httpx
            async with self.httpx.AsyncClient(timeout=30) as client:
                resp = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except self.httpx.HTTPStatusError as e:
            logger.error(f"OpenAI API error: {e.response.status_code} - {e.response.text}")
            return f"ERROR: OpenAI API returned status {e.response.status_code}. Check server logs."
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            return f"ERROR: Could not connect to OpenAI API. Details: {e}"


    def _analyze_time_series_features(self, doc: dict, neighbors: list[dict]) -> tuple[str, str]:
        """Computes a classification label and a prompt for the doc."""
        try:
            # Use self.np
            hr = self.np.array(doc["time_series"]["heart_rate"], dtype=float)
            cal = self.np.array(doc["time_series"]["calories_per_min"], dtype=float)
            spd = self.np.array(doc["time_series"]["speed_kph"], dtype=float)
        except (KeyError, TypeError):
            return ("Malformed Data", PLACEHOLDER_PROMPT)

        # Use self.np
        hr_avg = float(self.np.mean(hr))
        hr_max = float(self.np.max(hr))
        cal_sum = float(self.np.sum(cal))
        spd_std = float(self.np.std(spd))

        # Simple classification logic
        if spd_std > 2.5 and hr_max > 180:
            classification = "High Intensity Interval"
        elif spd_std < 1.0 and hr_avg > 130:
            classification = "Steady Aerobic"
        else:
            classification = "Mixed/Variable"

        # Summarize neighbors for the prompt
        lines = []
        if neighbors:
            for i, n in enumerate(neighbors):
                sid = n["_id"].split("_")[-1]
                score = n.get("score", 0)
                wtype = n.get("workout_type", "?")
                lines.append(f"- Neighbor {i+1}: id=#{sid}, Score={score:.4f}, Type={wtype}")
        else:
            lines.append("- No neighbors found or only one doc in DB.")

        neighbor_text = "\n".join(lines)
        prompt = f"""Workout ID: {doc.get('_id','?')}
[Time-Series Stats]
- HR Avg={hr_avg:.1f}, HR Max={hr_max:.1f}
- Total Calories={cal_sum:.1f}
- Speed StdDev={spd_std:.2f}

[Neighbors]
{neighbor_text}

**Please provide a final radiologist summary (<=3 sentences) highlighting anomalies or conflicts.**
"""
        return classification, prompt


    def _create_synthetic_apple_watch_data(self, suffix: int) -> dict:
        """Generates synthetic data with random variations."""
        # Use self.np
        self.np.random.seed(suffix)
        t = self.np.linspace(0, 2 * self.np.pi, 64)

        hr_base = 100 + (suffix % 7)*5
        cal_base = 5 + (suffix % 5)*1
        speed_base = 3.5 + (suffix % 6)*0.5

        hr_array = hr_base + 60*self.np.sin(t + self.np.random.rand()*0.5) + self.np.random.rand(64)*10
        hr_array[:5] *= 0.8
        hr_array[-5:] *= 0.9

        cal_array = cal_base + 4*self.np.sin(t + self.np.random.rand()*0.3) + self.np.random.rand(64)*2

        spd_array = self.np.full(64, speed_base) + self.np.random.rand(64)*0.4
        spd_array[:5] = 2.0 + self.np.random.rand(5)*0.4
        spd_array[-5:] = 1.2 + self.np.random.rand(5)*0.3

        power_base = 150 + (suffix % 8) * 10
        power_array = power_base + 50 * self.np.sin(t + self.np.random.rand() * 0.7) + self.np.random.rand(64) * 15
        power_array[power_array < 0] = 0

        cadence_base = 80 + (suffix % 4) * 5
        cadence_array = self.np.full(64, cadence_base) + self.np.random.rand(64) * 3
        cadence_array[10:15] = 0 
        cadence_array[40:45] = 0

        doc_id = f"workout_rad_{suffix}"
        return {
            "_id": doc_id,
            "time_series": {
                # Use self.np
                "heart_rate": list(self.np.round(self.np.maximum(hr_array, NORM_BOUNDS["heart_rate"][0]), 2)),
                "calories_per_min": list(self.np.round(self.np.maximum(cal_array, NORM_BOUNDS["calories_per_min"][0]), 2)),
                "speed_kph": list(self.np.round(self.np.maximum(spd_array, NORM_BOUNDS["speed_kph"][0]), 2)),
                "power": list(self.np.round(self.np.maximum(power_array, NORM_BOUNDS["power"][0]), 2)),
                "cadence": list(self.np.round(self.np.maximum(cadence_array, NORM_BOUNDS["cadence"][0]), 2)),
            },
            "start_time": datetime(2025, 10, 27, 10, 10 + (suffix % 40), 0, tzinfo=timezone.utc),
            "workout_type": self.np.random.choice(["Outdoor Run", "Cycling", "Strength", "Yoga"]),
            "session_tag": self.np.random.choice(["Race Day", "Recovery", "Z2 Cardio", "Tempo Pace", "Threshold"]),
            "post_session_notes": {
                "hydration_ml": int(self.np.random.randint(500, 2500)),
                "notes": self.np.random.choice(["Felt good", "Legs sore", "Pushed harder", "Casual run"]),
            },
            "gear_used": [
                {"item": "shoes_v3", "kilometers": float(self.np.random.randint(50, 200))},
                {"item": "hrm_strap", "battery_life_percent": int(self.np.random.randint(10, 100))},
            ],
            "ai_classification": PLACEHOLDER_CLASSIFICATION,
            "ai_summary": PLACEHOLDER_SUMMARY,
            "llm_analysis_prompt": PLACEHOLDER_PROMPT,
        }


    def _norm_array(self, x, lo, hi):
        """Clips and normalizes a NumPy array to 0-255 uint8."""
        # Use self.np
        x_clipped = self.np.clip(x, lo, hi)
        rng = hi - lo
        if rng <= 0:
            return self.np.zeros_like(x_clipped, dtype=self.np.uint8)
        return ((x_clipped - lo) / rng * 255).astype(self.np.uint8)


    def _generate_workout_viz_arrays(
        self, 
        doc: dict, 
        size=8, 
        r_key: str = "heart_rate", 
        g_key: str = "calories_per_min", 
        b_key: str = "speed_kph"
    ):
        """Generates the normalized 8x8x3 RGB array plus raw arrays."""
        
        def get_raw_data(key):
            return doc.get("time_series", {}).get(key, [0]*64)

        # Use self.np
        raw_data = {key: self.np.array(get_raw_data(key), dtype=float) for key in AVAILABLE_METRICS}
        
        r_bounds = NORM_BOUNDS.get(r_key, (0, 1))
        g_bounds = NORM_BOUNDS.get(g_key, (0, 1))
        b_bounds = NORM_BOUNDS.get(b_key, (0, 1))

        # Use self.np
        r_1d = self._norm_array(raw_data.get(r_key, self.np.zeros(64)), *r_bounds)
        g_1d = self._norm_array(raw_data.get(g_key, self.np.zeros(64)), *g_bounds)
        b_1d = self._norm_array(raw_data.get(b_key, self.np.zeros(64)), *b_bounds)

        r_2d = r_1d.reshape(size, size)
        g_2d = g_1d.reshape(size, size)
        b_2d = b_1d.reshape(size, size)

        # Use self.np
        rgb = self.np.stack([r_2d, g_2d, b_2d], axis=-1)
        
        return {
            "rgb_combined": rgb,
            "raw_data": raw_data, 
            "channel_r_2d": r_2d,
            "channel_g_2d": g_2d,
            "channel_b_2d": b_2d,
            "selected_keys": {"r": r_key, "g": g_key, "b": b_key},
            "selected_bounds": {"r": r_bounds, "g": g_bounds, "b": b_bounds}
        }


    def _get_feature_vector(self, doc: dict):
        """Flattens the 8x8x3 RGB array (192 values) into one vector."""
        arrays = self._generate_workout_viz_arrays(
            doc, 
            size=8,
            r_key="heart_rate",
            g_key="calories_per_min",
            b_key="speed_kph"
        )
        return arrays["rgb_combined"].reshape(-1)

    def _get_feature_vector_custom(self, doc: dict, r_key: str, g_key: str, b_key: str):
        """
        Generates a 192-element vector using custom RGB channel assignments.
        This allows finding similar workouts from different perspectives.
        """
        arrays = self._generate_workout_viz_arrays(
            doc,
            size=8,
            r_key=r_key,
            g_key=g_key,
            b_key=b_key
        )
        return arrays["rgb_combined"].reshape(-1)


    def _encode_png_b64(
        self,
        img_array,
        size=(128, 128),
        tint_color=None
    ) -> str:
        """Encodes a NumPy array to a Base64 PNG."""
        error_placeholder = (
            "iVBORw0KGgoAAAANSUEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42"
            "mNkYAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
        try:
            # Use self.np and self.Image
            if tint_color is not None and img_array.ndim == 2:
                colored_array = self.np.zeros((*img_array.shape, 3), dtype=self.np.uint8)
                for i in range(3):
                    if tint_color[i] > 0:
                        colored_array[..., i] = (
                            img_array.astype(float) * tint_color[i] / 255
                        ).astype(self.np.uint8)
                img = self.Image.fromarray(colored_array, "RGB")
            elif img_array.ndim == 2:
                img = self.Image.fromarray(img_array, "L")
            else:
                img = self.Image.fromarray(img_array, "RGB")

            if size:
                img = img.resize(size, self.Image.NEAREST)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.error(f"Image encoding failed: {e}")
            return error_placeholder


    def _generate_chart_base64(self, data, color="#FF6868") -> str:
        """Generates a Matplotlib line chart from a 1D list or array, returns base64 PNG."""
        # Use self.np and self.plt
        arr = self.np.array(data, dtype=float)
        
        self.plt.style.use("dark_background")
        fig, ax = self.plt.subplots(figsize=(4.5, 2.5), dpi=100)
        fig.patch.set_facecolor("#132A38")
        ax.set_facecolor("#132A38")

        ax.plot(arr, color=color, linewidth=2)
        ax.set_xlim(0, len(arr)-1 if len(arr) > 1 else 1)
        ax.tick_params(axis="x", colors="#A7B6C2")
        ax.tick_params(axis="y", colors="#A7B6C2")
        ax.set_xticks([0, len(arr)-1])
        ax.set_xticklabels(["Start", "End"])
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["bottom"].set_color("#23435B")
        ax.spines["left"].set_color("#23435B")

        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="PNG", facecolor=fig.get_facecolor(), bbox_inches='tight', pad_inches=0.1)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.error(f"Chart generation error: {e}")
            return ""
        finally:
            self.plt.close(fig)
            self.plt.style.use("default")

    # ============================================================================
    # End of Engine code
    # ============================================================================


    async def initialize(self):
        """
        Post-initialization hook: waits for vector index to be ready,
        then ensures at least ~10 records exist.
        """
        if not self.db:
            logger.warning(f"[{self.write_scope}-Actor] Skipping initialize - DB not ready.")
            return
        
        logger.info(f"[{self.write_scope}-Actor] Starting post-initialization setup...")
        
        # Wait a bit for vector search index to be ready
        logger.info(f"[{self.write_scope}-Actor] Waiting for vector search index '{self.vector_index_name}' to be ready...")
        await asyncio.sleep(3)
        
        from async_mongo_wrapper import AsyncAtlasIndexManager
        index_manager = AsyncAtlasIndexManager(self.db.database[self.write_scope + "_workouts"])
        max_wait = 30
        wait_interval = 2
        waited = 0
        
        while waited < max_wait:
            try:
                index_info = await index_manager.get_search_index(self.vector_index_name)
                if index_info and index_info.get("queryable"):
                    logger.info(f"[{self.write_scope}-Actor] Vector search index '{self.vector_index_name}' is ready!")
                    break
                elif index_info and index_info.get("status") == "FAILED":
                    logger.error(f"[{self.write_scope}-Actor] Vector search index '{self.vector_index_name}' is in FAILED state!")
                    break
                else:
                    logger.debug(f"[{self.write_scope}-Actor] Index '{self.vector_index_name}' not ready yet, waiting...")
            except Exception as e:
                logger.debug(f"[{self.write_scope}-Actor] Error checking index status: {e}")
            
            await asyncio.sleep(wait_interval)
            waited += wait_interval
        
        if waited >= max_wait:
            logger.warning(f"[{self.write_scope}-Actor] Timeout waiting for index, but continuing...")
        
        try:
            # Verify database connection and collection access before counting
            logger.info(f"[{self.write_scope}-Actor] Verifying database connection and collection access...")
            logger.info(f"[{self.write_scope}-Actor] write_scope='{self.write_scope}', read_scopes={self.read_scopes}")
            
            # First, try to verify we can access the collection by checking for at least one document
            # Use a simple query to test connectivity
            try:
                test_query = await self.db.workouts.find_one({}, {"_id": 1})
                logger.info(f"[{self.write_scope}-Actor] Database connection verified. Test query returned: {test_query is not None}")
            except Exception as test_e:
                logger.error(f"[{self.write_scope}-Actor] Database connection test failed: {test_e}", exc_info=True)
                raise
            
            # Now count documents with explicit logging
            logger.info(f"[{self.write_scope}-Actor] Counting existing workout records (scoped to experiment_id in {self.read_scopes})...")
            count = await self.db.workouts.count_documents({})
            logger.info(f"[{self.write_scope}-Actor] Found {count} existing workout records.")
            
            # Double-check by trying to find at least one document to verify the count is accurate
            if count == 0:
                # Double-check with a find() to make absolutely sure there are no documents
                sample_docs = await self.db.workouts.find({}, {"_id": 1}).limit(1).to_list(length=1)
                if sample_docs:
                    logger.warning(f"[{self.write_scope}-Actor] ⚠️ WARNING: count_documents returned 0, but find() found documents! Count={count}, Found docs: {len(sample_docs)}")
                    logger.warning(f"[{self.write_scope}-Actor] This indicates a potential scoping or counting bug. NOT generating new data.")
                    return
                
                logger.info(f"[{self.write_scope}-Actor] Verified: No records found (count={count}, sample check passed). Generating ~10 sample workout records...")
                NUM_TO_GENERATE = 10
                generated_ids = []
                
                for i in range(NUM_TO_GENERATE):
                    try:
                        # Calls the public generate_one method
                        new_id = await self.generate_one()
                        generated_ids.append(new_id)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.error(f"[{self.write_scope}-Actor] Error generating workout {i+1}/{NUM_TO_GENERATE}: {e}")
                
                logger.info(f"[{self.write_scope}-Actor] Successfully generated {len(generated_ids)} workout records: {generated_ids}")
            else:
                logger.info(f"[{self.write_scope}-Actor] Records already exist (count={count}). Skipping auto-generation.")
                
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error during initialization: {e}", exc_info=True)
        
        logger.info(f"[{self.write_scope}-Actor] Post-initialization setup complete.")

    def __del__(self):
        pass

    def _check_ready(self):
        """Check if actor is ready (follows pattern from other experiments)."""
        if not self.db:
            raise RuntimeError("Database not initialized. Check logs for import errors.")
        if not self.templates:
            raise RuntimeError("Templates not loaded. Check logs for import errors.")
        if not self.np or not self.plt or not self.httpx or not self.Image:
            raise RuntimeError("Heavy dependencies not loaded. Check logs for import errors.")

    # ---
    # --- Refactored helper for visualization data (OPTIMIZED)
    # ---
    async def _generate_viz_data(self, doc: dict, r_key: str, g_key: str, b_key: str) -> dict:
        """
        Generates all B64 images and labels for the selected keys.
        Returns a JSON-serializable dictionary.
        """
        self._check_ready()
        
        # --- Must call internal method ---
        arrays = self._generate_workout_viz_arrays(
            doc, 
            size=8,
            r_key=r_key,
            g_key=g_key,
            b_key=b_key
        )
        
        # --- Must call internal method ---
        b64_combined = self._encode_png_b64(arrays["rgb_combined"], (256,256))
        b64_r = self._encode_png_b64(arrays["channel_r_2d"], (128,128), tint_color=(255,0,0))
        b64_g = self._encode_png_b64(arrays["channel_g_2d"], (128,128), tint_color=(0,255,0))
        b64_b = self._encode_png_b64(arrays["channel_b_2d"], (128,128), tint_color=(0,0,255))

        # Helper to format labels
        def format_label(key_char: str, key: str) -> str:
            title = key.replace('_', ' ').title()
            bounds = NORM_BOUNDS.get(key, ['?','?'])
            return f"<b>{key_char.upper()}:</b> {title} ({bounds[0]}-{bounds[1]})"
            
        def format_short_label(key: str, color_class: str, channel_name: str) -> str:
            title = key.replace('_', ' ').title()
            return f"<strong>{title}</strong> data provides the pixel values for the <strong class=\"{color_class}\">{channel_name} channel</strong>."

        # Convert NumPy arrays to lists for JSON serialization
        raw_data_serializable = {
            key: arr.tolist() if hasattr(arr, 'tolist') else arr 
            for key, arr in arrays["raw_data"].items()
        }
        
        return {
            "b64_combined": b64_combined,
            "b64_r": b64_r,
            "b64_g": b64_g,
            "b64_b": b64_b,
            "label_r_full_html": format_label("r", r_key),
            "label_g_full_html": format_label("g", g_key),
            "label_b_full_html": format_label("b", b_key),
            "label_r_short_html": format_short_label(r_key, "red-label", "Red"),
            "label_g_short_html": format_short_label(g_key, "green-label", "Green"),
            "label_b_short_html": format_short_label(b_key, "blue-label", "Blue"),
            "raw_data": raw_data_serializable
        }

    # ---
    # --- Find similar workouts using custom RGB channels (MongoDB Vector Search)
    # ---
    async def find_similar_workouts_custom(
        self, 
        workout_id: int, 
        r_key: str, 
        g_key: str, 
        b_key: str,
        limit: int = 3
    ) -> list[dict]:
        """
        Finds similar workouts using custom RGB channel assignments.
        Intelligently leverages MongoDB Atlas Vector Search:
        1. Generates query vector on-the-fly with custom channels
        2. Uses $vectorSearch aggregation with the dynamically generated query vector
        3. Computes custom channel vectors for candidates and scores in-memory
        
        This approach balances MongoDB's indexed vector search performance with
        the flexibility of custom channel combinations.
        """
        self._check_ready()
        doc_id = f"workout_rad_{workout_id}"
        query_doc = await self.db.workouts.find_one({"_id": doc_id})
        if not query_doc:
            raise RuntimeError(f"Doc {doc_id} not found")
        
        # Generate query vector with custom channels
        query_vector = self._get_feature_vector_custom(query_doc, r_key, g_key, b_key)
        query_vector_list = query_vector.tolist()  # Convert NumPy array to list
        
        # Check if we're using indexed channel combinations (can leverage vector search directly)
        is_canonical = (
            r_key == "heart_rate" and 
            g_key == "calories_per_min" and 
            b_key == "speed_kph"
        )
        is_power_cadence_hr = (
            r_key == "power" and 
            g_key == "cadence" and 
            b_key == "heart_rate"
        )
        is_power_speed_hr = (
            r_key == "power" and 
            g_key == "speed_kph" and 
            b_key == "heart_rate"
        )
        is_speed_cadence_hr = (
            r_key == "speed_kph" and 
            g_key == "cadence" and 
            b_key == "heart_rate"
        )
        
        # Determine which index and vector field to use
        if is_canonical and isinstance(query_doc.get("workout_vector"), list):
            index_name = self.vector_index_name
            vector_field = "workout_vector"
            query_vec = query_doc["workout_vector"]
        elif is_power_cadence_hr and isinstance(query_doc.get("workout_vector_power_cadence_hr"), list):
            index_name = f"{self.write_scope}_workout_vector_power_cadence_hr_index"
            vector_field = "workout_vector_power_cadence_hr"
            query_vec = query_doc["workout_vector_power_cadence_hr"]
        elif is_power_speed_hr and isinstance(query_doc.get("workout_vector_power_speed_hr"), list):
            index_name = f"{self.write_scope}_workout_vector_power_speed_hr_index"
            vector_field = "workout_vector_power_speed_hr"
            query_vec = query_doc["workout_vector_power_speed_hr"]
        elif is_speed_cadence_hr and isinstance(query_doc.get("workout_vector_speed_cadence_hr"), list):
            index_name = f"{self.write_scope}_workout_vector_speed_cadence_hr_index"
            vector_field = "workout_vector_speed_cadence_hr"
            query_vec = query_doc["workout_vector_speed_cadence_hr"]
        else:
            # Not an indexed combination, fall back to hybrid approach
            index_name = None
        
        # Use indexed vector search if we have an indexed combination
        if index_name and query_vec:
            # Use MongoDB Vector Search with indexed vectors
            try:
                pipeline = [
                    {
                        "$vectorSearch": {
                            "index": index_name,
                            "path": vector_field,
                            "queryVector": query_vec,
                            "filter": {"_id": {"$ne": doc_id}},
                            "numCandidates": max(50, limit * 10),
                            "limit": limit * 3
                        }
                    },
                    {
                        "$project": {
                            "_id": 1,
                            "score": {"$meta": "vectorSearchScore"},
                            "workout_type": 1,
                            "session_tag": 1,
                            "ai_classification": 1
                        }
                    }
                ]
                
                cur = self.db.raw.workouts.aggregate(pipeline)
                candidates = await cur.to_list(length=None)
                
                # Vector search scores are valid for indexed combinations
                results = []
                for cand in candidates:
                    suffix = cand["_id"].split("_")[-1]
                    results.append({
                        "_id": cand["_id"],
                        "workout_id": int(suffix),
                        "score": float(cand.get("score", 0.0)),
                        "workout_type": cand.get("workout_type", "?"),
                        "session_tag": cand.get("session_tag"),
                        "ai_classification": cand.get("ai_classification")
                    })
                
                # Already sorted by vector search score, just take top N
                return results[:limit]
                
            except self.OperationFailure as oe:
                err_msg = oe.details.get('errmsg', str(oe))
                logger.warning(f"Vector search failed, falling back to in-memory: {err_msg}")
                # Fall through to in-memory computation
            except Exception as e:
                logger.warning(f"Vector search error, falling back to in-memory: {e}")
                # Fall through to in-memory computation
        
        # For non-canonical channels OR if vector search fails:
        # Use MongoDB to efficiently fetch candidates, then compute custom similarity
        # This leverages MongoDB for filtering/fetching but computes similarity with custom vectors
        try:
            # First, try to get a smart candidate set using canonical vector search
            # as a pre-filter, then re-rank with custom vectors
            if isinstance(query_doc.get("workout_vector"), list):
                # Use canonical vector search as a pre-filter to get likely candidates
                prefilter_pipeline = [
                    {
                        "$vectorSearch": {
                            "index": self.vector_index_name,
                            "path": "workout_vector",
                            "queryVector": query_doc["workout_vector"],
                            "filter": {"_id": {"$ne": doc_id}},
                            "numCandidates": 200,  # Get more candidates
                            "limit": 100  # Pre-filter to top 100
                        }
                    },
                    {
                        "$project": {
                            "_id": 1,
                            "workout_type": 1,
                            "session_tag": 1,
                            "ai_classification": 1,
                            "time_series": 1
                        }
                    }
                ]
                
                cur = self.db.raw.workouts.aggregate(prefilter_pipeline)
                candidate_docs = await cur.to_list(length=None)
                
                # If we got good candidates, use them; otherwise fetch all
                if not candidate_docs or len(candidate_docs) < limit:
                    # Fallback: fetch a reasonable subset
                    candidate_docs = await self.db.workouts.find(
                        {"_id": {"$ne": doc_id}},
                        {"_id": 1, "time_series": 1, "workout_type": 1, "session_tag": 1, "ai_classification": 1}
                    ).limit(500).to_list(length=None)
            else:
                # No canonical vector available, fetch candidates directly
                candidate_docs = await self.db.workouts.find(
                    {"_id": {"$ne": doc_id}},
                    {"_id": 1, "time_series": 1, "workout_type": 1, "session_tag": 1, "ai_classification": 1}
                ).limit(500).to_list(length=None)
            
            if not candidate_docs:
                return []
            
            # Compute cosine similarity with custom channel vectors
            similarities = []
            query_vec_norm = self.np.linalg.norm(query_vector)
            
            for doc in candidate_docs:
                try:
                    # Generate vector for this doc using the same custom channels
                    candidate_vector = self._get_feature_vector_custom(doc, r_key, g_key, b_key)
                    
                    # Compute cosine similarity
                    candidate_vec_norm = self.np.linalg.norm(candidate_vector)
                    if candidate_vec_norm == 0 or query_vec_norm == 0:
                        similarity = 0.0
                    else:
                        dot_product = float(self.np.dot(query_vector, candidate_vector))
                        similarity = dot_product / (query_vec_norm * candidate_vec_norm)
                    
                    # Extract workout ID suffix for display
                    suffix = doc["_id"].split("_")[-1]
                    
                    similarities.append({
                        "_id": doc["_id"],
                        "workout_id": int(suffix),
                        "score": similarity,
                        "workout_type": doc.get("workout_type", "?"),
                        "session_tag": doc.get("session_tag"),
                        "ai_classification": doc.get("ai_classification")
                    })
                except Exception as e:
                    logger.warning(f"Error computing similarity for {doc.get('_id')}: {e}")
                    continue
            
            # Sort by similarity (descending) and return top N
            similarities.sort(key=lambda x: x["score"], reverse=True)
            return similarities[:limit]
            
        except Exception as e:
            logger.error(f"Error in find_similar_workouts_custom: {e}", exc_info=True)
            raise RuntimeError(f"Failed to find similar workouts: {e}")

    # ---
    # --- Endpoint for client-side JS (returns JSON)
    # ---
    async def get_dynamic_viz_data(self, workout_id: int, r_key: str, g_key: str, b_key: str) -> dict:
        """
        Public method to be called by the new /viz API endpoint.
        """
        self._check_ready()
        doc_id = f"workout_rad_{workout_id}"
        doc = await self.db.workouts.find_one({"_id": doc_id})
        if not doc:
            raise RuntimeError(f"Doc {doc_id} not found")
        
        # Call the refactored helper
        return await self._generate_viz_data(doc, r_key, g_key, b_key)

    # --- Method 1: Replaces show_gallery ---
    async def render_gallery_page(self, request_context: dict) -> str:
        self._check_ready()
        try:
            docs = await self.db.workouts.find(
                {}, 
                {"_id": 1, "time_series": 1}
            ).sort("_id", 1).limit(200).to_list(length=None)
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] DB error in render_gallery_page: {e}")
            docs = []

        if not docs:
            snippet_list = ["<p>No workouts present. Click 'Generate' to create some!</p>"]
        else:
            semaphore = asyncio.Semaphore(10)
            
            def _generate_snippet_sync(doc: Dict[str, Any]) -> str:
                """Synchronous helper to generate snippet for a single document."""
                # --- Must call internal methods ---
                arrays = self._generate_workout_viz_arrays(
                    doc, size=8, r_key="heart_rate", g_key="calories_per_min", b_key="speed_kph"
                )
                b64_img = self._encode_png_b64(arrays["rgb_combined"], (128, 128))
                suffix = doc["_id"].split("_")[-1]
                return f"""
                  <div class="collection-item">
                    <a href="./workout/{suffix}">
                      <img src="data:image/png;base64,{b64_img}" alt="Workout {suffix}">
                      <p>Workout #{suffix}</p>
                    </a>
                  </div>
                """
            
            async def _generate_snippet_with_limit(doc: Dict[str, Any]) -> str:
                """Async wrapper that limits concurrency using semaphore."""
                async with semaphore:
                    return await asyncio.to_thread(_generate_snippet_sync, doc)
            
            snippet_tasks = [_generate_snippet_with_limit(d) for d in docs]
            snippet_list = await asyncio.gather(*snippet_tasks)

        response = self.templates.TemplateResponse(
            "index.html",
            {"request": request_context, "collection_images_html": "".join(snippet_list)},
        )
        return response.body.decode("utf-8")

    # --- Method 2: Replaces show_detail (UPDATED + FIXED) ---
    async def render_detail_page(
        self, 
        workout_id: int, 
        request_context: dict, 
        r_key: str, 
        g_key: str, 
        b_key: str
    ) -> str:
        self._check_ready()
        
        doc_id = f"workout_rad_{workout_id}"
        doc = await self.db.workouts.find_one({"_id": doc_id})
        if not doc:
            return f"<h1>404 - Not Found</h1><p>No workout with id {doc_id}</p>"

        viz_data = await self._generate_viz_data(doc, r_key, g_key, b_key)
        raw_data_for_charts = viz_data["raw_data"]

        summary_is_pending = (
            PLACEHOLDER_CLASSIFICATION in doc.get("ai_classification", "") or
            PLACEHOLDER_SUMMARY in doc.get("ai_summary", "")
        )

        neighbors_html = "<p>Vector data is missing, so no neighbors found.</p>"
        neighbors = []
        if isinstance(doc.get("workout_vector"), list) and len(doc["workout_vector"]) == 192:
            pipeline = [
                {"$vectorSearch": {"index": self.vector_index_name, "path": "workout_vector", "queryVector": doc["workout_vector"], "filter": {"_id": {"$ne": doc_id}}, "numCandidates": 50, "limit": 3}},
                {"$project": {"_id": 1, "score": {"$meta": "vectorSearchScore"}, "workout_type": 1, "session_tag": 1, "ai_classification": 1}}
            ]
            try:
                cur = self.db.raw.workouts.aggregate(pipeline)
                neighbors = await cur.to_list(None)
                if neighbors:
                    items = []
                    for n in neighbors:
                        sid = n["_id"].split("_")[-1]
                        context_span = f"Type: {n.get('workout_type','?')}"
                        if n.get("session_tag"): context_span += f" | Tag: {n['session_tag']}"
                        if n.get("ai_classification") != PLACEHOLDER_CLASSIFICATION:
                            context_span += f" | Pattern: {n['ai_classification']}"
                        
                        items.append(f'<li><a href="/experiments/{self.write_scope}/workout/{sid}">Workout #{sid}</a> <span>({context_span})</span><br>Similarity Score: {n["score"]:.4f}</li>')

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
        
        ephemeral_prompt = doc.get("llm_analysis_prompt", PLACEHOLDER_PROMPT)
        ai_class = doc.get("ai_classification", PLACEHOLDER_CLASSIFICATION)
        ai_sum = doc.get("ai_summary", PLACEHOLDER_SUMMARY)

        if summary_is_pending:
            # --- Must call internal method ---
            ephemeral_class, ephemeral_prompt = self._analyze_time_series_features(doc, neighbors)
            ai_class = ephemeral_class
        else:
            ephemeral_prompt = doc.get("llm_analysis_prompt", PLACEHOLDER_PROMPT)

        all_charts = {
            # --- Must call internal method ---
            "heart_rate": self._generate_chart_base64(raw_data_for_charts.get("heart_rate", []), "#FF6868"),
            "calories_per_min": self._generate_chart_base64(raw_data_for_charts.get("calories_per_min", []), "#00ED64"),
            "speed_kph": self._generate_chart_base64(raw_data_for_charts.get("speed_kph", []), "#58AEFF"),
            "power": self._generate_chart_base64(raw_data_for_charts.get("power", []), "#FFA554"),
            "cadence": self._generate_chart_base64(raw_data_for_charts.get("cadence", []), "#C792EA")
        }

        doc_copy = dict(doc)
        if isinstance(doc_copy.get("workout_vector"), list):
            vec_len = len(doc_copy["workout_vector"])
            short_vec = doc_copy["workout_vector"][:5]
            doc_copy["workout_vector"] = f"[{short_vec[0]:.2f}... {vec_len - 1} more elements]"
        doc_json = json.dumps(doc_copy, indent=2, default=str)

        gear_used_html = ""
        if doc.get("gear_used"):
             gear_used_html = "<ul>"
             for g in doc.get("gear_used", []):
                 gear_used_html += f"<li>{json.dumps(g)}</li>"
             gear_used_html += "</ul>"
        
        if summary_is_pending:
            ai_analysis_button_html = f"""
              <form id="analyzeForm" action="/experiments/{self.write_scope}/workout/{workout_id}/analyze" method="POST" style="margin:0;">
                <button type="submit" id="analyzeBtn" class="control-btn" style="background-color:var(--accent-blue);color:white;">
                  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"
                       fill="currentColor" viewBox="0 0 16 16"
                       style="vertical-align:-2px;margin-right:5px;">
                    <path d="M8 15A7 7 0 1 0 8 1a7 7 0 0 0 0 14zm-5.467 4.14C7.02 12.637 7.558 13 8 13c.448 0 .89-.37 1.341-.758.384-.33 1.164-.98 1.956-1.579.529-.396.958-.87 1.253-1.412.308-.567.452-1.217.452-1.921 0-.663-.122-1.284-.367-1.841-.247-.568-.62-1.11-1.12-1.583-.497-.47-1.127-.866-1.87-1.171C9.697 5.093 8.87 4.75 8 4.75c-.878 0-1.688.354-2.457.784-.735.41-1.353.94-1.854 1.572-.497.625-.873 1.342-1.124 2.144-.25.808-.372 1.68-.372 2.616 0 .666.126 1.298.375 1.879.248.568.618 1.107 1.117 1.582.497.47 1.127.865 1.87 1.171z"/>
                    <path fill-rule="evenodd" d="M8 15A7 7 0 1 0 8 1a7 7 0 0 0 0 14zM8 2A6 6 0 1 1 8 14 6 6 0 0 1 8 2z"/>
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
            "all_charts": all_charts,
            "json_data_pretty": doc_json,
            "ai_neighbors_html": neighbors_html,
            "ai_classification": ai_class,
            "ai_summary": ai_sum,
            "llm_analysis_prompt": ephemeral_prompt,
            "ai_analysis_button_html": ai_analysis_button_html,
            "b64_combined": viz_data["b64_combined"],
            "b64_r": viz_data["b64_r"],
            "b64_g": viz_data["b64_g"],
            "b64_b": viz_data["b64_b"],
            "label_r_full_html": viz_data["label_r_full_html"],
            "label_g_full_html": viz_data["label_g_full_html"],
            "label_b_full_html": viz_data["label_b_full_html"],
            "label_r_short_html": viz_data["label_r_short_html"],
            "label_g_short_html": viz_data["label_g_short_html"],
            "label_b_short_html": viz_data["label_b_short_html"],
            "all_metrics": AVAILABLE_METRICS,
            "selected_r_key": r_key,
            "selected_g_key": g_key,
            "selected_b_key": b_key,
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
                cur = self.db.raw.workouts.aggregate(pipeline)
                neighbors = await cur.to_list(None)
            except Exception as e:
                logger.error(f"[{self.write_scope}-Actor] Vector search error during final analysis: {e}")

        # --- Must call internal methods ---
        final_class, final_prompt = self._analyze_time_series_features(doc, neighbors)
        summary = await self._call_openai_api(final_prompt)

        await self.db.workouts.update_one(
            {"_id": doc_id},
            {"$set": {"ai_classification": final_class, "ai_summary": summary, "llm_analysis_prompt": final_prompt}}
        )
        logger.info(f"[{self.write_scope}-Actor] Analysis complete for {doc_id}.")
        return True

    # --- Method 4: generate_one (Now uses actor's scoped DB) ---
    async def generate_one(self) -> int:
        self._check_ready()

        pipeline = [
            {"$match": {"_id": {"$regex": "^workout_rad_\\d+$"}}},
            {"$project": {"num": {"$toInt": {"$arrayElemAt": [{"$split": ["$_id","_"]}, -1]}}}},
            {"$group": {"_id": None, "max_id": {"$max":"$num"}}},
        ]
        result_list = await self.db.raw.workouts.aggregate(pipeline).to_list(1)
        max_id = result_list[0]["max_id"] if result_list and 'max_id' in result_list[0] else -1
        new_suffix = max_id + 1

        for attempt in range(5):
            # --- Must call internal methods ---
            doc = self._create_synthetic_apple_watch_data(new_suffix)
            
            # Generate canonical vector (HR, Calories, Speed)
            feature_vec = self._get_feature_vector(doc)
            doc["workout_vector"] = feature_vec.tolist()
            
            # Generate common custom vectors for indexed combinations
            # Power, Cadence, Heart Rate (cycling-focused)
            vec_power_cadence_hr = self._get_feature_vector_custom(doc, "power", "cadence", "heart_rate")
            doc["workout_vector_power_cadence_hr"] = vec_power_cadence_hr.tolist()
            
            # Power, Speed, Heart Rate (performance-focused)
            vec_power_speed_hr = self._get_feature_vector_custom(doc, "power", "speed_kph", "heart_rate")
            doc["workout_vector_power_speed_hr"] = vec_power_speed_hr.tolist()
            
            # Speed, Cadence, Heart Rate (cycling-specific)
            vec_speed_cadence_hr = self._get_feature_vector_custom(doc, "speed_kph", "cadence", "heart_rate")
            doc["workout_vector_speed_cadence_hr"] = vec_speed_cadence_hr.tolist()
            
            try:
                await self.db.workouts.insert_one(doc)
                logger.info(f"[{self.write_scope}-Actor] Inserted new doc {doc['_id']} with indexed vectors")
                return new_suffix
            except Exception as e:
                if "duplicate" in str(e).lower() or "E11000" in str(e):
                    logger.warning(f"[{self.write_scope}-Actor] Collision on doc {doc['_id']}. Retrying...")
                    new_suffix += 1
                else:
                    raise
        
        logger.error(f"[{self.write_scope}-Actor] Could not generate new doc after 5 collisions.")
        raise Exception("Actor could not generate new doc after multiple collisions.")

    # --- Method 5: Replaces clear_all ---
    async def clear_all(self) -> dict:
        self._check_ready()
        result = await self.db.workouts.delete_many({})
        deleted_count = result.deleted_count
        logger.info(f"[{self.write_scope}-Actor] Cleared {deleted_count} documents.")
        return {"deleted_count": deleted_count}