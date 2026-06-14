# BhuMe Cadastral Alignment Submission

This repository contains the final alignment solution for the BhuMe assignment.

## Structure

```
BhuMe/
│
├── solve.py
├── run_all.py
├── analyze_data.py
├── README.md
├── requirements.txt
│
├── transcripts/
│
└── data/
    ├── 12409_malatavadi_chandgad_kolhapur/
    │   ├── input.geojson
    │   ├── imagery.tif
    │   ├── boundaries.tif
    │   └── predictions.geojson
    │
    └── 34855_vadnerbhairav_chandavad_nashik/
        ├── input.geojson
        ├── imagery.tif
        ├── boundaries.tif
        └── predictions.geojson
```

- **`solve.py`**: The core alignment pipeline. It executes the village-level registration and local refinements.
- **`run_all.py`**: Runs `solve.py` on every village folder under `data/` and writes `predictions.geojson` in each folder.
- **`analyze_data.py`**: Auxiliary analysis utilities.
- **`requirements.txt`**: Python dependencies needed to run the solver.
- **`transcripts/`**: Conversation transcripts documenting the research, architectural problem-solving, and auditing phases.

## Execution

To run the pipeline on a single village bundle:

```bash
python solve.py data/34855_vadnerbhairav_chandavad_nashik
python solve.py data/12409_malatavadi_chandgad_kolhapur
```

To process all villages inside `data/`:

```bash
python run_all.py
```

## Reproducible Release

To publish this project on GitHub with generated predictions:

```bash
git add solve.py run_all.py requirements.txt README.md \
  data/12409_malatavadi_chandgad_kolhapur/predictions.geojson \
  data/34855_vadnerbhairav_chandavad_nashik/predictions.geojson
+git commit -m "Add BhuMe solver and village predictions"
+git push
```

To regenerate the predictions locally after cloning:

```bash
python solve.py data/12409_malatavadi_chandgad_kolhapur
python solve.py data/34855_vadnerbhairav_chandavad_nashik
```

## Notes

- Each village folder contains the required input bundle and the resulting `predictions.geojson`.
- The repository is structured so others can reproduce predictions given the same village bundles and installed dependencies.

## Method Overview

1. **Global Shift (Village-Level)**: Uses zero-mean FFT cross-correlation of all officially drawn boundaries against a multi-scale edge map of the imagery to find the systematic village-level georeferencing error.
2. **Local Refinement**: Employs an energy-normalized cross-correlation window around the global shift.
3. **Adaptive Restraint**: Implements an edge-density-based dynamic threshold to prevent agricultural crop noise from triggering hallucinated shifts, maintaining alignment integrity in highly textured topologies.
