"""Google News RSS provider 的离线单元测试（本地补丁，不发起网络请求）。"""

import json
from datetime import datetime, timedelta, timezone

from src.googlenews_rss_provider import (
    GoogleNewsRssProvider,
    extract_news_keyword,
    parse_google_news_rss,
)


def _rss(items):
    rows = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<pubDate>{d}</pubDate><source url=\"http://x.com\">{s}</source></item>"
        for t, u, d, s in items
    )
    return f"<rss><channel>{rows}</channel></rss>"


def test_extract_keyword_prefers_stock_code():
    assert extract_news_keyword("招商轮船 601872 股票 最新消息") == "601872"


def test_extract_keyword_uses_quoted_topic():
    assert extract_news_keyword('"光伏设备" A股 最新消息 催化') == "光伏设备"


def test_extract_keyword_strips_noise_tokens():
    assert extract_news_keyword("芯片 舆情") == "芯片"


def test_parse_filters_old_news_and_extracts_source():
    now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    fresh_date = (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    stale_date = (now - timedelta(days=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    text = _rss(
        [
            ("601872 龙虎榜数据", "http://news.google.com/a", fresh_date, "证券时报网"),
            ("旧闻标题", "http://news.google.com/b", stale_date, "x"),
        ]
    )
    results = parse_google_news_rss(text, "601872 最新消息", 5, 3, now=now)
    assert len(results) == 1
    assert results[0].title == "601872 龙虎榜数据"
    assert results[0].source == "证券时报网"
    assert results[0].url == "http://news.google.com/a"
    assert results[0].published_date is not None


def test_parse_keeps_unparseable_date_and_respects_max_results():
    now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    text = _rss(
        [("新闻1", "u1", "not-a-date", "m"), ("新闻2", "u2", "", "m"), ("新闻3", "u3", "", "m")]
    )
    results = parse_google_news_rss(text, "601872", 2, 3, now=now)
    assert len(results) == 2


def test_parse_empty_feed_returns_empty_list():
    assert parse_google_news_rss("<rss><channel></channel></rss>", "601872", 5, 3) == []


def test_provider_availability_follows_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_NEWS_ENABLED", "false")
    assert not GoogleNewsRssProvider().is_available
    monkeypatch.setenv("GOOGLE_NEWS_ENABLED", "true")
    assert GoogleNewsRssProvider().is_available
