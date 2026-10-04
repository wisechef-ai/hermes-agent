from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "loopskill-search"
    / "scripts"
    / "loopskill_search.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("loopskill_search_skill", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ls = _load()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload: dict, seen: list | None = None):
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req.full_url)
        return _Resp(json.dumps(payload).encode())

    return _open


def _http_error(code: int):
    def _open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))

    return _open


SEARCH = {
    "skills": [
        {
            "title": "simple-english",
            "source": "hermes-hub",
            "source_badge": "Hermes Hub",
            "quality": "community",
            "installable": True,
            "install_ref": "hermes-hub:official-creative-simple-english",
            "origin_url": "https://github.com/NousResearch/hermes-agent/tree/HEAD/optional-skills/creative/simple-english",
            "description": "Rewrite text to ASD-STE100 Simplified Technical English.",
        },
        {
            "title": "some-repo",
            "source": "github-oss",
            "quality": "community",
            "installable": False,
            "install_ref": "github-oss:o--r",
            "origin_url": "https://github.com/o/r",
            "description": "",
        },
    ],
    "sources_degraded": ["clawhub"],
}


def test_search_encodes_the_query_and_caps_page_size():
    seen: list[str] = []
    with mock.patch.object(ls.urllib.request, "urlopen", _urlopen_returning(SEARCH, seen)):
        assert ls.main(["search", "pdf & text?", "--limit", "500"]) == 0
    assert "q=pdf+%26+text%3F" in seen[0]
    assert "page_size=50" in seen[0]
    assert seen[0].startswith(f"{ls.API_BASE}/api/skills/metasearch?")


def test_search_output_marks_installable_and_degraded_sources(capsys):
    with mock.patch.object(ls.urllib.request, "urlopen", _urlopen_returning(SEARCH)):
        ls.main(["search", "ste100"])
    out = capsys.readouterr().out
    assert "1. simple-english [Hermes Hub, community, installable]" in out
    assert "2. some-repo [github-oss, community, link only]" in out
    assert "ref: hermes-hub:official-creative-simple-english" in out
    assert "No answer in time from: clawhub" in out


def test_empty_search_tells_the_agent_how_to_rephrase(capsys):
    with mock.patch.object(ls.urllib.request, "urlopen", _urlopen_returning({"skills": []})):
        ls.main(["search", "zzz"])
    assert "Write the query as the task" in capsys.readouterr().out


def test_show_prints_commands_and_review_lines(capsys):
    body = "---\nname: x\n---\n# X\nRun the installer from https://x.example/i.sh.\nSet API_KEY first.\nPlain line.\n"
    payload = {
        "source": "skills-sh",
        "origin_url": "https://raw.githubusercontent.com/o/r/HEAD/skills/x/SKILL.md",
        "body": body,
        "preview_only": False,
        "commands": {"hermes": "hermes skills install https://raw.githubusercontent.com/o/r/HEAD/skills/x/SKILL.md", "skills_cli": "", "claude_code": ""},
    }
    with mock.patch.object(ls.urllib.request, "urlopen", _urlopen_returning(payload)):
        assert ls.main(["show", "skills-sh:o--r--x"]) == 0
    out = capsys.readouterr().out
    assert "install (hermes): hermes skills install" in out
    assert "install (skills_cli)" not in out, "empty commands are not printed"
    assert "2 line(s)" in out
    assert "5: Run the installer" in out and "6: Set API_KEY first." in out
    assert "Plain line." in out.split("--- SKILL.md")[1]


def test_show_not_found_points_to_the_next_result(capsys):
    with mock.patch.object(ls.urllib.request, "urlopen", _http_error(404)):
        assert ls.main(["show", "skills-sh:gone--gone--gone"]) == 0
    assert "Not found" in capsys.readouterr().out


def test_rate_limit_stops_with_exit_2_and_no_retry(capsys):
    calls = {"n": 0}

    def _open(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b"{}"))

    with mock.patch.object(ls.urllib.request, "urlopen", _open):
        assert ls.main(["search", "pdf"]) == 2
    assert calls["n"] == 1
    assert "Wait 60 seconds" in capsys.readouterr().err


def test_network_failure_exits_1(capsys):
    def _open(req, timeout=None):
        raise urllib.error.URLError("offline")

    with mock.patch.object(ls.urllib.request, "urlopen", _open):
        assert ls.main(["search", "pdf"]) == 1
    assert "Request failed" in capsys.readouterr().err


def test_api_base_override(monkeypatch):
    monkeypatch.setenv("LOOPSKILL_API_BASE", "https://ls.internal.example/")
    module = _load()
    assert module.API_BASE == "https://ls.internal.example"


def test_risk_lines_flag_code_blocks_urls_and_credentials_not_prose():
    body = (
        "# Skill\n"
        "Plain prose about the skill.\n"
        "```bash\n"
        "run-something --as-root /etc\n"
        "```\n"
        "See https://example.com/docs.\n"
        "Export your API_KEY first.\n"
        "More prose.\n"
    )
    assert ls.risk_lines(body) == [
        "4: run-something --as-root /etc",
        "6: See https://example.com/docs.",
        "7: Export your API_KEY first.",
    ]
