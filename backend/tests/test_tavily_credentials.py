"""Private local Tavily credential entry and storage."""

import importlib.util
from pathlib import Path

import pytest

from app.config import settings
from app.plugins.web_search import tavily_key


def test_hidden_key_entry_creates_private_file_without_overwrite(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/set-tavily-key.py"
    spec = importlib.util.spec_from_file_location("set_tavily_key", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "synthetic-key")
    module.main()
    stored = tmp_path / ".config/archer/tavily-api-key"
    assert stored.read_text() == "synthetic-key"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert "synthetic-key" not in capsys.readouterr().out
    monkeypatch.setattr(settings, "tavily_api_key_file", stored)
    assert tavily_key() == "synthetic-key"
    with pytest.raises(FileExistsError):
        module.main()
    assert stored.read_text() == "synthetic-key"
