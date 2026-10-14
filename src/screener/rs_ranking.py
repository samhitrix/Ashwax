"""
RS Ranking Engine — re-export shim.

Spec §8.2 locates this module at src/screener/rs_ranking.py.
The implementation lives in src/market_data/rs_ranking.py (historical location).
"""

# Explicit re-exports required by mypy --no-implicit-reexport
from src.market_data.rs_ranking import RSRankingEngine as RSRankingEngine
from src.market_data.rs_ranking import RSRankingResult as RSRankingResult
from src.market_data.rs_ranking import RSRecord as RSRecord
