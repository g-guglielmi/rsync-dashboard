# Placing a conftest.py at the repo root makes pytest add this directory to
# sys.path, so the tests can `import log_parser` regardless of how pytest
# is invoked (plain `pytest` vs `python -m pytest`).
