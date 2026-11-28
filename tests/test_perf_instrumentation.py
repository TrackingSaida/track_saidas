"""Testes da instrumentação de performance (PRD-017 Fase 0)."""
from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")


class TestNormalizePath(unittest.TestCase):
    def test_strips_numeric_segments(self):
        import perf_instrumentation as pi

        self.assertEqual(pi._normalize_path("/api/mobile/entregas/12345"), "/api/mobile/entregas/:id")
        self.assertEqual(pi._normalize_path("/api/health"), "/api/health")
        self.assertEqual(pi._normalize_path(""), "/")


class TestNoteRecalc(unittest.TestCase):
    def test_increments_when_context_sampled(self):
        import perf_instrumentation as pi

        ctx = pi.PerfRequestContext(
            request_id="abc",
            method="POST",
            path_template="/api/x",
            sampled=True,
        )
        token = pi._request_ctx.set(ctx)
        try:
            pi.note_recalc(rows=7)
            pi.note_recalc(rows=3)
            self.assertEqual(ctx.recalc_count, 2)
            self.assertEqual(ctx.recalc_rows, 10)
        finally:
            pi._request_ctx.reset(token)

    def test_noop_without_context(self):
        import perf_instrumentation as pi

        pi._request_ctx.set(None)
        pi.note_recalc(rows=5)  # não levanta


class TestMiddlewareLogging(unittest.IsolatedAsyncioTestCase):
    async def test_logs_perf_request_when_enabled(self):
        os.environ["PERF_INSTRUMENTATION"] = "1"
        os.environ["PERF_INSTRUMENTATION_SAMPLE_RATE"] = "1.0"
        import importlib
        import perf_instrumentation as pi

        importlib.reload(pi)
        self.assertTrue(pi.PERF_INSTRUMENTATION_ENABLED)

        recorded = []

        class Handler(logging.Handler):
            def emit(self, record):
                recorded.append(record.getMessage())

        handler = Handler()
        pi.logger.addHandler(handler)
        pi.logger.setLevel(logging.INFO)

        async def call_next(request):
            pi.note_recalc(rows=2)
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            return resp

        mw = pi.PerfInstrumentationMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {"x-request-id": "req-test-1"}
        request.method = "GET"
        request.url.path = "/api/mobile/entregas/99"

        try:
            response = await mw.dispatch(request, call_next)
            self.assertEqual(response.headers.get("X-Request-Id"), "req-test-1")
            self.assertTrue(any("perf_request" in m and "recalc_count=1" in m for m in recorded))
            self.assertTrue(any("path=/api/mobile/entregas/:id" in m for m in recorded))
            self.assertFalse(any("99" in m and "path=" in m for m in recorded if "perf_request" in m))
        finally:
            pi.logger.removeHandler(handler)
            os.environ.pop("PERF_INSTRUMENTATION", None)
            os.environ.pop("PERF_INSTRUMENTATION_SAMPLE_RATE", None)
            importlib.reload(pi)

    async def test_disabled_skips_logging(self):
        os.environ.pop("PERF_INSTRUMENTATION", None)
        import importlib
        import perf_instrumentation as pi

        importlib.reload(pi)
        self.assertFalse(pi.PERF_INSTRUMENTATION_ENABLED)

        recorded = []

        class Handler(logging.Handler):
            def emit(self, record):
                recorded.append(record.getMessage())

        handler = Handler()
        pi.logger.addHandler(handler)
        try:
            mw = pi.PerfInstrumentationMiddleware(app=MagicMock())
            request = MagicMock()
            request.headers = {}
            request.method = "GET"
            request.url.path = "/api/health"

            async def call_next(request):
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {}
                return resp

            await mw.dispatch(request, call_next)
            self.assertEqual(recorded, [])
        finally:
            pi.logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
