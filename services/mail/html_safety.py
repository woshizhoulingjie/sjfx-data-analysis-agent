"""Non-executing mail HTML preview and plain-text projection."""

from __future__ import annotations

from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import quote, unquote


RASTER_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}


def cid_image_urls(message_key, package_id, parts):
    """Resolve only unambiguous local MIME Content-IDs; never fetch remote URLs."""
    found = {}
    for part in parts:
        get = part.get if isinstance(part, dict) else lambda key, default=None: getattr(part, key, default)
        identifier = str(get('content_id') or '').strip().strip('<>')
        if not identifier:
            continue
        url = None
        if get('content_type') in RASTER_TYPES and get('is_inline_resource'):
            url = '/api/mail/messages/{}/parts/{}/preview-image?package_id={}'.format(
                quote(str(message_key), safe=''), quote(str(get('mime_path')), safe=''), quote(str(package_id), safe=''))
        found.setdefault(identifier, []).append(url)
    return {identifier: urls[0] for identifier, urls in found.items() if len(urls) == 1 and urls[0]}


class _SafeMailHTML(HTMLParser):
    _void_drop = {"embed", "base", "meta", "link"}
    _drop = {"script", "style", "iframe", "object", "form", "head"}
    _allowed = {"a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4", "hr", "i", "li", "ol", "p", "pre", "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul"}

    def __init__(self, cid_urls=None):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.text_parts = []
        self._drop_depth = 0
        self.cid_urls = cid_urls or {}

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self._void_drop:
            return
        if tag in self._drop:
            self._drop_depth += 1
            return
        if tag == 'img' and not self._drop_depth:
            attributes = dict(attrs)
            source = str(attributes.get('src') or '')
            identifier = unquote(source[4:]).strip().strip('<>') if source[:4].casefold() == 'cid:' else None
            url = self.cid_urls.get(identifier)
            alt = str(attributes.get('alt') or attributes.get('title') or '')[:500]
            if url:
                self.parts.append('<img src="{}" alt="{}" loading="lazy">'.format(escape(url, quote=True), escape(alt, quote=True)))
            elif alt:
                self.parts.append(escape('[图片：' + alt + ']'))
            if alt:
                self.text_parts.append(alt)
            return
        if self._drop_depth or tag not in self._allowed:
            return
        clean = []
        for name, value in attrs:
            name = name.casefold()
            value = str(value or "")
            if name.startswith("on") or name in {"style", "src", "srcset", "background", "action", "formaction"}:
                continue
            if tag == "a" and name == "href":
                # Preview must not turn an email into a tracking/navigation surface.
                continue
            if name in {"title", "colspan", "rowspan"}:
                clean.append((name, value[:500]))
        attributes = "".join(" {}=\"{}\"".format(name, escape(value, quote=True)) for name, value in clean)
        self.parts.append("<{}{}>".format(tag, attributes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self._drop:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if not self._drop_depth and tag in self._allowed and tag not in {"br", "hr"}:
            self.parts.append("</{}>".format(tag))
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if not self._drop_depth:
            self.parts.append(escape(data))
            self.text_parts.append(data)


def sanitize_html(value: str, cid_urls=None) -> str:
    parser = _SafeMailHTML(cid_urls=cid_urls)
    parser.feed(str(value or ""))
    parser.close()
    return "".join(parser.parts)


def html_to_text(value: str) -> str:
    parser = _SafeMailHTML()
    parser.feed(str(value or ""))
    parser.close()
    return "\n".join(" ".join("".join(parser.text_parts).split()).split(" \n ")).strip()


def html_to_translation_text(value: str) -> str:
    """Readable mail projection without changing existing indexing normalization."""
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True);self.parts=[];self.dropped=0;self.quote_depth=0
        def append(self, value):
            self.parts.append((value,self.quote_depth))
        def handle_starttag(self,tag,attrs):
            if tag in {"script","style","iframe","object","head"}:self.dropped+=1
            if self.dropped:return
            if tag=='blockquote':
                self.append('\n');self.quote_depth+=1
            if tag in {"p","div","tr","li","br","h1","h2","h3"}:self.append("\n")
            if tag in {"td","th"}:self.append("\t")
        def handle_endtag(self,tag):
            if tag in {"script","style","iframe","object","head"}:self.dropped=max(0,self.dropped-1)
            if not self.dropped and tag in {"p","div","tr","li"}:self.append("\n")
            if not self.dropped and tag=='blockquote':
                self.append('\n');self.quote_depth=max(0,self.quote_depth-1)
        def handle_data(self,data):
            if not self.dropped:self.append(data)
    import re
    reader=Reader();reader.feed(str(value or ""));reader.close()
    result=[];line_start=True
    for value,depth in reader.parts:
        for char in value.replace('\r\n','\n'):
            if line_start and char not in {'\n','\r'} and depth:
                result.append('> '*depth)
            result.append(char);line_start=char=='\n'
    text="".join(result)
    return re.sub(r"\n{3,}","\n\n",text).strip()
