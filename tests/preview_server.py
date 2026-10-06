"""Isolated browser-check server. Never touches the user's data directory."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from waitress import serve

with tempfile.TemporaryDirectory(prefix="research-browser-") as directory:
    serve(create_app(directory, testing=True), host="127.0.0.1", port=8766)
