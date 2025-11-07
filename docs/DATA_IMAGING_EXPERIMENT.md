# Data Imaging Experiment: The Workout Radiologist

## Overview

The **Data Imaging** experiment (also known as "The Workout Radiologist") demonstrates a powerful technique for converting time-series workout data into visual "fingerprints" that can be efficiently searched using vector similarity. This experiment showcases how to transform complex, multi-dimensional sensor data into a searchable format using MongoDB Atlas Vector Search.

## Core Concept: Visual Fingerprints

### The Problem

Traditional time-series data comparison is:
- **Slow**: Algorithms like DTW (Dynamic Time Warping) are computationally expensive
- **Unindexable**: Can't build fast search indexes - every search requires comparing against everything
- **Impractical**: Real-time "find similar patterns" is nearly impossible at scale

### The Solution

Convert time-series data into **visual fingerprints** (vectors) that can be:
- **Indexed**: MongoDB Atlas builds optimized vector indexes (HNSW)
- **Fast**: Finding similar patterns in millions takes milliseconds, not hours
- **Shape-Focused**: Captures the *structure* of the effort, perfect for finding workouts that *feel* the same

## How It Works

### 1. Data Collection

The experiment uses synthetic workout data with multiple time-series metrics:
- **Heart Rate** (HR): 64 data points
- **Calories per Minute**: 64 data points
- **Speed (kph)**: 64 data points
- **Power**: 64 data points
- **Cadence**: 64 data points

### 2. Visual Encoding Pipeline

The encoding process transforms 64-point time-series into an 8x8 pixel image:

```
Time-Series Data (64 points) → Normalize → Reshape to 8x8 → RGB Channels → 192-Element Vector
```

**Step-by-Step:**
1. **Normalize**: Each metric is normalized to 0-255 range based on expected bounds
2. **Reshape**: 64 data points become an 8x8 grid
3. **Channel Assignment**: 
   - **Red Channel**: Heart Rate
   - **Green Channel**: Calories per Minute
   - **Blue Channel**: Speed
4. **Flatten**: 8x8x3 RGB array becomes a 192-element vector

### 3. Vector Search

The 192-element vector is stored in MongoDB and indexed using Atlas Vector Search. When searching for similar workouts:
- MongoDB uses the vector index to find workouts with similar patterns
- Cosine similarity is used to measure similarity
- Results are ranked by similarity score

### 4. Advanced Features

#### Custom Channel Combinations

You can explore different perspectives by changing which metrics map to RGB channels:
- **Default**: HR (R), Calories (G), Speed (B)
- **Cycling Focus**: Power (R), Cadence (G), HR (B)
- **Performance Focus**: Power (R), Speed (G), HR (B)
- **60+ possible combinations** for different similarity perspectives

#### Alpha Channel (RGBA Mode)

The experiment supports a 4th channel (Alpha) for additional data:
- **Fourth Metric**: Encode cadence or power as transparency
- **Data Quality**: Encode sensor reliability (255 = good, 0 = bad/missing)
- **Global RPE**: Encode perceived exertion as a global alpha value

This extends vectors from 192 elements (RGB) to 256 elements (RGBA).

#### Hybrid Search

Combines multiple search strategies:

1. **Vector Search** (Vibe-Based):
   - Field: `workout_vector` (192-element vector)
   - Finds workouts with similar patterns/shapes
   - Fuzzy, semantic similarity

2. **Text Search** (Factual):
   - Fields: `post_session_notes.notes` (weight: 10), `session_tag` (weight: 8), `workout_type` (weight: 5), `ai_classification` (weight: 3)
   - Full-text search across notes, tags, types, and classifications
   - Precise, keyword-based matching

3. **Filters** (Exact Matches):
   - Fields: `workout_type`, `session_tag`
   - Exact match filters applied to both vector and text results

**Example Query:**
> "Show me workouts with a vibe similar to my last 'Tempo Run' (vector search)... but only show me ones where my notes said I 'felt strong' (text search)... and where the workout type was 'Outdoor Run' (filter)."

Results are combined with weighted scoring: **70% vector score + 30% text score**.

## Data Structure

### Workout Document Schema

