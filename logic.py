"""Pure application rules shared by the API and tests."""

from __future__ import annotations

import json
import math
import re
import unicodedata

SECTIONS = ("single", "multiple", "true_false", "fill_blank")
SECTION_LABELS = {
    "single": "单项选择题",
    "multiple": "不定项选择题",
    "true_false": "判断题",
    "fill_blank": "填空题",
}


def validate_exam_config(value: dict) -> dict:
    """校验并规范化管理员维护的模拟考试配置，阻止无效题量或评分参数落库。"""
    if not isinstance(value, dict):
        raise ValueError("考试配置格式不正确。")
    try:
        duration = int(value.get("duration_minutes", 0))
        counts_in = value.get("counts") or {}
        points_in = value.get("points") or {}
        penalties_in = value.get("wrong_penalties") or {}
        counts = {key: int(counts_in.get(key, 0)) for key in SECTIONS}
        points = {key: float(points_in.get(key, 0)) for key in SECTIONS}
        wrong_penalties = {key: float(penalties_in.get(key, 0)) for key in SECTIONS}
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("时长、题量和分值必须是数字。") from exc
    if not 1 <= duration <= 600:
        raise ValueError("考试时长需在 1 至 600 分钟之间。")
    if sum(counts.values()) < 1 or any(count < 0 or count > 1000 for count in counts.values()):
        raise ValueError("题量必须为非负整数，且总题量至少为 1。")
    if any(not math.isfinite(point) or point < 0 or point > 100 for point in points.values()):
        raise ValueError("每题分值需在 0 至 100 分之间。")
    if any(not math.isfinite(penalty) or penalty < 0 or penalty > 100 for penalty in wrong_penalties.values()):
        raise ValueError("答错扣分需在 0 至 100 分之间。")
    multiple_partial_credit = value.get("multiple_partial_credit", False)
    fill_partial_credit = value.get("fill_partial_credit", False)
    official_verified = value.get("official_verified", False)
    if not isinstance(multiple_partial_credit, bool) or not isinstance(fill_partial_credit, bool) or not isinstance(official_verified, bool):
        raise ValueError("评分开关和规则核验状态必须是布尔值。")
    official_reference = str(value.get("official_reference") or "").strip()[:500]
    if official_verified and not official_reference:
        raise ValueError("标记为已核验时，请填写正式规则来源。")
    return {
        "name": str(value.get("name") or "模拟考试")[:80],
        "duration_minutes": duration,
        "counts": counts,
        "points": points,
        "wrong_penalties": wrong_penalties,
        "multiple_partial_credit": multiple_partial_credit,
        "fill_partial_credit": fill_partial_credit,
        "official_verified": official_verified,
        "official_reference": official_reference,
    }


def _clean_choice_answer(value) -> str:
    if isinstance(value, (list, tuple, set)):
        chars = "".join(str(item).upper() for item in value)
    else:
        chars = str(value or "").upper()
    found = {char for char in chars if char in "ABCDEF"}
    return "".join(char for char in "ABCDEF" if char in found)


def _clean_true_false(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if text in {"√", "✓", "✔", "对", "正确", "是", "true", "1"}:
        return "√"
    if text in {"×", "✗", "✘", "错", "错误", "否", "false", "0"}:
        return "×"
    return text


def _clean_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[\s\u3000]+", "", text)
    return text.strip("，,。.;；:：")


def _split_fill(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[,，、;；\n\r]+", str(value or ""))
    return [_clean_text(part) for part in parts if _clean_text(part)]


def grade_answer(
    section: str,
    submitted,
    correct_answer: str,
    points: float,
    fill_partial_credit: bool = False,
    multiple_partial_credit: bool = False,
    wrong_penalty: float = 0,
) -> dict:
    """按题型评分，统一处理部分得分、错答扣分和漏答，并返回安全的答案表示。"""
    if section in ("single", "multiple"):
        actual = _clean_choice_answer(submitted)
        expected = _clean_choice_answer(correct_answer)
        correct = bool(actual) and actual == expected
        earned = points if correct else 0.0
        if not correct and section == "multiple" and multiple_partial_credit and actual and expected and set(actual).issubset(set(expected)):
            earned = points * len(actual) / len(expected)
        if actual and not correct and earned == 0:
            earned = -wrong_penalty
        return {"correct": correct, "earned": earned, "submitted": actual}
    if section == "true_false":
        actual = _clean_true_false(submitted)
        expected = _clean_true_false(correct_answer)
        correct = bool(actual) and actual == expected
        earned = points if correct else (-wrong_penalty if actual else 0.0)
        return {"correct": correct, "earned": earned, "submitted": actual}

    actual_parts = _split_fill(submitted)
    expected_parts = _split_fill(correct_answer)
    matched = sum(1 for index, expected in enumerate(expected_parts) if index < len(actual_parts) and actual_parts[index] == expected)
    correct = bool(expected_parts) and len(actual_parts) == len(expected_parts) and matched == len(expected_parts)
    if correct:
        earned = points
    elif fill_partial_credit and expected_parts:
        earned = points * matched / len(expected_parts)
    else:
        earned = 0.0
    if actual_parts and not correct and earned == 0:
        earned = -wrong_penalty
    return {"correct": correct, "earned": earned, "submitted": actual_parts}


def public_question(row, *, include_answer: bool = False, favorite: bool | None = None) -> dict:
    """生成 API 使用的题目对象；练习中默认不暴露标准答案。"""
    if isinstance(row, dict):
        get = row.get
    else:
        get = row.__getitem__
    result = {
        "id": get("id"),
        "section": get("section"),
        "number": get("number"),
        "source_page": get("source_page"),
        "stem": get("stem"),
        "options": json.loads(get("options_json") or "[]"),
        "explanation": get("explanation") or "",
        "subject": get("subject") or "",
        "difficulty": get("difficulty") or "",
    }
    if include_answer:
        result["answer"] = get("answer")
    if favorite is not None:
        result["favorite"] = favorite
    return result
