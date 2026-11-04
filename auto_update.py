#!/usr/bin/env python3
"""L03 交付罗盘 · 自动抓取与校准。

每天 09:00 运行：
1. 小鹏官网资讯（官方 A）
2. IT之家 RSS（媒体 B）
3. Google News RSS 多关键词（媒体 B / 社区 C）
4. 小鹏社区首页的 L03 提车帖（社区 C，低权重）

抓到的内容会：
- 去重后写入 data.json 的 sources；
- 从标题/正文抽取 L03 月交付、集团月交付、增程/纯电周期等信号；
- 用信号自动校准 model.calibration.weeks；
- 重新生成 index.html 和当日快照。

只依赖 Python 标准库；不调用任何付费 API。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import model
import update

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data.json"
BACKUP_DIR = ROOT / "backups"
AUTO_LOG_PATH = ROOT / "auto_log.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

OFFICIAL_NEWS = "https://www.xiaopeng.com/news/"
ITHOME_RSS = "https://www.ithome.com/rss/"
GOOGLE_NEWS = "https://news.google.com/rss/search"

GOOGLE_QUERIES = [
    "小鹏 MONA L03 交付",
    "小鹏 L03 产能",
    "小鹏 L03 提车",
    "小鹏 L03 增程",
    "小鹏汽车 月交付",
    "小鹏 广州工厂",
    "小鹏 武汉工厂",
    "小鹏 L05 产能",
    "小鹏 春节 交付",
]

B_DOMAINS = (
    "ithome.com", "cnmo.com", "36kr.com", "sohu.com", "sina.com.cn", "sina.cn",
    "autohome.com.cn", "yiche.com", "jiemian.com", "cls.cn", "eastmoney.com",
    "163.com", "qq.com", "thepaper.cn", "nbd.com.cn", "ifeng.com", "donews.com",
    "chejiahao", "d1ev.com", "gaoneng.bjx.com.cn", "stcn.com", "21jingji.com",
)
C_DOMAINS = ("xiaohongshu.com", "bbs.xiaopeng.com", "zhihu.com", "weibo.com", "tieba.baidu.com")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def get(url: str, timeout: int = 22) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(3_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="ignore")


def strip_html(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value: str) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(value).date()
    except Exception:  # noqa: BLE001
        return None


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def normalize_title(value: str) -> str:
    value = strip_html(value)
    value = re.sub(r"\s*[-_|—]\s*[^-_|—]{2,20}$", "", value)
    value = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    return value.lower()


def event_key(title: str) -> str:
    """把同一事件的不同媒体标题聚成一个 key，避免 20 条“10月交付2万”。"""
    text = re.sub(r"\s+", "", title.lower())
    compact = re.sub(r"[\s,，]", "", text)
    if "l03" in text:
        if ("10月" in text or "十月" in text) and ("交付" in text or "销量" in text or "月销" in text):
            return "l03-oct-delivery"
        if ("9月" in text or "九月" in text) and ("交付" in text or "销量" in text):
            return "l03-sep-delivery"
        if ("8月" in text or "八月" in text) and ("交付" in text or "销量" in text):
            return "l03-aug-delivery"
        if "交付" in text and ("2万" in text or "20000" in compact or "两万" in text):
            return "l03-oct-delivery"
        if "14000" in compact or "1.4万" in text:
            return "l03-sep-delivery"
        if "产能" in text or "扩产" in text or "工厂" in text:
            return "l03-capacity"
        if "提车" in text or "订车" in text or "等车" in text or "交付周期" in text or "配车" in text:
            return "l03-wait"
        if "上市" in text:
            return "l03-launch"
        if "出口" in text or "海外" in text:
            return "l03-export"
    if "50237" in compact or "50,237" in title:
        return "group-oct-delivery"
    if ("10月" in text or "十月" in text) and "小鹏" in text and ("交付" in text or "销量" in text):
        return "group-oct-delivery"
    if "11月" in text and "小鹏" in text and ("交付" in text or "销量" in text or "台阶" in text):
        return "group-nov-outlook"
    if ("增程" in text) and ("周" in text):
        return "erev-window"
    if ("纯电" in text) and ("周" in text):
        return "pure-window"
    if "广州" in text and ("工厂" in text or "二期" in text or "产能" in text):
        return "guangzhou-capacity"
    if "武汉" in text and ("工厂" in text or "二期" in text or "产能" in text):
        return "wuhan-capacity"
    if "l05" in text and ("产能" in text or "交付" in text):
        return "l05-capacity"
    return normalize_title(title)


def is_valuable(title: str, tier: str) -> bool:
    text = re.sub(r"\s+", "", title.lower())
    if any(word in text for word in (
        "值得买", "怎么样", "试驾", "探店", "对比", "导购", "优惠", "降价", "权益",
        "问答", "faq", "销量榜", "top15", "零跑", "小米", "理想", "蔚来", "鸿蒙",
        "新势力", "将启动", "上市即交付", "视频",
    )):
        return False
    if "l05" in text and "l03" not in text:
        return False
    if tier == "A":
        return any(word in text for word in ("l03", "月交付", "交付新车", "产能", "工厂", "下线"))
    if "l03" in text:
        return any(word in text for word in (
            "交付", "提车", "订车", "产能", "周期", "工厂", "下线", "配车", "等待",
            "生产", "扩产", "月销", "销量",
        ))
    return any(word in text for word in ("小鹏", "mona")) and any(
        word in text for word in ("月交付", "交付量", "产能", "工厂", "产量", "扩产", "交付周期", "月销")
    )


def in_days(value: date | None, days: int) -> bool:
    if not value:
        return False
    return (today() - value).days <= days and value <= today() + timedelta(days=1)


def is_l03(text: str) -> bool:
    return bool(re.search(r"L03|MONA\s*L03", text, re.I))


# ---------------------------------------------------------------------------
# 信源适配器
# ---------------------------------------------------------------------------

def fetch_official_news(max_age_days: int) -> dict[str, Any]:
    result: dict[str, Any] = {"sources": [], "facts": {}, "errors": [], "stats": {}}
    try:
        page = get(OFFICIAL_NEWS)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"official: {exc}")
        return result

    cards: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<a href="(/news/[^"]+)"[^>]*>.*?'
        r'<p class="card-info-desc">(.*?)</p>\s*'
        r'<span class="card-info-time">([^<]+)</span>',
        re.S,
    )
    for href, desc, time_text in pattern.findall(page):
        title = strip_html(desc)
        if not title:
            continue
        cards.append({
            "url": urllib.parse.urljoin(OFFICIAL_NEWS, html_lib.unescape(href)),
            "title": title,
            "date": parse_date(strip_html(time_text)),
        })

    seen: set[str] = set()
    for card in cards:
        if card["url"] in seen:
            continue
        seen.add(card["url"])
        if not in_days(card["date"], max_age_days):
            continue
        result["sources"].append({
            "date": (card["date"] or today()).isoformat(),
            "tier": "A",
            "publisher": "小鹏汽车官网",
            "title": card["title"],
            "url": card["url"],
            "claim": card["title"],
            "confidence": 0.96,
            "usedFor": "official",
            "status": "active",
            "auto": True,
        })

    # 最新一篇通常是月度交付，抓正文里的数字
    for card in cards[:2]:
        if not in_days(card["date"], max_age_days):
            continue
        try:
            article = get(card["url"])
            text = extract_article_text(article)
            facts = extract_facts(text, card["date"])
            result["facts"] = merge_facts(result["facts"], facts)
            if is_l03(text) or "交付" in card["title"]:
                claim = text[:220] if text else card["title"]
                for source in result["sources"]:
                    if source["url"] == card["url"]:
                        source["claim"] = claim
                        break
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"official-article: {exc}")

    result["stats"]["official_cards"] = len(cards)
    return result


def extract_article_text(page: str) -> str:
    # 优先取正文容器，失败则全文去标签
    for pattern in (
        r'<div class="news-detail[^"]*">(.*?)</div>\s*</div>',
        r'<article[^>]*>(.*?)</article>',
        r'<div class="article-content[^"]*">(.*?)</div>',
    ):
        match = re.search(pattern, page, re.S)
        if match:
            text = strip_html(match.group(1))
            if len(text) > 120:
                return text
    return strip_html(page)


def fetch_ithome(max_age_days: int) -> dict[str, Any]:
    result: dict[str, Any] = {"sources": [], "facts": {}, "errors": [], "stats": {}}
    try:
        xml_text = get(ITHOME_RSS)
        root = ET.fromstring(xml_text)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"ithome: {exc}")
        return result

    count = 0
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        pub_date = parse_date(item.findtext("pubDate") or "")
        text = strip_html(description)
        if not title or not link:
            continue
        if not (is_l03(title) or is_l03(text) or "小鹏" in title or "小鹏" in text):
            continue
        if not in_days(pub_date, max_age_days):
            continue
        count += 1
        result["sources"].append({
            "date": (pub_date or today()).isoformat(),
            "tier": "B",
            "publisher": "IT之家",
            "title": title,
            "url": link,
            "claim": text[:240] if text else title,
            "confidence": 0.86,
            "usedFor": "media",
            "status": "active",
            "auto": True,
        })
        result["facts"] = merge_facts(result["facts"], extract_facts(f"{title} {text}", pub_date))
    result["stats"]["ithome_matches"] = count
    return result


def fetch_google_news(max_age_days: int) -> dict[str, Any]:
    result: dict[str, Any] = {"sources": [], "facts": {}, "errors": [], "stats": {}}
    seen_titles: set[str] = set()
    total = 0
    for query in GOOGLE_QUERIES:
        query_count = 0
        url = GOOGLE_NEWS + "?" + urllib.parse.urlencode({
            "q": f"{query} when:{max_age_days}d",
            "hl": "zh-CN",
            "gl": "CN",
            "ceid": "CN:zh-Hans",
        })
        try:
            xml_text = get(url)
            root = ET.fromstring(xml_text)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"google[{query}]: {exc}")
            continue
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = parse_date(item.findtext("pubDate") or "")
            source_el = item.find("source")
            publisher = (source_el.text or "").strip() if source_el is not None else "Google 新闻"
            source_url = source_el.get("url", "") if source_el is not None else ""
            if not title or not link:
                continue
            clean_title = re.sub(r"\s*-\s*[^-]{2,24}$", "", title).strip()
            if not (is_l03(clean_title) or "小鹏" in clean_title or "MONA" in clean_title):
                continue
            if not in_days(pub_date, max_age_days):
                continue
            key = normalize_title(clean_title)
            if key in seen_titles:
                continue
            seen_titles.add(key)
            tier = classify_source(publisher, source_url)
            if tier is None:
                continue
            if not is_valuable(clean_title, tier):
                continue
            total += 1
            query_count += 1
            result["sources"].append({
                "date": (pub_date or today()).isoformat(),
                "tier": tier,
                "publisher": publisher or "Google 新闻",
                "title": clean_title,
                "url": link,
                "claim": clean_title,
                "confidence": 0.82 if tier == "B" else 0.62,
                "usedFor": "media" if tier == "B" else "community",
                "status": "active",
                "auto": True,
            })
            result["facts"] = merge_facts(result["facts"], extract_facts(clean_title, pub_date))
            if query_count >= 8:
                break
        time.sleep(0.25)
    result["stats"]["google_matches"] = total
    return result


def classify_source(publisher: str, url: str) -> str | None:
    host = urllib.parse.urlparse(url).netloc.lower()
    name = publisher.lower()
    if "xiaopeng" in host or "xpeng" in host or "小鹏汽车" in publisher or "xpeng" in name:
        return "A"
    if any(domain in host for domain in C_DOMAINS) or "社区" in publisher or "小红书" in publisher:
        return "C"
    if any(domain in host for domain in B_DOMAINS):
        return "B"
    # 没有域名时按常见媒体名兜底
    if any(key in publisher for key in ("IT之家", "CNMO", "36氪", "搜狐", "新浪", "汽车之家", "界面", "财联社", "每日经济", "第一财经")):
        return "B"
    return None


def fetch_bbs(max_age_days: int) -> dict[str, Any]:
    result: dict[str, Any] = {"sources": [], "reports": [], "facts": {}, "errors": [], "stats": {}}
    try:
        page = get("https://bbs.xiaopeng.com/")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"bbs: {exc}")
        return result

    links: list[str] = []
    for match in re.finditer(r'href="(/(?:article|thread)/\d+[^"]*)"', page):
        href = html_lib.unescape(match.group(1))
        if href not in links:
            links.append(href)
    candidates: list[dict[str, Any]] = []
    href_matches = list(re.finditer(r'href="(/(?:article|thread)/\d+[^"]*)"', page))
    for index, match in enumerate(href_matches):
        href = html_lib.unescape(match.group(1))
        if href not in links[:20]:
            continue
        start = match.start()
        end = href_matches[index + 1].start() if index + 1 < len(href_matches) else start + 5000
        block = page[start:end]
        if not is_l03(block):
            continue
        title_match = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        body_match = re.search(r"<article[^>]*>(.*?)</article>", block, re.S)
        time_match = re.search(r'time">([^<]+)', block)
        title = strip_html(title_match.group(1)) if title_match else ""
        body = strip_html(body_match.group(1)) if body_match else ""
        time_text = strip_html(time_match.group(1)) if time_match else ""
        if not title and body:
            title = body[:40]
        if not title or not is_l03(f"{title} {body}"):
            continue
        post_date = parse_post_date(time_text)
        candidates.append({
            "url": urllib.parse.urljoin("https://bbs.xiaopeng.com", href),
            "title": title,
            "body": body,
            "date": post_date,
        })

    # 同标题去重
    seen: set[str] = set()
    candidates = [c for c in candidates if not (normalize_title(c["title"]) in seen or seen.add(normalize_title(c["title"])))]
    for candidate in candidates[:6]:
        if not in_days(candidate["date"], max_age_days):
            continue
        result["sources"].append({
            "date": (candidate["date"] or today()).isoformat(),
            "tier": "C",
            "publisher": "小鹏社区",
            "title": candidate["title"],
            "url": candidate["url"],
            "claim": candidate["body"][:220] if candidate["body"] else candidate["title"],
            "confidence": 0.58,
            "usedFor": "community",
            "status": "active",
            "auto": True,
        })

    # 只抓前 3 篇正文，尝试提取明确的“锁单日 → 提车日”
    for candidate in candidates[:3]:
        try:
            detail = get(candidate["url"])
            text = extract_article_text(detail)
            if not text:
                text = candidate["body"]
            report = extract_report(text, candidate["url"], candidate["date"])
            if report:
                result["reports"].append(report)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"bbs-detail: {exc}")

    result["stats"]["bbs_candidates"] = len(candidates)
    return result


def parse_post_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    match = re.search(r"(\d{1,2})-(\d{1,2})", value)
    if not match:
        return parse_date(value)
    month, day = int(match.group(1)), int(match.group(2))
    year = today().year
    if month > today().month + 1:
        year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_report(text: str, url: str, post_date: date | None) -> dict[str, Any] | None:
    if not is_l03(text):
        return None
    lock = find_date_near(text, ("锁单", "下定", "大定", "订车", "小订"))
    delivery = find_date_near(text, ("提车",))
    if not delivery:
        delivery = find_date_near(text, ("配车", "下线"))
    if not lock or not delivery:
        return None
    if delivery < lock:
        # 同一年内，交付日期可能跨年
        if delivery.month < lock.month:
            delivery = date(delivery.year + 1, delivery.month, delivery.day)
        else:
            return None
    wait_weeks = round((delivery - lock).days / 7, 1)
    if not (3 <= wait_weeks <= 30):
        return None
    trim = "L03（未披露）"
    if "1330" in text:
        trim = "1330 Max" if "Max" in text or "max" in text else "1330"
    elif "525" in text:
        trim = "525 Max"
    elif "625" in text:
        trim = "625 Max"
    return {
        "id": f"auto-{lock.isoformat()}-{delivery.isoformat()}",
        "lockDate": lock.isoformat(),
        "deliveryDate": delivery.isoformat(),
        "observedAt": delivery.isoformat(),
        "status": "delivered",
        "trim": trim,
        "color": "未披露",
        "city": "未披露",
        "waitWeeks": wait_weeks,
        "source": "小鹏社区（自动）",
        "url": url,
        "confidence": "auto",
        "note": "自动抓取，需人工复核",
        "auto": True,
    }


def find_date_near(text: str, keywords: tuple[str, ...]) -> date | None:
    patterns = [
        re.compile(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?"),
        re.compile(r"(?<!\d)(\d{1,2})[月./-](\d{1,2})日?"),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 24):match.end() + 34]
            if not any(keyword in window for keyword in keywords):
                continue
            groups = match.groups()
            try:
                if len(groups) == 3:
                    y, m, d = map(int, groups)
                else:
                    m, d = map(int, groups)
                    y = today().year
                    if m > today().month + 1:
                        y -= 1
                value = date(y, m, d)
            except ValueError:
                continue
            if date(2026, 1, 1) <= value <= today() + timedelta(days=30):
                return value
    return None


# ---------------------------------------------------------------------------
# 事实抽取 & 自动校准
# ---------------------------------------------------------------------------

def extract_facts(text: str, source_date: date | None) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    text = strip_html(text)

    # 集团月交付：两种语序
    for pattern in (
        r"(20\d{2})年\s*(\d{1,2})月(?!\s*\d{1,2}\s*日)[^。]{0,40}?(?:小鹏集团|集团)[^。]{0,40}?交付(?:新车)?\s*([\d,]+)\s*台",
        r"(?:小鹏集团|集团)[^。]{0,40}?(20\d{2})年\s*(\d{1,2})月(?!\s*\d{1,2}\s*日)[^。]{0,40}?交付(?:新车)?\s*([\d,]+)\s*台",
    ):
        for y, m, n in re.findall(pattern, text):
            facts.setdefault("groupMonthly", {})[month_key(int(y), int(m))] = int(n.replace(",", ""))

    # L03 月交付：允许“10 月交付超 20,000 台”
    for m, n in re.findall(r"L03[^。]{0,50}?(\d{1,2})\s*月[^。]{0,25}?交付[^0-9]{0,10}([\d,]+)\s*台", text):
        facts.setdefault("l03Monthly", {})[month_key((source_date or today()).year, int(m))] = int(n.replace(",", ""))

    # L03 月交付（万辆）
    for m, n in re.findall(r"L03[^。]{0,50}?(\d{1,2})\s*月[^。]{0,25}?交付[^0-9]{0,10}(\d+(?:\.\d+)?)\s*万", text):
        facts.setdefault("l03Monthly", {})[month_key((source_date or today()).year, int(m))] = int(float(n) * 10000)

    # 增程/纯电周期
    for kind, low, high in re.findall(r"(增程|纯电)(?:版)?[^。]{0,28}?(\d{1,2})\s*[–\-—~至]\s*(\d{1,2})\s*周", text):
        key = "erevWindow" if kind == "增程" else "pureWindow"
        facts[key] = [int(low), int(high)]

    # 产能节点
    for y, m in re.findall(r"(20\d{2})年\s*(\d{1,2})月[^。]{0,18}?(?:投产|量产|建成)", text):
        facts.setdefault("capacityDates", []).append(month_key(int(y), int(m)))
    for n in re.findall(r"(\d{2,3})\s*万套", text):
        facts.setdefault("bodySets", []).append(int(n) * 10000)

    if source_date:
        facts["lastFactDate"] = source_date.isoformat()
    return facts


def merge_facts(base: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    out = dict(base or {})
    for key, value in (new or {}).items():
        if isinstance(value, dict):
            merged = dict(out.get(key) or {})
            merged.update(value)
            out[key] = merged
        elif isinstance(value, list):
            existing = list(out.get(key) or [])
            for item in value:
                if item not in existing:
                    existing.append(item)
            out[key] = existing
        else:
            out[key] = value
    return out


def compute_calibration(data: dict[str, Any], new_sources: list[dict[str, Any]]) -> dict[str, Any]:
    signals = data.setdefault("auto", {}).setdefault("signals", {})
    l03 = signals.get("l03Monthly") or {}
    if l03:
        latest_key = max(l03)
        latest_value = int(l03[latest_key])
    else:
        latest_value = int((data.get("model", {}).get("capacity", {}) or {}).get("referenceOutput", 22000))
    capacity_weeks = clamp((22000 - latest_value) / 10000 * 0.4, -0.8, 0.8)

    erev = signals.get("erevWindow")
    if isinstance(erev, list) and len(erev) == 2:
        midpoint = (float(erev[0]) + float(erev[1])) / 2
        window_weeks = clamp((midpoint - 13.5) * 0.15, -0.5, 0.5)
    else:
        window_weeks = 0.0

    auto_reports = [r for r in data.get("community", []) if r.get("auto") and r.get("status") == "delivered"]
    if auto_reports:
        recent = sorted(auto_reports, key=lambda r: r.get("deliveryDate", ""))[-6:]
        avg = sum(float(r.get("waitWeeks", 0)) for r in recent) / len(recent)
        cohort_weeks = clamp((avg - 12.6) * 0.08, -0.4, 0.4)
    else:
        cohort_weeks = 0.0

    risk_text = " ".join(
        f"{s.get('title', '')} {s.get('claim', '')}" for s in new_sources
    )
    positive = len(re.findall(r"提速|缩短|双班|满产|产能提升|提前交付|加快", risk_text))
    negative = len(re.findall(r"延迟|停摆|减产|芯片|供应链|出口分流|产能不足|等待延长", risk_text))
    risk_weeks = clamp((negative - positive) * 0.06, -0.3, 0.3)

    total = clamp(capacity_weeks + window_weeks + cohort_weeks + risk_weeks, -1.2, 1.2)
    return {
        "weeks": round(total, 2),
        "capacityWeeks": round(capacity_weeks, 2),
        "windowWeeks": round(window_weeks, 2),
        "cohortWeeks": round(cohort_weeks, 2),
        "riskWeeks": round(risk_weeks, 2),
        "updatedAt": now().isoformat(timespec="seconds"),
        "note": f"L03 最新月交付 {latest_value:,} 台；增程周期 {erev or '未抓到'}；自动样本 {len(auto_reports)} 条",
    }


# ---------------------------------------------------------------------------
# 合并/落盘
# ---------------------------------------------------------------------------

def source_exists(data: dict[str, Any], source: dict[str, Any]) -> bool:
    url = source.get("url")
    title_key = normalize_title(source.get("title", ""))
    for existing in data.get("sources", []):
        if url and existing.get("url") == url:
            return True
        if title_key and normalize_title(existing.get("title", "")) == title_key:
            return True
    return False


def source_score(source: dict[str, Any]) -> int:
    title = source.get("title", "").lower()
    tier = source.get("tier", "C")
    score = 0
    if "l03" in title:
        score += 100
        if any(word in title for word in ("交付超", "交付破", "月交付", "交付2万", "20000", "2万")):
            score += 20
        if "10月" in title or "十月" in title:
            score += 10
    if "占四成" in title:
        score -= 15
    if any(word in title for word in ("交付周期", "提车", "配车", "产能", "工厂", "下线", "增程", "纯电", "等待")):
        score += 40
    if tier == "A":
        score += 35
    elif tier == "B":
        score += 10
    if "月交付" in title or "交付量" in title:
        score += 10
    if any(word in title for word in ("销量榜", "新势力", "排名", "零跑", "小米", "理想", "蔚来", "鸿蒙")):
        score -= 60
    if "l05" in title:
        score -= 15
    return score


def select_sources(sources: list[dict[str, Any]], limit: int = 18) -> list[dict[str, Any]]:
    """按事件聚类 + 价值评分，避免同一件事刷屏。"""
    rank = {"A": 0, "B": 1, "C": 2}
    ordered = sorted(
        sources,
        key=lambda s: (
            -source_score(s),
            rank.get(s.get("tier"), 9),
            -date.fromisoformat(s.get("date") or today().isoformat()).toordinal(),
        ),
    )
    selected: list[dict[str, Any]] = []
    seen_events: set[str] = set()
    for source in ordered:
        if not is_valuable(source.get("title", ""), source.get("tier", "C")):
            continue
        key = event_key(source.get("title", ""))
        if key in seen_events:
            continue
        seen_events.add(key)
        selected.append(source)
        if len(selected) >= limit:
            break
    return selected


def add_auto_sources(data: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    for source in candidates:
        if source_exists(data, source):
            continue
        source["id"] = f"auto-s-{now().strftime('%Y%m%d%H%M%S')}-{len(added) + 1}"
        data.setdefault("sources", []).append(source)
        added.append(source)
    return added


def add_auto_reports(data: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    existing = {
        (r.get("lockDate"), r.get("deliveryDate"))
        for r in data.get("community", [])
    }
    for report in candidates:
        key = (report.get("lockDate"), report.get("deliveryDate"))
        if key in existing:
            continue
        data.setdefault("community", []).append(report)
        existing.add(key)
        added.append(report)
    return added


def prune_auto_sources(data: dict[str, Any], keep: int = 45) -> None:
    sources = data.get("sources", [])
    manual = [s for s in sources if not s.get("auto")]
    auto = [s for s in sources if s.get("auto")]
    auto.sort(key=lambda s: s.get("date", ""), reverse=True)
    data["sources"] = manual + auto[:keep]


def backup_data() -> Path | None:
    if not DATA_PATH.exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"data-{now().strftime('%Y%m%d-%H%M%S')}.json"
    shutil.copy2(DATA_PATH, path)
    backups = sorted(BACKUP_DIR.glob("data-*.json"))
    for old in backups[:-30]:
        old.unlink(missing_ok=True)
    return path


def run(max_age_days: int, dry_run: bool, no_build: bool, verbose: bool) -> int:
    started = time.time()
    data = update.read_json(DATA_PATH)
    before_p50 = model.compute_prediction(data)["p50"]["date"]

    adapters = [
        ("official", fetch_official_news),
        ("ithome", fetch_ithome),
        ("google", fetch_google_news),
        ("bbs", fetch_bbs),
    ]
    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, max_age_days): name for name, fn in adapters}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[name] = {"sources": [], "reports": [], "facts": {}, "errors": [f"{name}: {exc}"], "stats": {}}
            errors.extend(results[name].get("errors", []))

    all_sources: list[dict[str, Any]] = []
    all_reports: list[dict[str, Any]] = []
    all_facts: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    for name, result in results.items():
        all_sources.extend(result.get("sources", []))
        all_reports.extend(result.get("reports", []))
        all_facts = merge_facts(all_facts, result.get("facts", {}))
        stats[name] = result.get("stats", {})

    # 先做本地标题去重，避免 Google 多关键词重复
    deduped: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for source in all_sources:
        key = normalize_title(source.get("title", ""))
        if key in seen_titles:
            continue
        seen_titles.add(key)
        deduped.append(source)
    all_sources = select_sources(deduped, limit=18)

    if dry_run:
        print(f"\n[DRY RUN] 抓到 {len(all_sources)} 条候选信源、{len(all_reports)} 条候选样本")
        for source in all_sources[:20]:
            print(f"  [{source['tier']}] {source['date']} {source['publisher']} · {source['title'][:70]}")
        for report in all_reports[:5]:
            print(f"  样本 {report['lockDate']} → {report['deliveryDate']} · {report['waitWeeks']} 周 · {report['url']}")
        for error in errors[:8]:
            print(f"  ERR {error}")
        return 0

    backup = backup_data()
    added_sources = add_auto_sources(data, all_sources)
    added_reports = add_auto_reports(data, all_reports)
    prune_auto_sources(data)

    signals = data.setdefault("auto", {}).setdefault("signals", {})
    for key, value in all_facts.items():
        if isinstance(value, dict):
            merged = dict(signals.get(key) or {})
            merged.update(value)
            signals[key] = merged
        elif isinstance(value, list):
            existing = list(signals.get(key) or [])
            for item in value:
                if item not in existing:
                    existing.append(item)
            signals[key] = existing
        else:
            signals[key] = value

    old_calibration = float((data.get("model", {}).get("calibration", {}) or {}).get("weeks", 0.0) or 0.0)
    calibration = compute_calibration(data, added_sources or all_sources)
    data.setdefault("model", {})["calibration"] = calibration
    completed = [r for r in data.get("community", []) if r.get("status") == "delivered"]
    data["model"].setdefault("cohort", {})["sampleSize"] = len(completed)
    auto = data.setdefault("auto", {})
    auto.update({
        "enabled": True,
        "lastRun": now().isoformat(timespec="seconds"),
        "lastSuccess": now().isoformat(timespec="seconds") if not errors or added_sources else auto.get("lastSuccess"),
        "lastStatus": "ok" if not errors else "partial",
        "newSources": len(added_sources),
        "newReports": len(added_reports),
        "calibrationWeeks": calibration["weeks"],
        "errors": errors[-8:],
        "adapterStats": stats,
        "headlines": [s.get("title", "") for s in added_sources[:6]],
    })

    new_dates = [s.get("date") for s in added_sources if s.get("date")]
    new_dates += [r.get("deliveryDate") for r in added_reports if r.get("deliveryDate")]
    if new_dates:
        update.update_meta(data, new_dates)
    else:
        update.update_meta(data)

    changes: list[str] = []
    if added_sources:
        changes.append(f"自动抓取新增 {len(added_sources)} 条信源")
    if added_reports:
        changes.append(f"自动抓取新增 {len(added_reports)} 条车友样本")
    if abs(calibration["weeks"] - old_calibration) >= 0.01:
        changes.append(f"自动校准 {calibration['weeks']:+.2f} 周")
    if changes:
        update.append_changelog(data, changes)
        update.append_history(data, "自动更新：" + "；".join(changes))
    update.write_json(DATA_PATH, data)

    after_p50 = model.compute_prediction(data)["p50"]["date"]
    log = {
        "runAt": auto["lastRun"],
        "durationSeconds": round(time.time() - started, 1),
        "backup": str(backup) if backup else None,
        "newSources": [
            {"tier": s.get("tier"), "date": s.get("date"), "publisher": s.get("publisher"), "title": s.get("title"), "url": s.get("url")}
            for s in added_sources
        ],
        "newReports": added_reports,
        "calibration": calibration,
        "p50Before": before_p50,
        "p50After": after_p50,
        "errors": errors,
        "stats": stats,
    }
    AUTO_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not no_build:
        update.build(data)
        update.snapshot(data)

    print("\nL03 自动更新")
    print("-" * 46)
    print(f"新增信源   {len(added_sources)} 条（累计 {len(data.get('sources', []))} 条）")
    print(f"新增样本   {len(added_reports)} 条（累计 {len(data.get('community', []))} 条）")
    print(f"自动校准   {calibration['weeks']:+.2f} 周")
    print(f"中心估计   {before_p50} → {after_p50}")
    print(f"数据截至   {data['meta']['dataThrough']}｜耗时 {time.time() - started:.1f}s")
    if errors:
        print(f"警告       {len(errors)} 个抓取错误（已保留可用信源）")
    if backup:
        print(f"备份       {backup.name}")
    if not no_build:
        print("已生成 index.html 和当日快照")
    print("")

    if verbose:
        for source in added_sources:
            print(f"  + [{source['tier']}] {source['publisher']} · {source['title'][:80]}")
        for error in errors:
            print(f"  ! {error}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="L03 交付罗盘自动抓取/校准")
    parser.add_argument("--max-age-days", type=int, default=7, help="只收录最近 N 天的信源")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和打印，不写数据")
    parser.add_argument("--no-build", action="store_true", help="只更新 data.json，不生成网页")
    parser.add_argument("--verbose", action="store_true", help="打印新增明细")
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.max_age_days, args.dry_run, args.no_build, args.verbose))
    except KeyboardInterrupt:
        print("\n已取消")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
