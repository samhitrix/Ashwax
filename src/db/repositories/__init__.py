"""Repository layer — each module wraps one domain of DB tables."""

from src.db.repositories.learning import LearningRepository
from src.db.repositories.market_data import MarketDataRepository
from src.db.repositories.news import NewsRepository
from src.db.repositories.reporting import ReportingRepository
from src.db.repositories.supervisor import SupervisorRepository
from src.db.repositories.trades import TradeRepository

__all__ = [
    "TradeRepository",
    "SupervisorRepository",
    "MarketDataRepository",
    "NewsRepository",
    "LearningRepository",
    "ReportingRepository",
]
