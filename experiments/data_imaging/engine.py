import logging
import os
import io
import base64
import httpx
import numpy as np
import matplotlib
matplotlib.use("Agg")  # Set backend for non-GUI server
import matplotlib.pyplot as plt
from PIL import Image
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- Placeholders ---
PLACEHOLDER_CLASSIFICATION = "Pending Analysis"
PLACEHOLDER_SUMMARY = "Click 'Generate AI Summary' to analyze"
PLACEHOLDER_PROMPT = "(not yet generated)"

# --- Normalization Bounds ---
NORM_BOUNDS = {
    "heart_rate": (50, 200),
    "calories_per_min": (0, 20),
    "speed_kph": (0, 15),
}

# --- Functions ---

async def call_openai_api(prompt: str) -> str:
    """
    Calls the OpenAI Chat Completion endpoint.
    """
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
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()  # Raise an exception for 4xx or 5xx statuses
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API error: {e.response.status_code} - {e.response.text}")
        return f"ERROR: OpenAI API returned status {e.response.status_code}. Check server logs."
    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}")
        return f"ERROR: Could not connect to OpenAI API. Details: {e}"


def analyze_time_series_features(doc: dict, neighbors: list[dict]) -> tuple[str, str]:
    """
    Computes a classification label and a prompt for the doc.
    """
    try:
        hr = np.array(doc["time_series"]["heart_rate"], dtype=float)
        cal = np.array(doc["time_series"]["calories_per_min"], dtype=float)
        spd = np.array(doc["time_series"]["speed_kph"], dtype=float)
    except (KeyError, TypeError):
        return ("Malformed Data", PLACEHOLDER_PROMPT)

    hr_avg = float(np.mean(hr))
    hr_max = float(np.max(hr))
    cal_sum = float(np.sum(cal))
    spd_std = float(np.std(spd))

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


def create_synthetic_apple_watch_data(suffix: int) -> dict:
    """
    Generates synthetic data with random variations.
    """
    np.random.seed(suffix)
    t = np.linspace(0, 2 * np.pi, 64)

    hr_base = 100 + (suffix % 7)*5
    cal_base = 5 + (suffix % 5)*1
    speed_base = 3.5 + (suffix % 6)*0.5

    hr_array = hr_base + 60*np.sin(t + np.random.rand()*0.5) + np.random.rand(64)*10
    hr_array[:5] *= 0.8
    hr_array[-5:] *= 0.9

    cal_array = cal_base + 4*np.sin(t + np.random.rand()*0.3) + np.random.rand(64)*2

    spd_array = np.full(64, speed_base) + np.random.rand(64)*0.4
    spd_array[:5] = 2.0 + np.random.rand(5)*0.4
    spd_array[-5:] = 1.2 + np.random.rand(5)*0.3

    doc_id = f"workout_rad_{suffix}"
    return {
        "_id": doc_id,
        "time_series": {
            "heart_rate": list(np.round(np.maximum(hr_array, NORM_BOUNDS["heart_rate"][0]), 2)),
            "calories_per_min": list(np.round(np.maximum(cal_array, NORM_BOUNDS["calories_per_min"][0]), 2)),
            "speed_kph": list(np.round(np.maximum(spd_array, NORM_BOUNDS["speed_kph"][0]), 2)),
        },
        "start_time": datetime(2025, 10, 27, 10, 10 + (suffix % 40), 0, tzinfo=timezone.utc),
        "workout_type": np.random.choice(["Outdoor Run", "Cycling", "Strength", "Yoga"]),
        "session_tag": np.random.choice(["Race Day", "Recovery", "Z2 Cardio", "Tempo Pace", "Threshold"]),
        "post_session_notes": {
            "hydration_ml": int(np.random.randint(500, 2500)),
            "notes": np.random.choice(["Felt good", "Legs sore", "Pushed harder", "Casual run"]),
        },
        "gear_used": [
            {"item": "shoes_v3", "kilometers": float(np.random.randint(50, 200))},
            {"item": "hrm_strap", "battery_life_percent": int(np.random.randint(10, 100))},
        ],
        "ai_classification": PLACEHOLDER_CLASSIFICATION,
        "ai_summary": PLACEHOLDER_SUMMARY,
        "llm_analysis_prompt": PLACEHOLDER_PROMPT,
    }


def _norm_array(x, lo, hi):
    """
    Clips and normalizes a NumPy array to 0-255 uint8.
    """
    x_clipped = np.clip(x, lo, hi)
    rng = hi - lo
    if rng <= 0:
        return np.zeros_like(x_clipped, dtype=np.uint8)
    return ((x_clipped - lo) / rng * 255).astype(np.uint8)


def generate_workout_viz_arrays(doc: dict, size=8):
    """Generates the normalized 8x8x3 RGB array plus raw arrays."""
    raw_hr = doc.get("time_series", {}).get("heart_rate", [0]*64)
    raw_cal = doc.get("time_series", {}).get("calories_per_min", [0]*64)
    raw_spd = doc.get("time_series", {}).get("speed_kph", [0]*64)

    raw_hr = np.array(raw_hr, dtype=float)
    raw_cal = np.array(raw_cal, dtype=float)
    raw_spd = np.array(raw_spd, dtype=float)

    r_1d = _norm_array(raw_hr, *NORM_BOUNDS["heart_rate"])
    g_1d = _norm_array(raw_cal, *NORM_BOUNDS["calories_per_min"])
    b_1d = _norm_array(raw_spd, *NORM_BOUNDS["speed_kph"])

    r_2d = r_1d.reshape(size, size)
    g_2d = g_1d.reshape(size, size)
    b_2d = b_1d.reshape(size, size)

    rgb = np.stack([r_2d, g_2d, b_2d], axis=-1)
    return {
        "rgb_combined": rgb,
        "raw_hr": raw_hr,
        "raw_cal": raw_cal,
        "raw_speed": raw_spd,
        "channel_r_2d": r_2d,
        "channel_g_2d": g_2d,
        "channel_b_2d": b_2d,
    }


def get_feature_vector(doc: dict):
    """
    Flattens the 8x8x3 RGB array (192 values) into one vector.
    """
    arrays = generate_workout_viz_arrays(doc, size=8)
    return arrays["rgb_combined"].reshape(-1)  # returns a NumPy array


def encode_png_b64(
    img_array,
    size=(128, 128),
    tint_color=None
) -> str:
    """
    Encodes a NumPy array to a Base64 PNG.
    """
    error_placeholder = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42"
        "mNkYAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    try:
        # If 2D and we have a tint, produce a tinted RGB image
        if tint_color is not None and img_array.ndim == 2:
            colored_array = np.zeros((*img_array.shape, 3), dtype=np.uint8)
            for i in range(3):
                if tint_color[i] > 0:
                    colored_array[..., i] = (
                        img_array.astype(float) * tint_color[i] / 255
                    ).astype(np.uint8)
            img = Image.fromarray(colored_array, "RGB")
        elif img_array.ndim == 2:
            img = Image.fromarray(img_array, "L")
        else:
            img = Image.fromarray(img_array, "RGB")

        if size:
            img = img.resize(size, Image.NEAREST)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"Image encoding failed: {e}")
        return error_placeholder


def generate_chart_base64(data, color="#FF6868") -> str:
    """
    Generates a Matplotlib line chart from a 1D list or array, returns base64 PNG.
    """
    arr = np.array(data, dtype=float)
    
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(4.5, 2.5), dpi=100)
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
        plt.close(fig)
        plt.style.use("default")