import pytest
import numpy as np
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import hybrid_dsp_pipeline
from prediction_dashboard import compute_psd, get_band_power
import advanced_dashboard

def test_prediction_dashboard_compute_psd():
    sfreq = 256.0
    N = int(sfreq * 2)
    t = np.arange(N) / sfreq
    signal = np.sin(2 * np.pi * 10 * t) + 0.5 * np.sin(2 * np.pi * 50 * t)
    
    freqs, psd = compute_psd(signal, sfreq)
    
    assert len(freqs) > 0
    assert len(psd) == len(freqs)
    
    # 50 Hz should be notched out, so 10Hz power > 50Hz power
    alpha_power = get_band_power(freqs, psd, 8.0, 12.0)
    mains_power = get_band_power(freqs, psd, 45.0, 55.0)
    
    assert alpha_power > mains_power * 10

def test_prediction_dashboard_get_band_power_empty():
    freqs = np.array([0, 1, 2, 3])
    psd = np.array([0, 1, 2, 3])
    
    bp = get_band_power(freqs, psd, 10.0, 20.0)
    assert bp == 1e-12

def test_advanced_dashboard_integrate_band_powers():
    freqs = np.array([0, 1, 2, 3, 4, 5], dtype=np.float32)
    psd = np.array([0, 1, 1, 1, 0, 0], dtype=np.float32)
    band_edges = np.array([1.0, 3.0, 5.0], dtype=np.float32)
    
    band_powers = advanced_dashboard.integrate_band_powers(freqs, psd, band_edges)
    
    assert len(band_powers) == 2
    # df = 1.0. For 1.0 to 3.0, psd = 1(f=1), 1(f=2), 1(f=3). Sum = 3.0
    assert np.isclose(band_powers[0], 3.0)
    # For 3.0 to 5.0, psd = 1(f=3), 0(f=4), 0(f=5). Sum = 1.0. Note <= condition means f=3 is counted.
    assert np.isclose(band_powers[1], 1.0)

def test_advanced_dashboard_hybrid_dsp_pipeline():
    sfreq = 250.0
    N = int(sfreq * 4)
    t = np.arange(N) / sfreq
    signal = np.sin(2 * np.pi * 10 * t)
    
    band_edges = np.array([1.0, 4.0, 8.0, 12.0, 30.0, 50.0], dtype=np.float32)
    
    freqs, psd, band_powers = advanced_dashboard.hybrid_dsp_pipeline(signal, sfreq, band_edges)
    
    assert len(freqs) > 0
    assert len(psd) == len(freqs)
    assert len(band_powers) == 5
