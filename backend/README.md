# Archer Core Backend

FastAPI service powering Archer V1 on NVIDIA DGX Spark.

## Quickstart

```bash
# Sync dependencies and create venv
uv sync --all-extras

# Run development server
uv run uvicorn app.main:app --reload --port 8000

# Run test suite
uv run pytest
```
