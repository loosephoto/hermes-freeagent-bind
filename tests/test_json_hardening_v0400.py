"""v0.40.0 JSON 処理の硬化（NaN・孤立サロゲート・JSON 抽出・複製・上流応答の形）の回帰。

監査プローブの実測値をそのままアサーションにしている:
* 旧 `respond` は `{"x": NaN}` を出し、厳格な JSON パーサで落ちた
* 旧 `respond` は孤立サロゲートで `UnicodeEncodeError`（応答が黙って失われた）
* 旧 `_parse_agent_reply('{"a":1} note {x}')` は `{}` を返した
* 旧 `_call_once` は上流が配列・null を返すと `AttributeError`（原因の分からない例外）
"""
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _tmpstate import isolated_state  # noqa: E402  # type: ignore[import-not-found]

isolated_state("fa-v0400-")                 # 本番ストアへ書かない
from freeagent_bind import jsonutil as J  # noqa: E402
from freeagent_bind import server as S  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _strict_loads(raw):
    def bad(token):
        raise ValueError(f"非標準の定数: {token}")
    return json.loads(raw, parse_constant=bad)


class _Out:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        self.buffer.write(text.encode("utf-8"))

    def flush(self):
        pass


class DumpsTest(unittest.TestCase):
    def test_nan_and_infinity_become_null(self):
        text = J.dumps({"a": float("nan"), "b": [float("inf"), -float("inf"), 1.5]})
        self.assertEqual(_strict_loads(text), {"a": None, "b": [None, None, 1.5]})

    def test_lone_surrogate_is_replaced_and_encodable(self):
        text = J.dumps({"t": "abc\ud83dxyz", "日本語": "ok"})
        text.encode("utf-8")                       # 例外にならない
        self.assertEqual(json.loads(text), {"t": "abc\ufffdxyz", "日本語": "ok"})

    def test_surrogate_in_key_is_replaced(self):
        text = J.dumps({"k\ud800": 1})
        text.encode("utf-8")
        self.assertEqual(json.loads(text), {"k\ufffd": 1})

    def test_normal_value_is_identical_to_json_dumps(self):
        obj = {"a": "日本語", "b": [1, 2.5, None, True], "c": {"d": "x"}}
        self.assertEqual(J.dumps(obj), json.dumps(obj, ensure_ascii=False))
        self.assertEqual(J.dumps(obj, sort_keys=True, separators=(",", ":")),
                         json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def test_input_is_not_mutated(self):
        obj = {"a": [float("nan"), "x\ud800"]}
        J.dumps(obj)
        self.assertTrue(obj["a"][0] != obj["a"][0])        # NaN のまま
        self.assertEqual(obj["a"][1], "x\ud800")


class ExtractObjectTest(unittest.TestCase):
    def test_trailing_braces_do_not_break_extraction(self):
        self.assertEqual(J.extract_object('{"a":1} note {x}'), {"a": 1})

    def test_two_objects_returns_first(self):
        self.assertEqual(J.extract_object('```json\n{"a":1}\n```\n{"b":2}'), {"a": 1})

    def test_prose_braces_before_object(self):
        self.assertEqual(J.extract_object('see {x} then {"k": "v"}'), {"k": "v"})

    def test_nested_and_fenced(self):
        self.assertEqual(J.extract_object('pre {"a":{"b":[1,{"c":2}]}} post'), {"a": {"b": [1, {"c": 2}]}})

    def test_array_is_skipped_not_returned(self):
        self.assertEqual(J.extract_object('x [1,2] {"k":"v"}'), {"k": "v"})

    def test_garbage_and_none_give_empty(self):
        for text in ("", None, "no json", '{"a": 1', "{{{{", "[1,2]"):
            self.assertEqual(J.extract_object(text), {}, text)

    def test_server_parse_agent_reply_uses_it(self):
        self.assertEqual(S._parse_agent_reply('{"a":1} note {x}'), {"a": 1})


class LoadsSafeAndCopyTest(unittest.TestCase):
    def test_deep_nesting_does_not_raise(self):
        self.assertIsNone(J.loads_safe("[" * 200000 + "]" * 200000))
        self.assertEqual(J.loads_safe("{", {}), {})
        self.assertEqual(J.loads_safe(b'{"a": 1}'), {"a": 1})
        self.assertEqual(J.loads_safe("", "d"), "d")

    def test_deep_copy_is_independent(self):
        src = {"a": [{"b": 1}], "c": "s"}
        dup = J.deep_copy(src)
        self.assertEqual(dup, src)
        dup["a"][0]["b"] = 2
        self.assertEqual(src["a"][0]["b"], 1)

    def test_deep_copy_survives_nan(self):
        dup = J.deep_copy({"a": float("nan")})
        self.assertTrue(dup["a"] != dup["a"])


class WireTest(unittest.TestCase):
    def _respond(self, result):
        out = _Out()
        with mock.patch.object(sys, "stdout", out):
            S.respond(1, result)
        return out.buffer.getvalue()

    def test_respond_never_emits_nan(self):
        raw = self._respond({"x": float("nan"), "y": float("inf")})
        self.assertEqual(_strict_loads(raw.decode("utf-8"))["result"], {"x": None, "y": None})

    def test_respond_survives_lone_surrogate(self):
        raw = self._respond({"text": "abc\ud83dxyz"})
        self.assertEqual(json.loads(raw.decode("utf-8"))["result"]["text"], "abc\ufffdxyz")

    def test_write_line_last_resort_does_not_raise(self):
        out = _Out()
        with mock.patch.object(sys, "stdout", out):
            S.write_line('{"t": "\ud83d"}')
        out.buffer.getvalue().decode("utf-8")

    def test_stdio_deeply_nested_request_is_parse_error_not_crash(self):
        script = os.path.join(ROOT, "src", "freeagent_bind", "server.py")
        payload = ("[" * 100000 + "]" * 100000 + "\n"
                   '{"jsonrpc":"2.0","id":5,"method":"ping"}\n').encode("utf-8")
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.run([sys.executable, script], input=payload, capture_output=True,
                              timeout=60, env=env)
        lines = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
        self.assertTrue(any(m.get("error", {}).get("code") in (-32700, -32600) for m in lines), lines)
        self.assertTrue(any(m.get("id") == 5 and "result" in m for m in lines), lines)


class StateStoreTest(unittest.TestCase):
    def test_atomic_write_of_sanitized_dump_with_surrogate(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="fa-v0400-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "x.json")
        self.assertTrue(S._atomic_write(path, S._dumps({"t": "a\ud800b", "n": float("nan")})))
        self.assertEqual(S._load_json(path, None), {"t": "a\ufffdb", "n": None})


class UpstreamShapeTest(unittest.TestCase):
    """上流（推論プロバイダ）の応答が想定外の形でも、原因の分かる例外か空文字で済む。"""

    def test_extract_text_tolerates_odd_shapes(self):
        for data in (None, [], "x", {"choices": {}}, {"choices": [None]}, {"choices": ["s"]},
                     {"choices": [{"message": "str"}]}, {"choices": [{"message": None}]},
                     {"choices": [{"message": {"content": 5}}]}):
            self.assertEqual(S._extract_text(data), "", data)

    def test_extract_text_parts_with_null_text(self):
        data = {"choices": [{"message": {"content": [{"text": "a"}, {"text": None}, "x",
                                                      {"type": "image"}, {"text": "b"}]}}]}
        self.assertEqual(S._extract_text(data), "ab")

    def test_extract_text_normal(self):
        self.assertEqual(S._extract_text({"choices": [{"message": {"content": "hi"}}]}), "hi")

    def test_call_once_rejects_non_object_response_with_clear_message(self):
        for body in ([], None, "text", 5):
            with mock.patch.object(S, "provider_http", return_value=body):
                with self.assertRaises(ValueError) as ctx:
                    S._call_once("nous", "m", "p", "", 16, None)
            self.assertIn("JSON オブジェクトではありません", str(ctx.exception))

    def test_call_once_tolerates_non_dict_usage_and_finish(self):
        data = {"choices": [{"message": {"content": "ok"}, "finish_reason": 7}], "usage": 5}
        with mock.patch.object(S, "provider_http", return_value=data):
            out = S._call_once("nous", "m", "p", "", 16, None)
        self.assertEqual(out["text"], "ok")
        self.assertFalse(out["truncated"])
        self.assertEqual(out["tokens"], {"prompt": None, "completion": None})

    def test_provider_http_html_body_gives_clear_error(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"<html><body>Bad gateway</body></html>"
        with mock.patch.object(S, "_urlopen", return_value=Resp()):
            with self.assertRaises(ValueError) as ctx:
                S.provider_http("/models", provider="nous")
        self.assertIn("JSON ではありません", str(ctx.exception))
        self.assertIn("Bad gateway", str(ctx.exception))

    def test_provider_http_sends_valid_utf8_for_surrogate_prompt(self):
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake(req, timeout=None):
            sent["body"] = req.data
            return Resp()
        with mock.patch.object(S, "_urlopen", fake), mock.patch.object(S, "provider_ready", return_value=True):
            S.provider_http("/chat/completions", provider="nous",
                            payload={"messages": [{"content": "a\ud83db"}], "temperature": float("nan")})
        parsed = _strict_loads(sent["body"].decode("utf-8"))
        self.assertEqual(parsed["messages"][0]["content"], "a\ufffdb")
        self.assertIsNone(parsed["temperature"])


if __name__ == "__main__":
    unittest.main()
