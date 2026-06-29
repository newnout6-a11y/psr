"""
Unit tests for LLMRouter.

Validates requirements 10.1-10.5:
- Task-specific routing via env vars
- Health-score based fallback
- Exception with task name when all providers fail
- In-memory stats via get_provider_health()
- Dynamic provider discovery via API keys
"""

import os
import time
from unittest.mock import patch

import pytest

from src.brain.llm_router import LLMRouter, OpenAICompatibleClient, ProviderHealth


@pytest.fixture
def router():
    """Create a router with mock providers (no real API keys needed)."""
    with patch.dict(os.environ, {}, clear=False):
        r = LLMRouter.__new__(LLMRouter)
        r.providers = {
            "openai": "fake_openai",
            "deepseek": "fake_deepseek",
            "groq": {"api_key": "fake"},
        }
        r.health = {
            "openai": ProviderHealth(),
            "deepseek": ProviderHealth(),
            "groq": ProviderHealth(),
        }
        r._last_route = {}
        return r


class TestTaskSpecificRouting:
    """Requirement 10.1: task-specific маршрутизация через env-переменные."""

    def test_task_preferred_provider_from_env(self, router):
        """QUERY_GENERATION_PROVIDER=groq → groq is preferred."""
        with patch.dict(os.environ, {"QUERY_GENERATION_PROVIDER": "groq"}):
            result = router._task_preferred_provider("query_generation")
            assert result == "groq"

    def test_task_preferred_provider_scoring(self, router):
        """SCORING_PROVIDER=openai routes scoring to openai."""
        with patch.dict(os.environ, {"SCORING_PROVIDER": "openai"}):
            result = router._task_preferred_provider("scoring")
            assert result == "openai"

    def test_task_preferred_provider_proposal_writing(self, router):
        """PROPOSAL_WRITING_PROVIDER=deepseek → deepseek is preferred."""
        with patch.dict(os.environ, {"PROPOSAL_WRITING_PROVIDER": "deepseek"}):
            result = router._task_preferred_provider("proposal_writing")
            assert result == "deepseek"

    def test_task_preferred_provider_auto_returns_none(self, router):
        """QUERY_GENERATION_PROVIDER=auto → None (no preference)."""
        env = {k: v for k, v in os.environ.items() if "PROVIDER" not in k}
        env["QUERY_GENERATION_PROVIDER"] = "auto"
        with patch.dict(os.environ, env, clear=True):
            result = router._task_preferred_provider("query_generation")
            assert result is None

    def test_task_preferred_provider_unset_returns_none(self, router):
        """No env var set → None."""
        env = {k: v for k, v in os.environ.items() if "PROVIDER" not in k}
        with patch.dict(os.environ, env, clear=True):
            result = router._task_preferred_provider("query_generation")
            assert result is None

    def test_parser_provider_applies_to_query_generation(self, router):
        """PARSER_LLM_PROVIDER routes parser query generation when task-specific provider is auto."""
        with patch.dict(os.environ, {"QUERY_GENERATION_PROVIDER": "auto", "PARSER_LLM_PROVIDER": "deepseek"}):
            result = router._task_preferred_provider("query_generation")
            assert result == "deepseek"

    def test_task_provider_overrides_parser_provider(self, router):
        """Task-specific provider remains higher priority than parser-wide provider."""
        with patch.dict(os.environ, {"SCORING_PROVIDER": "openai", "PARSER_LLM_PROVIDER": "deepseek"}):
            result = router._task_preferred_provider("scoring")
            assert result == "openai"

    def test_global_provider_used_after_task_and_parser(self, router):
        """LLM_PROVIDER is the global fallback route."""
        env = {k: v for k, v in os.environ.items() if "PROVIDER" not in k}
        env["LLM_PROVIDER"] = "openai"
        with patch.dict(os.environ, env, clear=True):
            result = router._task_preferred_provider("proposal_writing")
            assert result == "openai"

    def test_select_model_from_env(self, router):
        """GROQ_MODEL_SCORING=custom-model → custom-model selected."""
        with patch.dict(os.environ, {"GROQ_MODEL_SCORING": "custom-model"}):
            result = router._select_model("groq", "scoring", None)
            assert result == "custom-model"

    def test_select_model_explicit_overrides_env(self, router):
        """Explicit model parameter takes priority over env."""
        with patch.dict(os.environ, {"GROQ_MODEL_SCORING": "env-model"}):
            result = router._select_model("groq", "scoring", "explicit-model")
            assert result == "explicit-model"

    def test_select_model_defaults(self, router):
        """Without env vars, defaults are used."""
        env = {k: v for k, v in os.environ.items() if "MODEL" not in k}
        with patch.dict(os.environ, env, clear=True):
            result = router._select_model("groq", "query_generation", None)
            assert result == "llama-3.1-8b-instant"

    def test_parser_model_applies_to_parser_tasks(self, router):
        """PARSER_LLM_MODEL applies to query generation and scoring."""
        env = {k: v for k, v in os.environ.items() if "MODEL" not in k}
        env["PARSER_LLM_PROVIDER"] = "openai"
        env["PARSER_LLM_MODEL"] = "gpt-5.5"
        with patch.dict(os.environ, env, clear=True):
            assert router._select_model("openai", "query_generation", None) == "gpt-5.5"

    def test_task_model_overrides_parser_model_for_deepseek(self, router):
        """DeepSeek Pro can write queries while Flash scores projects."""
        env = {k: v for k, v in os.environ.items() if "MODEL" not in k}
        env["PARSER_LLM_PROVIDER"] = "deepseek"
        env["PARSER_LLM_MODEL"] = "deepseek-v4-flash"
        env["DEEPSEEK_MODEL_QUERY_GENERATION"] = "deepseek-v4-pro"
        env["DEEPSEEK_MODEL_SCORING"] = "deepseek-v4-flash"
        with patch.dict(os.environ, env, clear=True):
            assert router._select_model("deepseek", "query_generation", None) == "deepseek-v4-pro"
            assert router._select_model("deepseek", "scoring", None) == "deepseek-v4-flash"

    def test_parser_model_does_not_leak_to_fallback_provider(self, router):
        """Parser model is scoped to PARSER_LLM_PROVIDER only."""
        env = {k: v for k, v in os.environ.items() if "MODEL" not in k}
        env["PARSER_LLM_PROVIDER"] = "deepseek"
        env["PARSER_LLM_MODEL"] = "deepseek-v4-pro"
        env["OPENAI_MODEL"] = "gpt-5.5"
        with patch.dict(os.environ, env, clear=True):
            assert router._select_model("openai", "query_generation", None) == "gpt-5.5"

    def test_incompatible_parser_model_is_ignored(self, router):
        """DeepSeek parser model should not be used when parser provider is OpenAI."""
        env = {k: v for k, v in os.environ.items() if "MODEL" not in k}
        env["PARSER_LLM_PROVIDER"] = "openai"
        env["PARSER_LLM_MODEL"] = "deepseek-v4-flash"
        env["OPENAI_MODEL"] = "gpt-5.5"
        with patch.dict(os.environ, env, clear=True):
            assert router._select_model("openai", "query_generation", None) == "gpt-5.5"

    def test_deepseek_parser_model_can_use_flash(self, router):
        """DeepSeek parser route can explicitly use DeepSeek V4 Flash."""
        env = {k: v for k, v in os.environ.items() if "MODEL" not in k}
        env["PARSER_LLM_PROVIDER"] = "deepseek"
        env["PARSER_LLM_MODEL"] = "deepseek-v4-flash"
        with patch.dict(os.environ, env, clear=True):
            assert router._select_model("deepseek", "query_generation", None) == "deepseek-v4-flash"

    def test_deepseek_v4_pro_alias(self, router):
        """User-facing v4 pro alias normalizes to the configured DeepSeek model id."""
        assert router._select_model("deepseek", "scoring", "v4 pro") == "deepseek-v4-pro"

    def test_deepseek_v4_flash_alias(self, router):
        """User-facing flash alias normalizes to the official DeepSeek V4 Flash model id."""
        assert router._select_model("deepseek", "scoring", "flash") == "deepseek-v4-flash"

    def test_candidate_providers_preferred_first(self, router):
        """Preferred provider from env appears first in candidates."""
        with patch.dict(os.environ, {"SCORING_PROVIDER": "openai"}):
            candidates = router._candidate_providers(task="scoring", preferred=None)
            assert candidates[0] == "openai"
            # All providers still present
            assert set(candidates) == {"openai", "deepseek", "groq"}

    def test_candidate_providers_explicit_preferred(self, router):
        """Explicitly passed preferred provider appears first."""
        candidates = router._candidate_providers(task="general", preferred="openai")
        assert candidates[0] == "openai"


