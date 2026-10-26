"""Extract the four ordered question sections from the supplied PDF bank."""

from __future__ import annotations

import bisect
import io
import re
from dataclasses import asdict, dataclass
from typing import Iterable

import pdfplumber


TYPE_BY_HEADING = (
    ("single", re.compile(r"^\s*一、\s*单项选择题")),
    ("multiple", re.compile(r"^\s*二、\s*不定项选择题")),
    ("true_false", re.compile(r"^\s*三、\s*判断题")),
    ("fill_blank", re.compile(r"^\s*四、\s*填空题")),
)
EXPECTED_COUNTS = {"single": 433, "multiple": 500, "true_false": 504, "fill_blank": 274}
QUESTION_START = re.compile(r"(?m)^\s*(\d{1,3})[.．、]\s*")
OPTION_MARK = re.compile(r"(?<![A-Za-z])([A-F])[.．、]\s*")
OPTION_MARK_NO_PUNCT = re.compile(r"(?m)^[ \t]*([A-F])[ \t]+(?=[\u4e00-\u9fff])")
ANSWER_MARK = re.compile(r"答案\s*[：:]\s*", re.S)
DOC_HEADER = "山东省司法鉴定人执业能力评估业务理论知识考试题库"


@dataclass
class Question:
    section: str
    number: int
    page: int
    stem: str
    options: list[dict[str, str]]
    answer: str
    explanation: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _clean_space(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def _section_lines(pdf_bytes: bytes) -> tuple[dict[str, str], dict[str, list[int]]]:
    sections = {key: [] for key, _ in TYPE_BY_HEADING}
    page_starts = {key: [] for key, _ in TYPE_BY_HEADING}
    active: str | None = None
    offset = {key: 0 for key, _ in TYPE_BY_HEADING}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if len(pdf.pages) < 7:
            raise ValueError("PDF 页数不足，无法识别题库正文。")
        for page_number, page in enumerate(pdf.pages, 1):
            text = page.extract_text(layout=False) or ""
            lines = text.splitlines()
            # 前六页是封面、说明和目录，目录也包含全部题型标题。
            if page_number > 6:
                for line in lines:
                    for section, heading in TYPE_BY_HEADING:
                        if heading.search(line):
                            active = section
                            break
            if active is None or page_number <= 6:
                continue

            kept = []
            for line in lines:
                if DOC_HEADER in line:
                    continue
                if re.match(r"^\s*[一二三四]、\s*(?:单项选择题|不定项选择题|判断题|填空题)", line):
                    continue
                if re.match(r"^\s*-\s*\d+\s*-\s*$", line):
                    continue
                kept.append(line)

            page_text = "\n".join(kept).strip()
            if not page_text:
                continue
            page_starts[active].append((offset[active], page_number))
            sections[active].append(page_text)
            offset[active] += len(page_text) + 1

    return ({key: "\n".join(value) for key, value in sections.items()}, page_starts)


def _find_options(text: str) -> tuple[str, list[dict[str, str]]]:
    matches = sorted(
        [
            (match.start(), match.end(), match.group(1), match)
            for pattern in (OPTION_MARK, OPTION_MARK_NO_PUNCT)
            for match in pattern.finditer(text)
        ],
        key=lambda item: item[0],
    )
    selected: list[re.Match] = []
    expected = "ABCDEF"
    for start, match in enumerate(matches):
        if match[2] != "A":
            continue
        run = [match]
        next_letter = 1
        for candidate in matches[start + 1 :]:
            if next_letter < len(expected) and candidate[2] == expected[next_letter]:
                run.append(candidate)
                next_letter += 1
                continue
            if candidate[2] == "A":
                break
        if len(run) >= 2:
            selected = run
            break

    if not selected:
        return _clean_space(text), []

    stem = _clean_space(text[: selected[0][0]])
    options = []
    for index, marker in enumerate(selected):
        end = selected[index + 1][0] if index + 1 < len(selected) else len(text)
        value = _clean_space(text[marker[1] : end])
        options.append({"key": marker[2], "text": value})
    return stem, options


def parse_question_bank(pdf_bytes: bytes, *, require_expected_counts: bool = True) -> list[Question]:
    """解析 PDF 中的四类题目并保持题型顺序及各类原题号顺序。

    正式导入时若题号、答案标记或目录题量不符则抛出 ValueError，
    以避免静默接受不完整或错位的题库抽取结果。
    """
    section_text, page_starts = _section_lines(pdf_bytes)
    all_questions: list[Question] = []
    problems: list[str] = []

    for section, _heading in TYPE_BY_HEADING:
        text = section_text[section]
        starts = list(QUESTION_START.finditer(text))
        questions: list[Question] = []
        expected_number = 1
        for index, marker in enumerate(starts):
            number = int(marker.group(1))
            # Numbering restarts at one for each section; ignore isolated
            # number-like text that is not the next question in sequence.
            if number != expected_number:
                continue
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            chunk = text[marker.end() : end]
            answer_matches = list(ANSWER_MARK.finditer(chunk))
            if not answer_matches:
                problems.append(f"{section} 第 {number} 题缺少答案标记")
                expected_number += 1
                continue
            answer_match = answer_matches[-1]
            stem_and_options = chunk[: answer_match.start()]
            answer = _clean_space(chunk[answer_match.end() :])
            if section == "true_false":
                answer = re.sub(r"\s*[（(]\s*[)）]\s*$", "", answer)
            stem, options = _find_options(stem_and_options)
            if not stem:
                problems.append(f"{section} 第 {number} 题题干为空")
            if section in ("single", "multiple") and len(options) < 2:
                problems.append(f"{section} 第 {number} 题未识别到选项")
            start_page_offsets = [start for start, _page in page_starts[section]]
            position = marker.start()
            page_index = bisect.bisect_right(start_page_offsets, position) - 1
            page_number = page_starts[section][max(0, page_index)][1]
            questions.append(
                Question(
                    section=section,
                    number=number,
                    page=page_number,
                    stem=stem,
                    options=options,
                    answer=answer,
                )
            )
            expected_number += 1

        expected_count = EXPECTED_COUNTS[section]
        if len(questions) != expected_count:
            problems.append(f"{section} 识别到 {len(questions)} 题，目录标注 {expected_count} 题")
        if questions and questions[-1].number != expected_count:
            problems.append(f"{section} 末题题号为 {questions[-1].number}，预期 {expected_count}")
        all_questions.extend(questions)

    if require_expected_counts and problems:
        first = "；".join(problems[:12])
        more = f"；另有 {len(problems) - 12} 项" if len(problems) > 12 else ""
        raise ValueError(first + more)
    return all_questions


def expected_counts() -> dict[str, int]:
    return EXPECTED_COUNTS.copy()


def main(argv: Iterable[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Preview the supplied司法鉴定题库 PDF extraction.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--json", type=Path, help="write parsed questions to a JSON file")
    args = parser.parse_args(argv)
    questions = parse_question_bank(args.pdf.read_bytes(), require_expected_counts=False)
    counts = {key: sum(question.section == key for question in questions) for key in EXPECTED_COUNTS}
    print(json.dumps({"counts": counts, "sample": [question.as_dict() for question in questions[:3]]}, ensure_ascii=False, indent=2))
    if args.json:
        args.json.write_text(json.dumps([question.as_dict() for question in questions], ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
