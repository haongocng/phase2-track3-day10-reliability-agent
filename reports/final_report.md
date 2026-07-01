# Day 10 Reliability Report

**Họ và tên: Nguyễn Ngọc Hảo**
**Mã học viên: 2A202600903**

## 1. Architecture summary

The Reliability Gateway coordinates a robust multi-tiered pipeline consisting of semantic caching, circuit breakers, backup providers, and graceful degradation fallback.

```
User Request
    |
    v
[Gateway] ---> [Cache check] ---> HIT? return cached (Latency: 0ms, Cost: $0)
    |                                 |
    v                                 v MISS
[Circuit Breaker: Primary] -------> Provider A (primary)
    |  (OPEN? skip / fail-fast)
    v
[Circuit Breaker: Backup] --------> Provider B (backup)
    |  (OPEN? skip / fail-fast)
    v
[Static fallback message]           "The service is temporarily degraded..."
```

- **Gateway**: The central router orchestrating the pipeline. It handles Cache Lookup -> Provider Fallback -> Static Fallback. It also monitors budget constraints and routes to cheaper models when budget is exceeded.
- **Cache Layers**:
  - **In-Memory Cache**: Uses character 3-grams and word token Cosine Similarity to find semantic matches, with security and false hit filters (checks distinct 4-digit numbers to prevent incorrect year/ID matches).
  - **Redis Cache**: A shared backend cache to synchronize state across multiple server instances, ensuring optimal token savings.
- **Circuit Breaker**: An active 3-state machine (CLOSED, OPEN, HALF_OPEN) wrapping each provider. It tracks sequential errors to trip the circuit to OPEN, prevents overloading broken providers (fail-fast), and probes them in HALF_OPEN after a cool-off timeout.
- **Fallback Chain**: Routes traffic from the primary provider to a backup provider when the primary fails or its circuit is OPEN.

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Triples consecutive failures to filter out transient network blips while maintaining fast detection of outage. |
| reset_timeout_seconds | 2.0 | Cooling off duration before moving the circuit to HALF_OPEN to check provider health. |
| success_threshold | 1 | Number of consecutive successful probe requests required to close the circuit and fully restore provider. |
| cache TTL | 300 | Caches results for 5 minutes to balance data freshless with high token savings. |
| similarity_threshold | 0.92 | High threshold to ensure semantic correctness and avoid incorrect cache matches (hallucinations). |
| load_test requests | 100 | Sufficient sample size per scenario to yield statistically significant SLI metrics. |

## 3. SLO definitions

We define and evaluate the target SLOs across the entire simulation (all scenarios combined):

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 98.8% | No (Met in baseline, but slightly below due to multi-outage chaos) |
| Latency P95 | < 2500 ms | 313.14 ms | Yes |
| Fallback success rate | >= 95% | 97.01% | Yes |
| Cache hit rate | >= 10% | 36.8% | Yes |
| Recovery time | < 5000 ms | 2300.40 ms | Yes |

## 4. Metrics

Summary of `reports/metrics.json` across all scenarios:

| Metric | Value |
|---|---:|
| availability | 98.8% |
| error_rate | 1.2% |
| latency_p50_ms | 270.17 ms |
| latency_p95_ms | 313.14 ms |
| latency_p99_ms | 319.36 ms |
| fallback_success_rate | 97.01% |
| cache_hit_rate | 36.8% |
| estimated_cost_saved | $0.184 |
| circuit_open_count | 24 |
| recovery_time_ms | 2300.40 ms |

## 5. Cache comparison

Comparing `all_healthy` scenario with cache enabled vs disabled:

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 212.99 ms | 220.73 ms | +7.74 ms (only slow calls measured) |
| latency_p95_ms | 304.67 ms | 303.28 ms | -1.39 ms |
| estimated_cost | $0.052392 | $0.018968 | -$0.033424 (-63.8% savings!) |
| cache_hit_rate | 0% | 64.0% | +64.0% |

*Note on Latency*: The `latency_ms` metrics only count active provider call latencies (latency_ms > 0). Including the 0ms cache hits, the overall system latency drops significantly under cached operations.

## 6. Redis shared cache

### Why Shared Cache Matters for Production
- **In-Memory Cache Insufficiency**: Local in-memory caches are isolated to individual application instances. In modern multi-instance deployments (such as autoscaled container gateways), this leads to duplicated API calls to LLM providers (cold-start cache misses on new instances) and unnecessary billing.
- **Shared Redis Cache Solution**: `SharedRedisCache` provides a single centralized cache accessible by all instances. Any prompt resolved and cached by one gateway instance is instantly usable by all other gateway instances, maximizing cost savings and lowering latency globally.

### Evidence of shared state

Two separate cache instances on the same Redis DB can write and read the same data:

```python
def test_shared_state_across_instances() -> None:
    """Two SharedRedisCache instances on same Redis should see same data."""
    c1 = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:shared:",
    )
    c2 = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:shared:",
    )
    c1.flush()
    c1.set("shared query", "shared response")
    cached, _ = c2.get("shared query")
    assert cached == "shared response"
    c1.flush()
    c1.close()
    c2.close()
```

### Redis CLI output

Running redis-cli to verify keys inside the Redis container:

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:e310945528af
rl:cache:5eb63bbbe01e
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic fallback to backup, primary circuit opens | Availability = 90.0%, primary circuit opened, backup served traffic. | Pass |
| primary_flaky_50 | Circuit oscillates, mix of primary and fallback | Circuit transitioned frequently between open/closed, availability = 93.0%. | Pass |
| all_healthy | All requests via primary, no circuit opens | High availability (99.0%), primary handled all queries, 0 circuit opens. | Pass |
| all_healthy_no_cache | Normal fallback operations, higher latency, no cache hits | Availability = 99.0%, cost is 2.7x higher, 0 cache hits. | Pass |
| primary_timeout_100_no_cache | Backup serves all traffic with zero cache support | Availability = 90.0%, primary circuit opens, backup cost and latency higher. | Pass |

## 8. Failure analysis

- **What could still go wrong?**
  - **Shared Circuit State**: Currently, circuit breaker state is kept in-memory. If one Gateway instance trips the circuit because Provider A is down, other Gateway instances still try to call Provider A until they trip their local circuit breakers. This leads to duplicate failing requests.
  - **Cold-Start Cache Storm**: If many identical requests are sent simultaneously (before the first one finishes and is cached), all of them will miss the cache and hit the LLM providers concurrently, creating a load spike.
- **What would you change?**
  - **Distributed Circuit Breakers**: Store circuit state and transition counts in Redis so all Gateway instances share breaker state.
  - **Cache Lock / Single Flight**: Implement a distributed lock (e.g. Redlock) or in-memory single-flight pattern to ensure only one request calls the LLM provider for a specific prompt, while concurrent identical requests wait to read the cached result.

## 9. Next steps

1. **Implement Redis-Backed Circuit Breakers**: Share the state (OPEN/CLOSED) and failure count across all running gateway instances to achieve instantaneous global fail-fast behavior.
2. **Implement Request Collapsing (Single Flight)**: Merge concurrent identical or highly similar incoming prompts to call the LLM provider only once, updating the cache for all waiting clients.
3. **Advanced Semantic Cache Model**: Replace the character 3-gram similarity logic with lightweight local embedding models (e.g. ONNX SentenceTransformers) to enable deeper semantic understanding and matching.
