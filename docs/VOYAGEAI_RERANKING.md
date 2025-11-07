# VoyageAI Reranking: Sight vs. Reading

## Overview

This document explains the two-stage retrieval pipeline used in the Workout Analyzer experiment, which combines **structural pattern matching** (pixel encoding) with **semantic meaning matching** (VoyageAI reranking).

The simplest way to think about it is **Sight vs. Reading**.

Your original pixel encoding is a brilliant **structural pattern matcher** (Sight). The VoyageAI Reranker is a **semantic meaning matcher** (Reading).

They do two completely different but highly complementary jobs.

---

## 1. Your 8x8 Pixel Encoding (The "Sight") 👁️

### What it Analyzes
**Numbers.** Specifically, the 192 values in your `workout_vector` (which come from the 8x8x3 pixel grid).

### What it Understands
**Structure and Shape.** It's like an AI looking at the *shape* of your 8x8 image. It can find other images that are also "bright in the top-left," "have jagged horizontal stripes," or are "a smooth, dark blue."

### What it's Good At
Finding workouts that are *mathematically* or *structurally* similar. It's incredibly fast at finding other workouts with the same "rhythm" (e.g., an interval pattern vs. a steady-state pattern).

### What it *Can't* Do
It has **zero understanding** of what a "Tempo Run" *means*. It doesn't know what "felt strong" implies. It only sees the pixels.

> **Example:** Your pixel vector could find two workouts with a high, spiky heart rate pattern.
> 
> - One is an "Interval Run."
> - The other is a "Factory Machine with sensor glitches."
> 
> To your pixel vector, they *look* identical.

---

## 2. The VoyageAI Reranker (The "Reading") 📚

### What it Analyzes
**Text.** Specifically, the strings we created with `_get_doc_as_rerank_string()` (e.g., "Type: Outdoor Run. Tag: Tempo Pace. Notes: Felt strong. Classification: High-Intensity.").

### What it Understands
**Meaning and Context.** It has been trained on billions of sentences. It knows that "Tempo Pace" is related to "High-Intensity" and that "Felt strong" is a positive human sentiment.

### What it's Good At
Understanding *nuance*. It knows that "Recovery Run" and "Easy Jog" are very similar, even though the words are different. It knows "Interval Run" and "Factory Machine" are *completely different concepts*, even if their sensor data (the pixels) look the same.

### What it *Can't* Do (Relatively)
It would be *very slow* to read every single document in your entire database (millions of docs). It's smart, but not built for that kind of speed.

---

## 3. Why We Use Both: The "Fast Sieve" + "Smart Filter"

This is the magic of the pipeline we just built.

### Stage 1: Fast Sieve (Your Pixel Vector)

Your pixel vector is a high-speed "candidate generator." We ask MongoDB: "Quick, find me 25 workouts that *look* like this one!" MongoDB does this in milliseconds because it's just comparing 192-number lists.

### Stage 2: Smart Filter (VoyageAI Reranker)

The reranker takes that "dumb" list of 25 *look-alikes*. It then *slowly and carefully reads the text* for each one. It's the "smart assistant" who says:

> "Okay, of these 25 workouts that *look* the same, I've read their notes and tags. Here are the 3 that are *actually* 'Interval Runs,' not 'Data Glitches' or 'Stress Events.'"

---

## Summary Table

| Feature | Your 8x8 Pixel Encoding | VoyageAI Reranker |
| :--- | :--- | :--- |
| **What it Analyzes** | `List[float]` (192 numbers) | `str` (Textual descriptions) |
| **What it Understands** | **Structural Patterns** & Shapes | **Semantic Meaning** & Context |
| **Primary Job** | **Retrieval** (Fast Search) | **Ranking** (Smart Scoring) |
| **Analogy** | **Sight** (Finds similar shapes) | **Reading** (Finds similar meanings) |
| **Role in Pipeline** | Finds 25 "look-alikes" | Finds the 3 "best matches" from that list |
| **Speed** | ⚡ Milliseconds | 🐢 Seconds (but only on 25 docs) |
| **Accuracy** | Good for structural similarity | Excellent for semantic similarity |

---

## Implementation Details

### How the Pipeline Works

1. **Broad Vector Search (MongoDB)**
   - Query: Find 25 workouts with similar `workout_vector` (192-dim pixel encoding)
   - Speed: ~10-50ms
   - Result: 25 candidates ranked by cosine similarity

2. **Semantic Reranking (VoyageAI)**
   - Input: 25 candidates + source workout text descriptions
   - Process: VoyageAI reads and understands the semantic meaning
   - Output: Top 3 most semantically similar workouts
   - Speed: ~200-500ms (only on 25 docs, not millions)

3. **Final Result**
   - The 3 workouts that are both *structurally* similar (pixel match) AND *semantically* similar (meaning match)

### Code Location

- **Actor Method**: `experiments/data_imaging/actor.py::get_neighbors_ab_test()`
- **API Endpoint**: `/workout/{workout_id}/ab-test-neighbors`
- **UI Component**: A/B Testing Modal in `detail.html`

### A/B Testing

You can compare results with and without VoyageAI reranking using the A/B Testing modal:

- **With VoyageAI**: Two-stage pipeline (vector search → reranking)
- **Without VoyageAI**: Single-stage pipeline (vector search only)

The modal shows side-by-side comparison with visual highlighting:
- 🟢 **Green**: Workouts appearing in both results
- 🟣 **Purple**: Workouts unique to VoyageAI reranking
- 🟠 **Orange**: Workouts only in vector-only search

---

## Key Takeaway

So, your pixel hack finds the *shape*; the Voyage reranker finds the *meaning* inside that shape. Together, you get the best of both worlds: **speed** and **semantic accuracy**.

---

## Related Documentation

- [DATA_IMAGING_EXPERIMENT.md](./DATA_IMAGING_EXPERIMENT.md) - Overview of the workout analyzer experiment
- [HYBRID_SEARCH_EXAMPLE.md](./HYBRID_SEARCH_EXAMPLE.md) - Combining vector and text search

