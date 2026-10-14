"""News Engine — multi-source poll → classify → route impact."""

from src.news.classifier import NewsClassification, NewsClassifier
from src.news.impact_router import ImpactRouter
from src.news.poller import NewsItem, NewsPoller

__all__ = [
    "NewsClassification",
    "NewsClassifier",
    "ImpactRouter",
    "NewsItem",
    "NewsPoller",
]
