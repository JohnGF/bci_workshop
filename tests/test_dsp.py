import pytest
import numpy as np
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import hybrid_dsp_pipeline, integrate_custom_bands

def test_hybrid_dsp_pipeline_sinewave():
    """Test hybrid_dsp_pipeline with a pure sine wave."""
    sfreq = 250.0
    duration = 4.0
    N = int(sfreq * duration)
    t = np.arange(N) / sfreq
    
    # 10 Hz alpha wave + 50 Hz power line noise
    signal = np.sin(2 * np.pi * 10 * t) + 0.5 * np.sin(2 * np.pi * 50 * t)
    
    band_ranges = np.array([
        [8.0, 12.0],  # Alpha
        [45.0, 55.0]  # Mains noise (should be attenuated)
    ], dtype=np.float32)
    
    freqs, psd, band_powers = hybrid_dsp_pipeline(
        signal, sfreq, band_ranges, notch_freq=50.0, notch_q=30.0, highpass=1.0, lowpass=100.0
    )
    
    assert len(freqs) > 0
    assert len(psd) == len(freqs)
    assert len(band_powers) == 2
    
    # Check that alpha power is significantly higher than 50Hz power 
    # since we applied a 50Hz notch filter.
    assert band_powers[0] > band_powers[1] * 10 

def test_hybrid_dsp_pipeline_empty_bands():
    """Test pipeline when no band ranges are provided."""
    sfreq = 256.0
    signal = np.random.randn(int(sfreq * 2))
    freqs, psd, band_powers = hybrid_dsp_pipeline(signal, sfreq, band_ranges=None)
    
    assert len(band_powers) == 0

def test_integrate_custom_bands():
    """Test numba-optimized integration."""
    freqs = np.array([0, 1, 2, 3, 4, 5], dtype=np.float32)
    psd = np.array([0, 1, 1, 1, 0, 0], dtype=np.float32)
    band_ranges = np.array([
        [1.0, 3.0], 
        [4.0, 5.0]
    ], dtype=np.float32)
    
    band_powers = integrate_custom_bands(freqs, psd, band_ranges)
    
    assert len(band_powers) == 2
    # df = 1.0. For 1.0 to 3.0, psd = 1, 1, 1. Sum = 3.0
    assert np.isclose(band_powers[0], 3.0)
    # For 4.0 to 5.0, psd = 0, 0. Sum = 0.0
    assert np.isclose(band_powers[1], 0.0)
