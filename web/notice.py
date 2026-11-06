"""Small Markdown notices: no raw HTML, images, or executable link schemes."""
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

TITLE_LIMIT = 100
BODY_LIMIT = 10000
_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True}).disable("image")
_validate_link = _markdown.validateLink


def _safe_link(url):
    try:
        return _validate_link(url) and urlsplit(url).scheme.lower() in ("", "http", "https", "mailto")
    except ValueError:
        return False


_markdown.validateLink = _safe_link


def _link_open(tokens, index, options, env):
    tokens[index].attrSet("target", "_blank")
    tokens[index].attrSet("rel", "noopener noreferrer")
    return _markdown.renderer.renderToken(tokens, index, options, env)


_markdown.renderer.rules["link_open"] = _link_open


def notice_fields(body):
    title, text, enabled = body.get("title", "站点公告"), body.get("body", ""), body.get("enabled", False)
    if not isinstance(title, str) or not isinstance(text, str) or not isinstance(enabled, bool):
        raise ValueError("公告标题、正文和启用状态格式不正确")
    if len(title) > TITLE_LIMIT or len(text) > BODY_LIMIT:
        raise ValueError("公告标题最多 100 字，正文最多 10000 字")
    if enabled and not text.strip():
        raise ValueError("请先填写公告正文再启用")
    return {"title": title.strip() or "站点公告", "body": text, "enabled": enabled}


def render_notice(text):
    return _markdown.render(text)
