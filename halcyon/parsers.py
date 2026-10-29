"""Read EDI, API and email orders into one unified order record.

EDI and API are structured and parsed deterministically. Email is free text:
  * if ANTHROPIC_API_KEY is set, Claude extracts the fields as JSON;
  * otherwise a rule-based extractor is used (works offline, used in the demo).
Both return the same fields plus a confidence score.
"""
from __future__ import annotations
import json, os, re
from dataclasses import dataclass, field, asdict
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from .common import STATE_REGION

SERVICE_WORDS = [("same_day", r"same[\s-]?day"), ("next_day", r"next[\s-]?day"), ("standard", r"standard")]


@dataclass
class UnifiedOrder:
    source: str                 # EDI | API | Email
    source_file: str
    order_ref: str = ""
    client_id: str = ""
    client_name: str = ""
    received: str = ""          # YYYY-MM-DD HH:MM
    service: str = ""
    sku: str = ""
    qty: int = 0
    street: str = ""
    dock: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    region: str = ""
    confidence: float = 1.0
    extractor: str = "structured"
    missing: list = field(default_factory=list)

    def to_dict(self): return asdict(self)


def _finish(o: UnifiedOrder) -> UnifiedOrder:
    o.region = STATE_REGION.get(o.state, "")
    req = {"street": o.street, "city": o.city, "state": o.state, "postal_code": o.postal_code,
           "sku": o.sku, "qty": o.qty, "service": o.service, "client": o.client_id}
    o.missing = [k for k, v in req.items() if not v]
    if o.postal_code and not re.fullmatch(r"\d{5}", o.postal_code):
        o.missing.append("postal_code (invalid)")
    return o


def parse_edi(text: str, name="") -> UnifiedOrder:
    o = UnifiedOrder("EDI", name)
    for seg in [s.strip() for s in text.split("~") if s.strip()]:
        f = seg.split("*")
        tag = f[0]
        if tag == "ISA":
            d, t = f[9], f[10]
            o.received = f"20{d[:2]}-{d[2:4]}-{d[4:6]} {t[:2]}:{t[2:4]}"
        elif tag == "BEG": o.order_ref = f[3]
        elif tag == "TD5": o.service = f[3].lower()
        elif tag == "N1": o.client_name = f[2]; o.client_id = f[4] if len(f) > 4 else ""
        elif tag == "N3":
            if len(f) > 2: o.street, o.dock = f[1], f[2]
            else: o.dock = f[1] if f[1].lower().startswith("dock") else ""; o.street = "" if o.dock else f[1]
        elif tag == "N4": o.city, o.state, o.postal_code = (f + ["", "", ""])[1:4]
        elif tag == "PO1": o.qty = int(f[2]); o.sku = f[-1]
    return _finish(o)


def parse_api(text: str, name="") -> UnifiedOrder:
    j = json.loads(text); st = j.get("ship_to", {}); line = (j.get("lines") or [{}])[0]
    o = UnifiedOrder("API", name, order_ref=j.get("order_ref", ""), client_id=j.get("client_id", ""),
                     client_name=st.get("name", ""), received=j.get("submitted_at", "").replace("T", " ")[:16],
                     service=j.get("service_level", ""), sku=line.get("sku", ""), qty=int(line.get("qty") or 0),
                     street=st.get("line1") or "", dock=st.get("line2") or "", city=st.get("city") or "",
                     state=st.get("state") or "", postal_code=st.get("postal_code") or "")
    return _finish(o)


