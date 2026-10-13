import pathlib
import sys

# the contract engine tests reuse helpers that live one level up (test_spans.Collector)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
