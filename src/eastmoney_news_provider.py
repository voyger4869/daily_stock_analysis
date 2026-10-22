"""东方财富个股新闻直连搜索 Provider（本地补丁，免 Key）。

上游搜索链路依赖 Tavily/SerpAPI 等需要 API Key 的搜索服务；本模块基于
东方财富公开的站内搜索接口（search-api-web.eastmoney.com，与 akshare
stock_news_em 同源）实现免 Key 的 A 股个股新闻检索，作为 provider 链的
兜底数据源。注册位置见 ``SearchService.__init__`` 第 8 步。

可通过环境变量 ``EASTMONEY_NEWS_ENABLED``（默认开启）整体停用本兜底源。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from src.search_service import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_EM_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_EM_REQUEST_TIMEOUT = 10
_EM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 构造东财关键词时剔除的通用修饰词（查询串形如 "招商轮船 601872 股票 最新消息"）
_KEYWORD_NOISE_TOKENS = {
    "股票", "股份", "最新消息", "新闻", "资讯", "舆情", "公告", "催化",
    "机构分析", "机构", "研报", "A股", "最新", "消息", "相关", "动态",
    "热点", "事件", "分析", "重估", "预期", "基本面",
}

_EM_TAG_RE = re.compile(r"</?em>")

# 6 位 A 股代码（前后不能紧邻数字，避免误命中长数字串）
_A_SHARE_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# 引号主题词（覆盖 topic 查询如 '"光伏设备" A股 最新消息 催化'）
_QUOTED_RE = re.compile(r"[\"'「『](.+?)[\"'」』]")


def extract_eastmoney_keyword(query: str) -> str:
    """从搜索查询中提取适合东财站内搜索的关键词。

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


def parse_eastmoney_search_payload(
    text: str,
    query: str,
    max_results: int,
    days: int,
    now: Optional[datetime] = None,
) -> List[SearchResult]:
    """解析东财 JSONP 响应文本为 SearchResult 列表（含时效过滤）。

    日期无法解析时保留该条目，避免因个别脏数据丢失有效新闻。
    """
    if not text:
        return []
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("响应不是合法的 JSONP 格式")
    data = json.loads(text[start + 1 : end])
    articles = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
    if not isinstance(articles, list):
        return []

    current_time = now or datetime.now()
    cutoff = current_time - timedelta(days=days)
    results: List[SearchResult] = []
    for item in articles:
        if not isinstance(item, dict):
            continue
        published_date = str(item.get("date") or "").strip()
        keep = True
        if published_date:
            try:
                keep = datetime.strptime(published_date, "%Y-%m-%d %H:%M:%S") >= cutoff
            except ValueError:
                keep = True
        if not keep:
            continue
        results.append(
            SearchResult(
                title=_EM_TAG_RE.sub("", str(item.get("title") or "")),
                snippet=_EM_TAG_RE.sub("", str(item.get("content") or ""))[:500],
                url=str(item.get("url") or ""),
                source=str(item.get("mediaName") or "东方财富"),
                published_date=published_date or None,
            )
        )
        if len(results) >= max_results:
            break
    return results


class EastMoneyNewsProvider(BaseSearchProvider):
    """东方财富个股新闻直连搜索（免 Key，A 股新闻兜底源）。

    基类要求非空 api_keys 才视为可用；东财接口本身无需鉴权，这里传入
    占位 key 以复用基类的搜索流程与错误统计。
    """

    def __init__(self) -> None:
        enabled = os.getenv("EASTMONEY_NEWS_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
            "disabled",
        }
        super().__init__(["eastmoney-free"] if enabled else [], "EastMoney")

    def _do_search(
        self,
        query: str,
        api_key: str,
        max_results: int,
        days: int = 7,
        **_kwargs: Any,
    ) -> SearchResponse:
        """执行东财站内新闻搜索（按时间倒序，客户端做时效过滤）。"""
        keyword = extract_eastmoney_keyword(query)
        if not keyword:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message="无法从查询中提取东财搜索关键词",
            )

        payload: Dict[str, Any] = {
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": max(20, max_results * 4),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        params = {
            "cb": "jsonp0",
            "param": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }
        headers = {"User-Agent": _EM_USER_AGENT, "Referer": "https://so.eastmoney.com/"}

        try:
            response = requests.get(
                _EM_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=_EM_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            results = parse_eastmoney_search_payload(
                response.text, query, max_results, days
            )
            logger.info(
                f"[EastMoney] 搜索完成，keyword='{keyword}', 返回 {len(results)} 条结果"
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