class TestHealthScoreFallback:
    """Requirement 10.2: fallback по health-score."""

    def test_healthy_provider_ranked_first(self, router):
        """Provider with higher health-score appears earlier (after preferred)."""
        # Make groq healthy, deepseek unhealthy
        router.health["groq"].success = 10
        router.health["deepseek"].failure = 5
        router.health["deepseek"].consecutive_failures = 3

        candidates = router._candidate_providers(task="general", preferred=None)
        groq_idx = candidates.index("groq")
        deepseek_idx = candidates.index("deepseek")
        assert groq_idx < deepseek_idx

    def test_consecutive_failures_lower_score(self, router):
        """Consecutive failures significantly lower health-score."""
        router.health["groq"].consecutive_failures = 5
        score_groq = router.health["groq"].score()
        score_openai = router.health["openai"].score()
        assert score_groq < score_openai

    def test_success_increases_score(self):
        """Recording successes increases health-score."""
        h = ProviderHealth()
        base_score = h.score()
        h.success = 5
        h.last_success_at = time.time()
        assert h.score() > base_score

    def test_failure_decreases_score(self):
        """Recording failures decreases health-score."""
        h = ProviderHealth()
        base_score = h.score()
        h.failure = 5
        h.consecutive_failures = 3
        h.last_failure_at = time.time()
        assert h.score() < base_score


