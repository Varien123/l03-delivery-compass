"""L03 交付预测模型（纯标准库）。

核心不是“算命”，而是把三类证据拆开：
1. 官方 APP 周期（强先验）
2. 同配置车友的锁单→提车样本（弱校准）
3. 产能/出口/春节等结构性修正（情景）

春节会把分布切成“年前”和“年后”两段，因此使用混合分布，
而不是简单正态分布。这样中位数可以落在年前，但尾部会诚实地延伸到年后。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import erf, exp, pi, sqrt
from typing import Any


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def add_days(value: str | date, days: int) -> date:
    return parse_date(value) + timedelta(days=days)


def days_between(start: str | date, end: str | date) -> int:
    return (parse_date(end) - parse_date(start)).days


CNY_CLOSURE_START = date(2027, 2, 4)
CNY_CLOSURE_END = date(2027, 2, 18)


def next_working_day(value: str | date) -> date:
    """把周末/春节停摆日顺延到下一个可交付工作日。

    只用于“实际提车日”的展示，不改变概率质量。
    """
    d = parse_date(value)
    if CNY_CLOSURE_START <= d <= CNY_CLOSURE_END:
        return CNY_CLOSURE_END + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def normal_pdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if abs(x - mean) < 1e-9 else 0.0
    z = (x - mean) / sigma
    return exp(-0.5 * z * z) / (sigma * sqrt(2 * pi))


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    z = (x - mean) / sigma
    return 0.5 * (1.0 + erf(z / sqrt(2)))


def _truncated_normal_mass(
    start: float, end: float, mean: float, sigma: float
) -> float:
    lo = normal_cdf(start, mean, sigma)
    hi = normal_cdf(end, mean, sigma)
    return max(0.0, hi - lo)


def mixture_daily_probabilities(
    lock_date: str | date,
    *,
    pre_weight: float,
    pre_mean_weeks: float,
    post_mean_weeks: float,
    sigma_weeks: float,
    pre_end: str | date,
    post_start: str | date,
    horizon_days: int = 150,
    extra_shift_weeks: float = 0.0,
    calibration_weeks: float = 0.0,
) -> list[dict[str, Any]]:
    """返回逐日概率质量。

    年前分量在 pre_end 截断；年后分量从 post_start 开始。
    两个分量分别重新归一化，保证总概率为 1。
    """
    lock = parse_date(lock_date)
    pre_end_offset = days_between(lock, pre_end)
    post_start_offset = days_between(lock, post_start)

    pre_mean = pre_mean_weeks * 7 + extra_shift_weeks * 7 + calibration_weeks * 7
    post_mean = post_mean_weeks * 7 + extra_shift_weeks * 7 + calibration_weeks * 7
    sigma = max(0.8, sigma_weeks * 7)

    # 年前：从锁单后第 55 天到春节前，避免把上市初期的异常值算进来
    pre_lo = 55.0
    pre_hi = float(pre_end_offset)
    pre_norm = _truncated_normal_mass(pre_lo, pre_hi, pre_mean, sigma)

    # 年后：从节后复工日到预测地平线
    post_lo = float(post_start_offset)
    post_hi = float(horizon_days)
    post_norm = _truncated_normal_mass(post_lo, post_hi, post_mean, sigma)

    rows: list[dict[str, Any]] = []
    for offset in range(55, horizon_days + 1):
        p = 0.0
        phase = "gap"
        if offset <= pre_end_offset and pre_norm > 0:
            p += pre_weight * normal_pdf(offset, pre_mean, sigma) / pre_norm
            phase = "pre"
        if offset >= post_start_offset and post_norm > 0:
            p += (1.0 - pre_weight) * normal_pdf(offset, post_mean, sigma) / post_norm
            phase = "post"
        if p <= 0:
            continue
        rows.append(
            {
                "offset": offset,
                "date": (lock + timedelta(days=offset)).isoformat(),
                "p": p,
                "phase": phase,
            }
        )

    total = sum(row["p"] for row in rows)
    if total <= 0:
        raise ValueError("模型概率为 0，请检查周期参数")
    for row in rows:
        row["p"] /= total
    return rows


def quantile_from_rows(rows: list[dict[str, Any]], q: float) -> dict[str, Any]:
    q = min(0.999, max(0.001, q))
    cumulative = 0.0
    for row in rows:
        cumulative += row["p"]
        if cumulative >= q:
            return row
    return rows[-1]


def weekly_buckets(rows: list[dict[str, Any]], lock_date: str | date) -> list[dict[str, Any]]:
    lock = parse_date(lock_date)
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        # 以锁单日为起点，每 7 天一个桶，便于阅读
        idx = (row["offset"] - 55) // 7
        bucket = buckets.setdefault(
            idx,
            {
                "start": (lock + timedelta(days=55 + idx * 7)).isoformat(),
                "end": (lock + timedelta(days=55 + idx * 7 + 6)).isoformat(),
                "p": 0.0,
                "pre": 0.0,
                "post": 0.0,
            },
        )
        bucket["p"] += row["p"]
        bucket[row["phase"]] = bucket.get(row["phase"], 0.0) + row["p"]
    return [buckets[key] for key in sorted(buckets)]


def compute_prediction(
    data: dict[str, Any],
    *,
    extra_shift_weeks: float = 0.0,
    horizon_days: int = 150,
) -> dict[str, Any]:
    order = data["order"]
    model = data["model"]
    mixture = model["mixture"]
    lock = parse_date(order["lockedAt"])
    calibration_weeks = float(model.get("calibration", {}).get("weeks", 0.0) or 0.0)

    rows = mixture_daily_probabilities(
        lock,
        pre_weight=float(mixture["preWeight"]),
        pre_mean_weeks=float(mixture["preMeanWeeks"]),
        post_mean_weeks=float(mixture["postMeanWeeks"]),
        sigma_weeks=float(mixture["sigmaWeeks"]),
        pre_end=mixture["preEnd"],
        post_start=mixture["postStart"],
        horizon_days=horizon_days,
        extra_shift_weeks=extra_shift_weeks,
        calibration_weeks=calibration_weeks,
    )

    q10 = quantile_from_rows(rows, 0.10)
    q25 = quantile_from_rows(rows, 0.25)
    q50 = quantile_from_rows(rows, 0.50)
    q75 = quantile_from_rows(rows, 0.75)
    q90 = quantile_from_rows(rows, 0.90)
    q95 = quantile_from_rows(rows, 0.95)

    official_weeks = order["appWindowWeeks"]
    official_start = lock + timedelta(weeks=official_weeks[0])
    official_end = lock + timedelta(weeks=official_weeks[1])

    def pack(row: dict[str, Any]) -> dict[str, Any]:
        raw = parse_date(row["date"])
        d = next_working_day(raw)
        return {
            "date": d.isoformat(),
            "rawDate": raw.isoformat(),
            "weekday": "一二三四五六日"[d.weekday()],
            "weeks": round(row["offset"] / 7, 1),
            "phase": row["phase"],
        }

    pre_window = {
        "start": (lock + timedelta(weeks=official_weeks[0])).isoformat(),
        "end": mixture["preEnd"],
        "label": "春节前",
    }
    post_window = {
        "start": mixture["postStart"],
        "end": mixture["postEnd"],
        "label": "春节后",
    }

    weekly = weekly_buckets(rows, lock)
    peak = max(weekly, key=lambda b: b["p"]) if weekly else None

    return {
        "asOf": data["meta"]["dataThrough"],
        "lockDate": lock.isoformat(),
        "p10": pack(q10),
        "p25": pack(q25),
        "p50": pack(q50),
        "p75": pack(q75),
        "p90": pack(q90),
        "p95": pack(q95),
        "officialWindow": {
            "start": official_start.isoformat(),
            "end": official_end.isoformat(),
            "weeks": official_weeks,
        },
        "preWindow": pre_window,
        "postWindow": post_window,
        "preProbability": round(float(mixture["preWeight"]), 3),
        "postProbability": round(1.0 - float(mixture["preWeight"]), 3),
        "confidence": float(model.get("confidence", 0.62)),
        "calibrationWeeks": round(calibration_weeks, 3),
        "weekly": weekly,
        "peakWeek": peak,
        "daily": rows,
        "meanWeeks": round(sum(r["offset"] * r["p"] for r in rows) / 7, 2),
        "medianWeeks": round(q50["offset"] / 7, 2),
        "modelVersion": model.get("version", "1.0"),
    }


def summarize(data: dict[str, Any]) -> str:
    p = compute_prediction(data)
    order = data["order"]
    return (
        f"{order['model']} {order['trim']} · {order['color']} · {order['city']}\n"
        f"锁单 {order['lockedAt']}｜官方 {order['appWindowWeeks'][0]}–{order['appWindowWeeks'][1]} 周\n"
        f"中心估计 {p['p50']['date']}（{p['medianWeeks']} 周）\n"
        f"P10 {p['p10']['date']}｜P90 {p['p90']['date']}\n"
        f"春节前 {p['preProbability'] * 100:.0f}%｜春节后 {p['postProbability'] * 100:.0f}%"
    )


if __name__ == "__main__":
    import json
    import pathlib

    path = pathlib.Path(__file__).with_name("data.json")
    print(summarize(json.loads(path.read_text(encoding="utf-8"))))