```json
{
  "_id": "workout_rad_0",
  "time_series": {
    "heart_rate": [120, 125, 130, ...],  // 64 points
    "calories_per_min": [5.2, 5.5, 5.8, ...],  // 64 points
    "speed_kph": [3.5, 3.6, 3.7, ...],  // 64 points
    "power": [150, 155, 160, ...],  // 64 points
    "cadence": [80, 82, 84, ...]  // 64 points
  },
  "workout_vector": [0.45, 0.67, 0.23, ...],  // 192-element vector (RGB)
  "workout_vector_power_cadence_hr": [...],  // Alternative vector combinations
  "data_quality": [255, 255, 0, ...],  // 64 points (255 = good, 0 = bad)
  "rpe": 7.5,  // Rated Perceived Exertion (1-10)
  "start_time": "2025-10-27T10:10:00Z",
  "workout_type": "Outdoor Run",
  "session_tag": "Tempo Pace",
  "post_session_notes": {
    "hydration_ml": 1500,
    "notes": "Felt strong, pushed harder"
  },
  "gear_used": [
    {"item": "shoes_v3", "kilometers": 150},
    {"item": "hrm_strap", "battery_life_percent": 85}
  ],
  "ai_classification": "High Intensity Interval",
  "ai_summary": "This workout shows...",
  "llm_analysis_prompt": "..."
}
```

## Indexes

### Vector Search Indexes

1. **Primary Vector Index**: `workout_vector_index`
   - Path: `workout_vector`
   - Dimensions: 192
   - Similarity: cosine

2. **Alternative Vector Indexes** (for common channel combinations):
   - `workout_vector_power_cadence_hr_index`
   - `workout_vector_power_speed_hr_index`
   - `workout_vector_speed_cadence_hr_index`

### Text Search Index

- **Name**: `workout_text_index`
- **Fields**:
  - `post_session_notes.notes` (weight: 10)
  - `session_tag` (weight: 8)
  - `workout_type` (weight: 5)
  - `ai_classification` (weight: 3)

## Advanced Features

### VoyageAI Reranking

The experiment now includes **VoyageAI reranking** for improved semantic accuracy. This two-stage retrieval pipeline combines:

1. **Fast Vector Search**: MongoDB finds 25 structurally similar workouts (milliseconds)
2. **Semantic Reranking**: VoyageAI finds the 3 most semantically similar from that list (seconds)

This combines the **speed of vector search** with the **deep semantic understanding** of VoyageAI's reranker.

**See**: [VOYAGEAI_RERANKING.md](./VOYAGEAI_RERANKING.md) for a detailed explanation of how pixel encoding (sight) and VoyageAI reranking (reading) work together.

### A/B Testing

You can compare results with and without VoyageAI reranking using the A/B Testing modal in the workout detail page. This allows you to see the value of semantic reranking side-by-side.

## Use Cases & Applications

### 1. Fitness & Health Applications

**Workout Planning:**
- Find workouts similar to ones where you felt strong
- Discover patterns in successful training sessions
- Identify workouts that match your current fitness level

**Injury Recovery:**
- Find workouts similar to pre-injury sessions
- Gradually increase intensity by finding progressively harder workouts
- Track recovery progress through pattern similarity

**Performance Analysis:**
- Compare current performance to historical patterns
- Identify training zones and intensity patterns
- Detect anomalies or unusual patterns

### 2. Medical & Healthcare

**ECG/EKG Analysis:**
- Convert heart rhythm patterns to visual fingerprints
- Find similar arrhythmia patterns across patients
- Detect anomalies in cardiac monitoring data

**Vital Signs Monitoring:**
- Track patient vital signs over time
- Find similar patterns in patient histories
- Early warning systems for deteriorating conditions

**Sleep Analysis:**
- Convert sleep stage patterns to vectors
- Find similar sleep patterns across nights
- Identify sleep quality trends

### 3. Industrial & IoT Applications

**Sensor Data Analysis:**
- Machine vibration patterns
- Temperature/pressure monitoring
- Equipment health monitoring

**Predictive Maintenance:**
- Find similar patterns before equipment failures
- Identify early warning signs
- Schedule maintenance based on pattern similarity

**Quality Control:**
- Compare production patterns
- Detect anomalies in manufacturing processes
- Find similar defect patterns

### 4. Financial Applications

**Trading Pattern Analysis:**
- Convert price/volume patterns to vectors
- Find similar market conditions
- Identify trading opportunities

**Risk Analysis:**
- Find similar risk patterns
- Detect anomalies in transaction patterns
- Fraud detection through pattern matching

### 5. Environmental Monitoring

