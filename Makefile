.PHONY: test lint demo benchmark gateway mock mcp

test:
	python -m pytest -q

lint:
	ruff check .
	ruff format --check .

demo:
	python scripts/demo.py

benchmark:
	python scripts/benchmark.py

gateway:
	uvicorn quilr_assessment.app:create_app --factory --host 127.0.0.1 --port 8000

mock:
	uvicorn quilr_assessment.mock_provider:app --host 127.0.0.1 --port 9001

mcp:
	python -m quilr_assessment.mcp_server
