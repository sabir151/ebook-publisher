import os, sys, io, json, re, time, html, zipfile, uuid, requests

LANG = os.environ.get("EB_LANG", "").strip().lower()
if LANG not in ("hindi", "english", "urdu"):
    sys.exit("lang galat hai (hindi/english/urdu chahiye)")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
PRICE_CENTS = os.environ.get("PRICE_CENTS", "500")
CTA = os.environ.get("EB_CTA", "download")
LANG_NAME = {"hindi": "Hindi (Devanagari script)", "english": "English",
             "urdu": "Urdu (Urdu script, right to left)"}[LANG]
LANG_CODE = {"hindi": "hi", "english": "en", "urdu": "ur"}[LANG]
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


def to_text(raw):
    return (raw.decode("utf-8-sig", errors="ignore") if isinstance(raw, bytes) else raw).strip()


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
    mds = [f for f in mine if f["name"].lower().endswith(".md")]
    texts = [f for f in mine if f["name"].lower().endswith(".txt")
             or f["mimeType"] in ("text/plain", "application/vnd.google-apps.document")]
    imgs = [f for f in mine if f["mimeType"].startswith("image/")]

    if mds:
        t = mds[0]
        print("eBook file (md):", t["name"])
        text = to_text(download(d, t))
        if len(text) < 200:
            sys.exit("md file khali ya bahut chhoti hai")
        open("work/ebook.md", "w", encoding="utf-8").write(text)
    else:
        if not texts:
            names = ", ".join(f["name"] for f in files[:30])
            sys.exit(f"'{LANG}' naam wali md/text file nahi mili. Folder mein: {names}")
        t = texts[0]
        print("Script file:", t["name"])
        text = to_text(download(d, t))
        if len(text) < 50:
            sys.exit("Text file khali ya bahut chhoti hai")
        open("work/script.txt", "w", encoding="utf-8").write(text)

    if imgs:
        from PIL import Image
        print("Cover file:", imgs[0]["name"])
        im = Image.open(io.BytesIO(download(d, imgs[0]))).convert("RGB")
        # PDF ke pehle page ke liye (A5 portrait) - bina kate
        fit(im, 1480, 2100).save("work/cover.jpg", quality=90)
        # Gumroad cover/thumbnail ke liye (square, 600x600) - bina kate
        fit(im, 600, 600).save(f"covers/{LANG}.jpg", quality=92)
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


def md_to_book(text):
    """Markdown ko title + chapters mein todta hai."""
    text = re.sub(r"```.*?```", "", text.replace("\r", ""), flags=re.S)
    intro_title = {"hindi": "भूमिका", "english": "Introduction", "urdu": "تعارف"}[LANG]

    def inline(s):
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"__(.+?)__", r"\1", s)
        s = re.sub(r"\*(.+?)\*", r"\1", s)
        s = re.sub(r"`([^`]*)`", r"\1", s)
        return s.strip()

    title = None
    chapters = []
    pre = []
    buf = []

    def add(p):
        p = inline(p)
        if p:
            (chapters[-1]["paras"] if chapters else pre).append(p)

    def flush():
        if buf:
            add(" ".join(buf))
            del buf[:]

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line):
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", line)
        if m:
            flush()
            lvl, h = len(m.group(1)), inline(m.group(2))
            if title is None and lvl == 1 and not chapters:
                title = h
            elif lvl <= 2:
                chapters.append({"title": h, "paras": []})
            else:
                add(h)
            continue
        if line.startswith("|"):
            flush()
            if re.match(r"^[\s:\-|]+$", line):
                continue
            cells = [inline(c) for c in line.strip("|").split("|")]
            add(" — ".join(c for c in cells if c))
            continue
        m = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            flush()
            mark = "•" if m.group(1) in ("-", "*", "+") else m.group(1)
            add(mark + " " + m.group(2))
            continue
        buf.append(re.sub(r"^>\s?", "", line))
    flush()

    if title is None:
        if chapters:
            first = chapters.pop(0)
            title = first["title"]
            pre.extend(first["paras"])
        elif pre:
            title = pre.pop(0)[:120]
        else:
            sys.exit("md file mein kuch likha hua nahi mila")
    subtitle = ""
    if pre and chapters and len(" ".join(pre)) < 200:
        subtitle = " ".join(pre)
        pre = []
    if not chapters:
        chapters = [{"title": title, "paras": pre}]
    elif pre:
        chapters.insert(0, {"title": intro_title, "paras": pre})
    out = [{"title": c["title"], "text": "\n\n".join(c["paras"])} for c in chapters if c["paras"]]
    if not out:
        sys.exit("md file mein chapters ka text nahi mila")
    return {"title": title, "subtitle": subtitle, "description": "", "keywords": [], "chapters": out}


