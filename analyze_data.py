#!/usr/bin/env python3
"""
BhuMe Data Analysis Utilities
==============================

Auxiliary analysis tools for examining input data, predictions, and alignment quality.

Functions:
  - compare_predictions: Compare predictions against example_truths
  - summarize_village: Print summary statistics for a village bundle
  - plot_alignment: Visualize predictions overlaid on imagery (requires matplotlib)
"""

from pathlib import Path
import json
import geopandas as gpd
import statistics
from typing import Optional, Dict, Any


def summarize_village(village_dir: Path) -> Dict[str, Any]:
    """
    Load and summarize a village bundle.
    
    Returns:
        Dictionary with keys: num_plots, bounds, crs, imagery_exists, boundaries_exist
    """
    village_dir = Path(village_dir)
    
    input_file = village_dir / "input.geojson"
    if not input_file.exists():
        raise FileNotFoundError(f"input.geojson not found in {village_dir}")
    
    # Load input
    gdf = gpd.read_file(input_file)
    
    summary = {
        "village": village_dir.name,
        "num_plots": len(gdf),
        "bounds": gdf.total_bounds.tolist(),
        "crs": str(gdf.crs),
        "imagery_exists": (village_dir / "imagery.tif").exists(),
        "boundaries_exist": (village_dir / "boundaries.tif").exists(),
        "predictions_exist": (village_dir / "predictions.geojson").exists(),
    }
    
    return summary


def compare_predictions(village_dir: Path, verbose: bool = False) -> Optional[Dict[str, Any]]:
    """
    Compare predictions.geojson against example_truths.geojson if available.
    
    Returns:
        Dictionary with comparison metrics, or None if example_truths not found.
    """
    village_dir = Path(village_dir)
    
    example_file = village_dir / "example_truths.geojson"
    pred_file = village_dir / "predictions.geojson"
    
    if not example_file.exists():
        if verbose:
            print(f"No example_truths.geojson in {village_dir}")
        return None
    
    if not pred_file.exists():
        if verbose:
            print(f"No predictions.geojson in {village_dir}")
        return None
    
    # Load both
    truths = gpd.read_file(example_file)
    preds = gpd.read_file(pred_file)
    
    if len(truths) != len(preds):
        if verbose:
            print(f"Warning: Different number of geometries: "
                  f"example_truths={len(truths)}, predictions={len(preds)}")
    
    # Compute distances between centroids
    distances = []
    for idx in range(min(len(truths), len(preds))):
        truth_geom = truths.iloc[idx].geometry
        pred_geom = preds.iloc[idx].geometry
        
        if truth_geom.is_empty or pred_geom.is_empty:
            continue
        
        dist = truth_geom.centroid.distance(pred_geom.centroid)
        distances.append(dist)
    
    if distances:
        comparison = {
            "village": village_dir.name,
            "num_compared": len(distances),
            "mean_distance_m": statistics.mean(distances),
            "median_distance_m": statistics.median(distances),
            "max_distance_m": max(distances),
            "min_distance_m": min(distances),
        }
        
        if len(distances) > 1:
            comparison["stdev_distance_m"] = statistics.stdev(distances)
        
        if verbose:
            print(f"\n{village_dir.name}")
            print(f"  Compared: {comparison['num_compared']} plots")
            print(f"  Mean distance: {comparison['mean_distance_m']:.2f} m")
            print(f"  Median distance: {comparison['median_distance_m']:.2f} m")
            print(f"  Max distance: {comparison['max_distance_m']:.2f} m")
        
        return comparison
    else:
        if verbose:
            print(f"No valid geometries to compare in {village_dir}")
        return None


def main():
    """Print analysis for all villages."""
    data_dir = Path("data")
    villages = sorted([d for d in data_dir.iterdir() if d.is_dir()])
    
    print("=" * 70)
    print("VILLAGE DATA SUMMARY")
    print("=" * 70)
    
    for village_dir in villages:
        try:
            summary = summarize_village(village_dir)
            print(f"\n{summary['village']}")
            print(f"  Plots: {summary['num_plots']}")
            print(f"  CRS: {summary['crs']}")
            print(f"  Imagery: {'✓' if summary['imagery_exists'] else '✗'}")
            print(f"  Boundaries hint: {'✓' if summary['boundaries_exist'] else '✗'}")
            print(f"  Predictions: {'✓' if summary['predictions_exist'] else '✗'}")
        except Exception as e:
            print(f"\nERROR in {village_dir.name}: {e}")
    
    print("\n" + "=" * 70)
    print("PREDICTION COMPARISON")
    print("=" * 70)
    
    for village_dir in villages:
        try:
            compare_predictions(village_dir, verbose=True)
        except Exception as e:
            print(f"ERROR in {village_dir.name}: {e}")


if __name__ == "__main__":
    main()
