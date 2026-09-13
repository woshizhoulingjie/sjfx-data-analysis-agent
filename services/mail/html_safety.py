"""Non-executing mail HTML preview and plain-text projection."""

from __future__ import annotations

from html import escape, unescape
from html.parser import HTMLParser


class _SafeMailHTML(HTMLParser):
    _drop = {"script", "style", "iframe", "object", "embed", "form", "base", "meta", "link"}
    _allowed = {"a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4", "hr", "i", "li", "ol", "p", "pre", "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.text_parts = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self._drop:
            self._drop_depth += 1
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


def sanitize_html(value: str) -> str:
    parser = _SafeMailHTML()
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
            super().__init__(convert_charrefs=True);self.parts=[];self.dropped=0
        def handle_starttag(self,tag,attrs):
            if tag in {"script","style","iframe","object","head"}:self.dropped+=1
            if self.dropped:return
            if tag in {"p","div","tr","li","br","h1","h2","h3"}:self.parts.append("\n")
            if tag in {"td","th"}:self.parts.append("\t")
        def handle_endtag(self,tag):
            if tag in {"script","style","iframe","object","head"}:self.dropped=max(0,self.dropped-1)
            if not self.dropped and tag in {"p","div","tr","li"}:self.parts.append("\n")
        def handle_data(self,data):
            if not self.dropped:self.parts.append(data)
    import re
    reader=Reader();reader.feed(str(value or ""));reader.close()
    text="".join(reader.parts).replace("\r\n","\n")
    return re.sub(r"\n{3,}","\n\n",text).strip()
