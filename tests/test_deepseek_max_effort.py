"""Every DeepSeek call runs v1 flash at max effort, and only newsfeed/deepseek.py builds a request.

Sam, 2026-11-05: "make sure wherever we are using deepseke for pvoenance, that we are using v1 flash on max
effort". DeepSeek's model list (GET https://api.deepseek.com/models, read 2026-11-05) names `deepseek-flash`
"DeepSeek-V4.1-Flash", the only Flash model it serves. Max effort is `reasoning_effort: "max"`
(https://api-docs.deepseek.com/api/create-chat-completion). Plan: https://claude.ai/artifact/Pe5EnGtSKPSjf2Znmx5bac

This file fails if a stage builds its own request, names another model in a call, or switches thinking off.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from newsfeed import config, deepseek  # noqa: E402

CLIENT = "newsfeed/deepseek.py"


def code_files() -> list[str]:
    files = []
    for folder in ("newsfeed", "sources"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            files.append(path.relative_to(ROOT).as_posix())
    return files


def read(file: str) -> str:
    return (ROOT / file).read_text(encoding="utf-8")


class ModelAndEffortTests(unittest.TestCase):
    def test_the_client_asks_for_v1_flash_at_max_effort(self) -> None:
        self.assertEqual(deepseek.DEFAULT_MODEL, "deepseek-flash")
        self.assertEqual(deepseek.EFFORT, "max")
        payload = deepseek.Client("sk-test").build_payload("system, json", "user", 100, 0.0, True)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "max")

    def test_the_default_cap_leaves_room_to_reason(self) -> None:
        # Largest answer measured at max effort on 2026-11-05: 2,198 tokens, from an extract call.
        self.assertGreaterEqual(deepseek.DEFAULT_MAX_TOKENS, 8192)

    def test_no_second_model_is_kept_for_a_later_stage(self) -> None:
        # VERIFY_MODEL named deepseek-v4-pro and nothing used it. Sam wants Flash everywhere.
        self.assertFalse(hasattr(deepseek, "VERIFY_MODEL"))

    def test_the_effort_is_in_the_extract_and_cluster_hashes(self) -> None:
        extract = config.extract_components()
        self.assertEqual(extract["thinking"], "enabled")
        self.assertEqual(extract["reasoning_effort"], "max")
        cluster = config.cluster_components("resolve-hash", "extract-hash")
        self.assertEqual(cluster["thinking"], "enabled")
        self.assertEqual(cluster["reasoning_effort"], "max")


class SourceScanTests(unittest.TestCase):
    def test_only_the_client_talks_to_deepseek(self) -> None:
        offenders = [f for f in code_files() if f != CLIENT and re.search(r"api\.deepseek\.com|chat/completions", read(f))]
        self.assertEqual(offenders, [])

    def test_no_file_switches_thinking_off_or_sets_an_effort(self) -> None:
        # config.py names the effort as a part of the config hash. It records the setting; only the client
        # sends it.
        offenders = [
            f for f in code_files()
            if f not in (CLIENT, "newsfeed/config.py")
            and re.search(r"reasoning_effort|[\"']thinking[\"']\s*:\s*\{", read(f))
        ]
        self.assertEqual(offenders, [])
        self.assertNotRegex(read(CLIENT), r"[\"']disabled[\"']")
        self.assertNotRegex(read("newsfeed/config.py"), r"THINKING\s*=\s*[\"']disabled[\"']")

    def test_no_call_names_another_model(self) -> None:
        # The prices table keeps deepseek-v4-pro's row: an unknown id is priced at the dearest known rate.
        named = []
        for f in code_files():
            for line in read(f).splitlines():
                if "Prices(" in line:
                    continue
                named += [(f, m) for m in re.findall(r"[\"'](deepseek-[a-z0-9.-]+)[\"']", line) if m != "deepseek-flash"]
        self.assertEqual(named, [])

    def test_every_client_is_built_with_the_default_model_or_the_cli_flag(self) -> None:
        for f in code_files():
            for call in re.findall(r"\bClient\(([^)]*)\)", read(f)):
                with self.subTest(file=f, call=call):
                    model = re.search(r"model\s*=\s*([^,)]+)", call)
                    self.assertTrue(model is None or model.group(1).strip() == "args.model", call)


if __name__ == "__main__":
    unittest.main()
