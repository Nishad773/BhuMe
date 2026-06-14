#!/usr/bin/env python3
"""
BhuMe Parcel Alignment — Generalized Solution
===============================================

Key insight: Dissolve the hard problem.

Instead of noisy per-plot cross-correlation (fragile, resolution-dependent),
use VILLAGE-LEVEL boundary matching (robust, resolution-independent):

Stage 1: GLOBAL SHIFT
  - Rasterize ALL official boundaries onto the imagery grid
  - Compute edge map of FULL imagery
  - FFT cross-correlate → one sharp peak → the systematic geo-referencing offset
  - This works because thousands of boundary pixels vote together

Stage 2: PER-PLOT REFINEMENT
  - For each plot, do a TINY cross-correlation (±8px around global shift)
  - If the refinement peak is clear, use it. Otherwise, use global.
  - This captures per-plot variation without being fooled by noise.

Stage 3: CONFIDENCE + DECISIONS
  - Post-shift boundary agreement (per-plot, physical)
  - Global correlation quality (village-level, shared baseline)
  - Area ratio + restraint signals

This generalizes because:
  - No parameters depend on pixel size (everything in metres)
  - The global correlation is dominated by the boundary NETWORK, not individual plots
  - The refinement window is narrow enough to avoid noise peaks

Usage:
    python solve.py <village_dir>
"""

from __future__ import annotations

import sys, os, warnings, math, statistics
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.windows import from_bounds
from rasterio.features import rasterize as rio_rasterize
from pyproj import Transformer
from scipy import ndimage
from scipy.signal import fftconvolve
from shapely.geometry.base import BaseGeometry
from shapely.geometry import mapping
from shapely.affinity import translate
from shapely.ops import transform as shp_transform

warnings.filterwarnings('ignore')


# ─────────────────────────────────────────────────────────────────────
# CONFIG — everything in metres, nothing pixel-size dependent
# ─────────────────────────────────────────────────────────────────────

MAX_SEARCH_M       = 40.0    # global search radius
REFINE_RADIUS_M    = 10.0    # per-plot refinement radius around global
BOUNDARY_WIDTH_M   = 3.0     # boundary template thickness
PATCH_PAD_M        = 40.0    # per-plot extraction padding
ZERO_SHIFT_M       = 2.0     # below this → already aligned
MAX_SHIFT_M        = 38.0    # flag if shift exceeds this
AREA_LO, AREA_HI   = 0.30, 3.0
BOUNDARY_HINT_W    = 0.15    # weight for boundaries.tif
DOWNSAMPLE_TARGET_M = 2.4    # target resolution for global correlation
CONF_CEIL, CONF_FLOOR = 0.93, 0.08


# ─────────────────────────────────────────────────────────────────────
# GEO HELPERS
# ─────────────────────────────────────────────────────────────────────

def _utm_for(geom):
    return f'EPSG:{32600 + int((geom.centroid.x + 180) // 6) + 1}'

def _to_crs(src):
    return Transformer.from_crs('EPSG:4326', src.crs, always_xy=True)

def _to_4326(src):
    return Transformer.from_crs(src.crs, 'EPSG:4326', always_xy=True)

def g2crs(src, g):
    tf = _to_crs(src)
    return shp_transform(lambda xs, ys, z=None: tf.transform(xs, ys), g)

def g2ll(src, g):
    tf = _to_4326(src)
    return shp_transform(lambda xs, ys, z=None: tf.transform(xs, ys), g)


# ─────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Village:
    slug: str; d: Path
    plots: gpd.GeoDataFrame
    img_path: Path
    bnd_path: Optional[Path]
    truths: Optional[gpd.GeoDataFrame]

def load_village(d):
    d = Path(d)
    plots = gpd.read_file(d / 'input.geojson')
    plots['plot_number'] = plots['plot_number'].astype(str)
    plots = plots.set_index('plot_number', drop=False)

    bp = d / 'boundaries.tif'
    tp = d / 'example_truths.geojson'
    truths = None
    if tp.exists():
        truths = gpd.read_file(tp)
        truths['plot_number'] = truths['plot_number'].astype(str)
        truths = truths.set_index('plot_number', drop=False)

    return Village(d.name, d, plots, d/'imagery.tif',
                   bp if bp.exists() else None, truths)


# ─────────────────────────────────────────────────────────────────────
# EDGE MAP (adaptive sigma)
# ─────────────────────────────────────────────────────────────────────

