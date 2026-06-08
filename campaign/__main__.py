"""campaign.__main__ — run the end-to-end demo via `python -m campaign`."""
import asyncio
import importlib.util
from pathlib import Path

_demo_path = Path(__file__).parent / "examples" / "demo.py"
_spec = importlib.util.spec_from_file_location("campaign.examples.demo", _demo_path)
_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_demo)

asyncio.run(_demo.main())
