#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VitalVault 健康知识采集器
----------------------------------------------------
抓取 RSS 来源 -> 清洗 -> 生成摘要 -> 关键词自动识别人体部位/系统
-> 增量去重后写入 articles_data.js（供 vitalvault.html 加载）

设计原则：
- 只用 Python 标准库，零依赖，离线/双击即可运行
- 只存“摘要”而非全文，数据文件保持轻量
- 自动分类用关键词规则，免费、离线、无算力消耗
- 封面只存图片 URL（不下载、不内嵌），页面端懒加载
- 每次运行只追加新条目（按链接去重），知识越积越多
"""

import os
import re
import sys
import json
import time
import hashlib
import urllib.request
from datetime import date
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "articles_data.js")
DENY = os.path.join(HERE, "denylist.json")  # 人工删除过的条目 id，永不再抓

UA = "Mozilla/5.0 (VitalVault-Collector; +offline) Python-urllib"
TIMEOUT = 20
PER_FEED = 6          # 每个来源每次最多取最新几条
SUMMARY_LEN = 150     # 摘要字数上限

# ============================================================
# RSS 来源配置（请按需增删；优先选有公开 RSS、内容为科普/研究摘要的来源）
# 注：抓取前请确认来源版权与使用条款，本脚本只提取标题/摘要级内容并回链原文。
# ============================================================
FEEDS = [
    {"org": "ScienceDaily", "name": "ScienceDaily 营养", "evidence": "B",
     "url": "https://www.sciencedaily.com/rss/health_medicine/nutrition.xml"},
    {"org": "ScienceDaily", "name": "ScienceDaily 健身", "evidence": "B",
     "url": "https://www.sciencedaily.com/rss/health_medicine/fitness.xml"},
    {"org": "ScienceDaily", "name": "ScienceDaily 睡眠", "evidence": "B",
     "url": "https://www.sciencedaily.com/rss/mind_brain/sleep_disorders.xml"},
    {"org": "ScienceDaily", "name": "ScienceDaily 心理", "evidence": "B",
     "url": "https://www.sciencedaily.com/rss/mind_brain/psychology.xml"},

    # ---- Substack 渠道（独立作者订阅源，与上方平级、无优先级）----
    {"org": "The RightDose", "name": "The RightDose", "evidence": "C",
     "url": "https://rightdose.substack.com/feed"},
    {"org": "Dr. Mary Claire", "name": "Dr. Mary Claire Haver", "evidence": "C",
     "url": "https://drmaryclairehaver.substack.com/feed"},
    {"org": "The Cell Lab", "name": "Variana Volk", "evidence": "C",
     "url": "https://varianavolk.substack.com/feed"},
    # 在此继续添加你的 Tier-1 来源 RSS：
    # {"org": "...", "name": "...", "evidence": "A", "url": "https://.../feed"},
]

# ============================================================
# 人体系统关键词（匹配标题+摘要的小写文本，命中最多者即归类）
# 标签 id 必须与 vitalvault.html 中 SYSTEMS 的 key 一致
# ============================================================
SYSTEM_KEYWORDS = {
    "brain":     ["brain", "mind", "cognit", "memory", "focus", "attention",
                  "mental", "mood", "depress", "anxi", "neuro", "sleep",
                  "dementia", "alzheim", "stress", "emotion"],
    "senses":    ["eye", "vision", "sight", "retina", "glaucoma",
                  "hearing", "ear ", "auditory", "sensory", "smell", "taste"],
    "heart":     ["heart", "cardiac", "cardiovascular", "blood pressure",
                  "hypertension", "cholesterol", "circulation", "artery",
                  "arterial", "stroke", "aerobic"],
    "lungs":     ["lung", "respiratory", "breathing", "breath", "asthma",
                  "oxygen", "pulmonary"],
    "immune":    ["immune", "immunity", "inflammation", "inflammat",
                  "infection", "vaccine", "antibod", "autoimmune"],
    "gut":       ["gut", "digest", "microbiome", "microbiota", "fiber",
                  "fibre", "intestin", "stomach", "bowel", "probiotic", "colon"],
    "hormones":  ["hormone", "endocrine", "thyroid", "insulin", "cortisol",
                  "metabolic", "metabolism", "circadian", "diabetes", "glucose"],
    "muscles":   ["muscle", "bone", "skelet", "strength", "joint",
                  "resistance training", "osteo", "posture", "fitness",
                  "exercise", "training", "mobility", "sarcopenia"],
    "skin":      ["skin", "derma", "sunscreen", "wound", "collagen", "uv "],
    "nutrition": ["nutrition", "diet", "protein", "vitamin", "food",
                  "eating", "calorie", "mineral", "omega", "nutrient"],
    "whole":     ["longevity", "aging", "ageing", "lifespan", "healthspan",
                  "wellness", "lifestyle", "habit", "recovery", "resilience"],
}

# 标签命中需要的最小权重；模糊（无命中）归入 whole
DEFAULT_SYSTEM = "whole"

# 重度医疗/疾病/药物/负面词黑名单：标题或摘要命中即跳过，
# 让自动采集只保留积极、预防与生活方式导向的内容（作为“灵感雷达”）。
BLOCKLIST = [
    "cancer", "tumor", "tumour", "carcinoma", "leukemia",
    "diabetes", "parkinson", "alzheimer", "dementia",
    "covid", "coronavirus", "hiv", "autism", "schizophrenia",
    "sclerosis", "arthritis", "stroke", "seizure",
    "drug", "pill", "medication", "side effect", "overdose",
    "mortality", "death", "deadly", "fatal", "lethal",
    "surgery", "clinical trial", "patients", "diagnosis",
    "symptom", "disorder", "syndrome", "disease",
    "outbreak", "epidemic", "infected", "infection",
]


def is_blocked(title, summary):
    text = (title + " " + summary).lower()
    return any(w in text for w in BLOCKLIST)


# ---------------- 工具函数 ----------------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def summarize(text, n=SUMMARY_LEN):
    text = strip_html(text)
    if len(text) <= n:
        return text
    cut = text[:n]
    # 尽量在标点/空格处收尾
    for sep in ["。", "！", "？", ". ", "! ", "? ", "，", ", ", " "]:
        idx = cut.rfind(sep)
        if idx > n * 0.5:
            return cut[:idx + (1 if sep.strip() else 0)].strip() + "…"
    return cut.strip() + "…"


def classify(title, summary):
    text = (title + " " + summary).lower()
    scores = {}
    for sysid, kws in SYSTEM_KEYWORDS.items():
        hits = sum(text.count(k) for k in kws)
        if hits:
            scores[sysid] = hits
    if not scores:
        return [DEFAULT_SYSTEM]
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out = [ranked[0][0]]
    # 第二名若得分接近，作为附加标签（最多 2 个）
    if len(ranked) > 1 and ranked[1][1] >= max(1, ranked[0][1] * 0.6):
        out.append(ranked[1][0])
    return out


def parse_date(s):
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return date.today().isoformat()


def find_image(item_xml, description):
    # media:content / media:thumbnail
    m = re.search(r'<media:(?:content|thumbnail)[^>]*\burl="([^"]+)"', item_xml)
    if m:
        return m.group(1)
    # enclosure（图片类型）
    m = re.search(r'<enclosure[^>]*\burl="([^"]+)"[^>]*type="image', item_xml)
    if m:
        return m.group(1)
    m = re.search(r'<enclosure[^>]*type="image[^"]*"[^>]*\burl="([^"]+)"', item_xml)
    if m:
        return m.group(1)
    # 描述里的第一张 <img>
    m = re.search(r'<img[^>]*\bsrc="([^"]+)"', description or "")
    if m:
        return m.group(1)
    return ""


def make_id(link):
    return "c" + hashlib.md5(link.encode("utf-8")).hexdigest()[:10]


def text_of(elem, *tags):
    for t in tags:
        c = elem.find(t)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_feed(raw, feed):
    """解析 RSS 2.0 / Atom，返回条目 dict 列表。"""
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items

    # RSS 2.0: channel/item ；Atom: entry
    nodes = root.findall(".//item")
    atom = False
    if not nodes:
        nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        atom = True

    for node in nodes[:PER_FEED]:
        if atom:
            title = text_of(node, "{http://www.w3.org/2005/Atom}title")
            link_el = node.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href") if link_el is not None else ""
            desc = text_of(node,
                           "{http://www.w3.org/2005/Atom}summary",
                           "{http://www.w3.org/2005/Atom}content")
            pub = text_of(node,
                          "{http://www.w3.org/2005/Atom}updated",
                          "{http://www.w3.org/2005/Atom}published")
        else:
            title = text_of(node, "title")
            link = text_of(node, "link")
            desc = text_of(node, "description")
            pub = text_of(node, "pubDate")

        if not title or not link:
            continue

        item_xml = ET.tostring(node, encoding="unicode")
        summary = summarize(desc) if desc else summarize(title)
        if is_blocked(title, summary):
            continue
        items.append({
            "id": make_id(link),
            "systems": classify(title, summary),
            "cover": find_image(item_xml, desc),
            "title": strip_html(title),
            "org": feed["org"],
            "author": feed.get("name", feed["org"]),
            "date": parse_date(pub),
            "sourceName": re.sub(r"^https?://(www\.)?", "", link).split("/")[0],
            "sourceUrl": link,
            "evidence": feed.get("evidence", "B"),
            "summary": summary,
            "body": [{"t": "p", "x": summary}],
        })
    return items


def load_existing():
    if not os.path.isfile(OUT):
        return []
    try:
        txt = open(OUT, "r", encoding="utf-8").read()
        s = txt.find("[")
        e = txt.rfind("]")
        if s == -1 or e == -1:
            return []
        return json.loads(txt[s:e + 1])
    except Exception as ex:
        print(f"  ! 读取现有数据失败（将从空开始）：{ex}")
        return []


def load_denylist():
    if not os.path.isfile(DENY):
        return set()
    try:
        return set(json.load(open(DENY, "r", encoding="utf-8")))
    except Exception as ex:
        print(f"  ! 读取黑名单失败（忽略）：{ex}")
        return set()


def write_out(articles):
    articles.sort(key=lambda a: a.get("date", ""), reverse=True)
    payload = json.dumps(articles, ensure_ascii=False, indent=2)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("// VitalVault 知识数据 —— 由 collect.py 自动维护（请勿手动编辑）\n")
        f.write("window.ARTICLES = ")
        f.write(payload)
        f.write(";\n")


def main():
    print("VitalVault 采集器启动…")
    existing = load_existing()
    denylist = load_denylist()
    seen = {a.get("sourceUrl") for a in existing}
    seen_ids = {a.get("id") for a in existing} | denylist
    print(f"现有知识：{len(existing)} 篇（黑名单 {len(denylist)} 条）")

    new_items = []
    for feed in FEEDS:
        print(f"抓取：{feed.get('name', feed['org'])} …", end=" ")
        try:
            raw = fetch(feed["url"])
        except Exception as ex:
            print(f"跳过（{ex}）")
            continue
        items = parse_feed(raw, feed)
        added = 0
        for it in items:
            if it["sourceUrl"] in seen or it["id"] in seen_ids:
                continue
            seen.add(it["sourceUrl"])
            seen_ids.add(it["id"])
            new_items.append(it)
            added += 1
        print(f"新增 {added} / 解析 {len(items)}")
        time.sleep(0.5)  # 轻量节流，对来源友好

    if not new_items:
        print("没有新内容。数据文件保持不变。")
        return

    merged = existing + new_items
    write_out(merged)
    by_sys = {}
    for it in new_items:
        for s in it["systems"]:
            by_sys[s] = by_sys.get(s, 0) + 1
    print(f"\n完成：新增 {len(new_items)} 篇，总计 {len(merged)} 篇")
    print("新增分布：" + ", ".join(f"{k}:{v}" for k, v in sorted(by_sys.items())))
    print(f"输出：{OUT}")


if __name__ == "__main__":
    main()