def edge_map(rgb, pixel_m):
    """Multi-scale gradient edges. Sigma adapts to pixel size
    so that the physical scale of detected features is constant."""
    gray = np.dot(rgb[..., :3].astype(np.float32), [0.299, 0.587, 0.114])
    # ~1m features at any resolution
    sig_fine  = max(0.7, 1.0 / pixel_m)
    sig_coarse = max(1.5, 2.5 / pixel_m)

    s1 = ndimage.gaussian_filter(gray, sigma=sig_fine)
    g1 = np.hypot(ndimage.sobel(s1, 1), ndimage.sobel(s1, 0))
    s2 = ndimage.gaussian_filter(gray, sigma=sig_coarse)
    g2 = np.hypot(ndimage.sobel(s2, 1), ndimage.sobel(s2, 0))

    combined = 0.5 * g1 + 0.5 * g2
    mx = np.percentile(combined, 99)
    if mx > 0: combined = np.clip(combined / mx, 0, 1)
    return combined.astype(np.float32)


# ─────────────────────────────────────────────────────────────────────
# STAGE 1: VILLAGE-LEVEL GLOBAL SHIFT
# ─────────────────────────────────────────────────────────────────────

@dataclass
class GlobalShift:
    dx_m: float; dy_m: float      # in imagery CRS metres
    dx_px: float; dy_px: float    # in native pixels
    magnitude_m: float
    peak_quality: float           # normalized peak value
    sharpness: float              # peak / 2nd-peak

def compute_global_shift(img_src, plots, bnd_src=None):
    """Cross-correlate ALL official boundaries with the full edge map.

    Uses ZERO-MEAN cross-correlation: subtracting means removes the
    DC component that dominates raw correlation, revealing the true
    displacement peak even when boundary coverage is small.
    """
    native_px = abs(img_src.res[0])
    H0, W0 = img_src.height, img_src.width

    # --- downsample: target ~1.2-2.4m effective pixel size ---
    ds = max(1, int(DOWNSAMPLE_TARGET_M / native_px))
    ds = min(ds, 3)  # never go coarser than 3x
    Hd = H0 // ds
    Wd = W0 // ds
    eff_px = native_px * ds
    print(f'  Global: ds={ds}x  eff_px={eff_px:.2f}m  grid={Wd}x{Hd}')

    # --- read imagery and compute edges ---
    full_rgb = img_src.read([1, 2, 3])
    full_rgb = np.transpose(full_rgb, (1,2,0))
    if ds > 1:
        full_rgb = full_rgb[:Hd*ds, :Wd*ds, :].reshape(Hd, ds, Wd, ds, 3).mean(axis=(1,3))
        full_rgb = full_rgb.astype(np.uint8)
    edges = edge_map(full_rgb, eff_px)
    del full_rgb

    # --- downsampled transform ---
    l, b, r, t = img_src.bounds
    ds_tf = rasterio.transform.from_bounds(l, b, r, t, Wd, Hd)

    # --- rasterize ALL official boundaries ---
    shapes = []
    for _, row in plots.iterrows():
        try:
            gc = g2crs(img_src, row.geometry)
            bdy = gc.boundary
            if bdy.is_empty: continue
            shapes.append((mapping(bdy.buffer(BOUNDARY_WIDTH_M)), 1.0))
        except: pass
    if not shapes:
        return GlobalShift(0,0,0,0,0,0,1)

    official = rio_rasterize(shapes, out_shape=(Hd, Wd),
                             transform=ds_tf, fill=0, dtype='float32')

    # --- optionally blend boundary hints into edge target ---
    target = edges.copy()
    if bnd_src is not None:
        try:
            hints = bnd_src.read(1).astype(np.float32) / 255.0
            from skimage.transform import resize
            hints = resize(hints, (Hd, Wd), preserve_range=True).astype(np.float32)
            target = (1 - BOUNDARY_HINT_W) * target + BOUNDARY_HINT_W * hints
        except: pass

    # ══════════════════════════════════════════════════════════════
    # ZERO-MEAN CROSS-CORRELATION — this is the critical fix.
    # Raw correlation is dominated by DC (mean×mean×N).
    # Subtracting means makes the peak stand out clearly.
    # ══════════════════════════════════════════════════════════════
    target_zm = target - target.mean()
    official_zm = official - official.mean()

    corr = fftconvolve(target_zm, official_zm[::-1, ::-1], mode='same')
    cy, cx = Hd // 2, Wd // 2

    search_px = max(8, int(MAX_SEARCH_M / eff_px))
    y0 = max(0, cy - search_px); y1 = min(Hd, cy + search_px + 1)
    x0 = max(0, cx - search_px); x1 = min(Wd, cx + search_px + 1)
    region = corr[y0:y1, x0:x1]

    region_sm = ndimage.gaussian_filter(region, sigma=1.2)
    peak_idx = np.unravel_index(np.argmax(region_sm), region_sm.shape)
    peak_val = float(region[peak_idx])

    dy_ds = int(peak_idx[0] + y0 - cy)
    dx_ds = int(peak_idx[1] + x0 - cx)

    # sharpness (should now be MUCH higher with zero-mean)
    sup = region_sm.copy()
    py, px = peak_idx
    r = max(3, int(6 / eff_px))
    sup[max(0,py-r):min(sup.shape[0],py+r+1),
        max(0,px-r):min(sup.shape[1],px+r+1)] = -np.inf
    valid = sup[sup > -np.inf]
    second = float(np.max(valid)) if len(valid) else peak_val * 0.5
    sharpness = peak_val / (second + 1e-8) if second > 0 else 2.0

    # normalize
    off_energy = np.sum(official**2)
    peak_norm = peak_val / (off_energy + 1e-8)

    # convert to metres (in imagery CRS)
    dx_m = dx_ds * eff_px
    dy_m = dy_ds * eff_px
    mag = math.sqrt(dx_m**2 + dy_m**2)

    # also express in native pixels
    dx_native = dx_m / native_px
    dy_native = dy_m / native_px

    print(f'  Global shift: dx={dx_m:.1f}m dy={dy_m:.1f}m '
          f'({mag:.1f}m) sharp={sharpness:.2f}')

    return GlobalShift(dx_m, dy_m, dx_native, dy_native,
                       mag, peak_norm, sharpness)


