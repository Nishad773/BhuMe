# BhuMe Cadastral Alignment Submission

This repository contains the final alignment solution for the BhuMe assignment.

## Structure

```
BhuMe/
├── solve.py                    # Core alignment pipeline
├── run_all.py                  # Batch runner for all villages
├── analyze_data.py             # Data analysis utilities
├── .gitignore                  # Git ignore for large raster files
├── README.md
├── requirements.txt
├── transcripts/                # AI assistance transcripts
│   ├── transcript.jsonl
│   └── README.md
└── data/
    ├── 12409_malatavadi_chandgad_kolhapur/
    │   ├── input.geojson
    │   ├── predictions.geojson
    │   ├── imagery.tif          # (provided separately)
    │   └── boundaries.tif       # (provided separately)
    └── 34855_vadnerbhairav_chandavad_nashik/
        ├── input.geojson
        ├── predictions.geojson
        ├── imagery.tif          # (provided separately)
        └── boundaries.tif       # (provided separately)
```

### File Descriptions

- **`solve.py`**: The core alignment pipeline. Executes village-level registration and local refinements.
- **`run_all.py`**: Batch runner that processes all village folders under `data/` and generates `predictions.geojson` for each.
- **`analyze_data.py`**: Auxiliary analysis utilities for examining input data, predictions, and alignment quality.
- **`requirements.txt`**: Python dependencies needed to run the solver.
- **`transcripts/`**: Complete conversation transcripts documenting research, architectural decisions, and development phases.
- **`.gitignore`**: Excludes large raster files (imagery.tif and boundaries.tif) to keep repository lightweight.

## Quick Start

### Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Place the village data bundles (imagery.tif and boundaries.tif for each village) in the appropriate folders under `data/`

### Running the Pipeline

**Process all villages at once:**

```bash
python run_all.py
```

**Process a single village manually:**

```bash
python solve.py data/12409_malatavadi_chandgad_kolhapur
python solve.py data/34855_vadnerbhairav_chandavad_nashik
```

### Data Analysis

Analyze and compare predictions against example truths:

```bash
python analyze_data.py
```

This will display:
- Village bundle summaries (number of plots, CRS, file availability)
- Comparison metrics between predictions and example_truths.geojson (if available)

## Reproducible Release

To publish this project on GitHub with generated predictions:

```bash
git add solve.py run_all.py analyze_data.py requirements.txt README.md .gitignore transcripts \
  data/12409_malatavadi_chandgad_kolhapur/predictions.geojson \
  data/34855_vadnerbhairav_chandavad_nashik/predictions.geojson
git commit -m "Add BhuMe solver, utilities, and village predictions"
git push
```

**Note**: Large raster files (`imagery.tif`, `boundaries.tif`) are excluded by `.gitignore` and should be provided separately to evaluators.

## Notes

- Each village folder contains the required input bundle and the resulting `predictions.geojson`.
- The repository is structured so others can reproduce predictions given the same village bundles and installed dependencies.

## Method Overview

1. **Global Shift (Village-Level)**: Uses zero-mean FFT cross-correlation of all officially drawn boundaries against a multi-scale edge map of the imagery to find the systematic village-level georeferencing error.
2. **Local Refinement**: Employs an energy-normalized cross-correlation window around the global shift.
3. **Adaptive Restraint**: Implements an edge-density-based dynamic threshold to prevent agricultural crop noise from triggering hallucinated shifts, maintaining alignment integrity in highly textured topologies.
