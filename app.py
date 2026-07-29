"""
Betterway Logistic — courier & delivery marketing site with a live quote assistant
====================================================================================
South East / South West London courier & man-with-a-van business. Visitors either
fill in the on-page quote form or talk to the chat assistant; either way, the full
details land in an email (and the customer can also just WhatsApp the number).

Environment variables to set in Render:
  GROQ_API_KEY     - your Groq key (the chat assistant's brain)
  RESEND_API_KEY   - your Resend key (sends the quote-request emails)
  NOTIFY_TO        - where leads go (defaults to betterwaylogistic4you@gmail.com)
  RESEND_FROM      - the "from" address (defaults to the frontdesk.org.uk sender)
  SECRET_KEY       - Flask session secret (any random string)
"""

from flask import Flask, request, jsonify, render_template_string, session, Response
import os
import re
import uuid
import html
import base64
import time
import requests
from groq import Groq

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-later")
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB

_groq_client = None
all_conversations = {}
session_images = {}
notified_sessions = set()
chat_activity = {}

PHONE_DISPLAY = "07982 739925"
PHONE_TEL = "+447982739925"
WHATSAPP_NUMBER = "447982739925"
EMAIL_ADDRESS = "betterwaylogistic4you@gmail.com"


def client_chat(**kwargs):
    """Create the Groq client only when chat is actually used."""
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    return _groq_client.chat.completions.create(**kwargs)


# --- Email notification settings ---
# Render's free tier blocks direct SMTP, so leads are sent over Resend's HTTPS API.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
NOTIFY_TO = os.environ.get("NOTIFY_TO", EMAIL_ADDRESS)
RESEND_FROM = os.environ.get("RESEND_FROM", "Betterway Logistic Website <leads@frontdesk.org.uk>")

# --- Photo upload settings (chat assistant only) -----------------------------
MAX_IMAGES_PER_SESSION = 6
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024


def _decode_image_data_url(data_url):
    """Validate and decode a browser data URL for a customer photo."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header:
        return None

    content_type = header[len("data:"):].split(";", 1)[0].lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return None

    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return None

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    return {
        "filename": f"job-photo-{uuid.uuid4().hex[:8]}.{ext}",
        "content_type": content_type,
        "b64": base64.b64encode(raw).decode("ascii"),
    }


# --- Contact-info extraction -------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+44|0)\d[\d\s\-\.]{8,11}(?!\d)")


def _customer_text(conversation):
    return " ".join(m["content"] for m in conversation if m.get("role") == "user")


def find_email(conversation):
    match = EMAIL_RE.search(_customer_text(conversation))
    return match.group(0) if match else None


def find_phone(conversation):
    text = _customer_text(conversation)
    for candidate in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        if digits.startswith("00"):
            continue
        if digits.startswith("44"):
            digits = "0" + digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            return f"{digits[:5]} {digits[5:]}"
    return None


def has_contact_info(conversation):
    return bool(find_email(conversation) or find_phone(conversation))


CLOSING_RE = re.compile(
    r"\b(no longer interested|not interested|no thanks|no thank you|"
    r"that'?s all|that'?s it|that'?s everything|nothing else|all good|"
    r"that'?s great thank|thanks that'?s|goodbye|bye for now|no more|"
    r"i'?m good|im good)\b",
    re.I,
)


def _looks_like_closing(text):
    return bool(CLOSING_RE.search(text or ""))


def _transcript(conversation):
    lines = []
    for msg in conversation:
        if msg["role"] == "user":
            lines.append(f"Customer: {msg['content']}")
        elif msg["role"] == "assistant":
            lines.append(f"Assistant: {msg['content']}")
    return "\n\n".join(lines)


LEAD_SUMMARY_PROMPT = """You are turning a website chat into a clean lead for a
London courier and man-with-a-van delivery company owner. Read the conversation
and output EXACTLY these labelled lines and nothing else. Fill each in from what
the customer actually said; write "Not specified" if they didn't say. Keep each
line short.

