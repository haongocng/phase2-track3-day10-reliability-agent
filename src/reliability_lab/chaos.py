from __future__ import annotations

import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs."""
    recovery_times = []
    for breaker in gateway.breakers.values():
        last_open_ts = None
        for entry in breaker.transition_log:
            to_state = entry.get("to")
            if to_state == "open":
                last_open_ts = entry.get("ts")
            elif to_state == "closed":
                if last_open_ts is not None and isinstance(last_open_ts, (int, float)) and isinstance(entry.get("ts"), (int, float)):
                    recovery_times.append((entry["ts"] - last_open_ts) * 1000)
                    last_open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()

    for _ in range(config.load_test.requests):
        if not queries:
            break
        query = random.choice(queries)
        result = gateway.complete(query)

        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost

        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            # Covers "primary" and "cache_hit" routes
            metrics.successful_requests += 1

        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    # Clean up Redis connection if necessary
    if gateway.cache is not None and hasattr(gateway.cache, "close"):
        try:
            gateway.cache.close()
        except Exception:
            pass

    # Count circuit opens
    circuit_open_count = 0
    for breaker in gateway.breakers.values():
        for entry in breaker.transition_log:
            if entry.get("to") == "open":
                circuit_open_count += 1

    metrics.circuit_open_count = circuit_open_count
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        # Define pass/fail criteria per scenario
        if scenario.name == "primary_timeout_100":
            passed = result.availability >= 0.85
        elif scenario.name == "primary_flaky_50":
            passed = result.availability >= 0.90
        elif scenario.name == "all_healthy":
            passed = result.availability >= 0.95
        else:
            passed = result.availability >= 0.80

        combined.scenarios[scenario.name] = "pass" if passed else "fail"

        # Accumulate metrics
        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    # Cache vs No-Cache Comparison Scenarios
    import copy

    no_cache_config = copy.deepcopy(config)
    no_cache_config.cache.enabled = False

    # 1. all_healthy with cache disabled
    scenario_no_cache_1 = ScenarioConfig(
        name="all_healthy_no_cache",
        description="Baseline with cache disabled",
        provider_overrides={}
    )
    result_nc1 = run_scenario(no_cache_config, queries, scenario_no_cache_1)
    passed_nc1 = result_nc1.availability >= 0.95
    combined.scenarios["all_healthy_no_cache"] = "pass" if passed_nc1 else "fail"

    combined.total_requests += result_nc1.total_requests
    combined.successful_requests += result_nc1.successful_requests
    combined.failed_requests += result_nc1.failed_requests
    combined.fallback_successes += result_nc1.fallback_successes
    combined.static_fallbacks += result_nc1.static_fallbacks
    combined.cache_hits += result_nc1.cache_hits
    combined.circuit_open_count += result_nc1.circuit_open_count
    combined.estimated_cost += result_nc1.estimated_cost
    combined.estimated_cost_saved += result_nc1.estimated_cost_saved
    combined.latencies_ms.extend(result_nc1.latencies_ms)
    if result_nc1.recovery_time_ms is not None:
        if combined.recovery_time_ms is None:
            combined.recovery_time_ms = result_nc1.recovery_time_ms
        else:
            combined.recovery_time_ms = (combined.recovery_time_ms + result_nc1.recovery_time_ms) / 2

    # 2. primary_timeout_100 with cache disabled
    scenario_no_cache_2 = ScenarioConfig(
        name="primary_timeout_100_no_cache",
        description="Primary fails 100% with cache disabled",
        provider_overrides={"primary": 1.0}
    )
    result_nc2 = run_scenario(no_cache_config, queries, scenario_no_cache_2)
    passed_nc2 = result_nc2.availability >= 0.85
    combined.scenarios["primary_timeout_100_no_cache"] = "pass" if passed_nc2 else "fail"

    combined.total_requests += result_nc2.total_requests
    combined.successful_requests += result_nc2.successful_requests
    combined.failed_requests += result_nc2.failed_requests
    combined.fallback_successes += result_nc2.fallback_successes
    combined.static_fallbacks += result_nc2.static_fallbacks
    combined.cache_hits += result_nc2.cache_hits
    combined.circuit_open_count += result_nc2.circuit_open_count
    combined.estimated_cost += result_nc2.estimated_cost
    combined.estimated_cost_saved += result_nc2.estimated_cost_saved
    combined.latencies_ms.extend(result_nc2.latencies_ms)
    if result_nc2.recovery_time_ms is not None:
        if combined.recovery_time_ms is None:
            combined.recovery_time_ms = result_nc2.recovery_time_ms
        else:
            combined.recovery_time_ms = (combined.recovery_time_ms + result_nc2.recovery_time_ms) / 2

    return combined
