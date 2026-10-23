"""Read explicit public VMall product pages; never search, log in or guess IDs.

Two official page shapes are supported:

* the legacy server-rendered page ``https://www.vmall.com/product/<prdId>.html``
  (JSON-LD or a single ``<main>`` product block), and
* the current single-page product page that redirects to
  ``https://item.vmall.com/product/comdetail/index.html?prdId=<prdId>`` and
  embeds the official parameter table as ``"parameterData"`` JSON.

Only the main product's own name and parameter table are used. Price, stock,
promotion, recommendation, review and navigation modules are ignored on purpose.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests


class VmallError(Exception):
    """Display-safe public-page retrieval error."""


_MAX_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 3
_WARM_UP_URL = "https://www.vmall.com/"
_UA = "ZhiHeng-PublicSource/1.0"
_HTML_ACCEPT = "text/html,application/xhtml+xml"
_URL = re.compile(r"https?://[^\s<>\"'`\[\](){}（）【】，。；！？、]+", re.I)
_PRODUCT_PATH = re.compile(r"/product/(\d{1,30})\.html")
_DETAIL_PATH = r"/product/comdetail/index.html"
_ALLOWED_DETAIL_PARAMS = {"prdId", "sbomCode", "lang", "country", "portal", "version"}
_HOSTS = {"www.vmall.com", "vmall.com", "m.vmall.com", "item.vmall.com"}
_SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "button", "noscript", "template"}
_RECOMMEND = re.compile(r"recommend|related|sidebar|猜你喜欢|推荐|热销|导航", re.I)
_YEAR = re.compile(r"20\d{2}")
_CAPACITY = re.compile(r"\d{1,4}(?:gb|tb)")
_TOKEN = re.compile(r"[a-z0-9+]+|[\u4e00-\u9fff]+")


def _validate_url(url: str) -> tuple[str, str]:
    """Return ``(canonical_url, prd_id)`` for an explicit official product page."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        raise VmallError("商城链接格式无效，请提供华为商城具体商品页链接。") from None
    if parsed.scheme != "https" or parsed.hostname not in _HOSTS:
        raise VmallError("商城链接须为华为商城官方 HTTPS 域名，不接受相似域名或非官方页面。")
    if parsed.username is not None or parsed.password is not None or parsed.port is not None or parsed.fragment:
        raise VmallError("商城链接不能包含账号、端口或锚点，请使用浏览器地址栏中的商品页链接。")
    if "\\" in str(url):
        raise VmallError("商城链接格式无效，请提供华为商城具体商品页链接。")
    path, prd_id = parsed.path, ""
    match = _PRODUCT_PATH.fullmatch(path)
    if match:
        if parsed.query:
            raise VmallError("商品链接请去掉跟踪参数后再试；签名或推广链接不作为商品依据。")
        prd_id = match.group(1)
    elif path == _DETAIL_PATH:
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if not params or set(params) - _ALLOWED_DETAIL_PARAMS:
            raise VmallError("商品链接请去掉跟踪参数后再试；签名或推广链接不作为商品依据。")
        prd_id = str(params.get("prdId", "")).strip()
        if not re.fullmatch(r"\d{1,30}", prd_id):
            raise VmallError("请提供华为商城具体商品页链接（含 prdId 的商品详情页），首页不能确认型号。")
    else:
        raise VmallError("请提供华为商城具体商品页链接（https://www.vmall.com/product/数字.html），首页不能确认型号。")
    return "https://www.vmall.com/product/" + prd_id + ".html", prd_id


def _redirect_target(current: str, location: str, prd_id: str) -> str:
    target = urljoin(current, str(location or "").strip())
    canonical, target_prd = _validate_url(target)
    if target_prd != prd_id:
        raise VmallError("商城跳转到了其他商品，已停止读取；请确认商品链接后重试。")
    return target


class _Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def text(self):
        ancestor = self
        while ancestor is not None:
            if ancestor.tag in _SKIP or _RECOMMEND.search(ancestor.attrs.get("id", "") + " " + ancestor.attrs.get("class", "")):
                return ""
            if "hidden" in ancestor.attrs or ancestor.attrs.get("aria-hidden") == "true" or re.search(r"display\s*:\s*none", ancestor.attrs.get("style", ""), re.I):
                return ""
            ancestor = ancestor.parent
        return " ".join(child.text() if isinstance(child, _Node) else child for child in self.children).strip()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.current = self.root
        self.jsonld = []
        self.script = None
        self.title = []

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {"meta", "link", "img", "br", "hr", "input", "source", "area", "wbr"}:
            self.current = node
        if tag == "script" and node.attrs.get("type", "").lower() == "application/ld+json":
            self.script = node
        if tag == "title":
            self._in_title = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.jsonld.append("".join(x for x in self.script.children if isinstance(x, str)))
            self.script = None
        if tag == "title":
            self._in_title = False
        node = self.current
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        if getattr(self, "_in_title", False):
            self.title.append(str(data))
        self.current.children.append(data)


def _plain(value) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    parser = _Page()
    parser.feed(str(value))
    return re.sub(r"\s+", " ", parser.root.text()).strip()


