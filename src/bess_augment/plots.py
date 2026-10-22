"""Charts (PNG for the README) and the interactive dashboard (docs/index.html, Plotly.js from a CDN).

Colours follow a fixed categorical order (one colour per strategy and per operating profile, never re-cycled), the
requirement is drawn as a dashed ink line, and every chart carries a legend.

Usage:
    from bess_augment import plots
    plots.make_all(cfg, profile_table, res, risk, bands, warranty, fit, Path("docs/img"), Path("docs/index.html"))
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from bess_augment import config as C

log = logging.getLogger(__name__)

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
STRATEGIES = {"none": "No action", "overbuild": "(a) Overbuild", "augment_fix": "(b) Augment, fixed years",
              "augment_opt": "(b) Augment, optimised", "replace_opt": "(d) Replace modules, optimised",
              "restrict_rce_1c": "(c) Max 1 cycle/day", "restrict_rce_2c": "(c) Max 2 cycles/day",
              "restrict_rce_w10": "(c) SoC window 10-90 %", "restrict_rce_w20": "(c) SoC window 20-80 %"}
STRAT_COLOR = {"none": MUTED, "overbuild": SERIES[0], "augment_fix": SERIES[1], "augment_opt": SERIES[2],
               "replace_opt": SERIES[3], "restrict_rce_1c": SERIES[4], "restrict_rce_2c": SERIES[5],
               "restrict_rce_w10": SERIES[6], "restrict_rce_w20": SERIES[7]}
PROFILES = ["rce_opt", "rce_2c", "rce_1c", "rce_w10", "rce_w20", "reserves"]
PROF_COLOR = dict(zip(PROFILES, SERIES))
TEMP_COLOR = {20.0: SERIES[0], 25.0: SERIES[2], 30.0: SERIES[3], 35.0: SERIES[1]}
FONT = "system-ui, -apple-system, Segoe UI, sans-serif"


def _layout(fig: go.Figure, title: str, height: int = 430, width: int = 900, **kw) -> go.Figure:
    opts = dict(title=dict(text=title, x=0.01, font=dict(size=15, color=INK)), height=height, width=width,
                paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, font=dict(family=FONT, size=12, color=INK2),
                legend=dict(orientation="h", y=-0.18, x=0, font=dict(size=11)),
                margin=dict(l=60, r=20, t=60, b=80), hovermode="x unified")
    opts.update(kw)
    fig.update_layout(**opts)
    fig.update_xaxes(gridcolor=GRID, linecolor=AXIS, zeroline=False, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, zeroline=False, tickfont=dict(color=MUTED))
    return fig


def _plabel(cfg: dict, name: str) -> str:
    return cfg["profiles"][name]["label"]


def soh_by_profile(cfg: dict, paths: pd.DataFrame) -> go.Figure:
    """Available energy without headroom (share of the initial energy) by profile, 2 h and 4 h."""
    t = cfg["analysis"]["temp_c"]
    durs = [2.0, 4.0]
    fig = make_subplots(1, 2, subplot_titles=[f"{d:g} h battery" for d in durs], shared_yaxes=True,
                        horizontal_spacing=0.05)
    for j, d in enumerate(durs, 1):
        for name in PROFILES:
            p = paths[(paths["profile"] == name) & (paths["duration_h"] == d) & (paths["temp_c"] == t)]
            if p.empty:
                continue
            x = np.r_[0, p["year"]]
            y = np.r_[1.0, p["avail_end_mwh"] / d] * 100
            fig.add_scatter(x=x, y=y, row=1, col=j, name=_plabel(cfg, name), legendgroup=name, showlegend=j == 1,
                            line=dict(color=PROF_COLOR[name], width=2),
                            hovertemplate="%{y:.1f} %<extra>" + _plabel(cfg, name) + "</extra>")
        req = C.requirement_mwh(cfg, 1.0, d) / d * 100
        fig.add_hline(req, line=dict(color=INK, width=1.5, dash="dash"), row=1, col=j,
                      annotation_text="4 h at net achievable power", annotation_position="bottom right",
                      annotation_font_color=INK2)
        for v, spec in cfg["capacity_market"]["vintages"].items():
            call = C.requirement_mwh(cfg, 1.0, d, v, mode="call") / d * 100
            if call < 99:
                fig.add_hline(call, line=dict(color=MUTED, width=1, dash="dot"), row=1, col=j,
                              annotation_text=f"call 6.08.2026, {spec['label']}", annotation_position="bottom right",
                              annotation_font_color=MUTED)
    fig.update_xaxes(title_text="contract year")
    fig.update_yaxes(title_text="available energy, % of initial", row=1, col=1)
    fig.update_yaxes(range=[0, 104])
    return _layout(fig, f"Available energy of a battery built to its nominal energy ({t:g} degC)", height=520)


def soh_by_temperature(cfg: dict, paths: pd.DataFrame, profile: str = "rce_opt") -> go.Figure:
    durs = [2.0, 4.0]
    fig = make_subplots(1, 2, subplot_titles=[f"{d:g} h battery" for d in durs], shared_yaxes=True,
                        horizontal_spacing=0.05)
    for j, d in enumerate(durs, 1):
        for t in cfg["analysis"]["temperatures_c"]:
            p = paths[(paths["profile"] == profile) & (paths["duration_h"] == d) & (paths["temp_c"] == t)]
            fig.add_scatter(x=np.r_[0, p["year"]], y=np.r_[1.0, p["soh_initial_modules"]] * 100, row=1, col=j,
                            name=f"{t:g} degC", legendgroup=str(t), showlegend=j == 1,
                            line=dict(color=TEMP_COLOR.get(float(t), MUTED), width=2),
                            hovertemplate="%{y:.1f} %<extra>" + f"{t:g} degC</extra>")
    fig.update_xaxes(title_text="contract year")
    fig.update_yaxes(title_text="state of health, %", row=1, col=1)
    return _layout(fig, f"State of health by cell temperature: {_plabel(cfg, profile)}", height=450)


def strategy_energy(cfg: dict, paths: pd.DataFrame, profile: str) -> go.Figure:
    t = cfg["analysis"]["temp_c"]
    durs = [2.0, 4.0]
    fig = make_subplots(1, 2, subplot_titles=[f"{d:g} h battery" for d in durs], horizontal_spacing=0.07)
    for j, d in enumerate(durs, 1):
        p = paths[(paths["profile"] == profile) & (paths["duration_h"] == d) & (paths["temp_c"] == t)]
        for s in ["none", "overbuild", "augment_fix", "augment_opt", "replace_opt"]:
            q = p[p["strategy"] == s]
            if q.empty:
                continue
            ys = q["avail_start_mwh"].to_numpy()
            ye = q["avail_end_mwh"].to_numpy()
            xx = np.ravel(np.c_[q["year"] - 1, q["year"]])                     # saw-tooth: start and end of each year
            yy = np.ravel(np.c_[ys, ye])
            fig.add_scatter(x=xx, y=yy, row=1, col=j, name=STRATEGIES[s], legendgroup=s, showlegend=j == 1,
                            line=dict(color=STRAT_COLOR[s], width=2),
                            hovertemplate="%{y:.2f} MWh<extra>" + STRATEGIES[s] + "</extra>")
        req = C.requirement_mwh(cfg, 1.0, d)
        fig.add_hline(req, line=dict(color=INK, width=1.5, dash="dash"), row=1, col=j,
                      annotation_text="requirement", annotation_font_color=INK2)
        fig.update_yaxes(title_text="available energy, MWh" if j == 1 else None, row=1, col=j)
    fig.update_xaxes(title_text="contract year")
    return _layout(fig, f"Available energy by strategy ({_plabel(cfg, profile)}, {t:g} degC)", height=470)


def npv_bars(cfg: dict, strat: pd.DataFrame, profile: str) -> go.Figure:
    t = cfg["analysis"]["temp_c"]
    s = strat[(strat["profile"] == profile) & (strat["temp_c"] == t) & (strat["scenario"] == "base")]
    durs = [2.0, 4.0]
    fig = make_subplots(1, 2, subplot_titles=[f"{d:g} h battery" for d in durs], horizontal_spacing=0.28)
    for j, d in enumerate(durs, 1):
        q = s[s["duration_h"] == d].sort_values("npv")
        v = (q["npv_vs_best"] / 1e3).round(0) + 0.0                       # + 0.0 turns -0 into 0
        fig.add_bar(y=[STRATEGIES.get(x, x) for x in q["strategy"]], x=v, orientation="h",
                    marker=dict(color=[STRAT_COLOR.get(x, MUTED) for x in q["strategy"]], cornerradius=4),
                    row=1, col=j, showlegend=False, text=[f"{x:,.0f}" for x in v], textposition="outside",
                    textfont=dict(color=INK2), cliponaxis=False,
                    hovertemplate="%{x:,.0f} k PLN vs best<extra></extra>")
        fig.update_xaxes(title_text="NPV vs best strategy, k PLN per MW", range=[v.min() * 1.18, -v.min() * 0.04],
                         row=1, col=j)
    return _layout(fig, f"Cost of each strategy against the cheapest ({_plabel(cfg, profile)}, {t:g} degC)",
                   height=440, hovermode="closest")


def schedule_heatmap(grid: pd.DataFrame, duration: float, profile: str, temp: float) -> go.Figure:
    g = grid[(grid["duration_h"] == duration) & (grid["profile"] == profile) & (grid["temp_c"] == temp)]
    m = g.pivot(index="year_2", columns="year_1", values="npv") / 1e3
    best = m.max().max()
    fig = go.Figure(go.Heatmap(z=(m - best).to_numpy(), x=m.columns, y=m.index,
                               colorscale=[[0, "#0d366b"], [0.5, "#5598e7"], [1, "#cde2fb"]],
                               colorbar=dict(title="k PLN vs best"),
                               hovertemplate="augment in years %{x} and %{y}: %{z:,.0f} k PLN<extra></extra>"))
    fig.update_xaxes(title_text="first augmentation (contract year)", dtick=1)
    fig.update_yaxes(title_text="second augmentation", dtick=1)
    return _layout(fig, f"NPV of two-augmentation schedules, {duration:g} h battery, against the best one",
                   height=480, width=640, hovermode="closest")


def risk_bands(cfg: dict, bands: pd.DataFrame, duration: float, profile: str) -> go.Figure:
    b = bands[(bands["duration_h"] == duration) & (bands["profile"] == profile)]
    fig = go.Figure()
    show = [("overbuild", "central", "(a) Overbuild, central design"),
            ("augment_opt", "central", "(b) Augment, central design, fixed sizes"),
            ("augment_opt", "P90", "(b) Augment, P90 first segment, adaptive sizes")]
    for k, (s, design, name) in enumerate(show):
        q = b[(b["strategy"] == s) & (b["design"] == design)]
        if q.empty:
            continue
        color = [SERIES[0], SERIES[1], SERIES[2]][k]
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        fig.add_scatter(x=np.r_[q["year"], q["year"][::-1]], y=np.r_[q["p95"], q["p05"][::-1]], fill="toself",
                        fillcolor=f"rgba{rgb + (0.15,)}", line=dict(width=0), hoverinfo="skip", showlegend=False,
                        legendgroup=name)
        fig.add_scatter(x=q["year"], y=q["p50"], name=name + " (median, 5-95 %)", legendgroup=name,
                        line=dict(color=color, width=2), hovertemplate="%{y:.2f} MWh<extra>" + name + "</extra>")
    fig.add_hline(C.requirement_mwh(cfg, 1.0, duration), line=dict(color=INK, width=1.5, dash="dash"),
                  annotation_text="requirement", annotation_font_color=INK2)
    fig.update_xaxes(title_text="contract year")
    fig.update_yaxes(title_text="available energy at year end, MWh")
    return _layout(fig, f"Uncertainty of the ageing model: {duration:g} h battery, {_plabel(cfg, profile)}",
                   height=480)


def sensitivity(cfg: dict, strat: pd.DataFrame) -> go.Figure:
    """NPV advantage of the optimised augmentation over overbuild, for each sensitivity."""
    a = cfg["analysis"]
    rows = []
    s = strat
    for d in [2.0, 4.0]:
        def adv(sel, label, group):
            pv = sel.pivot_table(index="strategy", values="npv", aggfunc="first")
            if {"augment_opt", "overbuild"} <= set(pv.index):
                rows.append({"duration_h": d, "label": label, "group": group,
                             "adv": float(pv.loc["augment_opt", "npv"] - pv.loc["overbuild", "npv"])})
        base = s[(s["duration_h"] == d) & (s["profile"] == a["base_profile"])]
        for t in a["temperatures_c"]:
            adv(base[(base["temp_c"] == t) & (base["scenario"] == "base")], f"{t:g} degC", "cell temperature")
        for sc in base.loc[base["temp_c"] == a["temp_c"], "scenario"].unique():
            if sc != "base":
                kind, val = sc.split("_", 1)
                grp = {"decline": "battery price path", "discount": "discount rate (real)",
                       "vintage": "capacity contract"}[kind]
                label = cfg["capacity_market"]["vintages"][val]["label"] if kind == "vintage" else val
                adv(base[(base["temp_c"] == a["temp_c"]) & (base["scenario"] == sc)], label, grp)
        for name in a["strategy_profiles"]:
            sel = s[(s["duration_h"] == d) & (s["profile"] == name) & (s["temp_c"] == a["temp_c"])
                    & (s["scenario"] == "base")]
            adv(sel, _plabel(cfg, name), "operating profile")
    df = pd.DataFrame(rows)
    fig = make_subplots(1, 2, subplot_titles=["2 h battery", "4 h battery"], shared_yaxes=True,
                        horizontal_spacing=0.03)
    groups = list(dict.fromkeys(df["group"]))
    for j, d in enumerate([2.0, 4.0], 1):
        q = df[df["duration_h"] == d]
        for k, g in enumerate(groups):
            r = q[q["group"] == g]
            fig.add_bar(y=[f"{g}: {x}" for x in r["label"]], x=r["adv"] / 1e3, orientation="h", name=g,
                        marker=dict(color=SERIES[k], cornerradius=4), legendgroup=g, showlegend=j == 1, row=1, col=j,
                        hovertemplate="%{x:,.0f} k PLN<extra></extra>")
        fig.update_xaxes(title_text="k PLN per MW", row=1, col=j)
    fig.update_yaxes(autorange="reversed")
    return _layout(fig, "Sensitivity: NPV of the optimised augmentation minus NPV of overbuild", height=620,
                   width=1000, hovermode="closest")


def warranty_chart(cfg: dict, warranty: pd.DataFrame) -> go.Figure:
    w = cfg["warranty"]
    years = np.arange(0, w["years"] + 1)
    fig = make_subplots(1, 2, subplot_titles=["2 h battery", "4 h battery"], shared_yaxes=True,
                        horizontal_spacing=0.05)
    for j, d in enumerate([2.0, 4.0], 1):
        for name in PROFILES:
            r = warranty[(warranty["profile"] == name) & (warranty["duration_h"] == d)]
            if r.empty:
                continue
            fig.add_scatter(x=years, y=years * float(r["fec_per_year"].iloc[0]), name=_plabel(cfg, name),
                            legendgroup=name, showlegend=j == 1, line=dict(color=PROF_COLOR[name], width=2),
                            row=1, col=j, hovertemplate="%{y:,.0f} cycles<extra>" + _plabel(cfg, name) + "</extra>")
        fig.add_hline(w["fec_total"], line=dict(color=INK, width=1.5, dash="dash"), row=1, col=j,
                      annotation_text="throughput allowance", annotation_font_color=INK2)
    fig.update_xaxes(title_text="year of operation")
    fig.update_yaxes(title_text="cumulative equivalent full cycles", row=1, col=1)
    return _layout(fig, f"Throughput against a generalised warranty ({w['fec_per_year']:g} cycles/year, "
                        f"{w['fec_total']:,} in {w['years']} years)", height=470)


def revenue_tradeoff(cfg: dict, ptable: pd.DataFrame, comp: pd.DataFrame) -> go.Figure:
    t = cfg["analysis"]["temp_c"]
    fig = make_subplots(1, 2, subplot_titles=["2 h battery", "4 h battery"], horizontal_spacing=0.08)
    for j, d in enumerate([2.0, 4.0], 1):
        for name in PROFILES:
            p = ptable[(ptable["profile"] == name) & (ptable["duration_h"] == d)]
            c = comp[(comp["profile"] == name) & (comp["duration_h"] == d) & (comp["temp_c"] == t)
                     & (comp["bol_margin"] == 0)]
            if p.empty or c.empty or not np.isfinite(p["revenue_pln_per_mw_year"].iloc[0]):
                continue
            fig.add_scatter(x=[float(p["cycles_per_day"].iloc[0])], y=[float(p["revenue_pln_per_mw_year"].iloc[0]) / 1e3],
                            mode="markers+text", text=[f"SoH {c['soh_end'].iloc[0]:.0%}"],
                            textposition="bottom center" if name == "rce_2c" else "top center",
                            marker=dict(size=12, color=PROF_COLOR[name], line=dict(color=SURFACE, width=2)),
                            name=_plabel(cfg, name), legendgroup=name, showlegend=j == 1, row=1, col=j,
                            hovertemplate="%{x:.2f} cycles/day, %{y:,.0f} k PLN/MW-yr<extra>" + _plabel(cfg, name)
                                          + "</extra>")
        fig.update_xaxes(title_text="cycles per day", row=1, col=j)
    fig.update_yaxes(title_text="arbitrage revenue, k PLN per MW-year", row=1, col=1)
    return _layout(fig, "Arbitrage revenue (perfect foresight, Oct 2025 - Sep 2026) against cycling; "
                        "label: SoH after the contract", height=470, hovermode="closest")


def cell_fit(fit: dict) -> go.Figure:
    from bess_augment.aging import load_cells
    cells = load_cells()
    m = fit["model"]
    fig = go.Figure()
    k = 0
    for (mode, temp, soc), g in cells.groupby(["mode", "chamber_temp_c", "storage_soc"], dropna=False):
        c0 = pd.concat([fit["calendar"].c0, fit["cycle"].c0])
        lab = f"{mode} {temp:g} degC" + ("" if pd.isna(soc) else f", SoC {soc:g} %")
        color = SERIES[k % len(SERIES)]
        for cell, h in g.groupby("cell"):
            h = h[h["checkup"] > 0]
            fig.add_scatter(x=h["days"], y=h["q_dis_ah"] / c0[cell] * 100, mode="markers", name=lab,
                            legendgroup=lab, showlegend=cell == g["cell"].iloc[0],
                            marker=dict(size=8, color=color, line=dict(color=SURFACE, width=1)),
                            hovertemplate="%{y:.1f} %<extra>" + cell + "</extra>")
        days = np.linspace(0, 900, 60)
        if mode == "calendar":
            pred = 1 - m.calendar_loss(days, soc / 100, temp)
        else:
            temp_c = float(g["cell_temp_c"].mean())
            socs = np.linspace(0, 1, 21)
            kc = np.mean(m.k_cal_at(socs, temp_c) ** 2) ** 0.5
            n = np.interp(days, np.r_[0, g.sort_values("days")["days"].iloc[1:]], np.r_[0, g.sort_values("days")["checkup"].iloc[1:]])
            pred = 1 - kc * days ** 0.5 - m.cycle_loss(n, temp_c)
        fig.add_scatter(x=days, y=pred * 100, mode="lines", line=dict(color=color, width=2), legendgroup=lab,
                        showlegend=False, hoverinfo="skip")
        k += 1
    fig.update_xaxes(title_text="days in test")
    fig.update_yaxes(title_text="capacity, % of initial")
    return _layout(fig, "Ageing model fitted to 12 LFP cells (Yagci et al. 2025): points measured, lines fitted",
                   height=480, hovermode="closest")


# ---------------------------------------------------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------------------------------------------------

def dashboard_data(cfg: dict, res: dict, bands: pd.DataFrame) -> dict:
    paths, strat, soh = res["paths"], res["strategies"], res["soh_path"]
    cases = {}
    for (d, name, t), p in soh.groupby(["duration_h", "profile", "temp_c"]):
        key = f"{d:g}|{name}|{t:g}"
        pw = cfg["battery"]["power_mw"]
        entry = {"e_op": d * pw, "e_req": C.requirement_mwh(cfg, pw, d), "kwd": C.kwd(cfg),
                 "p_net": C.net_power(cfg, pw, d), "obligation": C.obligation_mw(cfg, pw, d), "strategies": {}}
        entry["strategies"]["none"] = {"year": p["year"].tolist(), "start": p["avail_start_mwh"].round(4).tolist(),
                                       "end": p["avail_end_mwh"].round(4).tolist(),
                                       "installed": p["installed_mwh"].round(4).tolist(),
                                       "soh": p["soh_initial_modules"].round(4).tolist()}
        q = paths[(paths["duration_h"] == d) & (paths["profile"] == name) & (paths["temp_c"] == t)]
        sv = strat[(strat["duration_h"] == d) & (strat["profile"] == name) & (strat["temp_c"] == t)
                   & (strat["scenario"] == "base")]
        for s, g in q.groupby("strategy"):
            entry["strategies"][s] = {
                "year": g["year"].tolist(), "start": g["avail_start_mwh"].round(4).tolist(),
                "end": g["avail_end_mwh"].round(4).tolist(), "installed": g["installed_mwh"].round(4).tolist(),
                "soh": g["soh_initial_modules"].round(4).tolist(),
                "cf": {c: (g[c] / 1e3).round(2).tolist() for c in
                       ["capacity_pln", "arbitrage_pln", "penalty_pln", "om_pln", "augment_pln", "replace_pln"]}}
        entry["npv"] = {r["strategy"]: {"npv": round(r["npv"] / 1e3, 1), "vs_best": round(r["npv_vs_best"] / 1e3, 1),
                                        "plan": r["plan"], "capex0": round(r["capex0"] / 1e3, 1),
                                        "short": round(r["shortfall_mw_years"], 3),
                                        "warranty_break": int(r["warranty_break_year"])}
                        for _, r in sv.iterrows()}
        b = bands[(bands["duration_h"] == d) & (bands["profile"] == name)] if t == cfg["analysis"]["temp_c"] else bands.iloc[:0]
        entry["bands"] = {f"{s}|{des}": {"p05": g["p05"].round(4).tolist(), "p50": g["p50"].round(4).tolist(),
                                         "p95": g["p95"].round(4).tolist()}
                          for (s, des), g in b.groupby(["strategy", "design"])}
        cases[key] = entry
    return {"cases": cases, "profiles": {n: cfg["profiles"][n]["label"] for n in cfg["profiles"]},
            "strategies": STRATEGIES, "colors": STRAT_COLOR, "durations": cfg["battery"]["durations_h"],
            "temps": cfg["analysis"]["temperatures_c"], "base_temp": cfg["analysis"]["temp_c"],
            "vintage": C.vintage(cfg)["label"],
            "years": cfg["capacity_market"]["contract_years"]}


def write_dashboard(data: dict, path: Path) -> None:
    template = (Path(__file__).parent / "resources" / "dashboard.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def make_all(cfg: dict, ptable: pd.DataFrame, res: dict, risk: pd.DataFrame, bands: pd.DataFrame,
             warranty: pd.DataFrame, fit: dict, img_dir: Path, dashboard: Path) -> None:
    img_dir.mkdir(parents=True, exist_ok=True)
    a = cfg["analysis"]
    figs = {
        "soh_profiles": soh_by_profile(cfg, res["soh_path"]),
        "soh_temperature": soh_by_temperature(cfg, res["soh_path"], a["base_profile"]),
        "strategy_energy": strategy_energy(cfg, res["paths"], a["base_profile"]),
        "npv_strategies": npv_bars(cfg, res["strategies"], a["base_profile"]),
        "schedule_heatmap": schedule_heatmap(res["schedule_grid"], 2.0, a["base_profile"], a["temp_c"]),
        "risk_bands": risk_bands(cfg, bands, 2.0, a["base_profile"]),
        "sensitivity": sensitivity(cfg, res["strategies"]),
        "warranty": warranty_chart(cfg, warranty),
        "revenue_tradeoff": revenue_tradeoff(cfg, ptable, res["compliance"]),
        "cell_fit": cell_fit(fit),
    }
    for name, fig in figs.items():
        fig.write_image(img_dir / f"{name}.png", scale=2)
    write_dashboard(dashboard_data(cfg, res, bands), dashboard)
    log.info("charts in %s, dashboard %s", img_dir, dashboard)
