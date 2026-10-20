import os, sys, io, json, re, time, html, requests

LANG = os.environ.get("EB_LANG", "").strip().lower()
if LANG not in ("hindi", "english", "urdu"):
    sys.exit("lang galat hai (hindi/english/urdu chahiye)")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
PRICE_CENTS = os.environ.get("PRICE_CENTS", "500")
CTA = os.environ.get("EB_CTA", "download")
LANG_NAME = {"hindi": "Hindi (Devanagari script)", "english": "English",
             "urdu": "Urdu (Urdu script, right to left)"}[LANG]
os.makedirs("work", exist_ok=True)
os.makedirs("covers", exist_ok=True)


def drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def download(d, f):
    from googleapiclient.http import MediaIoBaseDownload
    if f["mimeType"] == "application/vnd.google-apps.document":
        return d.files().export_media(fileId=f["id"], mimeType="text/plain").execute()
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, d.files().get_media(fileId=f["id"]))
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def fit(im, w, h):
    """Image ko bina kate (w x h) mein fit karta hai, peeche dhundhla background."""
    from PIL import Image, ImageFilter, ImageOps
    bg = ImageOps.fit(im, (w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
    fg = ImageOps.contain(im, (w, h), Image.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def fetch():
    d = drive()
    r = d.files().list(
        q="name='Video_Output' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)").execute()
    if not r["files"]:
        sys.exit("Video_Output folder nahi mila. Service account ko folder share kiya?")
    fid = r["files"][0]["id"]
    files, token = [], None
    while True:
        r = d.files().list(
            q=f"'{fid}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType,modifiedTime)",
            orderBy="modifiedTime desc", pageSize=200, pageToken=token).execute()
        files += r["files"]
        token = r.get("nextPageToken")
        if not token:
            break
    mine = [f for f in files if LANG in f["name"].lower()]
    texts = [f for f in mine if f["name"].lower().endswith(".txt")
             or f["mimeType"] in ("text/plain", "application/vnd.google-apps.document")]
    imgs = [f for f in mine if f["mimeType"].startswith("image/")]
    if not texts:
        names = ", ".join(f["name"] for f in files[:30])
        sys.exit(f"'{LANG}' naam wali text file nahi mili. Folder mein: {names}")
    t = texts[0]
    print("Script file:", t["name"])
    raw = download(d, t)
    text = (raw.decode("utf-8-sig", errors="ignore") if isinstance(raw, bytes) else raw).strip()
    if len(text) < 50:
        sys.exit("Text file khali ya bahut chhoti hai")
    open("work/script.txt", "w", encoding="utf-8").write(text)
    if imgs:
        from PIL import Image
        print("Cover file:", imgs[0]["name"])
        im = Image.open(io.BytesIO(download(d, imgs[0]))).convert("RGB")
        # PDF ke pehle page ke liye (A5 portrait) - bina kate
        fit(im, 1480, 2100).save("work/cover.jpg", quality=90)
        # Gumroad cover/thumbnail ke liye (square, 1000x1000) - bina kate
        fit(im, 1000, 1000).save(f"covers/{LANG}.jpg", quality=92)
    else:
        print("WARNING: is language ki thumbnail/image nahi mili, bina cover ke banega")


def groq(messages, max_tokens=4000, json_mode=False):
    body = {"model": MODEL, "messages": messages, "temperature": 0.7, "max_tokens": max_tokens}
    if "gpt-oss" in MODEL:
        body["reasoning_effort"] = "low"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    for attempt in range(8):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": "Bearer " + os.environ["GROQ_API_KEY"]},
                          json=body, timeout=180)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"].get("content") or ""
            if content.strip():
                return content
            print("Groq ne khali jawab diya, dobara koshish...")
            time.sleep(5)
            continue
        if r.status_code == 400 and "reasoning_effort" in body:
            print("reasoning_effort support nahi, bina uske try kar rahe hain")
            body.pop("reasoning_effort", None)
            continue
        if r.status_code in (429, 500, 502, 503):
            print("Groq busy, ruk rahe hain...", r.status_code)
            time.sleep(20 * (attempt + 1))
            continue
        sys.exit(f"Groq error {r.status_code}: {r.text[:300]} (model naam badalna pad sakta hai: GROQ_MODEL)")
    sys.exit("Groq baar-baar fail hua")


def clean(t):
    t = re.sub(r"[#*_`>]+", "", t)
    return t.strip()


def parse_json(s):
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def write():
    script = open("work/script.txt", encoding="utf-8").read()[:6000]
    out = groq([
        {"role": "system", "content": "You are a professional eBook author. Reply only with JSON."},
        {"role": "user", "content":
            f"Below is a video script. Plan an eBook on the same topic, written in {LANG_NAME}.\n"
            'Return JSON: {"title": "...", "subtitle": "...", "description": "2-3 sentence sales description", '
            '"chapters": ["8 chapter titles"]}. All text in the target language.\n\nSCRIPT:\n' + script}
    ], max_tokens=2500, json_mode=True)
    plan = parse_json(out)
    chapters = plan["chapters"][:8]
    book = {"title": clean(plan["title"]), "subtitle": clean(plan.get("subtitle", "")),
            "description": clean(plan.get("description", "")), "chapters": []}
    for i, ct in enumerate(chapters, 1):
        print(f"Chapter {i}/{len(chapters)}: {ct}")
        body = groq([
            {"role": "system", "content": f"You write eBook chapters in {LANG_NAME}. Plain paragraphs only, no markdown, no headings."},
            {"role": "user", "content":
                f"eBook title: {book['title']}\nChapter {i}: {ct}\n"
                f"Write about 600 words for this chapter in {LANG_NAME}. Use the script below only as the base idea; "
                "add useful explanation and examples. Separate paragraphs with a blank line.\n\nSCRIPT:\n" + script}
        ], max_tokens=4000)
        book["chapters"].append({"title": clean(ct), "text": clean(body)})
        time.sleep(3)
    json.dump(book, open("work/book.json", "w", encoding="utf-8"), ensure_ascii=False)


def pdf():
    from weasyprint import HTML
    book = json.load(open("work/book.json", encoding="utf-8"))
    e = html.escape
    font = {"hindi": "'Noto Sans Devanagari','Noto Serif Devanagari'",
            "english": "'Noto Serif', serif",
            "urdu": "'Noto Nastaliq Urdu','Noto Naskh Arabic'"}[LANG]
    direction = "rtl" if LANG == "urdu" else "ltr"
    lh = "2.4" if LANG == "urdu" else "1.8"
    cover = ""
    if os.path.exists("work/cover.jpg"):
        cover = f'<div class="cover"><img src="file://{os.path.abspath("work/cover.jpg")}"></div>'
    parts = [f"""<html dir="{direction}"><head><meta charset="utf-8"><style>
@page {{ size: A5; margin: 18mm; }}
@page cover {{ size: A5; margin: 0; }}
body {{ font-family: {font}; font-size: 11pt; line-height: {lh}; }}
.cover {{ page: cover; page-break-after: always; }}
.cover img {{ width: 148mm; height: 210mm; }}
.title {{ text-align: center; margin-top: 60mm; page-break-after: always; }}
h1 {{ font-size: 24pt; }} h2 {{ font-size: 16pt; page-break-before: always; }}
p {{ margin: 0 0 3mm 0; }}
</style></head><body>{cover}
<div class="title"><h1>{e(book['title'])}</h1><p>{e(book['subtitle'])}</p></div>"""]
    for c in book["chapters"]:
        parts.append(f"<h2>{e(c['title'])}</h2>")
        for p in re.split(r"\n\s*\n", c["text"]):
            if p.strip():
                parts.append(f"<p>{e(p.strip())}</p>")
    parts.append("</body></html>")
    HTML(string="".join(parts), base_url=".").write_pdf("work/ebook.pdf")
    print("PDF ban gayi:", os.path.getsize("work/ebook.pdf"), "bytes")


def upload():
    tok = os.environ["GUMROAD_TOKEN"]
    api = "https://api.gumroad.com/v2"
    book = json.load(open("work/book.json", encoding="utf-8"))
    path = "work/ebook.pdf"
    size = os.path.getsize(path)
    r = requests.post(api + "/files/presign", data={
        "access_token": tok, "filename": f"{LANG}-ebook.pdf", "file_size": size}, timeout=60).json()
    if not r.get("success"):
        sys.exit("Gumroad presign fail: " + str(r))
    data = open(path, "rb").read()
    CH = 100 * 1024 * 1024
    form = [("access_token", tok), ("upload_id", r["upload_id"]), ("key", r["key"])]
    for p in r["parts"]:
        n = p["part_number"]
        pr = requests.put(p["presigned_url"], data=data[(n - 1) * CH:n * CH], timeout=300)
        pr.raise_for_status()
        form += [("parts[][part_number]", str(n)), ("parts[][etag]", pr.headers["ETag"])]
    c = requests.post(api + "/files/complete", data=form, timeout=60).json()
    file_url = c.get("file_url") or r["file_url"]
    desc = "<p>" + html.escape(book["description"]) + "</p>"
    prod = None
    for ntype in ("ebook", "digital"):
        pr = requests.post(api + "/products", data=[
            ("access_token", tok), ("native_type", ntype), ("name", book["title"][:200]),
            ("description", desc), ("price", PRICE_CENTS), ("price_currency_type", "usd"),
            ("files[][url]", file_url)], timeout=60).json()
        if pr.get("success"):
            prod = pr["product"]
            break
        print("Product create fail:", ntype, pr)
    if not prod:
        sys.exit("Gumroad product nahi bana. Upar ka error dekho.")
    pid = prod["id"]

    # Cover (square) - Gumroad iska thumbnail bhi isi se banata hai
    if os.path.exists(f"covers/{LANG}.jpg"):
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        url = f"https://raw.githubusercontent.com/{repo}/main/covers/{LANG}.jpg"
        time.sleep(8)
        cr = requests.post(f"{api}/products/{pid}/covers", data={"access_token": tok, "url": url}, timeout=60).json()
        print("Cover:", cr.get("success"), "" if cr.get("success") else cr)

    # Call to action (best-effort: Gumroad API ye support na kare to haath se badalna hoga)
    if CTA:
        try:
            ur = requests.put(f"{api}/products/{pid}", data={
                "access_token": tok, "call_to_action": CTA}, timeout=60).json()
            print("Call to action:", ur.get("success"), "" if ur.get("success") else ur)
        except Exception as ex:
            print("Call to action set nahi hua:", ex)

    # Publish ya draft - gumroad naya product apne aap live kar deta hai, isliye draft ke liye disable zaroori hai
    if os.environ.get("EB_PUBLISH", "false").lower() == "true":
        er = requests.put(f"{api}/products/{pid}/enable", data={"access_token": tok}, timeout=60).json()
        print("Publish:", er.get("success"))
    else:
        dr = requests.put(f"{api}/products/{pid}/disable", data={"access_token": tok}, timeout=60).json()
        if dr.get("success"):
            print("Draft/Unpublished kar diya. Gumroad mein jaake dekho aur publish karo.")
        else:
            print("WARNING: draft nahi ban paya, product LIVE ho sakta hai! Gumroad mein jaake Unpublish karo.", dr)
    print("Product link:", prod.get("short_url"))


{"fetch": fetch, "write": write, "pdf": pdf, "upload": upload}[sys.argv[1]]()