# ─────────────────────────────────────────────────────────────────────
# STAGE 2: PER-PLOT REFINEMENT (tiny window around global)
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PlotShift:
    dx_m: float; dy_m: float
    magnitude_m: float
    sharpness: float
    post_agreement: float   # boundary-edge agreement after shift
    pre_agreement: float    # agreement at zero shift (restraint signal)
    edge_density: float
    refined: bool           # was local refinement used?
    
    # --- Diagnostics ---
    peak_val: float = 0.0
    bg_val: float = 0.0
    prominence: float = 0.0
    second_peak: float = 0.0
    peak_dist: float = 0.0

def refine_plot(img_src, bnd_src, geom_4326, global_shift, pn):
    """Refine the global shift for one plot in a small window.

    Extracts a patch centered on the plot, does FFT correlation
    within ±REFINE_RADIUS_M of the global shift.
    Falls back to global if refinement is ambiguous.
    """
    native_px = abs(img_src.res[0])
    pad = PATCH_PAD_M

    try:
        gc = g2crs(img_src, geom_4326)
        minx, miny, maxx, maxy = gc.bounds
        left  = minx - pad; right = maxx + pad
        bottom = miny - pad; top  = maxy + pad

        dl, db, dr, dt = img_src.bounds
        left = max(left, dl); bottom = max(bottom, db)
        right = min(right, dr); top = min(top, dt)
        if right <= left or top <= bottom: return None

        window = from_bounds(left, bottom, right, top,
                             transform=img_src.transform)
        rgb = np.transpose(img_src.read([1,2,3], window=window), (1,2,0))
        if rgb.shape[0] < 10 or rgb.shape[1] < 10: return None

        ptf = img_src.window_transform(window)
        H, W = rgb.shape[:2]

        # edge map
        edges = edge_map(rgb, native_px)

        # boundary hints
        target = edges.copy()
        if bnd_src is not None:
            try:
                bl = max(left, bnd_src.bounds.left)
                bb = max(bottom, bnd_src.bounds.bottom)
                br = min(right, bnd_src.bounds.right)
                bt = min(top, bnd_src.bounds.top)
                if br > bl and bt > bb:
                    bw = from_bounds(bl, bb, br, bt, transform=bnd_src.transform)
                    bd = bnd_src.read(1, window=bw).astype(np.float32) / 255.0
                    if bd.shape != edges.shape:
                        from skimage.transform import resize
                        bd = resize(bd, edges.shape, preserve_range=True).astype(np.float32)
                    target = (1-BOUNDARY_HINT_W) * target + BOUNDARY_HINT_W * bd
            except: pass

        # rasterize boundary template
        bdy = gc.boundary
        if bdy.is_empty: return None
        buf = max(2, int(BOUNDARY_WIDTH_M / native_px)) * native_px
        tmpl = rio_rasterize(
            [(mapping(bdy.buffer(buf)), 1.0)],
            out_shape=(H, W), transform=ptf, fill=0, dtype='float32')
        if tmpl.sum() < 3: return None

        # --- Zero-mean FFT cross-correlation ---
        target_zm = target - target.mean()
        tmpl_zm = tmpl - tmpl.mean()
        corr = fftconvolve(target_zm, tmpl_zm[::-1, ::-1], mode='same')
        cy, cx = H // 2, W // 2

        # search: include BOTH the global shift AND the zero-shift position
        # so the correlation can find the best among: original pos, global, refined
        gdy = int(round(global_shift.dy_px))
        gdx = int(round(global_shift.dx_px))
        refine_r = max(5, int(REFINE_RADIUS_M / native_px))

        # Compute the bounding box that contains both (0,0) and (gdy,gdx)
        # plus the refinement radius around each
        search_y_min = min(0, gdy) - refine_r
        search_y_max = max(0, gdy) + refine_r
        search_x_min = min(0, gdx) - refine_r
        search_x_max = max(0, gdx) + refine_r

        y0 = max(0, cy + search_y_min)
        y1 = min(H, cy + search_y_max + 1)
        x0 = max(0, cx + search_x_min)
        x1 = min(W, cx + search_x_max + 1)

        region = corr[y0:y1, x0:x1]
        if region.size < 4: return None

        # --- 1. Compute peak on smoothed correlation surface ---
        region_sm = ndimage.gaussian_filter(region, sigma=0.8)
        pk = np.unravel_index(np.argmax(region_sm), region_sm.shape)
        
        ref_dy = int(pk[0] + y0 - cy)
        ref_dx = int(pk[1] + x0 - cx)
        
        # --- Diagnostics ---
        peak_val = float(region[pk])
        bg_val = float(np.median(region))
        std_val = float(np.median(np.abs(region - bg_val))) * 1.4826 + 1e-6
        prominence = (peak_val - bg_val) / std_val
        
        # Suppress local neighborhood to find second peak
        sup = region_sm.copy()
        sr = max(3, int(5.0 / native_px))
        sup[max(0, pk[0]-sr):min(sup.shape[0], pk[0]+sr+1),
            max(0, pk[1]-sr):min(sup.shape[1], pk[1]+sr+1)] = -np.inf
        valid = sup[sup > -np.inf]
        
        if len(valid) > 0:
            pk2 = np.unravel_index(np.argmax(sup), sup.shape)
            second_peak = float(region[pk2])
            peak_dist = math.hypot((pk2[1] + x0 - cx) - ref_dx, (pk2[0] + y0 - cy) - ref_dy) * native_px
        else:
            second_peak = peak_val
            peak_dist = 0.0

        sharp = peak_val / (second_peak + 1e-8) if second_peak > 0 else 2.0

        # ── RESTRAINT: compare normalized correlation at 3 positions ──
        # Since target and tmpl are zero-mean, corr[y, x] is their dot product.
        # Normalize by template energy to get a score independent of plot size.
        t_energy = float(np.sum(tmpl_zm**2)) + 1e-8
        
        def norm_agr(dy_px, dx_px):
            y = cy + dy_px
            x = cx + dx_px
            if 0 <= y < H and 0 <= x < W:
                return float(corr[y, x]) / t_energy
            return -1.0

        agr_zero    = norm_agr(0, 0)
        agr_global  = norm_agr(gdy, gdx)
        agr_refined = norm_agr(ref_dy, ref_dx)

        # edge density
        ed = float(np.mean(edges > 0.1))

        # Penalize refined shifts that are far from global
        dist_from_global = math.sqrt((ref_dx * native_px - global_shift.dx_m)**2 + 
                                     (ref_dy * native_px - global_shift.dy_m)**2)
        penalty = 0.005 * dist_from_global
        
        # Pick the BEST position among: original, global, refined
        # Note: we use the unpenalized value for 'zero' and 'global'
        candidates = [
            ('zero',    0,       0,       agr_zero, agr_zero),
            ('global',  global_shift.dx_m, global_shift.dy_m, agr_global, agr_global),
            ('refined', ref_dx * native_px, ref_dy * native_px, agr_refined - penalty, agr_refined),
        ]
        best = max(candidates, key=lambda c: c[3])
        best_name, dx_m, dy_m, score_penalized, post_agr = best

        # This is the key RESTRAINT: don't move if original is already good
        improvement = post_agr - agr_zero
        
        # Dynamic threshold based on edge density.
        dynamic_thresh = 0.05 + 0.08 * ed
        
        # Determine rejection reason for logging
        accepted = True
        reject_reason = "None"
        
        if post_agr < dynamic_thresh:
            dx_m, dy_m = 0.0, 0.0
            post_agr = agr_zero
            use_refined = False
            accepted = False
            reject_reason = f"post_agr ({post_agr:.3f}) < dynamic_thresh ({dynamic_thresh:.3f})"
        elif improvement < 0.02:
            dx_m, dy_m = 0.0, 0.0
            post_agr = agr_zero
            use_refined = False
            accepted = False
            reject_reason = f"improvement ({improvement:.3f}) < 0.02"
        else:
            use_refined = (best_name == 'refined')

        mag = math.sqrt(dx_m**2 + dy_m**2)

        return PlotShift(dx_m, dy_m, mag, sharp,
                         post_agr, agr_zero, ed, use_refined,
                         peak_val, bg_val, prominence, second_peak, peak_dist)
    except Exception as e:
        print(f"Error in refine_plot: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
# GEOMETRY SHIFT
# ─────────────────────────────────────────────────────────────────────

def shift_geom(geom_4326, dx_m, dy_m, img_src):
    """Shift geometry by (dx_m, dy_m) in imagery CRS, return in 4326.

    Uses the actual affine transform to correctly map dx/dy to map units,
    handling arbitrary rotations and negative scales natively.
    """
    tf_to = _to_crs(img_src)
    tf_from = _to_4326(img_src)
    gc = shp_transform(lambda xs, ys, z=None: tf_to.transform(xs, ys), geom_4326)
    
    # Calculate geometric shift via image affine transform
    # Convert metric dx/dy back to pure pixel shifts, then push through affine
    native_px = abs(img_src.res[0])
    dx_px = dx_m / native_px
    dy_px = dy_m / native_px
    
    x0, y0 = img_src.transform * (0, 0)
    x1, y1 = img_src.transform * (dx_px, dy_px)
    real_dx = x1 - x0
    real_dy = y1 - y0
    
    gc2 = translate(gc, xoff=real_dx, yoff=real_dy)
    return shp_transform(lambda xs, ys, z=None: tf_from.transform(xs, ys), gc2)


# ─────────────────────────────────────────────────────────────────────
# CONFIDENCE
# ─────────────────────────────────────────────────────────────────────

def confidence(ps: PlotShift, gs: GlobalShift, area_ratio, med_mag):
    """Multi-signal confidence with deliberate spread."""

    # 1. Post-shift boundary agreement (now norm_agr, typical range 0.05 to 0.35)
    s_agree = min(1.0, max(0.0, (ps.post_agreement - 0.05) / 0.25))

    # 2. Refinement sharpness (unimodal = confident)
    s_sharp = min(1.0, max(0, (ps.sharpness - 1.0) / 0.5))

    # 3. Edge signal (can we see boundaries at all?)
    s_signal = min(1.0, ps.edge_density / 0.15)

    # 4. Global peak quality (village-level signal)
    s_global = min(1.0, max(0, (gs.sharpness - 1.0) / 1.0))

    # 5. Displacement plausibility
    excess = max(0, ps.magnitude_m - med_mag - 15)
    s_disp = math.exp(-excess**2 / (2*20**2))

    # 6. Area ratio
    s_area = 1.0
    if area_ratio and area_ratio > 0:
        log_r = abs(math.log(max(0.01, area_ratio)))
        s_area = math.exp(-log_r**2 / (2*0.5**2))

    # 7. Pre-shift agreement bonus (restraint: if already aligned, boost confidence)
    s_pre = min(1.0, max(0.0, (ps.pre_agreement - 0.05) / 0.25))

    raw = (0.28 * s_agree + 0.15 * s_sharp + 0.08 * s_signal +
           0.12 * s_global + 0.10 * s_disp + 0.08 * s_area +
           0.09 * s_pre + 0.10 * (1.0 if ps.refined else 0.5))

    # Penalize very low agreement (norm_agr < 0.10)
    if ps.post_agreement < 0.10 and ps.magnitude_m > 3:
        raw *= 0.6

    return round(max(CONF_FLOOR, min(CONF_CEIL, raw)), 3)


# ─────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────

def solve(village_dir):
    vill = load_village(village_dir)
    n_tr = 0 if vill.truths is None else len(vill.truths)
    print(f'Village: {vill.slug}  ({len(vill.plots)} plots, {n_tr} truths)')

    img = rasterio.open(vill.img_path)
    native_px = abs(img.res[0])
    print(f'Imagery: {img.width}x{img.height}, {native_px:.3f}m/px')

    bnd = rasterio.open(vill.bnd_path) if vill.bnd_path else None

    # ── STAGE 1: Global shift ──
    print('\n--- Stage 1: Global shift ---')
    gs = compute_global_shift(img, vill.plots, bnd)

    # ── STAGE 2: Per-plot refinement ──
    print('\n--- Stage 2: Per-plot refinement ---')
    shifts: Dict[str, Optional[PlotShift]] = {}
    total = len(vill.plots)
    for i, pn in enumerate(vill.plots.index):
        if (i+1) % 250 == 0 or i == 0 or i == total-1:
            print(f'  {i+1}/{total}...')
        geom = vill.plots.loc[pn, 'geometry']
        shifts[pn] = refine_plot(img, bnd, geom, gs, pn)

    n_ref = sum(1 for s in shifts.values() if s is not None and s.refined)
    n_glob = sum(1 for s in shifts.values() if s is not None and not s.refined)
    n_fail = sum(1 for s in shifts.values() if s is None)
    print(f'  Refined: {n_ref}, Global-fallback: {n_glob}, Failed: {n_fail}')

    # ── STAGE 3: Decisions ──
    print('\n--- Stage 3: Decisions ---')
    valid_mags = [s.magnitude_m for s in shifts.values() if s]
    med_mag = statistics.median(valid_mags) if valid_mags else gs.magnitude_m

    rows = []
    for pn in vill.plots.index:
        geom = vill.plots.loc[pn, 'geometry']
        ps = shifts.get(pn)
        props = vill.plots.loc[pn]

        # area ratio
        ma = props.get('map_area_sqm', 0)
        ra = props.get('recorded_area_sqm')
        pk = props.get('pot_kharaba_ha', 0)
        ar = None
        if ra and ra > 0 and ma and ma > 0:
            total_rec = ra + (pk * 10000 if pk and pk > 0 else 0)
            ar = ma / total_rec if total_rec > 0 else None

        # flag conditions
        status = 'corrected'
        note = ''
        conf = 0.0

        if ps is None:
            status = 'flagged'
            note = 'extraction failed'
        elif ar is not None and (ar < AREA_LO or ar > AREA_HI):
            status = 'flagged'
            note = f'extreme area ratio ({ar:.2f})'
        elif ps.edge_density < 0.04:
            status = 'flagged'
            note = f'no edge signal ({ps.edge_density:.3f})'
        elif ps.magnitude_m > MAX_SHIFT_M:
            status = 'flagged'
            note = f'shift too large ({ps.magnitude_m:.1f}m)'
        else:
            conf = confidence(ps, gs, ar, med_mag)
            dx, dy = ps.dx_m, ps.dy_m
            mag = ps.magnitude_m

            if mag < ZERO_SHIFT_M:
                note = f'already aligned ({mag:.1f}m)'
                shifted_geom = geom   # don't move
            else:
                shifted_geom = shift_geom(geom, dx, dy, img)
                note = f'shifted dx={dx:.1f}m dy={dy:.1f}m ({mag:.1f}m) conf={conf:.2f}'

            rows.append({
                'plot_number': pn, 'status': 'corrected',
                'confidence': conf, 'method_note': note,
                'geometry': shifted_geom if mag >= ZERO_SHIFT_M else geom,
                'peak_val': ps.peak_val, 'bg_val': ps.bg_val,
                'prominence': ps.prominence, 'second_peak': ps.second_peak,
                'peak_dist': ps.peak_dist, 'sharpness': ps.sharpness,
                'post_agreement': ps.post_agreement
            })
            continue

        rows.append({
            'plot_number': pn, 'status': status,
            'confidence': None, 'method_note': note,
            'geometry': geom,
            'peak_val': ps.peak_val if ps else 0.0,
            'bg_val': ps.bg_val if ps else 0.0,
            'prominence': ps.prominence if ps else 0.0,
            'second_peak': ps.second_peak if ps else 0.0,
            'peak_dist': ps.peak_dist if ps else 0.0,
            'sharpness': ps.sharpness if ps else 0.0,
            'post_agreement': ps.post_agreement if ps else 0.0
        })

    preds = gpd.GeoDataFrame(rows, crs='EPSG:4326')
    nc = sum(1 for r in rows if r['status'] == 'corrected')
    nf = sum(1 for r in rows if r['status'] == 'flagged')
    confs = [r['confidence'] for r in rows if r['status'] == 'corrected' and r['confidence'] is not None]
    print(f'  Corrected: {nc}, Flagged: {nf}  '
          f'({nf/(nc+nf):.0%} flag rate)')
    if confs:
        print(f'  Confidence: {min(confs):.2f} / {statistics.median(confs):.2f} / {max(confs):.2f}')

    img.close()
    if bnd: bnd.close()
    return preds


# ─────────────────────────────────────────────────────────────────────
# OUTPUT + SCORING
# ─────────────────────────────────────────────────────────────────────

def write_preds(preds, path):
    gdf = preds.copy()
    if gdf.crs is None: gdf = gdf.set_crs('EPSG:4326')
    else: gdf = gdf.to_crs('EPSG:4326')
    keep = [c for c in ('plot_number','status','confidence','method_note','geometry')
            if c in gdf.columns]
    Path(path).write_text(gdf[keep].to_json())

def self_score(preds, vill):
    if vill.truths is None: print('No truths.'); return
    truth = vill.truths
    utm = _utm_for(truth.geometry.iloc[0])
    tu = truth.to_crs(utm)
    ou = vill.plots.to_crs(utm)
    pi = preds.copy()
    pi['plot_number'] = pi['plot_number'].astype(str)
    pi = pi.set_index('plot_number', drop=False)
    pu = pi.to_crs(utm)

    print(f'\n{"="*60}')
    print(f'SCORING: {vill.slug} ({len(truth)} truths)')
    print(f'{"="*60}')

    ious_p, ious_o, imps, cerrs, confs = [], [], [], [], []
    for pn in truth.index:
        t = tu.loc[pn, 'geometry']
        o = ou.loc[pn, 'geometry']
        io = _iou(o, t); ious_o.append(io)
        if pn in pu.index:
            row = pi.loc[pn]
            st = str(row.get('status',''))
            if st == 'corrected':
                pg = pu.loc[pn, 'geometry']
                ip = _iou(pg, t); ious_p.append(ip)
                imps.append(ip - io)
                cerrs.append(pg.centroid.distance(t.centroid))
                c = row.get('confidence', 0.5); confs.append(c)
                print(f'  {pn}: IoU {io:.3f}->{ip:.3f} ({ip-io:+.3f}) '
                      f'err={cerrs[-1]:.1f}m conf={c:.2f}')
            else:
                note = row.get('method_note','')
                print(f'  {pn}: FLAGGED IoU_off={io:.3f} ({note})')
        else:
            print(f'  {pn}: MISSING')

    print()
    if ious_p:
        print(f'Median IoU:    {statistics.median(ious_p):.3f} (off: {statistics.median(ious_o):.3f})')
        print(f'Improvement:   {statistics.median(imps):+.3f}')
        if cerrs: print(f'Centroid err:  {statistics.median(cerrs):.1f}m')
        ni = sum(1 for x in imps if x > 0)
        print(f'Improved:      {ni}/{len(imps)}')
        if len(confs) >= 3 and len(set(confs)) > 1:
            from scipy.stats import spearmanr
            corr, _ = spearmanr(confs, ious_p)
            print(f'Spearman:      {corr:.3f}')

def _iou(a, b):
    if not a or not b or a.is_empty or b.is_empty: return 0.0
    u = a.union(b).area
    return float(a.intersection(b).area / u) if u > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────

def main():
    d = sys.argv[1] if len(sys.argv) > 1 else \
        'data/34855_vadnerbhairav_chandavad_nashik'
    preds = solve(d)
    out = Path(d) / 'predictions.geojson'
    write_preds(preds, out)
    print(f'\nWrote {len(preds)} -> {out}')
    self_score(preds, load_village(d))

if __name__ == '__main__':
    main()