def extract_email_rules(sender: str, body: str, directory: dict) -> dict:
    """Rule-based extraction; returns fields + confidence."""
    g = lambda p, s=body, fl=re.I: (m.group(1).strip() if (m := re.search(p, s, fl)) else "")
    out = dict(order_ref=g(r"\b(ORD-\d{8})\b"), sku=g(r"\b(SKU-\d{5})\b", fl=0))
    q = g(r"(?:qty|quantity)\s*:?\s*(\d+)") or g(r"\bx\s*(\d+)\b") or g(r"\|\s*(\d+)\s*EA") or g(r"(\d+)\s+units")
    out["qty"] = int(q) if q else 0
    out["service"] = next((k for k, p in SERVICE_WORDS if re.search(p, body, re.I)), "")
    out["street"] = g(r"(\d{2,5}\s+[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\s(?:Avenue|Ave|Parkway|Pkwy|Drive|Dr|Way|Street|St|Boulevard|Blvd|Road|Rd|Lane|Ln|Court|Ct|Place|Pl|Highway|Hwy|Circle|Cir|Trail|Trl))\b", fl=0)
    out["dock"] = g(r"\b(Dock\s*\d+)\b")
    m = re.search(r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?),\s*([A-Z]{2})\b", body)
    out["city"], out["state"] = (m.group(1), m.group(2)) if m else ("", "")
    out["postal_code"] = g(r"(?<![-\d])(\d{5})(?!\d)", fl=0)
    addr = (re.search(r"[\w.+-]+@[\w.-]+", sender) or [""])[0].lower()
    cid = directory.get(addr) or directory.get(addr.split("@")[-1], "")
    if not cid:   # e.g. "Client ID: C190" or "Client 190" in the body
        m2 = re.search(r"\bclient(?:\s*id)?\s*[:#]?\s*C?(\d{1,3})\b", body, re.I) or re.search(r"\b(C\d{3})\b", body)
        if m2:
            n = int(re.sub(r"\D", "", m2.group(1)))
            if 1 <= n <= 280: cid = f"C{n:03d}"
    out["client_id"] = cid
    found = sum(bool(out[k]) for k in ["order_ref", "sku", "qty", "service", "street", "city", "state", "postal_code", "client_id"])
    out["confidence"] = round(found / 9, 2)
    return out


def extract_email_llm(sender: str, body: str, directory: dict) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        prompt = ("Extract the order from this email. Reply with JSON only, keys: order_ref, sku, qty (int), "
                  "service (same_day|next_day|standard), street, dock, city, state (2-letter), postal_code, "
                  "confidence (0-1, your certainty that every field is correct). Use empty string for anything "
                  "not stated; never guess an address.\n\nFrom: " + sender + "\n\n" + body)
        msg = client.messages.create(model=os.environ.get("HALCYON_MODEL", "claude-sonnet-5-5"), max_tokens=500,
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        j = json.loads(re.sub(r"```(json)?", "", text).strip())
        j["qty"] = int(j.get("qty") or 0)
        addr = (re.search(r"[\w.+-]+@[\w.-]+", sender) or [""])[0].lower()
        j["client_id"] = directory.get(addr) or directory.get(addr.split("@")[-1], "")
        return j
    except Exception as e:  # fall back to rules rather than drop the order
        print("LLM extraction failed, using rules:", e)
        return None


def parse_email(raw: bytes, name: str, directory: dict, names: dict) -> UnifiedOrder:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    sender = str(msg["From"] or "")
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":  # Outlook often sends HTML only
        body = re.sub(r"<(br|/p|/div|/tr)[^>]*>", "\n", body, flags=re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"&nbsp;", " ", body); body = re.sub(r"[ \t]+", " ", body)
    fields = extract_email_llm(sender, body, directory)
    extractor = "claude" if fields else "rules"
    if not fields:
        fields = extract_email_rules(sender, body, directory)
    try:
        dt = parsedate_to_datetime(msg["Date"])
        received = (dt.astimezone() if dt.tzinfo else dt).strftime("%Y-%m-%d %H:%M")   # server's local time, like other channels
    except Exception: received = ""
    o = UnifiedOrder("Email", name, received=received, extractor=extractor,
                     client_name=names.get(fields.get("client_id", ""), ""),
                     **{k: fields.get(k, "") or (0 if k == "qty" else "") for k in
                        ["order_ref", "client_id", "service", "sku", "qty", "street", "dock", "city", "state", "postal_code"]})
    o.confidence = float(fields.get("confidence", 0))
    return _finish(o)
