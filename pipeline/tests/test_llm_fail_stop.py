#!/usr/bin/env python3
"""LLM 失败即停契约测试；所有 transport 与 sleep 均为离线 mock。"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import config as C  # noqa: E402
from voc_analytics import llm  # noqa: E402


class _JsonResponse(io.BytesIO):
    def __init__(self, value: dict):
        super().__init__(json.dumps(value).encode())

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _http_error(status: int, detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid",
        status,
        "mock error",
        {},
        io.BytesIO(detail.encode()),
    )


class LLMFailStopTest(unittest.TestCase):
    def setUp(self) -> None:
        llm.reset_for_tests()
        self.key_patch = mock.patch.object(C, "BAILIAN_KEY", "offline-test-key")
        self.retry_patch = mock.patch.object(C, "LLM_RETRY", 3)
        self.key_patch.start()
        self.retry_patch.start()

    def tearDown(self) -> None:
        self.retry_patch.stop()
        self.key_patch.stop()
        llm.reset_for_tests()

    def test_missing_api_key_is_fatal_without_transport(self) -> None:
        transport = mock.Mock()
        with mock.patch.object(C, "BAILIAN_KEY", ""), \
             mock.patch.object(llm.urllib.request, "urlopen", transport):
            with self.assertRaisesRegex(llm.FatalLLMError, "BAILIAN_API_KEY"):
                llm._post("/test", {})

        transport.assert_not_called()

    def test_auth_arrearage_and_hard_quota_are_fatal_on_first_response(self) -> None:
        cases = [
            (401, ""),
            (403, '{"code":"Throttling","message":"try later"}'),
            (400, '{"code":"Arrearage","message":"account overdue"}'),
            (400, '{"code":"InvalidApiKey","message":"invalid api key"}'),
            (400, '{"code":"AuthenticationError"}'),
            (400, '{"code":"Unauthorized"}'),
            (429, '{"code":"insufficient_quota","message":"check billing"}'),
            (429, '{"code":"QuotaExhausted"}'),
            (429, '{"code":"QuotaExceeded"}'),
            (429, '{"code":"AllocationQuotaExceeded"}'),
            (429, '{"code":"Throttling.AllocationQuota"}'),
            (429, '{"message":"Allocation quota has been exhausted"}'),
            (402, ''),
            (400, '{"message":"余额不足，请充值"}'),
        ]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                llm.reset_for_tests()
                transport = mock.Mock(side_effect=_http_error(status, body))
                sleeper = mock.Mock()
                with mock.patch.object(llm.urllib.request, "urlopen", transport), \
                     mock.patch.object(llm.time, "sleep", sleeper):
                    with self.assertRaises(llm.FatalLLMError):
                        llm._post("/test", {})

                self.assertEqual(transport.call_count, 1)
                sleeper.assert_not_called()

    def test_fatal_error_opens_circuit_and_preserves_first_reason(self) -> None:
        transport = mock.Mock(
            side_effect=_http_error(
                400,
                '{"code":"Arrearage","message":"account overdue"}',
            )
        )
        with mock.patch.object(llm.urllib.request, "urlopen", transport):
            with self.assertRaises(llm.FatalLLMError) as first:
                llm._post("/first", {})
            with self.assertRaises(llm.FatalLLMError) as second:
                llm._post("/must-not-reach-transport", {})

        self.assertEqual(transport.call_count, 1)
        self.assertEqual(str(second.exception), str(first.exception))

    def test_local_breaker_is_open_before_shared_publication(self) -> None:
        observed: list[str | None] = []

        def publish(reason: str) -> str:
            observed.append(llm._circuit_reason())
            return reason

        with mock.patch.object(llm, "_publish_shared_circuit", publish):
            self.assertEqual(llm._open_circuit("first fatal"), "first fatal")

        self.assertEqual(observed, ["first fatal"])

    def test_circuit_is_checked_again_after_gate_acquisition(self) -> None:
        class _GateThatOpensCircuit:
            def __enter__(self) -> None:
                llm._open_circuit("fatal while waiting for gate")

            def __exit__(self, *args: object) -> None:
                return None

        transport = mock.Mock()
        with mock.patch.object(llm, "_GATE", _GateThatOpensCircuit()), \
             mock.patch.object(llm.urllib.request, "urlopen", transport):
            with self.assertRaisesRegex(llm.FatalLLMError, "while waiting"):
                llm._post("/test", {})

        transport.assert_not_called()

    def test_shared_circuit_stops_peer_process_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            circuit = str(pathlib.Path(directory) / "run.fatal")
            with mock.patch.dict(
                    os.environ, {"VOC_LLM_CIRCUIT_FILE": circuit}, clear=False):
                first_transport = mock.Mock(
                    side_effect=_http_error(400, '{"code":"Arrearage"}'))
                with mock.patch.object(
                        llm.urllib.request, "urlopen", first_transport):
                    with self.assertRaises(llm.FatalLLMError):
                        llm._post("/first-process", {})

                published = pathlib.Path(circuit).read_text()
                self.assertTrue(published.startswith("pid="))
                self.assertIn("Arrearage", published)

                # 模拟另一个 Python 进程：它没有本地 breaker 状态，但共享
                # 文件仍属于同一 run，故请求前必须直接 fatal。
                llm.reset_usage()
                peer_transport = mock.Mock()
                with mock.patch.object(
                        llm.urllib.request, "urlopen", peer_transport):
                    with self.assertRaisesRegex(llm.FatalLLMError, "Arrearage"):
                        llm._post("/peer-process", {})
                peer_transport.assert_not_called()

    def test_peer_fatal_while_request_is_in_flight_discards_response(self) -> None:
        class _ResponseThatOpensCircuit(_JsonResponse):
            def __exit__(self, *args: object) -> None:
                llm._open_circuit("peer fatal during request")
                super().__exit__(*args)

        transport = mock.Mock(
            return_value=_ResponseThatOpensCircuit({"ok": True}))
        with mock.patch.object(llm.urllib.request, "urlopen", transport):
            with self.assertRaisesRegex(
                    llm.FatalLLMError, "peer fatal during request"):
                llm._post("/in-flight", {})
        transport.assert_called_once()

    def test_reset_usage_clears_usage_and_circuit_for_new_run(self) -> None:
        failing = mock.Mock(side_effect=_http_error(401, "unauthorized"))
        with mock.patch.object(llm.urllib.request, "urlopen", failing):
            with self.assertRaises(llm.FatalLLMError):
                llm._post("/test", {})
        llm._account("qwen-plus", {"prompt_tokens": 12, "completion_tokens": 5,
                                   "total_tokens": 17})

        llm.reset_usage()
        self.assertEqual(llm.usage(), {"calls": 0, "tokens": 0, "by_model": {}})

        succeeding = mock.Mock(return_value=_JsonResponse({"ok": True}))
        with mock.patch.object(llm.urllib.request, "urlopen", succeeding):
            self.assertEqual(llm._post("/test", {}), {"ok": True})
        succeeding.assert_called_once()

    def test_access_denied_unpurchased_retries_instead_of_opening_circuit(self) -> None:
        """权限变更后的传播延迟，实测 1–2 分钟自愈（运维交接 §3.1）。

        把它当致命会让一次 3.5 小时的全量重跑因为一个会自己好的错误中断。
        这是鉴权类里唯一的可重试例外——普通 AccessDenied 仍必须致命，
        否则密钥授权范围配错会被无限重试掩盖过去。
        """
        llm.reset_for_tests()
        self.assertFalse(llm._is_fatal("AccessDenied.Unpurchased"))
        self.assertFalse(
            llm._is_fatal('{"code":"AccessDenied.Unpurchased","message":"not purchased"}')
        )
        # 相邻语义不得被这条例外带偏
        self.assertTrue(llm._is_fatal("AccessDenied"))
        self.assertTrue(llm._is_fatal("Access denied by API-Key restrictions"))
        self.assertTrue(llm._is_fatal("Arrearage"))

    def test_transport_truncation_is_retryable_not_fatal(self) -> None:
        """传输层截断必须可重试，否则一次网络抖动就打断整轮全量重跑。

        2026-08-17 压测实测：embedding 批量响应约 200 KB，高并发下会被截断成
        http.client.IncompleteRead。它当时既不匹配 _RETRYABLE 也不属于
        URLError/TimeoutError/ConnectionError，于是一次截断直接失败；
        再叠加 2c 的失败即停，整轮 3.5 小时的重跑会被一次抖动中止。
        """
        import http.client

        transient = [
            http.client.IncompleteRead(b"partial", 500),
            http.client.BadStatusLine("garbage"),
            http.client.RemoteDisconnected("closed by peer"),
        ]
        for error in transient:
            with self.subTest(error=type(error).__name__):
                text = f"{type(error).__name__}: {error}"
                self.assertTrue(llm._is_retryable(error, text))
                self.assertFalse(llm._is_fatal(text))

        # 程序错误不得被这条放宽带进重试
        self.assertFalse(llm._is_retryable(ValueError("bad json"), "ValueError: bad json"))

    def test_plain_429_throttling_retries(self) -> None:
        transport = mock.Mock(side_effect=[
            _http_error(429, '{"code":"Throttling.RateLimit","message":"slow down"}'),
            _JsonResponse({"ok": True}),
        ])
        sleeper = mock.Mock()
        with mock.patch.object(llm.urllib.request, "urlopen", transport), \
             mock.patch.object(llm.time, "sleep", sleeper):
            self.assertEqual(llm._post("/test", {}), {"ok": True})

        self.assertEqual(transport.call_count, 2)
        sleeper.assert_called_once_with(1)

    def test_rate_quota_wording_does_not_look_like_hard_quota(self) -> None:
        transport = mock.Mock(side_effect=[
            _http_error(
                429,
                '{"code":"Throttling.RateQuota","message":"rate quota exceeded; retry"}',
            ),
            _JsonResponse({"ok": True}),
        ])
        sleeper = mock.Mock()
        with mock.patch.object(llm.urllib.request, "urlopen", transport), \
             mock.patch.object(llm.time, "sleep", sleeper):
            self.assertEqual(llm._post("/test", {}), {"ok": True})

        self.assertEqual(transport.call_count, 2)
        sleeper.assert_called_once_with(1)

    def test_rate_quota_exceeded_code_remains_retryable(self) -> None:
        transport = mock.Mock(side_effect=[
            _http_error(
                429,
                '{"code":"RateQuotaExceeded","message":"rate quota exceeded"}',
            ),
            _JsonResponse({"ok": True}),
        ])
        sleeper = mock.Mock()
        with mock.patch.object(llm.urllib.request, "urlopen", transport), \
             mock.patch.object(llm.time, "sleep", sleeper):
            self.assertEqual(llm._post("/test", {}), {"ok": True})

        self.assertEqual(transport.call_count, 2)
        sleeper.assert_called_once_with(1)

    def test_network_and_5xx_failures_retry(self) -> None:
        failures = [
            urllib.error.URLError("temporary DNS failure"),
            _http_error(503, '{"code":"ServiceUnavailable"}'),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                llm.reset_for_tests()
                transport = mock.Mock(side_effect=[
                    failure,
                    _JsonResponse({"ok": True}),
                ])
                sleeper = mock.Mock()
                with mock.patch.object(llm.urllib.request, "urlopen", transport), \
                     mock.patch.object(llm.time, "sleep", sleeper):
                    self.assertEqual(llm._post("/test", {}), {"ok": True})

                self.assertEqual(transport.call_count, 2)
                sleeper.assert_called_once_with(1)

    def test_non_retryable_response_stays_nonfatal_llm_error(self) -> None:
        transport = mock.Mock(
            side_effect=_http_error(400, '{"code":"InvalidParameter"}')
        )
        sleeper = mock.Mock()
        with mock.patch.object(llm.urllib.request, "urlopen", transport), \
             mock.patch.object(llm.time, "sleep", sleeper):
            with self.assertRaises(llm.LLMError) as caught:
                llm._post("/test", {})

        self.assertNotIsInstance(caught.exception, llm.FatalLLMError)
        self.assertEqual(transport.call_count, 1)
        sleeper.assert_not_called()

    def test_embedding_response_must_cover_every_input_with_expected_dimension(self) -> None:
        incomplete = {
            "data": [{"index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"total_tokens": 1},
        }
        with mock.patch.object(llm, "_post", return_value=incomplete):
            with self.assertRaisesRegex(llm.LLMError, "索引不完整"):
                llm.embed(["a", "b"], dim=2)

        wrong_dimension = {
            "data": [{"index": 0, "embedding": [0.1]}],
            "usage": {"total_tokens": 1},
        }
        with mock.patch.object(llm, "_post", return_value=wrong_dimension):
            with self.assertRaisesRegex(llm.LLMError, "维度不匹配"):
                llm.embed(["a"], dim=2)


class ParallelFailStopTest(unittest.TestCase):
    class WorkerFailure(LookupError):
        pass

    def setUp(self) -> None:
        llm.reset_for_tests()

    def tearDown(self) -> None:
        llm.reset_for_tests()

    def test_parallel_map_preserves_input_order(self) -> None:
        items = [5, 1, 9, 2]
        self.assertEqual(
            llm.parallel_map(lambda value: value * 10, items, workers=3),
            [50, 10, 90, 20],
        )

    def test_parallel_map_reraises_same_exception_object(self) -> None:
        original = self.WorkerFailure("map worker failed")

        def work(value: int) -> int:
            if value == 0:
                raise original
            return value

        with self.assertRaises(self.WorkerFailure) as caught:
            llm.parallel_map(work, [0, 1, 2], workers=1)
        self.assertIs(caught.exception, original)

    def test_parallel_imap_reraises_same_exception_object(self) -> None:
        original = self.WorkerFailure("imap worker failed")

        def work(value: int) -> int:
            if value == 0:
                raise original
            return value

        with self.assertRaises(self.WorkerFailure) as caught:
            list(llm.parallel_imap(work, [0, 1, 2], workers=1))
        self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)

    def test_parallel_map_only_trips_global_circuit_on_fatal(self) -> None:
        """非致命错误不得污染全局熔断，否则一次输出抖动就杀死整轮几小时的重跑。

        2026-08-17 的 2b 连续两轮死于此：先是 Stage1 响应被 max_tokens 截断，
        后是 Stage4 复核返回的 JSON 里 why 字段混了单引号。两者都只是单个条目
        的输出瑕疵，却经 parallel_map 升格成全局取消，把另一条生命周期一起带走。
        致命错误（配额/鉴权）仍必须立刻停——继续跑确实是白费。
        """
        llm.reset_for_tests()

        def one_bad(value):
            if value == 2:
                raise llm.LLMError("JSON 解析失败: 模拟")
            return value

        with self.assertRaises(llm.LLMError):
            llm.parallel_map(one_bad, [1, 2, 3], workers=2)
        self.assertIsNone(llm._circuit_reason(), "非致命错误不应打开全局熔断")

        llm.reset_for_tests()

        def one_fatal(value):
            if value == 2:
                raise llm.FatalLLMError("Arrearage")
            return value

        with self.assertRaises(llm.FatalLLMError):
            llm.parallel_map(one_fatal, [1, 2, 3], workers=2)
        self.assertIsNotNone(llm._circuit_reason(), "致命错误必须打开全局熔断")
