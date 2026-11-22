"""Setup fingerprint hasher for similarity lookup."""

from __future__ import annotations

import hashlib
import json

from src.strategies.backtest.pre_trade import SetupFingerprint


class FingerprintHasher:
    def hash(self, fp: SetupFingerprint, precision: int = 1) -> str:
        key = {
            "strategy": fp.strategy_name,
            "regime": fp.regime,
            "vix_bucket": round(fp.vix / 5) * 5,
            "rs_bucket": round(fp.rs_rank / 10) * 10,
            "quality_bucket": round(fp.setup_quality_score / 10) * 10,
            "vol_ratio_bucket": round(fp.volume_ratio, precision),
        }
        return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]
