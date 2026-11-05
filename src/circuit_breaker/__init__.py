"""Circuit breaker module — enhanced circuit breakers per docs/09-multiprocess-engine.md."""

from src.circuit_breaker.breakers import (
    CircuitBreakerEngine,
    CircuitBreakerResult,
    CircuitBreakerState,
)

__all__ = ["CircuitBreakerState", "CircuitBreakerResult", "CircuitBreakerEngine"]