Name:
Job / what needs moving:
Pickup area or postcode:
Delivery area or postcode:
Size / weight / quantity:
Preferred timing:
Urgency (1-5 where 1=no rush, 5=urgent - infer from what they said):
Other notes:"""


def summarise_lead(conversation):
    try:
        resp = client_chat(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": LEAD_SUMMARY_PROMPT},
                {"role": "user", "content": _transcript(conversation)},
            ],
            max_tokens=250,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"Lead summary failed: {e}")
        return None


def _post_resend(subject, text, html_body=None, attachments=None):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY not set, skipping email")
        return

    payload = {"from": RESEND_FROM, "to": [NOTIFY_TO], "subject": subject, "text": text}
    if html_body:
        payload["html"] = html_body
    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"], "content": a["b64"]} for a in attachments
        ]

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json=payload,
            timeout=15,
        )
        if response.status_code >= 300:
            print(f"Resend error: {response.status_code} {response.text}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def _parse_summary(structured):
    out = {}
    if not structured:
        return out
    for line in structured.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            out[key.strip().lower()] = val.strip()
    return out


def _lead_fields(conversation):
    s = _parse_summary(summarise_lead(conversation))

    def pick(*keys):
        for k in keys:
            v = s.get(k)
            if v and v.lower() not in ("not specified", "not provided", "n/a", "none", "-"):
                return v
        return None

    return {
        "Name": pick("name"),
        "Phone": find_phone(conversation),
        "Email": find_email(conversation),
        "Job": pick("job / what needs moving", "job", "what needs moving"),
        "Pickup": pick("pickup area or postcode", "pickup"),
        "Delivery": pick("delivery area or postcode", "delivery"),
        "Size / weight": pick("size / weight / quantity", "size", "weight"),
        "Preferred timing": pick("preferred timing", "timing"),
        "Urgency": pick("urgency (1-5 where 1=no rush, 5=urgent - infer from what they said)", "urgency"),
        "Notes": pick("other notes", "notes"),
    }


def _row(label, value):
    if not value:
        return ""
    return (
        '<tr>'
        f'<td style="padding:10px 16px;border-bottom:1px solid #eee;color:#8a8a8a;'
        f'font-size:13px;white-space:nowrap;vertical-align:top;width:130px">{html.escape(label)}</td>'
        f'<td style="padding:10px 16px;border-bottom:1px solid #eee;color:#1a1a1a;'
        f'font-size:14px;font-weight:600">{html.escape(str(value))}</td>'
        '</tr>'
    )


def _transcript_html(conversation):
    rows = []
    for msg in conversation:
        if msg["role"] == "user":
            who, color, bg = "Customer", "#0a0a0a", "#f2f7f2"
        elif msg["role"] == "assistant":
            who, color, bg = "Betterway Assistant", "#0f9b46", "#ffffff"
        else:
            continue
        text = html.escape(msg["content"]).replace("\n", "<br>")
        rows.append(
            f'<div style="margin:0 0 12px">'
            f'<div style="font-size:11px;letter-spacing:.05em;text-transform:uppercase;'
            f'color:{color};font-weight:700;margin-bottom:4px">{who}</div>'
            f'<div style="background:{bg};border:1px solid #e6ece6;border-radius:10px;'
            f'padding:11px 14px;font-size:14px;color:#2a2a2a;line-height:1.5">{text}</div>'
            f'</div>'
        )
    return "".join(rows)


def _urgency_badge(urgency_str):
    if not urgency_str:
        return ""
    m = re.search(r"[1-5]", str(urgency_str))
    if not m:
        return ""
    score = int(m.group(0))
    colours = {
        1: ("#e8f5e9", "#2e7d32", "1 — No rush"),
        2: ("#f1f8e9", "#558b2f", "2 — Low"),
        3: ("#fff8e1", "#f57f17", "3 — Moderate"),
        4: ("#fff3e0", "#e65100", "4 — Fairly urgent"),
        5: ("#ffebee", "#b71c1c", "5 — URGENT — reply ASAP"),
    }
    bg, fg, label = colours.get(score, ("#f5f5f5", "#555", str(score)))
    return (
        f'<div style="margin:0 0 20px">'
        f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:#999;font-weight:700;margin-bottom:6px">Urgency</div>'
        f'<span style="display:inline-block;background:{bg};color:{fg};border:1px solid {fg};'
        f'border-radius:999px;padding:5px 14px;font-size:13px;font-weight:700">'
        f'{label}</span></div>'
    )


def _email_shell(eyebrow, title, inner):
    return (
        '<!DOCTYPE html><html><body style="margin:0;background:#eef2ee;padding:24px;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:14px;'
        'overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.07)">'
        '<div style="background:#07100a;padding:24px 28px">'
        f'<div style="color:#39e75f;font-size:12px;letter-spacing:.18em;text-transform:uppercase;'
        f'font-weight:700">{eyebrow}</div>'
        f'<div style="color:#fff;font-size:21px;font-weight:700;margin-top:5px">{title}</div></div>'
        f'<div style="padding:26px 28px">{inner}</div>'
        '<div style="background:#f7faf7;padding:16px 28px;border-top:1px solid #eee;'
        'font-size:12px;color:#aaa">Sent automatically by the Betterway Logistic website.</div>'
        '</div></body></html>'
    )


def send_lead_email(conversation, images=None):
    """Emails a tidy quote-request summary captured by the chat assistant."""
    images = images or []
    fields = _lead_fields(conversation)
    transcript = _transcript(conversation)

    text_lines = ["NEW QUOTE REQUEST - Betterway Logistic", "========================"]
    for k, v in fields.items():
        if v:
            text_lines.append(f"{k}: {v}")
    if images:
        text_lines.append(f"Photos attached: {len(images)}")
    text_lines += ["========================", "", "Full conversation:", "", transcript]
    text_body = "\n".join(text_lines)

    urgency_val = fields.pop("Urgency", None)
    rows = "".join(_row(k, v) for k, v in fields.items())
    photos_line = ""
    if images:
        photos_line = (
            '<p style="margin:0 0 20px;font-size:14px;color:#1a1a1a">'
            f'\U0001F4CE <strong>{len(images)} photo(s)</strong> attached to this email.</p>'
        )
    inner = (
        '<p style="margin:0 0 20px;font-size:14px;color:#666">'
        'Captured by the website chat assistant:</p>'
        f'{_urgency_badge(urgency_val)}'
        f'{photos_line}'
        '<table style="width:100%;border-collapse:collapse;border:1px solid #eee;'
        f'border-radius:8px;overflow:hidden;margin-bottom:28px">{rows}</table>'
        '<div style="font-size:12px;letter-spacing:.05em;text-transform:uppercase;'
        'color:#999;font-weight:700;margin-bottom:14px">Full conversation</div>'
        f'{_transcript_html(conversation)}'
    )
    html_body = _email_shell("Betterway Logistic", "New quote request from your website", inner)

    urgency_m = re.search(r"[1-5]", str(urgency_val)) if urgency_val else None
    urgency_score = int(urgency_m.group(0)) if urgency_m else 0
    urgent_prefix = "🔴 URGENT — " if urgency_score >= 5 else ("🟠 " if urgency_score >= 4 else "")

    contact = fields.get("Phone") or fields.get("Email") or "no number yet"
    bits = [b for b in (fields.get("Name"), fields.get("Pickup")) if b]
    subject = urgent_prefix + "New quote request - " + (" · ".join(bits + [contact]) if bits else contact)
    _post_resend(subject, text_body, html_body=html_body, attachments=images)


def send_photo_followup(conversation, images):
    if not images:
        return
    phone = find_phone(conversation) or "Not provided"
    email = find_email(conversation) or "Not provided"
    text_body = (
        "ADDITIONAL PHOTO(S) - Betterway Logistic\n"
        "This relates to a quote request you've already been emailed about.\n"
        f"Phone: {phone}\nEmail: {email}\nPhotos attached: {len(images)}\n"
    )
    inner = (
        f'<p style="margin:0 0 18px;font-size:14px;color:#666">This relates to a quote request '
        f'you\'ve already been emailed about. <strong>{len(images)} new photo(s)</strong> attached below.</p>'
        '<table style="width:100%;border-collapse:collapse;border:1px solid #eee;border-radius:8px;'
        f'overflow:hidden">{_row("Phone", phone)}{_row("Email", email)}</table>'
    )
    html_body = _email_shell("Betterway Logistic", "More photos for an existing request", inner)
    _post_resend(f"Photo added - quote request: {phone}", text_body, html_body=html_body, attachments=images)


def send_quote_form_email(fields):
    """Emails the details submitted through the on-page 'Get a Quote' form."""
    text_lines = ["NEW QUOTE REQUEST (website form) - Betterway Logistic", "========================"]
    for k, v in fields.items():
        if v:
            text_lines.append(f"{k}: {v}")
    text_body = "\n".join(text_lines)

    rows = "".join(_row(k, v) for k, v in fields.items())
    inner = (
        '<p style="margin:0 0 20px;font-size:14px;color:#666">Submitted directly through the '
        '"Get a Quote" form on your website:</p>'
        f'<table style="width:100%;border-collapse:collapse;border:1px solid #eee;'
        f'border-radius:8px;overflow:hidden">{rows}</table>'
    )
    html_body = _email_shell("Betterway Logistic", "New quote request from your website", inner)

    contact = fields.get("Phone") or fields.get("Email") or "no number yet"
    bits = [b for b in (fields.get("Name"), fields.get("Service")) if b]
    subject = "New quote request - " + (" · ".join(bits + [contact]) if bits else contact)
    _post_resend(subject, text_body, html_body=html_body)


SYSTEM_PROMPT = """
You are the virtual assistant for Betterway Logistic, a courier and delivery
service covering Greater London, based mainly across South East and South West
London.

About the business:
- Services: same-day courier collections, next-day parcel delivery, man-with-a-van
  for small furniture jobs and removals, and regular collections for local
  businesses.
- Core patch is South East and South West London, but jobs are taken on right
  across Greater London.
- There are no instant online prices - every quote is worked out personally by
  the owner once he has the full details, then sent back by email or WhatsApp.
- Customers can attach a photo of the item in the chat with the paperclip if
  that's easier than describing it.

YOUR TONE:
Write like a friendly, straight-talking London courier texting back. Keep
replies short, direct and helpful. Ask one thing at a time. Do not use long
paragraphs, bullet lists, or customer service phrases like "I'd be happy to
assist".

