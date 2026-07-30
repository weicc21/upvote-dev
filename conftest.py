"""Test-session bootstrap.

`shared/config.py` builds its `Settings` instance at import time by design, so the
environment has to be populated before any test module imports it — hence a root
conftest rather than a fixture.

These are throwaway values. No test in this suite reaches a real Supabase, Redis,
or network endpoint; clients are injected through `backend.deps`.
"""

from __future__ import annotations

import os

_TEST_ENV = {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_SERVICE_KEY": "test-service-key",
    "SUPABASE_JWT_SECRET": "test-jwt-secret",
    "REDIS_URL": "redis://localhost:6379/0",
    "LLM_BASE_URL": "https://llm.test",
    "LLM_API_KEY": "test-llm-key",
    "TARGET_PROMPT_DIR": "/tmp/target-prompts",
    "COMPILE_COMMAND": "echo compile",
    "RENDER_WEBHOOK_SECRET": "test-webhook-secret",
    "SANDBOX_ALLOWED_HOSTS": "*.onrender.com",
    "FORUM_ORIGIN": "http://localhost:5173",
    # Route tests need a resolvable caller without minting real JWTs; dev mode
    # takes the id from X-Dev-User. The config tests build their own isolated
    # Settings, so this does not weaken the DEV_MODE=false assertions there.
    "DEV_MODE": "true",
}

for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)