**Weather Pattern Analysis:**
- Convert weather time-series to vectors
- Find similar weather patterns
- Climate trend analysis

**Pollution Monitoring:**
- Track air/water quality patterns
- Find similar pollution events
- Environmental anomaly detection

## Key Advantages

### 1. Scalability

- **Indexed Search**: Vector indexes enable fast searches across millions of records
- **Sub-second Queries**: Typical searches complete in 5-50ms
- **Horizontal Scaling**: MongoDB Atlas scales automatically

### 2. Flexibility

- **Multiple Perspectives**: Different channel combinations reveal different patterns
- **Custom Metrics**: Easy to add new metrics or change channel assignments
- **Hybrid Search**: Combine vector, text, and filter searches

### 3. Interpretability

- **Visual Representation**: The 8x8 images provide intuitive visual feedback
- **Channel Breakdown**: See which metrics contribute to similarity
- **Score Transparency**: Vector and text scores are visible

### 4. Practicality

- **Real-World Ready**: Works with actual sensor data
- **No Complex Algorithms**: Simple normalization and reshaping
- **Standard Tools**: Uses standard MongoDB and vector search

## Technical Implementation

### Vector Generation

```python
def _generate_workout_viz_arrays(doc, r_key="heart_rate", g_key="calories_per_min", b_key="speed_kph"):
    # Normalize each metric to 0-255
    r_1d = normalize(doc["time_series"][r_key], bounds[r_key])
    g_1d = normalize(doc["time_series"][g_key], bounds[g_key])
    b_1d = normalize(doc["time_series"][b_key], bounds[b_key])
    
    # Reshape to 8x8
    r_2d = r_1d.reshape(8, 8)
    g_2d = g_1d.reshape(8, 8)
    b_2d = b_1d.reshape(8, 8)
    
    # Stack into RGB
    rgb = stack([r_2d, g_2d, b_2d], axis=-1)
    
    # Flatten to vector
    vector = rgb.reshape(-1)  # 192 elements
    
    return vector
```

### Vector Search Query

```python
pipeline = [
    {
        "$vectorSearch": {
            "index": "workout_vector_index",
            "path": "workout_vector",
            "queryVector": query_vector,
            "numCandidates": 100,
            "limit": 5
        }
    },
    {
        "$project": {
            "_id": 1,
            "score": {"$meta": "vectorSearchScore"}
        }
    }
]
```

### Hybrid Search

```python
# 1. Vector search for similar patterns
vector_results = vector_search(base_workout_vector, filters)

# 2. Text search for keyword matches
text_results = text_search("felt strong", filters)

# 3. Combine with weighted scoring
combined_score = (vector_score * 0.7) + (text_score * 0.3)
```

## Performance Characteristics

### Indexed Combinations
- **Speed**: ~5-10ms
- **Method**: Direct vector index lookup
- **Examples**: HR/Calories/Speed, Power/Cadence/HR

### Non-Indexed Combinations
- **Speed**: ~200-500ms
- **Method**: Pre-filter with indexed vector, then re-rank with custom vectors
- **Examples**: Any other channel combination

### Hybrid Search
- **Speed**: ~50-200ms (depends on text query complexity)
- **Method**: Parallel vector + text search, then merge
- **Scoring**: Weighted combination of both scores

## Future Enhancements

### Potential Improvements

1. **Multi-Metric Vectors**: Support more than 3-4 metrics simultaneously
2. **Temporal Patterns**: Include time-of-day, day-of-week patterns
3. **User Preferences**: Learn which channel combinations users prefer
4. **Real-Time Streaming**: Process live sensor data streams
5. **ML Integration**: Use vectors as features for ML models

### Advanced Applications

1. **Anomaly Detection**: Find workouts that don't match any patterns
2. **Clustering**: Group workouts by pattern similarity
3. **Recommendation**: Suggest workouts based on pattern preferences
4. **Trend Analysis**: Track how patterns change over time

## Conclusion

The Data Imaging experiment demonstrates a powerful, practical approach to time-series data analysis. By converting complex sensor data into visual fingerprints, we enable:

- **Fast similarity search** across millions of records
- **Intuitive pattern matching** that captures how data "feels"
- **Flexible exploration** through different metric perspectives
- **Practical applications** across fitness, healthcare, IoT, and more

This technique bridges the gap between complex time-series analysis and practical, scalable search - making pattern matching accessible and fast.

