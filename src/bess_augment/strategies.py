"""Compliance strategies and the search for the cheapest augmentation (or replacement) schedule.

A plan is a sequence of actions: the initial build at year 1 and up to ``max_events`` later augmentations (new
modules added) or replacements (a share of the initial modules swapped for new ones). For a given timing, every
action is sized as the smallest that keeps the available energy at or above the requirement until the next action
(or the end of the contract), found by :func:`bess_augment.fleet.bridge`. With falling battery prices and
discounting, adding more than needed earlier only costs more, so the timing is the real decision. The search
enumerates every timing with up to ``max_events`` actions as a tree: each node is the fleet state after bridging to
an action year, shared by all timings with the same prefix, so a contract of L years with three events needs about
L^3 / 6 bridgings. Every complete timing is then valued with :func:`bess_augment.economics.cashflows` and the plan
with the highest NPV wins.

Strategies compared per battery and operating profile:

    none         build the operating energy, no action: compliance is lost in year 1 and the shortfall is paid for
    overbuild    (a) enough energy at the start to stay compliant until the end of the contract
    augment_fix  (b) augmentation at fixed years (config), each sized by bridging
    augment_opt  (b) the searched schedule (timing and sizes) with the highest NPV
    restrict     (c) the best plan under a restricted operating profile (cycle cap or SoC window), which ages more
                 slowly but earns less arbitrage revenue; built by the pipeline from the restricted profile
    replace_opt  (d) searched schedule of module replacements instead of augmentation

Usage:
    from bess_augment.strategies import search, compare
    ranked = search(case, value, kind="augment", max_events=3, min_bol=2.0)
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from bess_augment.fleet import Case, Event, Plan, bridge

log = logging.getLogger(__name__)


def bridge_plan(case: Case, years: list[int], kind: str = "augment", min_bol: float | None = None,
                hi_factor: float = 3.0) -> Plan | None:
    """Plan with actions at the given years, each sized by bridging to the next one (None if infeasible)."""
    min_bol = case.e_op if min_bol is None else min_bol
    stops = list(years) + [case.years + 1]
    size, fleet = bridge(None, case, "augment", 1, stops[0], min_bol, hi_factor * case.e_req)
    if not np.isfinite(size[0]):
        return None
    bol, events = float(size[0]), []
    for start, stop in zip(stops[:-1], stops[1:]):
        hi = 1.0 if kind == "replace" else hi_factor * case.e_req
        size, fleet = bridge(fleet, case, kind, start, stop, 0.0, hi)
        if not np.isfinite(size[0]):
            return None
        events.append(Event(start, kind, float(size[0])))
    name = "overbuild" if not years else ("augment" if kind == "augment" else "replace")
    return Plan(name, bol, tuple(events))


def search(case: Case, value: Callable[[Plan], float], kind: str = "augment", max_events: int = 3,
           first_year: int = 2, min_bol: float | None = None, hi_factor: float = 3.0) -> list[tuple[float, Plan]]:
    """All timings with up to ``max_events`` actions from ``first_year`` on, ranked by ``value(plan)`` (NPV)."""
    min_bol = case.e_op if min_bol is None else min_bol
    end = case.years + 1
    out: list[tuple[float, Plan]] = []

    def rec(fleet, start: int, bol: float, events: tuple[Event, ...]):
        last = len(events) + (fleet is not None) >= max_events      # no action left after this one
        for stop in ([end] if last else list(range(max(start + 1, first_year), end)) + [end]):
            if fleet is None:
                size, nxt = bridge(None, case, "augment", 1, stop, min_bol, hi_factor * case.e_req)
            else:
                size, nxt = bridge(fleet, case, kind, start, stop, 0.0, 1.0 if kind == "replace" else hi_factor * case.e_req)
            s = float(size[0])
            if not np.isfinite(s):
                break                                    # a longer bridge needs even more
            new_bol, new_events = (s, events) if fleet is None else (bol, events + (Event(start, kind, s),))
            if stop == end:
                plan = Plan(kind if new_events else "overbuild", new_bol, new_events)
                out.append((value(plan), plan))
            else:
                rec(nxt, stop, new_bol, new_events)

    rec(None, 1, 0.0, ())
    out.sort(key=lambda t: -t[0])
    log.debug("search %s: %d plans", kind, len(out))
    return out