def write():
    if os.path.exists("work/ebook.md"):
        book = md_to_book(open("work/ebook.md", encoding="utf-8").read())
        body = " ".join(c["text"] for c in book["chapters"])[:3000]
        book["description"] = body[:300]
        try:
            out = groq([
                {"role": "system", "content": "You are an eBook marketer. Reply only with JSON."},
                {"role": "user", "content":
                    f"eBook title: {book['title']}\nLanguage: {LANG_NAME}\n"
                    'Return JSON: {"subtitle": "short subtitle", "description": "2-3 sentence sales description", '
                    '"keywords": ["7 short search keywords"]}. All text in the same language as the eBook.\n\n'
                    "EBOOK TEXT:\n" + body}
            ], max_tokens=1500, json_mode=True)
            meta = parse_json(out)
            book["description"] = clean(meta.get("description", "")) or book["description"]
            if not book["subtitle"]:
                book["subtitle"] = clean(meta.get("subtitle", ""))
            book["keywords"] = [clean(str(k)) for k in meta.get("keywords", [])][:7]
        except (Exception, SystemExit) as ex:
            print("Description/keywords Groq se nahi bane, simple wala use kiya:", ex)
        print("md se eBook tayyar:", book["title"], "-", len(book["chapters"]), "chapters")
        json.dump(book, open("work/book.json", "w", encoding="utf-8"), ensure_ascii=False)
        return

    # md file na ho to purana tarika: script se AI eBook likhta hai
    script = open("work/script.txt", encoding="utf-8").read()[:6000]
    out = groq([
        {"role": "system", "content": "You are a professional eBook author. Reply only with JSON."},
        {"role": "user", "content":
            f"Below is a video script. Plan an eBook on the same topic, written in {LANG_NAME}.\n"
            'Return JSON: {"title": "...", "subtitle": "...", "description": "2-3 sentence sales description", '
            '"keywords": ["7 short search keywords"], '
            '"chapters": ["8 chapter titles"]}. All text in the target language.\n\nSCRIPT:\n' + script}
    ], max_tokens=2500, json_mode=True)
    plan = parse_json(out)
    chapters = plan["chapters"][:8]
    book = {"title": clean(plan["title"]), "subtitle": clean(plan.get("subtitle", "")),
            "description": clean(plan.get("description", "")),
            "keywords": [clean(str(k)) for k in plan.get("keywords", [])][:7],
            "chapters": []}
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
    try:
        epub()
    except Exception as ex:
        print("EPUB nahi bani (Gumroad upload par asar nahi):", ex)


def epub():
    """KDP aur Google Play Books ke liye EPUB + cover + publish-info banata hai (covers/ folder mein)."""
    from PIL import Image
    book = json.load(open("work/book.json", encoding="utf-8"))
    e = html.escape
    rtl = LANG == "urdu"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    has_cover = os.path.exists("work/cover.jpg")
    kdp_cover = f"covers/{LANG}-kdp-cover.jpg"
    if has_cover:
        im = Image.open("work/cover.jpg").convert("RGB")
        fit(im, 1600, 2560).save(kdp_cover, quality=92)

    css = ("body{font-family:serif;line-height:1.7;direction:%s;} "
           "h1,h2{text-align:center;} p{margin:0 0 .8em 0;}" % ("rtl" if rtl else "ltr"))

    def page(title, body):
        return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
                f'lang="{LANG_CODE}" xml:lang="{LANG_CODE}"><head><meta charset="utf-8"/>'
                f'<title>{e(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/>'
                f'</head><body>{body}</body></html>')

    pages = [("title", book["title"],
              f"<h1>{e(book['title'])}</h1><p style='text-align:center'>{e(book['subtitle'])}</p>")]
    for i, c in enumerate(book["chapters"], 1):
        body = f"<h2>{e(c['title'])}</h2>"
        for p in re.split(r"\n\s*\n", c["text"]):
            if p.strip():
                body += f"<p>{e(p.strip())}</p>"
        pages.append((f"ch{i}", c["title"], body))

    nav_items = "".join(f'<li><a href="{pid}.xhtml">{e(t)}</a></li>' for pid, t, _ in pages)
    nav = ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
           f'lang="{LANG_CODE}" xml:lang="{LANG_CODE}"><head><meta charset="utf-8"/><title>Contents</title></head>'
           f'<body><nav epub:type="toc" id="toc"><ol>{nav_items}</ol></nav></body></html>')

    manifest = ('<item id="css" href="style.css" media-type="text/css"/>'
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    if has_cover:
        manifest += '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
    manifest += "".join(f'<item id="{pid}" href="{pid}.xhtml" media-type="application/xhtml+xml"/>'
                        for pid, _, _ in pages)
    spine = '<itemref idref="nav" linear="no"/>' + "".join(f'<itemref idref="{pid}"/>' for pid, _, _ in pages)
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="bookid">urn:uuid:{uuid.uuid4()}</dc:identifier>'
           f'<dc:title>{e(book["title"])}</dc:title><dc:language>{LANG_CODE}</dc:language>'
           f'<dc:creator>Dr. Sabir Hussain</dc:creator>'
           f'<dc:description>{e(book["description"])}</dc:description>'
           f'<meta property="dcterms:modified">{now}</meta></metadata>'
           f'<manifest>{manifest}</manifest>'
           f'<spine page-progression-direction="{"rtl" if rtl else "ltr"}">{spine}</spine></package>')
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')

    out = f"covers/{LANG}.epub"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", css, compress_type=zipfile.ZIP_DEFLATED)
        for pid, t, body in pages:
            z.writestr(f"OEBPS/{pid}.xhtml", page(t, body), compress_type=zipfile.ZIP_DEFLATED)
        if has_cover:
            z.write(kdp_cover, "OEBPS/cover.jpg", compress_type=zipfile.ZIP_DEFLATED)

    kdp_note = ("KDP par upload KARO (Hindi/English eBook)" if LANG != "urdu"
                else "KDP Urdu support nahi karta - sirf Google Play Books par daalo")
    info = (f"TITLE: {book['title']}\nSUBTITLE: {book['subtitle']}\nLANGUAGE: {LANG_NAME}\n"
            f"KEYWORDS: {', '.join(book.get('keywords', []))}\n\nDESCRIPTION:\n{book['description']}\n\n"
            f"FILES: {LANG}.epub (book), {LANG}-kdp-cover.jpg (cover 1600x2560)\n"
            f"KDP: {kdp_note}\nGoogle Play Books: upload KARO\n")
    open(f"covers/{LANG}-publish-info.txt", "w", encoding="utf-8").write(info)
    print("EPUB + publish-info ban gayi: covers/ folder dekho")


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
