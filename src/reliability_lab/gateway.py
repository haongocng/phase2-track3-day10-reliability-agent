from __future__ import annotations

from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float = 999.0,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cumulative_cost = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        TODO(student): Implement the full request routing pipeline:

        1. CACHE CHECK — if self.cache is not None:
           - Call self.cache.get(prompt) → (cached_text, score)
           - If cached_text is not None, return GatewayResponse with:
             route=f"cache_hit:{score:.2f}", cache_hit=True, latency=0, cost=0

        2. PROVIDER FALLBACK CHAIN — iterate self.providers in order:
           - Get the circuit breaker: self.breakers[provider.name]
           - Try breaker.call(provider.complete, prompt)
           - On success:
             a. Store in cache: self.cache.set(prompt, response.text, {"provider": provider.name})
             b. Determine route: "primary" if first provider, else "fallback"
             c. Return GatewayResponse with provider info, latency, cost
           - On ProviderError or CircuitOpenError: save error, continue to next provider

        3. STATIC FALLBACK — if all providers fail:
           - Return GatewayResponse with:
             text="The service is temporarily degraded. Please try again soon."
             route="static_fallback", error=last_error

        BONUS TODO: Add cost budget tracking — if cumulative cost exceeds a threshold,
        skip expensive providers and route to cache or cheaper fallback.
        """
        # 1. CACHE CHECK
        if self.cache is not None:
            try:
                cached_text, score = self.cache.get(prompt)
                if cached_text is not None:
                    return GatewayResponse(
                        text=cached_text,
                        route=f"cache_hit:{score:.2f}",
                        provider=None,
                        cache_hit=True,
                        latency_ms=0.0,
                        estimated_cost=0.0,
                    )
            except Exception:
                pass

        # 2. PROVIDER FALLBACK CHAIN
        last_error = None
        
        # BONUS: Cost Budget Tracking
        # If the cumulative cost exceeds the cost budget, we only route to cheaper providers
        # (e.g. whose cost per 1k tokens is <= minimum cost of all available providers * 1.5)
        # to prevent running out of budget.
        allowed_providers = self.providers
        if self.cumulative_cost >= self.cost_budget and self.providers:
            min_cost = min(p.cost_per_1k_tokens for p in self.providers)
            allowed_providers = [p for p in self.providers if p.cost_per_1k_tokens <= min_cost * 1.5]

        for i, provider in enumerate(allowed_providers):
            breaker = self.breakers.get(provider.name)
            if breaker is None:
                continue
            try:
                # Call provider complete method wrapped in the circuit breaker
                response = breaker.call(provider.complete, prompt)
                
                # Successful call: update cumulative cost
                self.cumulative_cost += response.estimated_cost
                
                # Cache response
                if self.cache is not None:
                    try:
                        self.cache.set(prompt, response.text, {"provider": provider.name})
                    except Exception:
                        pass
                
                # Determine route
                route = "primary" if i == 0 else "fallback"
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=response.latency_ms,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as e:
                last_error = str(e)
                continue
            except Exception as e:
                last_error = str(e)
                continue

        # 3. STATIC FALLBACK (when all allowed providers fail or list is empty)
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error or "All providers failed",
        )
