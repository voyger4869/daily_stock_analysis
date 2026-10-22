"""东财新闻直连 provider 的离线单元测试（本地补丁，不发起网络请求）。"""

import json
from datetime import datetime

from src.eastmoney_news_provider import (
    EastMoneyNewsProvider,
    extract_eastmoney_keyword,
    parse_eastmoney_search_payload,
)


def _jsonp(articles):
    payload = {"code": 0, "result": {"cmsArticleWebOld": articles}}
    return "cb(" + json.dumps(payload, ensure_ascii=False) + ")"


def test_extract_keyword_prefers_stock_code():
    assert extract_eastmoney_keyword("招商轮船 601872 股票 最新消息") == "601872"


def test_extract_keyword_uses_quoted_topic():
    assert extract_eastmoney_keyword('"光伏设备" A股 最新消息 催化') == "光伏设备"


def test_extract_keyword_strips_noise_tokens():
    assert extract_eastmoney_keyword("芯片 舆情") == "芯片"
    assert extract_eastmoney_keyword("券商 最新消息") == "券商"


def test_parse_filters_old_news_and_strips_em_tags():
    now = datetime(2026, 10, 22, 12, 0, 0)
    fresh = {
        "date": "2026-10-21 09:00:00",
        "title": "<em>601872</em> 龙虎榜数据",
        "content": "招商轮船因跌幅偏离值达7%上榜。",
        "mediaName": "东方财富Choice数据",
        "url": "http://stock.eastmoney.com/a/x.html",
    }
    stale = {
        "date": "2026-08-01 09:00:00",
        "title": "旧闻",
        "content": "内容",
        "mediaName": "x",
        "url": "http://eastmoney.com/1",
    }
    results = parse_eastmoney_search_payload(
        _jsonp([stale, fresh]), "601872 最新消息", max_results=5, days=3, now=now
    )
    assert len(results) == 1
    assert results[0].title == "601872 龙虎榜数据"
    assert results[0].source == "东方财富Choice数据"
    assert results[0].published_date == "2026-10-21 09:00:00"


def test_parse_keeps_unparseable_date_and_respects_max_results():
    now = datetime(2026, 10, 22, 12, 0, 0)
    articles = [
        {"date": "", "title": f"新闻{i}", "content": "c", "mediaName": "m", "url": "u"}
        for i in range(4)
    ]
    results = parse_eastmoney_search_payload(
        _jsonp(articles), "601872", max_results=2, days=3, now=now
    )
    assert len(results) == 2


def test_provider_availability_follows_env(monkeypatch):
    monkeypatch.setenv("EASTMONEY_NEWS_ENABLED", "false")
    assert not EastMoneyNewsProvider().is_available
    monkeypatch.setenv("EASTMONEY_NEWS_ENABLED", "true")
    assert EastMoneyNewsProvider().is_available