def _normal(value: str) -> str:
    return re.sub(r"[\s()（）_·-]", "", unicodedata.normalize("NFKC", str(value or "")).casefold()).replace("huawei", "").replace("华为", "")


def _tokens(value: str) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("huawei", " ").replace("华为", " ")
    return _TOKEN.findall(text)


def _identity(product: str, title: str, extra: str = "") -> bool:
    """Conservative identity check for any official Huawei product family.

    The requested model must equal the main product name (token by token), with
    only trailing storage capacities tolerated. This keeps phones, tablets,
    wearables and PCs on one rule: never accept a neighbouring SKU.
    """
    requested = _tokens(product)
    if not requested:
        return False
    # Require at least one Latin/digit token: a generic Chinese noun is not a model.
    if not any(re.search(r"[a-z0-9]", token) for token in requested):
        return False
    names = [_tokens(title)]
    if str(extra or "").strip():
        names.append(_tokens(str(title) + " " + str(extra)))
    for actual in names:
        if not actual:
            continue
        if requested == actual:
            return True
        if actual[:len(requested)] == requested:
            trailing = actual[len(requested):]
            if trailing and all(_CAPACITY.fullmatch(token) for token in trailing):
                return True
    return False


def _products(value):
    """Walk only top-level/graph/mainEntity products, never recommendation lists."""
    values = value if isinstance(value, list) else [value]
    for obj in values:
        if not isinstance(obj, dict):
            continue
        types = obj.get("@type", [])
        if "Product" in ([types] if isinstance(types, str) else types):
            yield obj
        for key in ("@graph", "mainEntity"):
            if key in obj:
                yield from _products(obj[key])


def _jsonld_details(parser: _Page, product: str, url: str) -> tuple[str, str, str] | None:
    structured = []
    for raw in parser.jsonld:
        try:
            structured.extend(_products(json.loads(raw)))
        except (ValueError, TypeError, RecursionError):
            continue
    if not structured:
        return None
    linked = []
    for item in structured:
        try:
            if item.get("url") and _validate_url(urljoin(url, str(item["url"])))[0] == url:
                linked.append(item)
        except VmallError:
            continue
    candidates = linked if linked else structured if len(structured) == 1 and not structured[0].get("url") else []
    if len(candidates) != 1:
        raise VmallError("商城页面包含多个商品，无法确认当前商品身份；未使用推荐商品资料。")
    item = candidates[0]
    title, model = _plain(item.get("name", "")), _plain(item.get("model", ""))
    main_titles = [node.text() for scope in parser.root.walk() if scope.tag == "main"
                   for node in scope.walk() if node.tag == "h1" and node.text()]
    if any(not _identity(product, heading, model) for heading in main_titles):
        raise VmallError("商城主标题与结构化商品身份冲突，未使用推荐商品资料。")
    if not _identity(product, title, model):
        raise VmallError("商城当前商品的型号、年份、系统或款式与所选型号不完全一致，已停止补充。")
    details = [_plain(item.get("description", ""))]
    properties = item.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties if isinstance(properties, list) else []:
        if isinstance(prop, dict):
            details.append(_plain(prop.get("name", "")) + "：" + _plain(prop.get("value", "")))
    details = list(dict.fromkeys(text for text in details if text))
    if len("".join(details)) < 20:
        raise VmallError("商城商品详情不可读取或内容不足，未用标题、搜索摘要或模型常识补造事实。")
    return title[:300], title + "\n" + "\n".join(details), model


def _embedded_details(parser: _Page, product: str) -> tuple[str, str, str] | None:
    """Parse the current VMall product page's embedded ``parameterData`` table."""
    anchor = parser.raw_text.find('"parameterData"')
    if anchor < 0:
        return None
    brace = parser.raw_text.find("{", anchor + len('"parameterData"'))
    if brace < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(parser.raw_text[brace:])
    except ValueError:
        return None
    groups = data.get("list", {}).get("data", []) if isinstance(data, dict) else []
    if not isinstance(groups, list):
        return None
    title = _plain("".join(parser.title))
    promotion = []
    lines = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = _plain(group.get("name", ""))
        attrs = group.get("data")
        rows = []
        for attr in attrs if isinstance(attrs, list) else []:
            if not isinstance(attr, dict):
                continue
            name = _plain(attr.get("attrName", ""))
            value = _plain(attr.get("attrValue", ""))
            if not name or not value:
                continue
            remark = _plain(attr.get("attrRemark", "")).lstrip("*").strip()
            if len(remark) > 180:
                remark = remark[:179] + "…"
            rows.append(name + "：" + value + ("（条件：" + remark + "）" if remark else ""))
            if name in {"传播名", "型号", "系列", "上市时间"}:
                promotion.append(value)
        if rows:
            lines.append("【" + (group_name or "参数") + "】" + "；".join(rows))
    if not title and promotion:
        title = promotion[0]
    if not title or not lines:
        return None
    if not _identity(product, title, " ".join(promotion)):
        raise VmallError(
            "商城当前商品为「" + title[:80] + "」，与填写的型号不完全一致；"
            "请把产品型号改成与商城主商品名称一致后重试（不会自动替你更换型号）。")
    return title[:300], title + "\n" + "\n".join(lines), " ".join(promotion)


