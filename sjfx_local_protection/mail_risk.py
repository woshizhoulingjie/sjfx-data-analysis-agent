"""Offline email identity/inducement rules; imported headers are never trusted."""
from __future__ import annotations
from email.utils import getaddresses
from html.parser import HTMLParser
import json
import re
import unicodedata
from urllib.parse import urlsplit

REVISION = "sjfx-mail-combination-20261014-v1"
# This small local brand list is rule scope, not DNS/URL reputation intelligence.
BRANDS = {
    "microsoft": ("microsoft.com", "office.com", "outlook.com", "live.com"),
    "微软": ("microsoft.com", "office.com", "outlook.com", "live.com"),
    "paypal": ("paypal.com",), "apple": ("apple.com", "icloud.com"),
    "苹果": ("apple.com", "icloud.com"), "google": ("google.com", "gmail.com"),
    "谷歌": ("google.com", "gmail.com"), "支付宝": ("alipay.com",),
}
_CONFUSABLE = str.maketrans("аесорхуіјѕӏορν", "aecopxyijslopv")
_CREDENTIAL = re.compile(r"(?:log\s*in|sign\s*in|verify|confirm|update|reset|provide|send|enter|submit).{0,90}(?:account|password|credential|otp|verification\s*code)|(?:password|otp|credential).{0,70}(?:send|provide|enter|submit)|(?:登录|登陆|验证|核实|更新|重置|输入|提交|提供|发送|回复).{0,45}(?:账户|帐号|账号|密码|口令|验证码)|(?:密码|验证码|口令).{0,30}(?:回复|发送|提供)", re.I)
_PAYMENT = re.compile(r"(?:wire|transfer|pay|remit).{0,60}(?:money|funds|payment|account|invoice)|(?:转账|汇款|付款|打款|支付).{0,40}(?:账户|账号|发票|款项|资金|账单)|(?:更换|变更).{0,15}(?:收款|银行).{0,15}(?:账户|账号)", re.I)
_URGENCY = re.compile(r"urgent|immediate|within\s+\d+\s*(?:hour|minute)|suspend|locked|expire|final\s*(?:warning|notice)|do\s+not\s+(?:call|tell)|紧急|立即|马上|今日内|小时内|冻结|停用|封禁|到期|最后通知|保密|不要.{0,8}(?:电话|告知)", re.I)


def domain(value):
    value = value.rstrip(".").casefold()
    try:
        encoded = value.encode("idna").decode("ascii")
        if len(encoded) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', encoded):
            return ''
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in encoded.split('.')):
            return ''
        return encoded
    except UnicodeError:
        return ""


def belongs(host, allowed):
    return any(host == item or host.endswith("." + item) for item in allowed)


def skeleton(host):
    try:
        host = host.encode("ascii").decode("idna")
    except (UnicodeError, UnicodeEncodeError):
        pass
    return unicodedata.normalize("NFKC", host).casefold().translate(_CONFUSABLE).translate(str.maketrans({'0':'o','1':'l','3':'e','5':'s'}))