class TestAllProvidersFail:
    """Requirement 10.3: исключение с task и ошибкой последнего провайдера."""

    @pytest.mark.asyncio
    async def test_all_fail_raises_with_task_name(self, router):
        """When all providers fail, ValueError includes task name."""
        # Make router have only one provider that will fail
        router.providers = {"groq": {"api_key": "fake"}}
        router.health = {"groq": ProviderHealth()}

        with pytest.raises(ValueError, match="task=scoring"):
            await router.generate(prompt="test", task="scoring")

    @pytest.mark.asyncio
    async def test_no_providers_raises(self):
        """When no providers are registered, raises immediately."""
        r = LLMRouter.__new__(LLMRouter)
        r.providers = {}
        r.health = {}
        r._last_route = {}

        with pytest.raises(ValueError, match="Нет доступных LLM провайдеров"):
            await r.generate(prompt="test", task="general")


    @pytest.mark.asyncio
    async def test_explicit_model_does_not_leak_to_fallback_provider(self, router, monkeypatch):
        """Provider-specific model aliases should not be reused for fallback providers."""
        router.providers = {"deepseek": "fake_deepseek", "openai": "fake_openai"}
        router.health = {"deepseek": ProviderHealth(), "openai": ProviderHealth()}
        calls = []

        async def fail_once(provider, prompt, model, temperature, max_tokens, system_prompt):
            calls.append((provider, model))
            raise RuntimeError("fail")

        monkeypatch.setattr(router, "_generate_once", fail_once)
        with pytest.raises(ValueError):
            await router.generate(prompt="test", provider="deepseek", model="flash", task="proposal_writing")

        assert calls[0] == ("deepseek", "deepseek-v4-flash")
        assert calls[1] == ("openai", "gpt-5.5")


class TestProviderHealth:
    """Requirement 10.4: in-memory статистика через get_provider_health()."""

    def test_get_provider_health_structure(self, router):
        """get_provider_health returns complete stats for each provider."""
        health = router.get_provider_health()

        assert "openai" in health
        assert "deepseek" in health
        assert "groq" in health

        for _name, stats in health.items():
            assert "success" in stats
            assert "failure" in stats
            assert "consecutive_failures" in stats
            assert "last_success_at" in stats
            assert "last_failure_at" in stats
            assert "last_error" in stats
            assert "last_model" in stats
            assert "tasks" in stats
            assert "health_score" in stats

    def test_record_success_updates_stats(self, router):
        """_record_success increments success counter and resets consecutive_failures."""
        router._record_success("groq", "scoring", "llama-3.3-70b")

        health = router.get_provider_health()["groq"]
        assert health["success"] == 1
        assert health["consecutive_failures"] == 0
        assert health["last_model"] == "llama-3.3-70b"
        assert health["tasks"]["scoring"]["success"] == 1

    def test_record_failure_updates_stats(self, router):
        """_record_failure increments failure counters and stores error."""
        router._record_failure("groq", "scoring", "llama-3.3-70b", Exception("timeout"))

        health = router.get_provider_health()["groq"]
        assert health["failure"] == 1
        assert health["consecutive_failures"] == 1
        assert health["last_error"] == "timeout"
        assert health["tasks"]["scoring"]["failure"] == 1

    def test_consecutive_failures_reset_on_success(self, router):
        """After failures, a success resets consecutive_failures to 0."""
        router._record_failure("groq", "general", "model", Exception("err1"))
        router._record_failure("groq", "general", "model", Exception("err2"))
        assert router.health["groq"].consecutive_failures == 2

        router._record_success("groq", "general", "model")
        assert router.health["groq"].consecutive_failures == 0

    def test_health_score_in_output(self, router):
        """health_score is computed and included in output."""
        router._record_success("groq", "general", "model")
        health = router.get_provider_health()["groq"]
        assert isinstance(health["health_score"], float)
        assert health["health_score"] > 100.0  # base is 100, success adds bonus


