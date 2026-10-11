import pytest
import numpy as np
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import game_input

@pytest.fixture(autouse=True)
def mock_stdin_thread(monkeypatch):
    """Prevent GameInputManager from starting background stdin thread which crashes in pytest capture."""
    monkeypatch.setattr(game_input.threading.Thread, 'start', lambda self: None)

def test_input_manager_init():
    manager = game_input.GameInputManager()
    
def test_input_manager_process_bci_command():
    manager = game_input.GameInputManager()
    
    # Simulate commands coming from stdin / BCI
    manager.add_command("left")
    manager.update()
    assert manager.is_left() == True
    
    manager.add_command("right")
    manager.update()
    assert manager.is_right() == True

    manager.add_command("up")
    manager.update()
    assert manager.is_up() == True
    
    manager.add_command("down")
    manager.update()
    assert manager.is_down() == True

    manager.add_command("action")
    manager.update()
    assert manager.is_action() == True
