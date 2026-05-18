from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")


def _add_pipeline_repo_to_path() -> None:
    default_pipeline_path = Path(__file__).resolve().parents[1] / "alp-metrics-pipeline"
    pipeline_path = Path(os.getenv("ALP_PIPELINE_REPO_PATH", default_pipeline_path)).expanduser()
    if pipeline_path.exists():
        sys.path.insert(0, str(pipeline_path))


_add_pipeline_repo_to_path()

from backend import create_app

app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")))
