#!/usr/bin/env python3
"""
BhuMe Batch Runner
==================

Runs solve.py on all village folders under data/ and writes predictions.geojson
for each village.

Usage:
    python run_all.py
"""

import sys
import subprocess
from pathlib import Path

def main():
    data_dir = Path("data")
    
    if not data_dir.exists():
        print(f"Error: {data_dir} directory not found")
        sys.exit(1)
    
    # Find all village folders
    villages = sorted([d for d in data_dir.iterdir() if d.is_dir()])
    
    if not villages:
        print(f"No village folders found in {data_dir}")
        sys.exit(1)
    
    print(f"Found {len(villages)} village(s)")
    print()
    
    failed = []
    
    for i, village_dir in enumerate(villages, 1):
        village_name = village_dir.name
        print(f"[{i}/{len(villages)}] Processing {village_name}...")
        
        try:
            result = subprocess.run(
                ["python", "solve.py", str(village_dir)],
                check=True,
                capture_output=False
            )
        except subprocess.CalledProcessError as e:
            print(f"  ERROR: Failed to process {village_name}")
            failed.append(village_name)
        
        print()
    
    # Summary
    print("=" * 60)
    if failed:
        print(f"FAILED: {len(failed)} village(s)")
        for village in failed:
            print(f"  - {village}")
        sys.exit(1)
    else:
        print(f"SUCCESS: All {len(villages)} village(s) processed")
        print("Predictions written to each village folder as predictions.geojson")
        sys.exit(0)

if __name__ == "__main__":
    main()
