"""Command line interface.

Usage:
    python -m bess_augment fetch                    # RCE prices from the PSE API into data/rce
    python -m bess_augment profiles --jobs 8        # operating profiles (arbitrage MILP per day, reserves)
    python -m bess_augment fit                      # ageing model fitted to the bundled cell data
    python -m bess_augment run --jobs 8             # everything: results/, docs/img/*.png, docs/index.html
    python -m bess_augment run --config my.toml     # with other assumptions

Data are cached in ./data, results go to ./results, charts to ./docs/img and the dashboard to ./docs/index.html.
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np

from bess_augment import config as C
from bess_augment import pipeline
from bess_augment.aging import fit_cells
from bess_augment.pse_api import fetch_range

log = logging.getLogger("bess_augment")


def _prices(cfg: dict, args):
    d = cfg["data"]
    return fetch_range(date.fromisoformat(d["start"]), date.fromisoformat(d["end"]), args.data_dir / "rce",
                       refresh=args.refresh)


def cmd_fetch(args, cfg):
    p = _prices(cfg, args)
    log.info("RCE: %d intervals, %s .. %s", len(p), p.index.min(), p.index.max())
    return p


def cmd_profiles(args, cfg):
    cached = all(pipeline._profile_path(args.data_dir, n, d).exists()
                 for n, s in cfg["profiles"].items() for d in s["durations"])
    prices = None if cached and not args.refresh else _prices(cfg, args)
    profiles = pipeline.build_profiles(cfg, prices, args.data_dir, jobs=args.jobs, refresh=args.refresh)
    st = pipeline.stresses(profiles)
    table = pipeline.profile_summary(cfg, profiles, st)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.results_dir / "profiles.csv", index=False, float_format="%.5g")
    return profiles, st, table


def cmd_fit(args, cfg):
    fit = fit_cells()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    out = {k: {"params": {p: float(v) for p, v in fit[k].params.items()},
               "stderr": {p: float(v) for p, v in fit[k].stderr.items()},
               "rmse_pct": fit[k].rmse_pct} for k in ("calendar", "cycle")}
    out["model"] = {k: float(v) for k, v in asdict(fit["model"]).items()}
    (args.results_dir / "aging_fit.json").write_text(json.dumps(out, indent=2))
    log.info("ageing model: %s", out["model"])
    return fit


def cmd_run(args, cfg):
    from bess_augment import plots
    t0 = time.time()
    profiles, st, ptable = cmd_profiles(args, cfg)
    fit = cmd_fit(args, cfg)
    model = fit["model"]
    cache = args.data_dir / "analysis.pkl"
    if args.reuse_analysis and cache.exists():
        res = pickle.loads(cache.read_bytes())
        log.info("analysis loaded from %s", cache)
    else:
        res = pipeline.analyse(cfg, profiles, model, jobs=args.jobs)
        cache.write_bytes(pickle.dumps(res))
    r = args.results_dir
    res["soh_path"].to_csv(r / "soh_paths.csv", index=False, float_format="%.5g")
    res["compliance"].to_csv(r / "compliance_years.csv", index=False, float_format="%.5g")
    res["strategies"].to_csv(r / "strategies.csv", index=False, float_format="%.6g")
    res["paths"].to_csv(r / "strategy_paths.csv", index=False, float_format="%.6g")
    res["schedule_grid"].to_csv(r / "schedule_grid.csv", index=False, float_format="%.6g")
    pipeline.capture_sensitivity(cfg, res["strategies"]).to_csv(r / "capture_sensitivity.csv", index=False,
                                                                float_format="%.6g")
    curves = {k: pipeline.curve_for(cfg, profiles, *k) for k in profiles}
    risk, bands = pipeline.risk(cfg, fit, st, res["plans"], curves)
    risk.to_csv(r / "risk.csv", index=False, float_format="%.5g")
    bands.to_csv(r / "risk_bands.csv", index=False, float_format="%.5g")
    warranty = pipeline.warranty_table(cfg, profiles, st, res["soh_path"])
    warranty.to_csv(r / "warranty.csv", index=False, float_format="%.5g")
    calls = pipeline.call_exposure(cfg, res["soh_path"])
    calls.to_csv(r / "call_exposure.csv", index=False, float_format="%.5g")
    plots.make_all(cfg, ptable, res, risk, bands, warranty, fit, args.img_dir, args.dashboard)
    (r / "run.json").write_text(json.dumps({
        "prices": cfg["data"], "contract_years": cfg["capacity_market"]["contract_years"],
        "requirement": cfg["capacity_market"]["requirement"], "temp_c": cfg["analysis"]["temp_c"],
        "risk_samples": cfg["risk"]["samples"], "runtime_s": round(time.time() - t0, 1)}, indent=2))
    log.info("done in %.0f s", time.time() - t0)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bess_augment", description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["fetch", "profiles", "fit", "run"])
    ap.add_argument("--config", type=Path, default=C.DEFAULT, help="assumptions file (TOML)")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--results-dir", type=Path, default=Path("results"))
    ap.add_argument("--img-dir", type=Path, default=Path("docs/img"))
    ap.add_argument("--dashboard", type=Path, default=Path("docs/index.html"))
    ap.add_argument("--jobs", type=int, default=1, help="worker processes for the optimisation")
    ap.add_argument("--refresh", action="store_true", help="download prices and recompute profiles again")
    ap.add_argument("--reuse-analysis", action="store_true",
                    help="reuse the strategy analysis cached in data/analysis.pkl (after changing only charts or risk)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    np.seterr(over="ignore")
    cfg = C.load(args.config)
    {"fetch": cmd_fetch, "profiles": cmd_profiles, "fit": cmd_fit, "run": cmd_run}[args.command](args, cfg)


if __name__ == "__main__":
    main()
