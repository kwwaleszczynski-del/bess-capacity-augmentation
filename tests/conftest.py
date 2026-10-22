import numpy as np
import pytest

from bess_augment.aging import AgingModel, profile_stress


@pytest.fixture
def model():
    return AgingModel(k_cal=1.6e-3, ea_cal=30_000, gamma=0.0, k_cyc=2.1e-5, ea_cyc=32_000)


@pytest.fixture
def daily_cycle_stress():
    """One full cycle a day for a year, 15-minute steps: 48 steps down, 48 up."""
    day = np.r_[np.linspace(1.0, 0.0, 49)[1:], np.linspace(0.0, 1.0, 49)[1:]]
    soc = np.r_[1.0, np.tile(day, 365)]
    return profile_stress(soc, dt_hours=0.25)
