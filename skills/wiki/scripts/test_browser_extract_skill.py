"""wiki-browser-extract SKILL.md の構造 lint（既存スキルとのパリティ）.

SKILL.md は薄いルーターであり、指示 prose は references/browser-extract-guide.md に
委譲する（wiki-tool-query と同じ三層 SoT）。本テストは「入口として壊れていないこと」を
機械検証する:

* frontmatter に name / description があり name が wiki-browser-extract
* 参照する repo 相対リンク先が実在する（特に browser-extract-guide.md）
* browser_extract_run.py のサブコマンド例が実在サブコマンドと整合する
* approve は人間の TTY 操作で LLM は代行しない、の安全文言がある（Security 契約）

ブラウザ非依存・常時実行。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parents[2]
_SKILL_PATH = _REPO_ROOT / "skills" / "wiki-browser-extract" / "SKILL.md"

# browser_extract_run.py の argparse サブコマンド（真実源）
_REAL_SUBCOMMANDS = frozenset(
    {"prepare", "approve", "execute", "doctor", "login", "catalog-validate"}
)


def _read_skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md の先頭に YAML frontmatter がない"
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class TestSkillStructure:
    def test_skill_file_exists(self) -> None:
        assert _SKILL_PATH.is_file(), f"{_SKILL_PATH} が存在しない"

    def test_frontmatter_names_the_skill(self) -> None:
        fm = _frontmatter(_read_skill())
        assert fm.get("name") == "wiki-browser-extract"
        assert fm.get("description"), "description が空"

    def test_delegates_prose_to_guide_which_exists(self) -> None:
        text = _read_skill()
        assert "browser-extract-guide.md" in text, "guide への委譲リンクがない"
        # SKILL.md 内の repo 相対 markdown リンク先がすべて実在する
        for target in re.findall(r"\]\(([^)]+\.md)\)", text):
            if target.startswith(("http://", "https://")):
                continue
            resolved = (_SKILL_PATH.parent / target).resolve()
            assert resolved.is_file(), f"参照先が存在しない: {target}"

    def test_command_examples_use_real_subcommands(self) -> None:
        text = _read_skill()
        assert "browser_extract_run.py" in text, "実行スクリプトの参照がない"
        # `browser_extract_run.py <subcommand>` の形で現れる語がすべて実在サブコマンド
        cited = set(re.findall(r"browser_extract_run\.py\s+([a-z-]+)", text))
        assert cited, "サブコマンド例が1つもない"
        unknown = cited - _REAL_SUBCOMMANDS
        assert not unknown, f"実在しないサブコマンド例: {sorted(unknown)}"

    def test_states_human_approves_llm_must_not(self) -> None:
        text = _read_skill()
        assert "LLM" in text and "substitute" in text, (
            "SKILL.md must state that the LLM does not substitute for human approval"
        )
        assert "seal-at-prepare" in text, "the seal-at-prepare approval model must be stated"

    @pytest.mark.parametrize("subcommand", sorted(_REAL_SUBCOMMANDS))
    def test_every_subcommand_is_documented(self, subcommand: str) -> None:
        # 入口として全サブコマンドのワークフロー起点に触れていること
        assert subcommand in _read_skill(), f"{subcommand} が SKILL.md で触れられていない"