CONVERSATION FLOW - work through these one at a time:
1. Find out what needs collecting or delivering: a parcel, package, piece of
   furniture, small removal, pallet, or a regular round.
2. Get the pickup postcode or area, and the delivery postcode or area.
3. Get a sense of size, weight or quantity - offer the paperclip for a photo if
   that's easier than describing it.
4. Ask when it's needed: today (same-day), tomorrow (next-day), or a specific
   date/time.
5. Ask for their name.
6. Ask for their best phone number or email, then repeat it back to confirm.
7. Only once everything above has been asked, let them know their details have
   been sent over and Betterway Logistic will come back to them shortly with a
   price.

IMPORTANT:
Only add the hidden [[READY]] tag once you have asked about the job,
pickup/delivery areas, size, timing, name and confirmed contact details. The
customer never sees [[READY]]. Put it at the very end of the final wrap-up
message, on its own line.
"""


BASE_STYLE = """
<link rel="icon" type="image/svg+xml" href="/static/images/logo.svg">
<meta name="theme-color" content="#07100a">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Betterway Logistic">
<meta property="og:title" content="Betterway Logistic - Courier &amp; Delivery, South East &amp; South West London">
<meta property="og:description" content="Same-day and next-day courier, man-with-a-van and regular business collections across Greater London.">
<meta property="og:image" content="/static/images/logo.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#f2fbf4; --mut:#a9c2ad; --line:rgba(57,231,95,.22);
    --g1:#0f9b46; --g2:#39e75f; --bg:#050806; --panel:#0c1410;
    --paper:#f3f6f2; --paper-ink:#131a14; --btnink:#04220d;
  }
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:Manrope,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased;overflow-x:hidden}
  a{color:var(--g2)} img,video{max-width:100%;display:block}
  .wrap{max-width:1180px;margin:0 auto;width:100%}.narrow{max-width:760px}
  nav{position:sticky;top:0;z-index:50;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:18px;padding:14px 24px;background:rgba(5,8,6,.86);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
  .brand{grid-column:2;display:flex;align-items:center;gap:11px;color:#fff;text-decoration:none;font-weight:800;letter-spacing:.1em;font-size:16px;font-family:Sora,sans-serif}
  .brand img{width:40px;height:40px;border-radius:9px}
  .brand .way{color:var(--g2)}
  .navside{display:flex;align-items:center;gap:20px;flex-wrap:wrap}.navside a{color:#e8f5ea;text-decoration:none;font-size:13px;font-weight:700;letter-spacing:.02em}.navside a:hover{color:var(--g2)}
  .navside.left{grid-column:1;justify-content:flex-start}
  .navside.right{grid-column:3;justify-content:flex-end}
  .hero{position:relative;overflow:hidden;padding:70px 24px 58px;background:
     radial-gradient(900px 520px at 78% 8%,rgba(15,155,70,.32),transparent 60%),
     radial-gradient(760px 520px at 8% 96%,rgba(57,231,95,.14),transparent 55%),#050806}
  .hero:before{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(57,231,95,.10) 1px,transparent 1px);background-size:26px 26px;opacity:.5;mask:linear-gradient(#000,transparent 75%)}
  .hero-inner{position:relative;z-index:1;display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:46px;align-items:center}
  .eyebrow{font-size:12px;letter-spacing:.26em;text-transform:uppercase;color:var(--g2);font-weight:800}
  h1{font-family:Sora,sans-serif;font-size:clamp(36px,5.6vw,62px);line-height:1.05;margin:18px 0 18px;letter-spacing:-.5px;font-weight:800}
  h1 .g{background:linear-gradient(120deg,var(--g1),var(--g2));-webkit-background-clip:text;background-clip:text;color:transparent}
  .hero p{font-size:18px;color:#d9e8db;max-width:560px;margin:0 0 26px}
  .btns{display:flex;gap:12px;flex-wrap:wrap}
  .btn{display:inline-flex;align-items:center;gap:9px;justify-content:center;border:0;border-radius:999px;background:linear-gradient(135deg,var(--g1),var(--g2));color:var(--btnink);text-decoration:none;font-weight:800;padding:14px 24px;box-shadow:0 16px 38px rgba(15,155,70,.28);font-size:15px}
  .btn svg{width:18px;height:18px;fill:currentColor}
  .btn.ghost{background:rgba(255,255,255,.05);border:1px solid var(--line);box-shadow:none;color:#fff}
  .btn.wa{background:linear-gradient(135deg,#1faa53,#25d366);color:#fff}
  .hcard{position:relative}
  .hcard .frame{border-radius:20px;overflow:hidden;border:1px solid var(--line);box-shadow:0 30px 80px rgba(0,0,0,.5);background:#000}
  .hcard .frame img{width:100%;aspect-ratio:4/5;object-fit:cover}
  .hbadge{position:absolute;left:-18px;bottom:26px;background:rgba(9,16,11,.94);border:1px solid var(--line);border-radius:16px;padding:13px 16px;display:flex;align-items:center;gap:11px;box-shadow:0 16px 40px rgba(0,0,0,.5)}
  .hbadge b{font-size:19px;color:#fff;line-height:1.2;font-family:Sora,sans-serif}
  .hbadge span{display:block;font-size:11px;color:var(--mut);letter-spacing:.03em}
  .marquee{overflow:hidden;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
  .marquee .track{display:inline-flex;white-space:nowrap;animation:mq 30s linear infinite}
  .marquee:hover .track{animation-play-state:paused}
  .marquee .grp{display:inline-flex;align-items:center;padding:13px 0;font-weight:700;font-size:13.5px;color:#e3f3e5;letter-spacing:.02em}
  .marquee .grp i{margin:0 24px;color:var(--g2);font-style:normal}
  @keyframes mq{from{transform:translateX(0)}to{transform:translateX(-50%)}}
  .band{padding:74px 24px}.paper{background:var(--paper);color:var(--paper-ink)}
  .head{margin-bottom:34px}.head h2{font-family:Sora,sans-serif;font-size:clamp(28px,4.2vw,44px);line-height:1.08;margin:10px 0;font-weight:800}.sub{color:#c3d9c6;max-width:700px;font-size:16px}.paper .sub{color:#57614f}.rule{width:52px;height:3px;background:linear-gradient(90deg,var(--g1),var(--g2));border-radius:999px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(232px,1fr));gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:26px;transition:transform .25s,border-color .25s}
  .card:hover{transform:translateY(-4px);border-color:rgba(57,231,95,.4)}
  .card h3{margin:12px 0 8px;font-size:18px;color:#fff}.card p{margin:0;color:var(--mut);font-size:14px}.num{color:var(--g2);font-weight:900;font-size:12px;letter-spacing:.18em}
  .cards-media{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}
  .cardm{background:var(--panel);border:1px solid var(--line);border-radius:18px;overflow:hidden;transition:transform .25s}
  .cardm:hover{transform:translateY(-4px)}
  .cardm img{width:100%;aspect-ratio:4/3;object-fit:cover}
  .cardm .body{padding:20px}.cardm h3{margin:0 0 8px;color:#fff;font-size:17px}.cardm p{margin:0;color:var(--mut);font-size:14px}
  .quote-card{background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:32px}
  .qgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:6px}
  .qgrid label{display:flex;flex-direction:column;gap:7px;font-size:12.5px;font-weight:700;letter-spacing:.03em;color:#bcd6bf;text-transform:uppercase}
  .qgrid label.full{grid-column:1/-1}
  .qgrid input,.qgrid select,.qgrid textarea{font-family:inherit;font-size:15px;font-weight:500;color:#fff;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:10px;padding:11px 13px;outline:none}
  .qgrid input:focus,.qgrid select:focus,.qgrid textarea:focus{border-color:var(--g2)}
  .qgrid textarea{resize:vertical;min-height:86px}
  .qgrid select{color:#fff}
  .qgrid select option{color:#111}
  .qfoot{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:20px}
  .qmsg{margin-top:16px;font-size:14px;border-radius:10px;padding:12px 15px}
  .qmsg.ok{background:rgba(57,231,95,.12);border:1px solid rgba(57,231,95,.4);color:#c6f5cf}
  .qmsg.err{background:rgba(231,57,57,.1);border:1px solid rgba(231,57,57,.35);color:#f6c6c6}
  .qmsg a{color:#fff;font-weight:800;text-decoration:underline}
  .contact-box{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:28px}.contact-box p{margin:10px 0}
  .prose{color:#c8d9ca;max-width:760px}.prose h3{color:#fff;font-size:18px;margin:26px 0 8px}.prose p{margin:0 0 12px;font-size:15px;line-height:1.7}.prose a{color:var(--g2)}
  .ctaband{background:linear-gradient(135deg,#0f1a11,#080d08);border:1px solid var(--line);border-radius:24px;padding:42px;text-align:center}
  .ctaband h2{font-family:Sora,sans-serif;font-size:clamp(24px,3.6vw,36px);margin:0 0 10px;color:#fff;font-weight:800}.ctaband p{color:var(--mut);margin:0 0 22px}
  .badges{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-top:16px}
  .badge{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:800;color:#cfe7d1;border:1px solid var(--line);border-radius:999px;padding:8px 13px;background:rgba(255,255,255,.02)}
  footer{padding:46px 24px 30px;text-align:center;color:var(--mut);border-top:1px solid var(--line);background:#040705}footer img{width:64px;margin:0 auto 14px;border-radius:12px}
  .wa-float{position:fixed;left:20px;bottom:22px;z-index:999998;width:58px;height:58px;border-radius:50%;background:#25d366;display:grid;place-items:center;box-shadow:0 12px 34px rgba(0,0,0,.4);transition:transform .2s}
  .wa-float:hover{transform:scale(1.07)}.wa-float svg{width:32px;height:32px;fill:#fff}
  .reveal{opacity:0;transform:translateY(18px);transition:opacity .7s ease,transform .7s ease}.reveal.in{opacity:1;transform:none}
  .cov{display:grid;grid-template-columns:1.1fr .9fr;gap:26px;align-items:stretch}
  .cov-map{border-radius:20px;overflow:hidden;border:1px solid var(--line);box-shadow:0 18px 50px rgba(0,0,0,.45);background:#0b120d}
  #covmap{height:100%;min-height:400px;width:100%;background:#0b120d}
  .leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#101c13;color:#fff}.leaflet-container{font-family:Manrope,sans-serif}
  .cov-list{display:flex;flex-direction:column;justify-content:center}
  .cov-pill{display:inline-flex;align-items:center;gap:8px;align-self:flex-start;font-weight:800;font-size:13px;color:var(--btnink);background:linear-gradient(135deg,var(--g1),var(--g2));padding:9px 15px;border-radius:999px;margin-bottom:18px}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.chip{font-size:12px;font-weight:800;border:1px solid var(--line);border-radius:999px;padding:7px 12px;color:#dff3e1;background:rgba(255,255,255,.03)}
  .cov-note{margin-top:18px;color:var(--mut);font-size:14px}
  @media(max-width:860px){.cov{grid-template-columns:1fr}#covmap{min-height:300px}}
  @media(max-width:860px){
    .hero{padding:38px 18px 30px}
    .hero-inner{grid-template-columns:1fr;gap:26px}
    .hcard{max-width:320px;margin:0 auto;width:100%}
    .hcard .frame img{aspect-ratio:16/11}
    .hbadge{left:auto;right:10px;bottom:10px;padding:9px 12px}.hbadge b{font-size:16px}
    .navside{display:none}
    .band{padding:50px 18px}
  }
</style>
"""

NAV = """
<nav>
  <div class="navside left"><a href="/#services">Services</a><a href="/#quote">Get a Quote</a></div>
  <a class="brand" href="/"><img src="/static/images/logo.svg" alt="Betterway Logistic logo"><span>BETTER<span class="way">WAY</span> LOGISTIC</span></a>
  <div class="navside right"><a href="/#coverage">Coverage</a><a href="/contact">Contact</a></div>
</nav>
"""

WA_SVG = '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 .4C7.4.4.5 7.3.5 15.9c0 2.8.7 5.4 2 7.8L.3 31.6l8.1-2.1c2.3 1.3 4.9 1.9 7.6 1.9 8.6 0 15.5-6.9 15.5-15.5S24.6.4 16 .4zm0 28.3c-2.4 0-4.7-.6-6.7-1.8l-.5-.3-4.8 1.3 1.3-4.7-.3-.5a12.7 12.7 0 0 1-2-6.8C3.2 8.8 8.9 3.2 16 3.2c7 0 12.7 5.7 12.7 12.7S23 28.7 16 28.7zm7-9.5c-.4-.2-2.3-1.1-2.6-1.3-.3-.1-.6-.2-.8.2-.2.4-.9 1.3-1.1 1.5-.2.2-.4.3-.8.1-.4-.2-1.6-.6-3.1-1.9-1.1-1-1.9-2.3-2.1-2.7-.2-.4 0-.6.2-.8l.6-.7c.2-.2.3-.4.4-.6.1-.2 0-.5 0-.7-.1-.2-.8-2-1.1-2.8-.3-.7-.6-.6-.8-.6h-.7c-.2 0-.6.1-1 .5-.3.4-1.3 1.3-1.3 3.1s1.3 3.6 1.5 3.9c.2.2 2.6 4 6.3 5.6.9.4 1.6.6 2.1.8.9.3 1.7.2 2.3.1.7-.1 2.3-.9 2.6-1.8.3-.9.3-1.6.2-1.8-.1-.1-.3-.2-.7-.4z"/></svg>'

MARQUEE_GRP = ('<span class="grp">South East &amp; South West London <i>&bull;</i> Same-day courier <i>&bull;</i> '
               'Next-day delivery <i>&bull;</i> Man &amp; van removals <i>&bull;</i> Business collections <i>&bull;</i> '
               'Real person, fast reply <i>&bull;</i></span>')
MARQUEE = '<div class="marquee"><div class="track">' + MARQUEE_GRP + MARQUEE_GRP + '</div></div>'

FOOTER = """
<section class="band"><div class="wrap"><div class="ctaband reveal">
  <h2>Got something that needs moving?</h2>
  <p>Fill in the quote form or drop us a message - we'll come back with a price.</p>
  <div class="btns" style="justify-content:center"><a class="btn" href="tel:""" + PHONE_TEL + """">Call """ + PHONE_DISPLAY + """</a><a class="btn wa" href="https://wa.me/""" + WHATSAPP_NUMBER + """" target="_blank" rel="noopener">WhatsApp</a></div>
  <div class="badges"><span class="badge">South East London</span><span class="badge">South West London</span><span class="badge">Greater London on request</span><span class="badge">Real person support</span></div>
</div></div></section>
<footer>
  <img src="/static/images/logo.svg" alt="Betterway Logistic logo">
  <div style="color:#fff;font-weight:800;letter-spacing:.14em">BETTERWAY LOGISTIC</div>
  <div style="margin-top:6px">South East &amp; South West London &middot; covering Greater London</div>
  <div style="margin-top:12px"><a href="tel:""" + PHONE_TEL + """">""" + PHONE_DISPLAY + """</a> &nbsp;|&nbsp; <a href="mailto:""" + EMAIL_ADDRESS + """">""" + EMAIL_ADDRESS + """</a> &nbsp;|&nbsp; <a href="/privacy-policy">Privacy Policy</a></div>
</footer>
<a class="wa-float" href="https://wa.me/""" + WHATSAPP_NUMBER + """" target="_blank" rel="noopener" aria-label="WhatsApp Betterway Logistic">""" + WA_SVG + """</a>
"""

WIDGET_INCLUDE = '<script src="/widget.js"></script>'

SCRIPTS = """
<script>
(function(){
  var els=document.querySelectorAll('.reveal');
  if(!('IntersectionObserver' in window)){els.forEach(function(e){e.classList.add('in')});return;}
  var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}})},{threshold:.12});
  els.forEach(function(e){io.observe(e)});
})();
(function(){
  if(!window.L) return;
  var el=document.getElementById('covmap'); if(!el) return;
  var center=[51.4650,-0.1050];
  var map=L.map(el,{scrollWheelZoom:false,zoomControl:true}).setView(center,10);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap, &copy; CARTO'}).addTo(map);
  L.circle(center,{radius:16000,color:'#39e75f',weight:2,fillColor:'#39e75f',fillOpacity:.14}).addTo(map);
  L.circleMarker(center,{radius:7,color:'#fff',weight:2,fillColor:'#39e75f',fillOpacity:1}).addTo(map).bindPopup('Betterway Logistic &middot; SE / SW London base');
  setTimeout(function(){map.invalidateSize();},250);
})();
function resizeImage(file){return new Promise(function(resolve,reject){var reader=new FileReader();reader.onload=function(){var img=new Image();img.onload=function(){var max=1600,w=img.naturalWidth,h=img.naturalHeight;if(Math.max(w,h)>max){if(w>=h){h=Math.round(h*max/w);w=max}else{w=Math.round(w*max/h);h=max}}var canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;var ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);ctx.drawImage(img,0,0,w,h);resolve(canvas.toDataURL('image/jpeg',.82))};img.onerror=reject;img.src=reader.result};reader.onerror=reject;reader.readAsDataURL(file)})}
async function submitQuote(ev){
  ev.preventDefault();
  var f = document.getElementById('quoteForm');
  var statusEl = document.getElementById('quoteStatus');
  var website = document.getElementById('qWebsite').value.trim();
  if (website) { return false; }
  var data = {
    name: document.getElementById('qName').value.trim(),
    phone: document.getElementById('qPhone').value.trim(),
    email: document.getElementById('qEmail').value.trim(),
    service: document.getElementById('qService').value,
    pickup: document.getElementById('qPickup').value.trim(),
    delivery: document.getElementById('qDelivery').value.trim(),
    details: document.getElementById('qDetails').value.trim(),
    when: document.getElementById('qWhen').value.trim(),
  };
  if (!data.name || (!data.phone && !data.email)) {
    statusEl.className = 'qmsg err';
    statusEl.textContent = 'Please add your name and a phone number or email so we can send the quote back.';
    return false;
  }
  var btn = document.getElementById('quoteSubmit');
  btn.disabled = true; btn.textContent = 'Sending...';
  try {
    var r = await fetch('/quote', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data), credentials:'same-origin'});
    var d = await r.json();
    if (d.ok) {
      var waText = encodeURIComponent(
        'Hi Betterway Logistic, quote request:\\n' +
        'Name: ' + data.name + '\\n' +
        (data.phone ? 'Phone: ' + data.phone + '\\n' : '') +
        (data.email ? 'Email: ' + data.email + '\\n' : '') +
        (data.service ? 'Service: ' + data.service + '\\n' : '') +
        (data.pickup ? 'Pickup: ' + data.pickup + '\\n' : '') +
        (data.delivery ? 'Delivery: ' + data.delivery + '\\n' : '') +
        (data.when ? 'When: ' + data.when + '\\n' : '') +
        (data.details ? 'Details: ' + data.details : '')
      );
      statusEl.className = 'qmsg ok';
      statusEl.innerHTML = "Thanks " + (data.name.split(' ')[0] || '') + " - that's been emailed over and we'll come back to you with a price shortly. Prefer WhatsApp? <a href='https://wa.me/""" + WHATSAPP_NUMBER + """?text=' target='_blank' rel='noopener' id='waLink'>Send it on WhatsApp too</a>.";
      document.getElementById('waLink').href = 'https://wa.me/""" + WHATSAPP_NUMBER + """?text=' + waText;
      f.reset();
    } else {
      statusEl.className = 'qmsg err';
      statusEl.textContent = d.error || 'Something went wrong - please try again or call """ + PHONE_DISPLAY + """.';
    }
  } catch (e) {
    statusEl.className = 'qmsg err';
    statusEl.textContent = 'Something went wrong - please try again or call """ + PHONE_DISPLAY + """.';
  }
  btn.disabled = false; btn.textContent = 'Get a Quote';
  return false;
}
</script>
"""

QUOTE_FORM = """
<section class="band" id="quote"><div class="wrap narrow">
  <div class="head reveal"><div class="eyebrow">Get a quote</div><div class="rule"></div><h2>Tell us the details, we'll price it up.</h2><p class="sub">Fill this in and it lands straight in our inbox - we'll come back to you with a price by email or WhatsApp, usually within the hour during working hours.</p></div>
  <form class="quote-card reveal" id="quoteForm" onsubmit="return submitQuote(event)">
    <input type="text" id="qWebsite" name="website" autocomplete="off" tabindex="-1" style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0" aria-hidden="true">
    <div class="qgrid">
      <label>Name<input type="text" id="qName" placeholder="Your name" required></label>
      <label>Phone<input type="tel" id="qPhone" placeholder="07..."></label>
      <label>Email<input type="email" id="qEmail" placeholder="you@example.com"></label>
      <label>Service<select id="qService">
        <option value="Same-day courier">Same-day courier</option>
        <option value="Next-day delivery">Next-day delivery</option>
        <option value="Man &amp; van / small removal">Man &amp; van / small removal</option>
        <option value="Regular business collection">Regular business collection</option>
        <option value="Not sure yet">Not sure yet</option>
      </select></label>
      <label>Pickup postcode / area<input type="text" id="qPickup" placeholder="e.g. SE15"></label>
      <label>Delivery postcode / area<input type="text" id="qDelivery" placeholder="e.g. SW11"></label>
      <label>When it's needed<input type="text" id="qWhen" placeholder="Today, tomorrow, or a date"></label>
      <label class="full">What needs moving<textarea id="qDetails" placeholder="What is it, roughly how big / heavy, how many items"></textarea></label>
    </div>
    <div class="qfoot">
      <button class="btn" id="quoteSubmit" type="submit">Get a Quote</button>
      <span style="color:var(--mut);font-size:13px">No obligation - we only ever call or message back.</span>
    </div>
    <div id="quoteStatus"></div>
  </form>
</div></section>
"""

COVERAGE = """
<section class="band" id="coverage"><div class="wrap">
  <div class="head reveal"><div class="eyebrow">Where we work</div><div class="rule"></div><h2>Based in SE &amp; SW London, moving all across the capital.</h2><p class="sub">Betterway Logistic is based across South East and South West London, and takes on jobs right across Greater London.</p></div>
  <div class="cov">
    <div class="cov-map">
      <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
      <div id="covmap"></div>
    </div>
    <div class="cov-list">
      <div class="cov-pill">&#128205; SE &amp; SW London based &middot; Greater London covered</div>
      <div class="chips">
        <span class="chip">Greenwich</span><span class="chip">Lewisham</span><span class="chip">Bromley</span><span class="chip">Croydon</span><span class="chip">Bexley</span><span class="chip">Southwark</span><span class="chip">Peckham</span><span class="chip">Catford</span><span class="chip">Wandsworth</span><span class="chip">Clapham</span><span class="chip">Tooting</span><span class="chip">Wimbledon</span><span class="chip">Kingston</span><span class="chip">Richmond</span><span class="chip">Sutton</span><span class="chip">Merton</span><span class="chip">+ all of Greater London</span>
      </div>
      <p class="cov-note">Not sure if we cover your area? Just ask in the chat or send a message - we'll let you know straight away.</p>
    </div>
  </div>
</div></section>
"""

HOME_PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Betterway Logistic - Courier &amp; Delivery, South East &amp; South West London</title>
<meta name="description" content="Same-day and next-day courier, man-with-a-van and regular business collections across South East, South West and Greater London.">
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + BASE_STYLE + """</head><body>""" + NAV + """
<header class="hero"><div class="wrap hero-inner">
  <div>
    <div class="eyebrow">South East &amp; South West London</div>
    <h1>Betterway Logistic<br>delivers <span class="g">fast &amp; reliable</span>, across London.</h1>
    <p>Same-day courier, next-day delivery and man-with-a-van removals - tell us what needs moving and where, and we'll come back with a straight, fair price.</p>
    <div class="btns">
      <a class="btn" href="#quote">Get a Quote</a>
      <a class="btn wa" href="https://wa.me/""" + WHATSAPP_NUMBER + """" target="_blank" rel="noopener">WhatsApp</a>
    </div>
  </div>
  <aside class="hcard reveal">
    <div class="frame"><img src="https://images.unsplash.com/photo-1620455800201-7f00aeef12ed?w=900&q=80" alt="Van loaded with parcels ready for delivery"></div>
    <div class="hbadge"><b>Same-day available</b><span>SE &amp; SW London base &middot; Greater London on request</span></div>
  </aside>
</div></header>
""" + MARQUEE + QUOTE_FORM + """
<section class="band paper" id="services"><div class="wrap">
  <div class="head reveal"><div class="eyebrow">Services</div><div class="rule"></div><h2>Whatever needs moving, we've got a way.</h2><p class="sub">From a single parcel to a van full of furniture - across London, on your schedule.</p></div>
  <div class="cards-media reveal">
    <div class="cardm"><img src="https://images.unsplash.com/photo-1591706515036-cb0f48dc5e62?w=700&q=80" alt="Courier van parked on a residential street"><div class="body"><h3>Same-Day Courier</h3><p>Need it there today? We collect quickly and get it across London the same day.</p></div></div>
    <div class="cardm"><img src="https://images.unsplash.com/photo-1449247666642-264389f5f5b1?w=700&q=80" alt="Packing a parcel ready for collection"><div class="body"><h3>Next-Day Delivery</h3><p>Not urgent but still needs moving? Book ahead for next-day collection and drop-off.</p></div></div>
    <div class="cardm"><img src="https://images.unsplash.com/photo-1730154838368-c37b1fdebcf6?w=700&q=80" alt="Boxes packed for a small house move"><div class="body"><h3>Man &amp; Van / Small Removals</h3><p>Moving a few items, a sofa or a single room? One van, one driver, sorted.</p></div></div>
  </div>
  <div class="cards reveal" style="margin-top:20px">
    <div class="card"><div class="num">04</div><h3>Business &amp; Regular Collections</h3><p>Running the same route every week? Set up a regular collection and skip the re-booking.</p></div>
    <div class="card"><div class="num">05</div><h3>Real person, fast reply</h3><p>No call centres - message or call and speak directly to the person doing the job.</p></div>
    <div class="card"><div class="num">06</div><h3>Fair, upfront pricing</h3><p>Tell us the details and we'll give you a straight price before anything is booked in.</p></div>
  </div>
</div></section>
""" + COVERAGE + FOOTER + SCRIPTS + WIDGET_INCLUDE + "</body></html>"

SERVICES_PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Services - Betterway Logistic</title><meta name="viewport" content="width=device-width, initial-scale=1">""" + BASE_STYLE + """</head><body>""" + NAV + """
<section class="band"><div class="wrap"><div class="head reveal"><div class="eyebrow">Services</div><div class="rule"></div><h2>Courier, delivery &amp; man-with-a-van.</h2><p class="sub">Based across South East and South West London, covering Greater London on request.</p></div><div class="cards">
<div class="card reveal"><div class="num">01</div><h3>Same-Day Courier</h3><p>Collected within the hour where possible, delivered across London the same day.</p></div>
<div class="card reveal"><div class="num">02</div><h3>Next-Day Delivery</h3><p>Book ahead and we'll collect and deliver the following day.</p></div>
<div class="card reveal"><div class="num">03</div><h3>Man &amp; Van / Small Removals</h3><p>Furniture, single rooms and small house moves - one van, one driver.</p></div>
<div class="card reveal"><div class="num">04</div><h3>Business &amp; Regular Collections</h3><p>Set up a recurring collection for your shop, studio or office.</p></div>
</div></div></section>""" + QUOTE_FORM + FOOTER + SCRIPTS + WIDGET_INCLUDE + "</body></html>"

CONTACT_PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Contact - Betterway Logistic</title><meta name="viewport" content="width=device-width, initial-scale=1">""" + BASE_STYLE + """</head><body>""" + NAV + """
<section class="band"><div class="wrap narrow"><div class="head reveal"><div class="eyebrow">Contact</div><div class="rule"></div><h2>Send us the job details.</h2><p class="sub">Use the quote form for the fastest reply, or get in touch directly.</p></div><div class="contact-box reveal">
<p><strong style="color:#fff">Phone:</strong> <a href="tel:""" + PHONE_TEL + """">""" + PHONE_DISPLAY + """</a></p>
<p><strong style="color:#fff">Email:</strong> <a href="mailto:""" + EMAIL_ADDRESS + """">""" + EMAIL_ADDRESS + """</a></p>
<p><strong style="color:#fff">WhatsApp:</strong> <a href="https://wa.me/""" + WHATSAPP_NUMBER + """" target="_blank" rel="noopener">Message us</a></p>
<p><strong style="color:#fff">Area:</strong> South East &amp; South West London, covering Greater London.</p>
</div></div></section>""" + QUOTE_FORM + FOOTER + SCRIPTS + WIDGET_INCLUDE + "</body></html>"

PRIVACY_PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Privacy Policy - Betterway Logistic</title><meta name="viewport" content="width=device-width, initial-scale=1">""" + BASE_STYLE + """</head><body>""" + NAV + """
<section class="band"><div class="wrap narrow">
  <div class="head reveal"><div class="eyebrow">Legal</div><div class="rule"></div><h2>Privacy Policy</h2><p class="sub">How Betterway Logistic handles the information you share through this website.</p></div>
  <div class="prose reveal">
    <p>This policy explains what information we collect when you use this website, its quote form or its chat assistant, why we collect it, and how it is kept. By sending a quote request you agree to the points below.</p>
    <h3>What we collect</h3>
    <p>When you contact us through the quote form, chat assistant, phone, email or WhatsApp we may collect your name, phone number, email address, pickup and delivery details, and any photos you choose to upload. We only collect what you choose to give us.</p>
    <h3>Why we collect it</h3>
    <p>We use this information for one purpose: to understand the job, reply to you, and provide a quote or arrange the collection. We do not use it for marketing unless you ask us to.</p>
    <h3>How it is handled</h3>
    <p>Enquiry details are sent to our own inbox so we can respond. To run the site we use trusted service providers - including a chat/AI provider to power the assistant and an email provider to deliver enquiries - who process the information only to provide that service. We do not sell or rent your information to anyone.</p>
    <h3>How long we keep it</h3>
    <p>We keep enquiry information only for as long as needed to deal with your job and our records, then remove it.</p>
    <h3>Your rights</h3>
    <p>You can ask us what information we hold about you, ask us to correct it, or ask us to delete it at any time. Just get in touch using the details below.</p>
    <h3>Contact</h3>
    <p>Betterway Logistic, South East / South West London.<br>Email: <a href="mailto:""" + EMAIL_ADDRESS + """">""" + EMAIL_ADDRESS + """</a> &nbsp;|&nbsp; Phone: <a href="tel:""" + PHONE_TEL + """">""" + PHONE_DISPLAY + """</a></p>
  </div>
</div></section>""" + FOOTER + SCRIPTS + WIDGET_INCLUDE + "</body></html>"

WIDGET_JS = """
(function(){
  var base = new URL(document.currentScript.src).origin;
  var bubble = document.createElement('button');
  bubble.innerHTML = 'Chat';
  bubble.setAttribute('aria-label','Open quote assistant');
  bubble.style.cssText='position:fixed;right:22px;bottom:22px;z-index:999999;border:0;border-radius:999px;background:linear-gradient(135deg,#0f9b46,#39e75f);color:#04220d;font-weight:900;padding:15px 18px;box-shadow:0 12px 34px rgba(0,0,0,.34);cursor:pointer';
  var frame = document.createElement('iframe');
  frame.src = base + '/widget-frame';
  function size(){ frame.style.cssText = window.innerWidth <= 640 ? 'position:fixed;inset:0;width:100vw;height:100dvh;border:0;z-index:999999;display:none;background:white' : 'position:fixed;right:22px;bottom:84px;width:410px;height:610px;border:0;border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.45);z-index:999999;display:none;background:white'; }
  size(); window.addEventListener('resize',size);
  bubble.onclick=function(){ frame.style.display='block'; bubble.style.display=window.innerWidth<=640?'none':'block'; document.body.style.overflow=window.innerWidth<=640?'hidden':''; };
  window.addEventListener('message',function(e){ if(e.data==='close-bw-chat'){ frame.style.display='none'; bubble.style.display='block'; document.body.style.overflow=''; }});
  document.body.appendChild(bubble); document.body.appendChild(frame);
})();
"""

WIDGET_FRAME = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}html,body{margin:0;height:100%;font-family:Manrope,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f2f6f2;color:#141a15;overflow:hidden}
#chatWindow{height:100dvh;display:flex;flex-direction:column;background:#f2f6f2}#chatHeader{background:#07100a;color:white;padding:16px;display:flex;align-items:center;gap:12px;justify-content:space-between}.hbrand{display:flex;gap:10px;align-items:center}.hbrand img{width:38px;height:38px;border-radius:8px}.title{font-weight:900}.sub{font-size:12px;color:#bfe6c6}.close{font-size:28px;color:#39e75f;cursor:pointer;padding:2px 8px}#status{font-size:12px;color:#5c6d5e;background:#fff;padding:10px 14px;border-bottom:1px solid #dbe8dc}#chatbox{flex:1;overflow:auto;padding:16px;-webkit-overflow-scrolling:touch}.msg{max-width:84%;margin:10px 0;padding:12px 14px;border-radius:16px;line-height:1.45;font-size:15px}.bot{background:#fff;border:1px solid #dbe8dc}.user{margin-left:auto;background:#0c1610;color:white}.photo-msg{padding:5px;background:#0c1610}.photo{width:210px;border-radius:12px}#inputRow{flex:none;display:flex;gap:8px;padding:10px;background:white;border-top:1px solid #dbe8dc;padding-bottom:max(10px,env(safe-area-inset-bottom))}#userInput{flex:1;min-width:0;border:1px solid #bcd6bf;border-radius:999px;padding:12px 14px;font-size:16px;outline:none}#sendBtn,#attachBtn{border:0;border-radius:50%;width:46px;height:46px;display:grid;place-items:center;background:#0c1610;color:white;font-weight:900;cursor:pointer;flex:none}#attachBtn{background:#e3f3e5;color:#0c1610}#fileInput{display:none}.typing{color:#7c8c7e}
</style></head><body><div id="chatWindow"><div id="chatHeader"><div class="hbrand"><img src="/static/images/logo.svg"><div><div class="title">Betterway Assistant</div><div class="sub">Quote details captured in minutes</div></div></div><div class="close" onclick="window.parent.postMessage('close-bw-chat','*')">&times;</div></div><div id="status">Tell us what needs collecting and where it's going</div><div id="chatbox"></div><div id="inputRow"><label id="attachBtn" title="Attach a photo"><input type="file" id="fileInput" accept="image/*" multiple onchange="handleFiles(this)">+</label><input type="text" id="hpField" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0"><input id="userInput" type="text" placeholder="Type your message..." onkeypress="if(event.key==='Enter')sendMessage()"><button id="sendBtn" onclick="sendMessage()">&gt;</button></div></div>
<script>
addMessage("Hi, I can help get a free quote for a courier job, delivery or man-with-a-van move. What needs collecting?", "bot");
function addMessage(t,s){var c=document.getElementById('chatbox'),d=document.createElement('div');d.className='msg '+s;d.textContent=t;c.appendChild(d);c.scrollTop=c.scrollHeight;}
function typing(){var c=document.getElementById('chatbox'),d=document.createElement('div');d.id='typing';d.className='msg bot typing';d.textContent='...';c.appendChild(d);c.scrollTop=c.scrollHeight}
function untyping(){var t=document.getElementById('typing');if(t)t.remove()}
async function sendMessage(){var i=document.getElementById('userInput'),m=i.value.trim();if(!m)return;addMessage(m,'user');i.value='';typing();try{var r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m,website:(document.getElementById('hpField')||{}).value||''}),credentials:'same-origin'});var d=await r.json();untyping();addMessage(d.reply,'bot')}catch(e){untyping();addMessage("Sorry, that did not send. Please try again.",'bot')}}
function addImage(src){var c=document.getElementById('chatbox'),d=document.createElement('div'),img=document.createElement('img');d.className='msg user photo-msg';img.className='photo';img.src=src;d.appendChild(img);c.appendChild(d);c.scrollTop=c.scrollHeight;}
function resizeImage(file){return new Promise(function(resolve,reject){var reader=new FileReader();reader.onload=function(){var img=new Image();img.onload=function(){var max=1600,w=img.naturalWidth,h=img.naturalHeight;if(Math.max(w,h)>max){if(w>=h){h=Math.round(h*max/w);w=max}else{w=Math.round(w*max/h);h=max}}var canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;var ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);ctx.drawImage(img,0,0,w,h);resolve(canvas.toDataURL('image/jpeg',.82))};img.onerror=reject;img.src=reader.result};reader.onerror=reject;reader.readAsDataURL(file)})}
async function handleFiles(input){var files=Array.from(input.files||[]);input.value='';for(const file of files){if(!file.type.startsWith('image/')){addMessage('Please choose a photo file.','bot');continue}try{var dataUrl=await resizeImage(file);addImage(dataUrl);var r=await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image:dataUrl}),credentials:'same-origin'});var d=await r.json();addMessage(d.reply,'bot')}catch(e){addMessage('Sorry, I could not upload that photo. Try another JPG or PNG.','bot')}}}
</script></body></html>
"""


def ensure_session():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())


@app.route("/sitemap.xml")
def sitemap():
    pages = ["/", "/services", "/contact", "/privacy-policy"]
    base = "https://betterwaylogistic.co.uk"
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: https://betterwaylogistic.co.uk/sitemap.xml", mimetype="text/plain")


@app.route("/")
def home():
    ensure_session()
    return render_template_string(HOME_PAGE)


@app.route("/services")
def services():
    ensure_session()
    return render_template_string(SERVICES_PAGE)


@app.route("/contact")
def contact():
    ensure_session()
    return render_template_string(CONTACT_PAGE)


@app.route("/privacy")
@app.route("/privacy-policy")
def privacy():
    ensure_session()
    return render_template_string(PRIVACY_PAGE)


@app.route("/widget.js")
def widget_js():
    return Response(WIDGET_JS, mimetype="application/javascript")


@app.route("/widget-frame")
def widget_frame():
    ensure_session()
    return render_template_string(WIDGET_FRAME)


@app.route("/quote", methods=["POST"])
def quote_endpoint():
    data = request.get_json(silent=True) or {}
    if (data.get("website") or "").strip():
        # Honeypot - pretend success to bots, skip the email.
        return jsonify({"ok": True})

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or not (phone or email):
        return jsonify({"ok": False, "error": "Please add your name and a phone number or email."}), 400

    fields = {
        "Name": name,
        "Phone": phone or None,
        "Email": email or None,
        "Service": (data.get("service") or "").strip() or None,
        "Pickup": (data.get("pickup") or "").strip() or None,
        "Delivery": (data.get("delivery") or "").strip() or None,
        "When needed": (data.get("when") or "").strip() or None,
        "Details": (data.get("details") or "").strip() or None,
    }
    send_quote_form_email(fields)
    return jsonify({"ok": True})


@app.route("/chat", methods=["POST"])
def chat_endpoint():
    session_id = session.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        session["session_id"] = session_id

    if session_id not in all_conversations:
        all_conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    conversation = all_conversations[session_id]
    data = request.get_json(silent=True) or {}

    if (data.get("website") or "").strip():
        return jsonify({"reply": "Thanks!"})

    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"reply": "Sorry, I didn't catch that - could you type that again?"})

    now = time.time()
    recent = [t for t in chat_activity.get(session_id, []) if now - t < 60]
    if len(recent) >= 20:
        return jsonify({"reply": "You're sending messages very quickly - give it a few seconds and try again."})
    if len(conversation) >= 60:
        return jsonify({"reply": "Thanks for all the detail! Drop your name and number and Betterway Logistic will pick this up with you personally."})
    recent.append(now)
    chat_activity[session_id] = recent

    conversation.append({"role": "user", "content": user_message})

    try:
        response = client_chat(
            model="llama-3.3-70b-versatile",
            messages=conversation,
            max_tokens=256,
            timeout=20,
        )
        ai_reply = response.choices[0].message.content
    except Exception as e:
        print(f"Chat completion failed: {e}")
        conversation.pop()
        return jsonify({"reply": "Sorry, I had a brief hiccup there - could you send that again?"})

    lead_ready = bool(re.search(r"\[\[?\s*READY\s*\]?\]", ai_reply, re.I))
    ai_reply = re.sub(r"\[\[?\s*READY\s*\]?\]", "", ai_reply)
    ai_reply = ai_reply.replace("[LEAD_CAPTURED]", "").strip()
    if not ai_reply:
        ai_reply = ("Thanks - that's everything we need for now. Betterway Logistic "
                    "will be in touch shortly with a price.")

    conversation.append({"role": "assistant", "content": ai_reply})

    if session_id not in notified_sessions and has_contact_info(conversation):
        if lead_ready or _looks_like_closing(user_message) or len(conversation) >= 24:
            notified_sessions.add(session_id)
            conversation_copy = list(conversation)
            images_copy = list(session_images.get(session_id, []))
            send_lead_email(conversation_copy, images_copy)

    return jsonify({"reply": ai_reply})


@app.route("/upload", methods=["POST"])
def upload_endpoint():
    session_id = session.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        session["session_id"] = session_id

    if session_id not in all_conversations:
        all_conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    conversation = all_conversations[session_id]

    data = request.get_json(silent=True) or {}
    image = _decode_image_data_url(data.get("image", ""))
    if image is None:
        return jsonify({"reply": "Sorry, I couldn't read that image. Please try a JPG or PNG photo."}), 400

    images = session_images.setdefault(session_id, [])
    if len(images) >= MAX_IMAGES_PER_SESSION:
        return jsonify({
            "reply": "Thanks - that's plenty of photos for now. Leave your name and number and we'll take a look and get you a quote."
        })

    images.append(image)
    conversation.append({"role": "user", "content": "(Customer attached a photo of the item)"})
    reply = (
        "Thanks, got the photo - that really helps. You can add another, "
        "or tell me if that is all and I will carry on with the quote details."
    )
    conversation.append({"role": "assistant", "content": reply})

    if session_id in notified_sessions:
        send_photo_followup(list(conversation), [image])

    return jsonify({"reply": reply})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