class TestDynamicProviderDiscovery:
    """Requirement 10.5: новые провайдеры через API-ключ в .env."""

    def test_groq_discovered_from_env(self):
        """GROQ_API_KEY in env → groq provider registered."""
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}, clear=True):
            r = LLMRouter()
            assert "groq" in r.providers
            assert "groq" in r.health

    def test_no_keys_no_providers(self):
        """No API keys → no providers registered."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "OPENAI_API_KEYS", "DEEPSEEK_API_KEY", "GROQ_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            r = LLMRouter()
            assert len(r.providers) == 0

    def test_multiple_keys_multiple_providers(self):
        """Multiple API keys → multiple providers registered."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "openai-key",
            "DEEPSEEK_API_KEY": "deepseek-key",
            "GROQ_API_KEY": "groq-key",
        }, clear=True):
            r = LLMRouter()
            assert "openai" in r.providers
            assert "deepseek" in r.providers
            assert "groq" in r.providers

    def test_openai_discovered_from_env(self):
        """OPENAI_API_KEY in env registers OpenAI-compatible provider."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://api.byesu.com"}, clear=True):
            r = LLMRouter()
            assert "openai" in r.providers
            assert "openai" in r.health

    def test_openai_key_pool_discovered_from_env(self):
        """OPENAI_API_KEYS registers one OpenAI provider with a rotating key pool."""
        env = {
            "OPENAI_API_KEY": "key-a",
            "OPENAI_API_KEYS": "key-b,key-a;key-c",
            "OPENAI_BASE_URL": "https://api.byesu.com",
        }
        with patch.dict(os.environ, env, clear=True):
            r = LLMRouter()
            client = r.providers["openai"]
            assert client.api_keys == ["key-a", "key-b", "key-c"]

    def test_deepseek_discovered_from_env(self):
        """DEEPSEEK_API_KEY in env registers DeepSeek provider."""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True):
            r = LLMRouter()
            assert "deepseek" in r.providers
            assert "deepseek" in r.health


class TestOpenAICompatibleClient:
    def test_responses_output_text_extraction(self):
        data = {"output_text": "ok"}
        assert OpenAICompatibleClient._extract_text(data) == "ok"

    def test_responses_nested_output_extraction(self):
        data = {"output": [{"content": [{"type": "output_text", "text": "hello"}]}]}
        assert OpenAICompatibleClient._extract_text(data) == "hello"

    def test_chat_choices_extraction(self):
        data = {"choices": [{"message": {"content": "chat text"}}]}
        assert OpenAICompatibleClient._extract_text(data) == "chat text"

    def test_url_adds_configured_prefix_once(self):
        client = OpenAICompatibleClient(api_key="k", base_url="https://api.byesu.com", api_prefix="/v1")
        assert client._url("responses") == "https://api.byesu.com/v1/responses"
        client = OpenAICompatibleClient(api_key="k", base_url="https://api.byesu.com/v1", api_prefix="/v1")
        assert client._url("responses") == "https://api.byesu.com/v1/responses"

    def test_api_keys_rotate(self):
        client = OpenAICompatibleClient(
            api_keys=["k1", "k2"],
            base_url="https://api.byesu.com",
            api_prefix="/v1",
        )
        assert client._headers()["Authorization"] == "Bearer k1"
        assert client._headers()["Authorization"] == "Bearer k2"
        assert client._headers()["Authorization"] == "Bearer k1"