class _Body(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.hosts = [], set()

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key.lower() in {"href", "action", "formaction"} and value:
                try:
                    parsed = urlsplit(value)
                    if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                        self.hosts.add(domain(parsed.hostname))
                except ValueError:
                    pass


def inspect_message(message, add, policy):
    def headers(key):
        values = message.get_all(key, [])
        if len(values) > 32:
            add('mail_header_limit', key, 'incomplete')
        result = []
        for value in values[:32]:
            text = str(value)
            if len(text) > 8192:
                add('mail_header_limit', key, 'incomplete')
            result.append(text[:8192])
        return result
    from_headers, reply_headers = headers('From'), headers('Reply-To')
    subject_headers, authentication_headers = headers('Subject'), headers('Authentication-Results')
    metadata = {"revision": REVISION, "authentication": {"status": "unverified",
        "reason": "Imported Authentication-Results lacks a trusted receiving chain",
        "reported_headers": [str(v)[:1000] for v in authentication_headers[:8]]},
        "reputation": {"status": "not_checked", "source": "offline_rules_only", "updated": "2026-10-14",
            "scope": "Explicit brand domains and combination rules; no online URL reputation"}}
    senders = getaddresses(from_headers)
    replies = getaddresses(reply_headers)
    if len(senders) > 32 or len(replies) > 32:
        add('mail_address_limit', 'Sender/reply address budget exhausted', 'incomplete')
        senders, replies = senders[:32], replies[:32]
    def valid(address):
        return bool(re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", address) and domain(address.rsplit("@", 1)[1]))
    if from_headers and (len(from_headers) != 1 or len(senders) != 1 or not valid(senders[0][1])):
        add("mail_sender_invalid", ' '.join(from_headers), "incomplete")
    if reply_headers and (not replies or len(reply_headers) != 1 or any(not valid(address) for _, address in replies)):
        add("mail_reply_to_invalid", ' '.join(reply_headers), "incomplete")
    display, address = senders[0] if senders else ("", "")
    sender_domain = domain(address.rsplit("@", 1)[-1]) if valid(address) else ""
    metadata.update(sender={"display_name": display, "address": address, "domain": sender_domain},
        reply_to=[{"display_name": label, "address": value} for label, value in replies])
    identity_signals = []
    if sender_domain and any(domain(value.rsplit("@", 1)[-1]) != sender_domain for _, value in replies if valid(value)):
        add("mail_reply_to_domain_mismatch", json.dumps({"from": address, "reply_to": replies}, ensure_ascii=False))
        identity_signals.append("reply_to_domain_mismatch")
    claimed = getaddresses([display]) if "@" in display else []
    if claimed and any(valid(value) and domain(value.rsplit("@", 1)[1]) != sender_domain for _, value in claimed):
        add("mail_display_address_mismatch", display + " <" + address + ">")
        identity_signals.append("display_address_mismatch")
    brands = policy.get("mail_brand_domains", BRANDS)
    if not isinstance(brands, dict) or len(brands) > 1000:
        add("mail_rules_configuration_invalid", "mail_brand_domains", "incomplete")
        return metadata
    all_domains = []
    for label, values in brands.items():
        if not isinstance(label, str) or len(label)>200 or not isinstance(values, (list, tuple)) or len(values)>32 or any(not isinstance(v, str) or not domain(v) for v in values):
            add("mail_rules_configuration_invalid", "mail_brand_domains", "incomplete")
            return metadata
        allowed = [domain(v) for v in values]
        all_domains.extend(allowed)
        match = label.casefold() in display.casefold() if any(ord(c) > 127 for c in label) else re.search(r"(?<![\w])" + re.escape(label) + r"(?![\w])", display, re.I)
        if sender_domain and match and not belongs(sender_domain, allowed):
            add("mail_brand_sender_mismatch", json.dumps({"brand": label, "sender_domain": sender_domain, "expected_domains": allowed}, ensure_ascii=False))
            identity_signals.append("brand_sender_mismatch")
    for expected in set(all_domains):
        if sender_domain and not belongs(sender_domain, [expected]) and (
                skeleton(sender_domain) == expected or sender_domain.startswith(expected + ".") or
                expected in sender_domain.replace("-", "").replace("_", "")):
            add("mail_lookalike_sender_domain", sender_domain + " resembles " + expected)
            identity_signals.append("lookalike_sender_domain")
    texts, hosts = subject_headers, set()
    stack, members, remaining = [(message, 0)], 0, policy["max_html_bytes"]
    while stack:
        part, depth = stack.pop()
        members += 1
        if members > policy["max_members"] or depth > policy["max_depth"]:
            add("mail_combination_budget", "MIME traversal incomplete", "incomplete")
            break
        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                stack.extend((child, depth + 1) for child in reversed(children))
            continue
        if part.get_filename() or part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True) or b""
        if len(payload) > remaining:
            add("mail_combination_budget", "Body text coverage incomplete", "incomplete")
            payload = payload[:remaining]
        remaining -= len(payload)
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", "strict")
        except (UnicodeError, LookupError):
            add("mail_combination_decode_error", part.get_content_charset() or "utf-8", "incomplete")
            text = payload.decode("utf-8", "replace")
        if part.get_content_type() == "text/html":
            parser = _Body()
            parser.feed(text); parser.close()
            text = " ".join(parser.text)
            hosts.update(parser.hosts)
        for url in re.findall(r"https?://[^\s<>\"']+", text, re.I):
            try:
                if urlsplit(url).hostname: hosts.add(domain(urlsplit(url).hostname))
            except ValueError:
                pass
        texts.append(text)
    body = " ".join(texts)
    if len(hosts) > policy['max_members'] * 3:
        add('mail_link_domain_limit', 'Link identity coverage incomplete', 'incomplete')
        hosts = set(sorted(hosts)[:policy['max_members'] * 3])
    for host in hosts:
        for expected in set(all_domains):
            if host and not belongs(host, [expected]) and (skeleton(host) == expected or host.startswith(expected + '.') or expected in host.replace('-', '').replace('_', '')):
                add('mail_lookalike_link_domain', host + ' resembles ' + expected)
                identity_signals.append('lookalike_link_domain')
    credential = bool(_CREDENTIAL.search(body))
    payment = bool(_PAYMENT.search(body))
    urgency = bool(_URGENCY.search(body))
    if credential: add("mail_credential_inducement", "Mail requests account login, password, or verification code")
    if payment: add("mail_payment_inducement", "Mail requests payment or changed recipient account")
    if urgency: metadata["urgency_signal"] = True
    if identity_signals and (credential or payment) and (urgency or hosts or "lookalike_sender_domain" in identity_signals):
        add("mail_phishing_combination", json.dumps({"identity_signals": sorted(set(identity_signals)),
            "credential_request": credential, "payment_request": payment, "urgency": urgency,
            "linked_domains": sorted(hosts)[:40]}, ensure_ascii=False), "dangerous")
    metadata["identity_signals"] = sorted(set(identity_signals))
    return metadata
