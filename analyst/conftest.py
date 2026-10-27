# The analyst's tests need its own requirements and a model-free fake, so the
# platform's CI (which installs only ../requirements.txt) skips them.
# Run them with: ANALYST_TESTS=1 .venv/bin/python -m pytest -q analyst
import os

collect_ignore_glob = [] if os.environ.get("ANALYST_TESTS") == "1" else ["*"]
