// Cloudflare Worker for Betterway Logistic. The shared chat engine is in core.js;
// this file holds the Betterway wording and email layout (same as app.py).
import { makeWorker, json, row, transcript, transcriptHtml, urgencyBadge, urgentPrefix, photosLine, missedLine,
  findPhone, findEmail, sendEmail, limited } from "./core.js";
import { SYSTEM_PROMPT, LEAD_SUMMARY_PROMPT } from "./prompts.js";

function shell(eyebrow, title, inner) {
  return '<!DOCTYPE html><html><body style="margin:0;background:#eef2ee;padding:24px;' +
    'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">' +
    '<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.07)">' +
    '<div style="background:#07100a;padding:24px 28px">' +
    `<div style="color:#39e75f;font-size:12px;letter-spacing:.18em;text-transform:uppercase;font-weight:700">${eyebrow}</div>` +
    `<div style="color:#fff;font-size:21px;font-weight:700;margin-top:5px">${title}</div></div>` +
    `<div style="padding:26px 28px">${inner}</div>` +
    '<div style="background:#f7faf7;padding:16px 28px;border-top:1px solid #eee;font-size:12px;color:#aaa">Sent automatically by the Betterway Logistic website.</div>' +
    '</div></body></html>';
}

const table = (rows, mb = true) => '<table style="width:100%;border-collapse:collapse;border:1px solid #eee;' +
  `border-radius:8px;overflow:hidden${mb ? ";margin-bottom:28px" : ""}">${rows}</table>`;

function subjectFor(word, fields, second) {
  const contact = fields.Phone || fields.Email || "no number yet";
  const bits = [fields.Name, fields[second]].filter(Boolean);
  return `New ${word} - ` + (bits.length ? [...bits, contact].join(" · ") : contact);
}

function leadEmail({ pick, conv, images, missed }) {
  const fields = {
    Name: pick("name"),
    Phone: findPhone(conv),
    Email: findEmail(conv),
    Job: pick("job / what needs moving", "job", "what needs moving"),
    Pickup: pick("pickup area or postcode", "pickup"),
    Delivery: pick("delivery area or postcode", "delivery"),
    "Size / weight": pick("size / weight / quantity", "size", "weight"),
    "Preferred timing": pick("preferred timing", "timing"),
    Urgency: pick("urgency (1-5 where 1=no rush, 5=urgent - infer from what they said)", "urgency"),
    Notes: pick("other notes", "notes"),
  };
  const text = ["NEW QUOTE REQUEST - Betterway Logistic", "========================"];
  for (const [k, v] of Object.entries(fields)) if (v) text.push(`${k}: ${v}`);
  if (images.length) text.push(`Photos attached: ${images.length}`);
  if (missed) text.push(`Photos not attached (customer left the page): ${missed}`);
  text.push("========================", "", "Full conversation:", "", transcript(conv));

  const { Urgency, ...rest } = fields;
  const inner = '<p style="margin:0 0 20px;font-size:14px;color:#666">Captured by the website chat assistant:</p>' +
    urgencyBadge(Urgency) + photosLine(images.length) + missedLine(missed) +
    table(Object.entries(rest).map(([k, v]) => row(k, v)).join("")) +
    '<div style="font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:#999;font-weight:700;margin-bottom:14px">Full conversation</div>' +
    transcriptHtml(conv, { userBg: "#f2f7f2", botLabel: "Betterway Assistant", botColor: "#0f9b46", border: "#e6ece6" });
  return {
    subject: urgentPrefix(Urgency) + subjectFor("quote request", fields, "Pickup"),
    text: text.join("\n"),
    html: shell("Betterway Logistic", "New quote request from your website", inner),
  };
}

function followUpEmail({ conv, images }) {
  const phone = findPhone(conv) || "Not provided";
  const email = findEmail(conv) || "Not provided";
  const inner = '<p style="margin:0 0 18px;font-size:14px;color:#666">This relates to a quote request ' +
    `you've already been emailed about. <strong>${images.length} new photo(s)</strong> attached below.</p>` +
    table(row("Phone", phone) + row("Email", email), false);
  return {
    subject: `Photo added - quote request: ${phone}`,
    text: "ADDITIONAL PHOTO(S) - Betterway Logistic\nThis relates to a quote request you've already been emailed about.\n" +
      `Phone: ${phone}\nEmail: ${email}\nPhotos attached: ${images.length}\n`,
    html: shell("Betterway Logistic", "More photos for an existing request", inner),
  };
}

// The "Get a Quote" form on the page (POST /quote, same as app.py).
const str = (v) => (typeof v === "string" ? v.trim().slice(0, 3000) : "");
async function quoteForm(request, env) {
  if (await limited(env.LEAD_LIMIT, request)) return json({ ok: false, error: "Please wait a moment before sending another request." }, 429);
  const data = await request.json().catch(() => ({}));
  if (str(data.website)) return json({ ok: true });
  const name = str(data.name), phone = str(data.phone), email = str(data.email);
  if (!name || !(phone || email)) return json({ ok: false, error: "Please add your name and a phone number or email." }, 400);
  const fields = {
    Name: name, Phone: phone, Email: email, Service: str(data.service), Pickup: str(data.pickup),
    Delivery: str(data.delivery), "When needed": str(data.when), Details: str(data.details),
  };
  const text = ["NEW QUOTE REQUEST (website form) - Betterway Logistic", "========================"];
  for (const [k, v] of Object.entries(fields)) if (v) text.push(`${k}: ${v}`);
  const inner = '<p style="margin:0 0 20px;font-size:14px;color:#666">Submitted directly through the ' +
    '"Get a Quote" form on your website:</p>' + table(Object.entries(fields).map(([k, v]) => row(k, v)).join(""), false);
  try {
    await sendEmail(env, {
      subject: subjectFor("quote request", fields, "Service"), text: text.join("\n"),
      html: shell("Betterway Logistic", "New quote request from your website", inner),
      replyTo: /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? email : null,
    });
  } catch (e) { console.log("Quote form email failed:", e.message); }
  return json({ ok: true });
}

export default makeWorker({
  SYSTEM_PROMPT,
  LEAD_SUMMARY_PROMPT,
  MAXED_REPLY: "Thanks for all the detail! Drop your name and number and Betterway Logistic will pick this up with you personally.",
  EMPTY_REPLY: "Thanks - that's everything we need for now. Betterway Logistic will be in touch shortly with a price.",
  leadEmail,
  followUpEmail,
  routes: { "/quote": quoteForm },
});
