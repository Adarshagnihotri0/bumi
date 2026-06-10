# Project Organization Guide

## Files for Submission (Essential)

### Core Algorithm
- **`solve.py`** (26 KB) — Complete boundary alignment solver
  - 15 functions, 467 lines
  - No external ML/training files
  - Self-contained, runnable

### Predictions
- **`predictions.geojson`** (2.1 MB) — Algorithm output
  - 2,457 features (1,889 corrected + 568 flagged)
  - Properties: plot_number, status, confidence, method_note
  - Ready for submission to BhuMe scorer

### Documentation
- **`ARCHITECTURE.md`** (7.5 KB) — Algorithm design & structure
  - Pipeline overview
  - Function reference
  - Configuration parameters
  - Performance metrics

- **`README.md`** (3.5 KB) — Project overview
  - Approach summary
  - Results validation
  - Next steps

### Input Data (Required for Running solve.py)
- **`data/BhuMe Boundary Input.geojson`** (2.2 MB)
- **`data/BhuMe Boundary Truths.geojson`** (2.2 KB)
- **`data/BhuMe Boundary Imagery.tif`** (13 MB)
- **`data/BhuMe Boundary Take-Home.tif`** (16 MB)

---

## Reference Materials (Not Required for Submission)

### Testing & Validation
- **`reference/bhume-starter-kit/`** — Official test suite
  - Contains `bhume.score.py` (scorer implementation)
  - `baseline.py` (comparison implementation)
  - Use for validation: `bhume.score(predictions.geojson)`

### Development Transcripts
- **`transcripts/copilot-session.md`** — Full reasoning log
  - Iteration history (FFT → ICP → validation)
  - Design decisions & rationale
  - Debugging notes

- **`transcripts/README.md`** — Transcript index

---

## Quick Commands

### Run the Algorithm
```bash
cd /Users/adarshagnihotri/land
python3 solve.py
```
Output: `predictions.geojson` (updates in place)

### Validate Output
```bash
python3 -c "from reference.bhume-starter-kit.bhume import score; print(score('predictions.geojson'))"
```

### Check File Sizes
```bash
du -sh data/* solve.py predictions.geojson
```

### Verify Data Integrity
```bash
python3 -c "
import json
with open('data/BhuMe Boundary Input.geojson') as f:
    plots = json.load(f)
print(f'Input: {len(plots[\"features\"])} plots')
with open('predictions.geojson') as f:
    preds = json.load(f)
print(f'Output: {len(preds[\"features\"])} predictions')
"
```

---

## Structure Summary

```
land/                                    # Root
├── SUBMISSION FILES                    # Essential for review
│   ├── solve.py                        # Main algorithm (26 KB)
│   ├── predictions.geojson             # Output (2.1 MB)
│   ├── ARCHITECTURE.md                 # Design document
│   └── README.md                       # Project summary
│
├── DATA FILES                          # Required for running
│   └── data/
│       ├── BhuMe Boundary Input.geojson
│       ├── BhuMe Boundary Truths.geojson
│       ├── BhuMe Boundary Imagery.tif
│       └── BhuMe Boundary Take-Home.tif
│
├── REFERENCE (Optional)                # For testing/validation
│   └── reference/
│       └── bhume-starter-kit/          # Official test suite
│           ├── bhume/
│           ├── pyproject.toml
│           └── quickstart.py
│
└── TRANSCRIPTS (Optional)              # Development documentation
    └── transcripts/
        ├── README.md
        └── copilot-session.md
```

---

## Submission Checklist

- [x] `solve.py` — algorithm complete and tested
- [x] `predictions.geojson` — output generated (1,889 corrected, 568 flagged)
- [x] `ARCHITECTURE.md` — design documented
- [x] `README.md` — results validated
- [x] `data/` folder — all input files organized
- [x] `reference/` folder — test suite available
- [x] `transcripts/` folder — development notes recorded

---

## Notes for Reviewer

1. **Algorithm Position:** `solve.py` (450 lines, no dependencies except scipy/rasterio)
2. **Output Format:** Standard GeoJSON FeatureCollection with properties
3. **Confidence Calibration:** h1_score combines 4 signals; meaningful spread (0.26–0.88)
4. **Accuracy:** +0.182 IoU improvement vs baseline on 6 ground truth plots
5. **Architecture:** Clean separation of concerns; easy to modify parameters

See `ARCHITECTURE.md` for detailed design rationale.
