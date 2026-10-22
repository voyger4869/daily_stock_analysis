"""Google News RSS 新闻搜索 Provider（本地补丁，免 Key）。

上游搜索链路依赖 Tavily/SerpAPI 等需要 API Key 的搜索服务；本模块基于
Google News 的公开 RSS 接口实现免 Key 的新闻检索，作为 provider 链的
兜底数据源。GitHub Actions（海外 runner）可直连；注册位置见
``SearchService.__init__`` 第 8 步。

可通过环境变量 ``GOOGLE_NEWS_ENABLED``（默认开启）整体停用本兜底源。
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, List, Optional
from urllib.parse import quote_plus

import requests

from src.search_service import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
_GOOGLE_REQUEST_TIMEOUT = 12
_GOOGLE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 查询串中的通用修饰词，构造新闻关键词时剔除（如 "招商轮船 601872 股票 最新消息"）
_KEYWORD_NOISE_TOKENS = {
    "股票", "股份", "最新消息", "新闻", "资讯", "舆情", "公告", "催化",
    "机构分析", "机构", "研报", "A股", "最新", "消息", "相关", "动态",
    "热点", "事件", "分析", "重估", "预期", "基本面",
}

# 6 位 A 股代码（前后不能紧邻数字，避免误命中长数字串）
_A_SHARE_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# 引号主题词（覆盖 topic 查询如 '"光伏设备" A股 最新消息 催化'）
_QUOTED_RE = re.compile(r"[\"'「『](.+?)[\"'」』]")


def extract_news_keyword(query: str) -> str:
    """从搜索查询中提取适合新闻检索的关键词。

    优先级：6 位 A 股代码 > 引号内主题词 > 剔除修饰词后的剩余文本。
    """
    if not query:
        return ""
    code_match = _A_SHARE_CODE_RE.search(query)
    if code_match:
        return code_match.group(0)
    quoted = _QUOTED_RE.search(query)
    if quoted:
        keyword = quoted.group(1).strip()
        if keyword:
            return keyword
    tokens = [t for t in re.split(r"[\s,，、;；]+", query) if t]
    kept = [t for t in tokens if t not in _KEYWORD_NOISE_TOKENS]
    keyword = " ".join(kept).strip()
    return keyword or query.strip()


def _google_when_operator(days: int) -> str:
    """将请求窗口映射为 Google News RSS 支持的 when: 算子。

    Google 对未知时间档位可能直接忽略算子，这里只映射到已知可用的档位，
    超出部分由调用方的客户端时效过滤兜底。
    """
    if days <= 1:
        return "when:1d"
    if days <= 7:
        return "when:7d"
    return "when:1y"


def parse_google_news_rss(
    text: str,
    query: str,
    max_results: int,
    days: int,
    now: Optional[datetime] = None,
) -> List[SearchResult]:
    """解析 Google News RSS XML 为 SearchResult 列表（含时效过滤）。

    发布时间无法解析时保留该条目，避免因个别脏数据丢失有效新闻。
    """
    if not text:
        return []
    root = ET.fromstring(text)
    items = root.findall("./channel/item")
    if not items:
        return []

    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=days)
    results: List[SearchResult] = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        source_name = ""
        source_el = item.find("source")
        if source_el is not None and source_el.text:
            source_name = source_el.text.strip()

        published_date: Optional[str] = None
        keep = True
        if pub_date_raw:
            try:
                published_dt = parsedate_to_datetime(pub_date_raw)
                published_date = published_dt.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                keep = published_dt >= cutoff
            except (TypeError, ValueError):
                keep = True
        if not keep:
            continue

        results.append(
            SearchResult(
                title=title,
                snippet=f"{title}" if title else "",
                url=link,
                source=source_name or "Google News",
                published_date=published_date,
            )
        )
        if len(results) >= max_results:
            break
    return results


class GoogleNewsRssProvider(BaseSearchProvider):
    """Google News RSS 新闻搜索（免 Key 兜底源）。

    基类要求非空 api_keys 才视为可用；RSS 接口本身无需鉴权，这里传入
    占位 key 以复用基类的搜索流程与错误统计。
    """

    def __init__(self) -> None:
        enabled = os.getenv("GOOGLE_NEWS_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
            "disabled",
        }
        super().__init__(["googlenews-free"] if enabled else [], "GoogleNews")

    def _do_search(
        self,
        query: str,
        api_key: str,
        max_results: int,
        days: int = 7,
        **_kwargs: Any,
    ) -> SearchResponse:
        """执行 Google News RSS 新闻搜索（服务端限近窗 + 客户端时效过滤）。"""
        keyword = extract_news_keyword(query)
        if not keyword:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message="无法从查询中提取新闻搜索关键词",
            )

        rss_url = (
            f"{_GOOGLE_NEWS_RSS_URL}?q={quote_plus(keyword)}+{_google_when_operator(days)}"
            f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        )
        headers = {"User-Agent": _GOOGLE_USER_AGENT}

        try:
            response = requests.get(
                rss_url, headers=headers, timeout=_GOOGLE_REQUEST_TIMEOUT
            )
            response.raise_for_status()
            results = parse_google_news_rss(
                response.text, query, max_results, days
            )
            logger.info(
                f"[GoogleNews] 搜索完成，keyword='{keyword}', 返回 {len(results)} 条结果"
            )
            return SearchResponse(
                query=query,
                results=results,
                provider=self.name,
                success=True,
            )
        except Exception as e:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message=str(e),
            )