def _extract(page: str, product: str, url: str) -> tuple[str, str]:
    parser = _Page()
    parser.raw_text = page
    try:
        parser.feed(page)
    except (ValueError, RecursionError):
        raise VmallError("商城页面结构无法可靠解析，请在浏览器核对具体商品。") from None
    detail = _jsonld_details(parser, product, url)
    if detail is not None:
        title, body, _extra = detail
        return title, body[:26000]
    detail = _embedded_details(parser, product)
    if detail is not None:
        title, body, _extra = detail
        return title, body[:26000]
    scopes = [node for node in parser.root.walk() if node.tag == "main" or "schema.org/Product" in node.attrs.get("itemtype", "")]
    if len(scopes) != 1:
        raise VmallError("商城页面未提供可核验的主商品结构，可能仅有 JavaScript 页面；请提供可读取的具体商品页。")
    scope = scopes[0]
    headings = [node for node in scope.walk() if node.tag == "h1" and node.text()]
    if len(headings) != 1 or not _identity(product, headings[0].text()):
        raise VmallError("商城主商品标题无法确认所选精确型号；推荐区或全页提及不能作为商品身份依据。")
    title = headings[0].text()
    details = [node.text() for node in scope.walk() if node.tag in {"p", "dl", "table"} and node.text()]
    details = list(dict.fromkeys(text for text in details if text))
    if len("".join(details)) < 20:
        raise VmallError("商城商品详情不可读取或内容不足，未用标题、搜索摘要或模型常识补造事实。")
    return title[:300], (title + "\n" + "\n".join(details))[:26000]


def _read_body(response) -> str:
    length = response.headers.get("Content-Length", "")
    if str(length).isdigit() and int(length) > _MAX_BYTES:
        raise VmallError("商城页面超过读取大小上限，已停止。")
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
        raise VmallError("商城返回的内容不是可核验的 HTML 商品页。")
    data = bytearray()
    for chunk in response.iter_content(65536):
        data.extend(chunk)
        if len(data) > _MAX_BYTES:
            raise VmallError("商城页面超过读取大小上限，已停止。")
    return bytes(data).decode("utf-8-sig", errors="replace")


def _request(session, url: str, timeout: float):
    try:
        return session.get(url, timeout=(min(6, timeout), timeout), allow_redirects=False, stream=True,
                           headers={"Accept": _HTML_ACCEPT, "User-Agent": _UA})
    except requests.RequestException:
        raise VmallError("商城商品页连接失败或超时，请在浏览器确认可访问性；本次未取得商城依据。") from None


def _warm_up(session, timeout: float) -> None:
    try:
        with session.get(_WARM_UP_URL, timeout=(min(6, timeout), timeout), allow_redirects=True, stream=True,
                         headers={"Accept": _HTML_ACCEPT, "User-Agent": _UA}):
            pass
    except requests.RequestException:
        # The product request below reports the actionable error.
        pass


def _download(session, url: str, prd_id: str, timeout: float) -> tuple[str, str]:
    current = url
    warmed = False
    for _ in range(_MAX_REDIRECTS):
        response = _request(session, current, timeout)
        with response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = _redirect_target(current, response.headers.get("Location", ""), prd_id)
                if not warmed:
                    # The current product page requires the WAF cookies handed out
                    # by the mall home page; no account data is involved.
                    _warm_up(session, timeout)
                    warmed = True
                current = target
                continue
            if response.status_code != 200:
                raise VmallError("商城商品页当前不可公开读取（HTTP 状态异常），请在浏览器核对；未绕过访问限制。")
            return _read_body(response), url
    raise VmallError("商城重定向次数超过上限，未取得依据。")


def _product_ids(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _URL.finditer(str(text or "")):
        raw = match.group().rstrip(".,;:!?）】")
        if "vmall" not in raw.casefold():
            continue
        canonical, prd_id = _validate_url(raw)
        if (canonical, prd_id) not in found:
            found.append((canonical, prd_id))
    return found


def fetch_vmall_sources(product: str, text: str, *, timeout=20, max_sources=2) -> list[dict]:
    """Fetch only explicit canonical product URLs in user requirements/revisions."""
    links = _product_ids(text)
    limit = max(0, min(int(max_sources), 2))
    if not links or not limit:
        return []
    timeout = max(1.0, min(float(timeout), 40.0))
    sources = []
    # A fresh isolated session neither reads netrc/proxy credentials nor reuses app
    # auth/cookies. Requests are fixed to validated public hosts and manual redirects.
    with requests.Session() as session:
        session.trust_env = False
        session.auth = None
        for canonical, prd_id in links[:limit]:
            session.cookies.clear()
            content, final_url = _download(session, canonical, prd_id, timeout)
            title, body = _extract(content, str(product), final_url)
            sources.append({"title": title, "text": body, "kind": "facts", "official_url": final_url,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(), "origin": "official_vmall"})
    return sources
