#!/usr/bin/env python3
"""L03 交付罗盘 · 构建 / 每日更新脚本（纯标准库）。

常用：
    python update.py                    # 重算并生成 index.html
    python update.py --check            # 只看当前结论，不写文件
    python update.py --report "lock=2026-09-20;delivery=2026-12-15;trim=1330 Max;city=成都"
    python update.py --source "date=2026-11-05;tier=A;publisher=小鹏汽车;title=...;url=https://...;claim=..."
    python update.py --merge l03-update-2026-11-05.json
    python update.py --snapshot         # 另存一份当日快照

设计原则：
- 数据只从 data.json 进入；网页里的“本地录入”通过导出更新包再合并。
- 所有第三方产能测算都保留 tier，不把分析当官方。
- 每次构建都记录 predictionHistory，方便回看预测是怎么变的。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import model

ROOT = pathlib.Path(__file__).resolve().parent
DATA_PATH = ROOT / "data.json"
TEMPLATE_PATH = ROOT / "template.html"
APP_JS_PATH = ROOT / "app.js"
INDEX_PATH = ROOT / "index.html"
SNAPSHOT_DIR = ROOT / "snapshots"
TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
TIER_CONFIDENCE = {"A": 0.95, "B": 0.8, "C": 0.55}


def now() -> datetime:
    return datetime.now(TZ)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_pairs(text: str) -> dict[str, str]:
    text = text.replace("；", ";")
    out: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def clamp_date(value: str) -> str:
    return value[:10]


def update_meta(data: dict[str, Any], dates: list[str] | None = None, as_of: str | None = None) -> None:
    meta = data.setdefault("meta", {})
    current = meta.get("dataThrough") or date.today().isoformat()
    candidates = [current]
    if dates:
        candidates.extend(d for d in dates if d)
    if as_of:
        candidates.append(as_of)
    resolved = max(clamp_date(d) for d in candidates)
    # 信源日期可能晚于今天（预告/时区），数据截止日不回写到未来
    if not as_of and resolved > now().date().isoformat():
        resolved = now().date().isoformat()
    meta["dataThrough"] = resolved
    meta["generatedAt"] = now().isoformat(timespec="seconds")
    review_day = date.fromisoformat(meta["dataThrough"]) + timedelta(days=1)
    meta["nextReview"] = f"{review_day.isoformat()}T09:00:00+08:00"


def summary(data: dict[str, Any]) -> dict[str, Any]:
    p = model.compute_prediction(data)
    return {
        "asOf": data["meta"]["dataThrough"],
        "lockDate": p["lockDate"],
        "p10": p["p10"],
        "p50": p["p50"],
        "p90": p["p90"],
        "preProbability": p["preProbability"],
        "postProbability": p["postProbability"],
        "meanWeeks": p["meanWeeks"],
        "medianWeeks": p["medianWeeks"],
        "confidence": p["confidence"],
        "calibrationWeeks": p.get("calibrationWeeks", 0),
        "officialWindow": p["officialWindow"],
        "postWindow": p["postWindow"],
        "modelVersion": p["modelVersion"],
    }


def append_history(data: dict[str, Any], note: str) -> None:
    s = summary(data)
    history = data.setdefault("predictionHistory", [])
    entry = {
        "date": data["meta"]["dataThrough"],
        "p50": s["p50"]["date"],
        "p10": s["p10"]["date"],
        "p90": s["p90"]["date"],
        "confidence": s["confidence"],
        "note": note,
    }
    if history and all(history[-1].get(k) == entry.get(k) for k in ("date", "p50", "p10", "p90")):
        history[-1]["note"] = note
    else:
        history.append(entry)
    data["predictionHistory"] = history[-60:]


def append_changelog(data: dict[str, Any], changes: list[str]) -> None:
    if not changes:
        return
    log = data.setdefault("changelog", [])
    log.append(
        {
            "date": data["meta"]["dataThrough"],
            "version": data["meta"].get("version", "1.0.0"),
            "changes": changes,
        }
    )


def normalize_report(raw: dict[str, str], index: int) -> dict[str, Any]:
    lock = raw.get("lock") or raw.get("lockDate") or raw.get("锁单") or ""
    if not lock:
        raise ValueError("report 缺少 lock=YYYY-MM-DD")
    lock = clamp_date(lock)
    delivery = raw.get("delivery") or raw.get("deliveryDate") or raw.get("提车")
    delivery = clamp_date(delivery) if delivery else None
    status = raw.get("status") or ("delivered" if delivery else "waiting")
    if status not in {"delivered", "allocation", "waiting"}:
        status = "waiting"
    if delivery and delivery < lock:
        raise ValueError("提车/配车日期不能早于锁单日期")
    observed = clamp_date(raw.get("observed") or raw.get("observedAt") or delivery or now().date().isoformat())
    wait_weeks = round(((date.fromisoformat(delivery or observed) - date.fromisoformat(lock)).days) / 7, 1)
    return {
        "id": raw.get("id") or f"local-{now().strftime('%Y%m%d%H%M%S')}-{index}",
        "lockDate": lock,
        "deliveryDate": delivery,
        "observedAt": observed,
        "status": status,
        "trim": raw.get("trim") or "1330 Max",
        "color": raw.get("color") or "未披露",
        "city": raw.get("city") or "未披露",
        "waitWeeks": max(0.0, wait_weeks),
        "source": raw.get("source") or "手动录入",
        "url": raw.get("url") or "",
        "confidence": raw.get("confidence") or "local",
        "note": raw.get("note") or "每日更新新增样本",
    }


def add_report(data: dict[str, Any], raw: dict[str, str], quiet: bool = False) -> bool:
    reports = data.setdefault("community", [])
    existing = {str(r.get("id")) for r in reports}
    report = normalize_report(raw, len(reports) + 1)
    if report["id"] in existing:
        if not quiet:
            print(f"跳过：样本 id 已存在（{report['id']}）")
        return False
    reports.append(report)
    if not quiet:
        print(f"已加入样本：{report['lockDate']} 锁单 → {report['deliveryDate'] or '等车中'}，{report['waitWeeks']} 周")
    return True


def normalize_source(raw: dict[str, str], index: int) -> dict[str, Any]:
    url = raw.get("url") or ""
    if not re.match(r"^https?://", url):
        raise ValueError("source 缺少合法的 http(s) url")
    tier = (raw.get("tier") or "B").upper()
    if tier not in {"A", "B", "C"}:
        raise ValueError("tier 必须是 A/B/C")
    return {
        "id": raw.get("id") or f"local-s-{now().strftime('%Y%m%d%H%M%S')}-{index}",
        "date": clamp_date(raw.get("date") or now().date().isoformat()),
        "tier": tier,
        "publisher": raw.get("publisher") or "未注明",
        "title": raw.get("title") or "未命名信源",
        "url": url,
        "claim": raw.get("claim") or raw.get("summary") or "未填写结论",
        "confidence": float(raw.get("confidence") or TIER_CONFIDENCE[tier]),
        "usedFor": raw.get("usedFor") or "user",
        "status": raw.get("status") or "active",
    }


def add_source(data: dict[str, Any], raw: dict[str, str], quiet: bool = False) -> bool:
    sources = data.setdefault("sources", [])
    if any(s.get("url") == raw.get("url") for s in sources if raw.get("url")):
        if not quiet:
            print(f"跳过：该 URL 已存在（{raw.get('url')}）")
        return False
    source = normalize_source(raw, len(sources) + 1)
    sources.append(source)
    if not quiet:
        print(f"已加入信源：[{source['tier']}] {source['publisher']} · {source['title']}")
    return True


def merge_package(data: dict[str, Any], package: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    dates: list[str] = []
    for raw in package.get("reports", []) or []:
        try:
            report = {k: str(v) for k, v in raw.items() if v is not None}
            if add_report(data, report, quiet=True):
                changes.append(f"新增车友样本：{report.get('lock') or report.get('lockDate')} 锁单")
                dates.append(report.get("delivery") or report.get("observed") or report.get("lock") or "")
        except Exception as exc:  # noqa: BLE001
            print(f"样本跳过：{exc}")
    for raw in package.get("sources", []) or []:
        try:
            source = {k: str(v) for k, v in raw.items() if v is not None}
            if add_source(data, source, quiet=True):
                changes.append(f"新增信源：[{source.get('tier', '?')}] {source.get('title', '')}")
                dates.append(source.get("date") or "")
        except Exception as exc:  # noqa: BLE001
            print(f"信源跳过：{exc}")
    update_meta(data, dates)
    if changes:
        append_changelog(data, changes)
    return changes


def validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("meta", "order", "model", "capacity", "community", "sources", "methodology"):
        if key not in data:
            errors.append(f"缺少顶层字段：{key}")
    if errors:
        return errors
    try:
        weeks = data["order"]["appWindowWeeks"]
        if not (isinstance(weeks, list) and len(weeks) == 2 and 0 < weeks[0] < weeks[1]):
            errors.append("order.appWindowWeeks 必须是 [低, 高] 且低 < 高")
        date.fromisoformat(data["order"]["lockedAt"])
        date.fromisoformat(data["meta"]["dataThrough"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"日期字段错误：{exc}")
    for report in data.get("community", []):
        try:
            date.fromisoformat(report["lockDate"])
            if report.get("deliveryDate"):
                date.fromisoformat(report["deliveryDate"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"样本 {report.get('id')} 日期错误：{exc}")
    for source in data.get("sources", []):
        if source.get("tier") not in {"A", "B", "C"}:
            errors.append(f"信源 {source.get('id')} tier 非法")
    return errors


def build(data: dict[str, Any]) -> pathlib.Path:
    errors = validate(data)
    if errors:
        raise SystemExit("数据校验失败：\n- " + "\n- ".join(errors))
    if not TEMPLATE_PATH.exists():
        raise SystemExit(f"缺少模板：{TEMPLATE_PATH}")
    if not APP_JS_PATH.exists():
        raise SystemExit(f"缺少 app.js：{APP_JS_PATH}")

    data["prediction"] = summary(data)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    app_js = APP_JS_PATH.read_text(encoding="utf-8")
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = html.replace("__DATA_JSON__", payload)
    html = html.replace("/*__APP_JS__*/", app_js)
    INDEX_PATH.write_text(html, encoding="utf-8")
    return INDEX_PATH


def snapshot(data: dict[str, Any]) -> pathlib.Path:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{data['meta']['dataThrough']}.json"
    write_json(path, data)
    return path


def print_check(data: dict[str, Any]) -> None:
    p = summary(data)
    tiers = {"A": 0, "B": 0, "C": 0}
    for source in data.get("sources", []):
        tiers[source.get("tier", "C")] = tiers.get(source.get("tier", "C"), 0) + 1
    reports = data.get("community", [])
    delivered = [r for r in reports if r.get("status") == "delivered"]
    latest_source = max(data.get("sources", []), key=lambda s: s.get("date", ""), default=None)
    stale = max(0, (now().date() - date.fromisoformat(data["meta"]["dataThrough"])).days)
    print("")
    print("L03 交付罗盘 · 当前状态")
    print("-" * 46)
    print(f"订单      {data['order']['model']} {data['order']['trim']} · {data['order']['color']} · {data['order']['city']}")
    print(f"锁单      {data['order']['lockedAt']}｜官方 {data['order']['appWindowWeeks'][0]}–{data['order']['appWindowWeeks'][1]} 周")
    print(f"中心估计  {p['p50']['date']}（{p['medianWeeks']} 周，周{p['p50']['weekday']}）")
    print(f"P10–P90   {p['p10']['date']} – {p['p90']['date']}")
    print(f"春节分叉  年前 {p['preProbability']*100:.0f}% / 年后 {p['postProbability']*100:.0f}%")
    print(f"数据截至  {data['meta']['dataThrough']}（{stale} 天前）｜下次核对 {data['meta'].get('nextReview', '')}")
    auto = data.get("auto", {})
    if auto.get("lastRun"):
        auto_sources = sum(1 for s in data.get("sources", []) if s.get("auto"))
        print(f"自动更新  {auto.get('lastRun')}｜{auto.get('lastStatus')}｜自动信源 {auto_sources} 条｜校准 {p.get('calibrationWeeks', 0):+.2f} 周")
    print(f"信源      {len(data.get('sources', []))} 条（A {tiers.get('A',0)} / B {tiers.get('B',0)} / C {tiers.get('C',0)}）")
    print(f"样本      {len(reports)} 条（已完成 {len(delivered)} 条）")
    if latest_source:
        print(f"最新信源  [{latest_source.get('tier')}] {latest_source.get('date')} {latest_source.get('title')}")
    print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="L03 交付罗盘构建/更新脚本")
    parser.add_argument("--check", action="store_true", help="只打印当前结论，不写文件")
    parser.add_argument("--no-build", action="store_true", help="只更新 data.json，不生成 index.html")
    parser.add_argument("--snapshot", action="store_true", help="另存一份当日快照到 snapshots/")
    parser.add_argument("--as-of", help="覆盖数据截止日期（YYYY-MM-DD）")
    parser.add_argument("--report", action="append", default=[], help="新增车友样本，key=value;...")
    parser.add_argument("--source", action="append", default=[], help="新增信源，key=value;...")
    parser.add_argument("--merge", help="合并网页导出的更新包 JSON")
    args = parser.parse_args()

    try:
        data = read_json(DATA_PATH)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"读取 data.json 失败：{exc}")

    changed = False
    changes: list[str] = []
    dates: list[str] = []

    for raw in args.report:
        try:
            pairs = parse_pairs(raw)
            if add_report(data, pairs):
                changed = True
                changes.append(f"新增车友样本：{pairs.get('lock') or pairs.get('lockDate')} 锁单")
                dates.append(pairs.get("delivery") or pairs.get("observed") or pairs.get("lock") or "")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"--report 参数错误：{exc}")

    for raw in args.source:
        try:
            pairs = parse_pairs(raw)
            if add_source(data, pairs):
                changed = True
                changes.append(f"新增信源：[{pairs.get('tier', '?')}] {pairs.get('title', '')}")
                dates.append(pairs.get("date") or "")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"--source 参数错误：{exc}")

    if args.merge:
        try:
            package = read_json(pathlib.Path(args.merge))
            merged = merge_package(data, package)
            if merged:
                changed = True
                changes.extend(merged)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"合并更新包失败：{exc}")

    if args.as_of:
        dates.append(args.as_of)
        changed = True

    if changed:
        update_meta(data, dates)
        append_changelog(data, changes)
        append_history(data, "每日更新：" + ("；".join(changes) if changes else "数据截止日调整"))
        if not args.check:
            write_json(DATA_PATH, data)
    else:
        update_meta(data, as_of=args.as_of)
        # 即使没有新增，也刷新构建时间；不改动 predictionHistory
        if not args.check:
            write_json(DATA_PATH, data)

    print_check(data)

    if args.check:
        return

    if args.snapshot:
        path = snapshot(data)
        print(f"快照已写入：{path}")

    if args.no_build:
        return

    path = build(data)
    size_kb = path.stat().st_size / 1024
    print(f"已生成：{path}（{size_kb:.1f} KB）")
    print("直接双击 index.html 即可查看。")
    print("")


if __name__ == "__main__":
    main()
