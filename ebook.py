import os, sys, io, json, re, time, html, zipfile, uuid, shutil, requests

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


def download(d, f, md=False):
    from googleapiclient.http import MediaIoBaseDownload
    if f["mimeType"] == "application/vnd.google-apps.document":
        if md:
            try:
                return d.files().export_media(fileId=f["id"], mimeType="text/markdown").execute()
            except Exception:
                pass
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


def list_all_images(d):
    out, token = [], None
    while True:
        r = d.files().list(
            q="mimeType contains 'image/' and trashed=false",
            fields="nextPageToken,files(id,name,mimeType,modifiedTime)",
            pageSize=1000, pageToken=token).execute()
        out += r["files"]
        token = r.get("nextPageToken")
        if not token:
            break
    out.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return out


def fetch():
    from PIL import Image
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
    old_imgs = [f for f in mine if f["mimeType"].startswith("image/")]

    if mds:
        t = mds[0]
        print("eBook file (md):", t["name"])
        text = to_text(download(d, t, md=True))
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

    # Cover aur chapter images: kisi bhi shared folder se (jaise 11min/images)
    cover_f, chs = None, {}
    for f in list_all_images(d):
        nm = f["name"].lower()
        if cover_f is None and re.match(rf"^{LANG}_cover\.(jpg|jpeg|png|webp)$", nm):
            cover_f = f
        m = re.match(rf"^{LANG}_ch(\d+)\.(jpg|jpeg|png|webp)$", nm)
        if m and int(m.group(1)) not in chs:
            chs[int(m.group(1))] = f
    if cover_f is None and old_imgs:
        cover_f = old_imgs[0]

    if cover_f:
        print("Cover file:", cover_f["name"])
        im = Image.open(io.BytesIO(download(d, cover_f))).convert("RGB")
        fit(im, 1480, 2100).save("work/cover.jpg", quality=90)
        fit(im, 600, 600).save(f"covers/{LANG}.jpg", quality=92)
    else:
        print("WARNING: cover image nahi mili ({}_cover.jpg), bina cover ke banega".format(LANG))

    for n, f in sorted(chs.items()):
        try:
            im = Image.open(io.BytesIO(download(d, f))).convert("RGB")
            im.thumbnail((1200, 1200))
            im.save(f"work/img_ch{n}.jpg", quality=85)
        except Exception as ex:
            print("Chapter image nahi chali:", f["name"], ex)
    if chs:
        print("Chapter images mili:", sorted(chs))
    else:
        print("WARNING: chapter images nahi mili ({}_ch1.jpg ...). images folder service account ko share karo.".format(LANG))


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


SKIP_TITLES = ("विषय-सूची", "विषय सूची", "विषय-सुची", "table of contents", "contents",
               "فہرست", "فہرستِ مضامین", "مضامین")
SKIP_LINES = ("समाप्त", "the end", "end", "ختم", "اختتام")
CHAPTER_RE = re.compile(r"^(अध्याय|chapter|باب|فصل)\s*[\d०-९]+\s*[:.\-–—]?", re.I)


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
    skipping = False

    def add(p):
        p = inline(p)
        if p and not skipping:
            (chapters[-1]["paras"] if chapters else pre).append(p)

    def flush():
        if buf:
            add(" ".join(buf))
            del buf[:]

    def is_skip_title(h):
        return h.strip().strip(":").lower() in SKIP_TITLES

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line):
            flush()
            continue
        if inline(line).strip(" .।").lower() in SKIP_LINES:
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", line)
        h = None
        lvl = 2
        if m:
            lvl, h = len(m.group(1)), inline(m.group(2))
        elif CHAPTER_RE.match(inline(line)) and len(line) < 200:
            h = inline(line)
        if h is not None:
            flush()
            if title is None and lvl == 1 and not chapters:
                title = h
                skipping = False
            elif lvl <= 2:
                if is_skip_title(h):
                    skipping = True
                else:
                    skipping = False
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
    # title ke baad ki chhoti line (jaise author naam) ko chhod dete hain
    if chapters and pre and len(" ".join(pre)) < 120:
        pre = []
    if not chapters:
        chapters = [{"title": title, "paras": pre}]
    elif pre:
        chapters.insert(0, {"title": intro_title, "paras": pre, "intro": True})
    out = []
    n = 0
    for c in chapters:
        if not c["paras"]:
            continue
        item = {"title": c["title"], "text": "\n\n".join(c["paras"]), "n": 0}
        if not c.get("intro"):
            n += 1
            item["n"] = n  # chapter number = image number (ch1, ch2, ...)
        out.append(item)
    if not out:
        sys.exit("md file mein chapters ka text nahi mila")
    return {"title": title, "subtitle": "", "description": "", "keywords": [], "chapters": out}


def write():
    if os.path.exists("work/ebook.md"):
        book = md_to_book(open("work/ebook.md", encoding="utf-8").read())
        old_title = book["title"]
        body = " ".join(c["text"] for c in book["chapters"])[:4000]
        chap_list = "; ".join(c["title"] for c in book["chapters"])
        book["description"] = body[:300]
        try:
            out = groq([
                {"role": "system", "content": "You are an expert eBook marketer and SEO writer. Reply only with JSON."},
                {"role": "user", "content":
                    f"Language of the eBook: {LANG_NAME}. Current working title: {old_title}\n"
                    f"Chapters: {chap_list}\n"
                    "Write a strong, searchable, benefit-focused SEO title for this eBook (max 70 characters), "
                    "a short subtitle, a 3-4 sentence sales description, and 7 search keywords. "
                    'Return JSON: {"title": "...", "subtitle": "...", "description": "...", "keywords": ["..."]}. '
                    "All text must be in the same language as the eBook.\n\nEBOOK TEXT:\n" + body}
            ], max_tokens=1800, json_mode=True)
            meta = parse_json(out)
            book["title"] = clean(meta.get("title", "")) or old_title
            book["subtitle"] = clean(meta.get("subtitle", ""))
            book["description"] = clean(meta.get("description", "")) or book["description"]
            book["keywords"] = [clean(str(k)) for k in meta.get("keywords", [])][:7]
        except (Exception, SystemExit) as ex:
            print("SEO Groq se nahi bana, file ka title use kiya:", ex)
        print("eBook title:", book["title"], "-", len(book["chapters"]), "chapters")
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
        book["chapters"].append({"title": clean(ct), "text": clean(body), "n": i})
        time.sleep(3)
    json.dump(book, open("work/book.json", "w", encoding="utf-8"), ensure_ascii=False)


def chap_img(c):
    n = c.get("n", 0)
    p = f"work/img_ch{n}.jpg"
    return p if n and os.path.exists(p) else None


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
.ci {{ display: block; width: 100%; max-height: 85mm; object-fit: contain; margin: 0 auto 5mm auto; }}
</style></head><body>{cover}
<div class="title"><h1>{e(book['title'])}</h1><p>{e(book['subtitle'])}</p></div>"""]
    for c in book["chapters"]:
        parts.append(f"<h2>{e(c['title'])}</h2>")
        ip = chap_img(c)
        if ip:
            parts.append(f'<img class="ci" src="file://{os.path.abspath(ip)}">')
        for p in re.split(r"\n\s*\n", c["text"]):
            if p.strip():
                parts.append(f"<p>{e(p.strip())}</p>")
    parts.append("</body></html>")
    HTML(string="".join(parts), base_url=".").write_pdf("work/ebook.pdf")
    print("PDF ban gayi:", os.path.getsize("work/ebook.pdf"), "bytes")
    # KDP / Google Play ke liye PDF ki copy covers/ folder mein
    try:
        shutil.copyfile("work/ebook.pdf", f"covers/{LANG}.pdf")
        print(f"PDF covers/{LANG}.pdf mein bhi save ki")
    except Exception as ex:
        print("PDF covers/ mein copy nahi hui:", ex)
    try:
        epub()
    except Exception as ex:
        print("EPUB nahi bani (Gumroad par sirf PDF jayegi):", ex)


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

    css = ("body{font-family:serif;line-height:1.7;direction:DIR;} "
           "h1,h2{text-align:center;} p{margin:0 0 .8em 0;} "
           "img{max-width:100%;height:auto;display:block;margin:0 auto 1em auto;}"
           ).replace("DIR", "rtl" if rtl else "ltr")

    def page(title, body):
        return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
                f'lang="{LANG_CODE}" xml:lang="{LANG_CODE}"><head><meta charset="utf-8"/>'
                f'<title>{e(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/>'
                f'</head><body>{body}</body></html>')

    pages = [("title", book["title"],
              f"<h1>{e(book['title'])}</h1><p style='text-align:center'>{e(book['subtitle'])}</p>")]
    imgs_used = []
    for i, c in enumerate(book["chapters"], 1):
        body = f"<h2>{e(c['title'])}</h2>"
        ip = chap_img(c)
        if ip:
            body += f'<img src="img_ch{c["n"]}.jpg" alt=""/>'
            imgs_used.append((c["n"], ip))
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
    manifest += "".join(f'<item id="img{n}" href="img_ch{n}.jpg" media-type="image/jpeg"/>' for n, _ in imgs_used)
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
        for n, ip in imgs_used:
            z.write(ip, f"OEBPS/img_ch{n}.jpg", compress_type=zipfile.ZIP_DEFLATED)
        if has_cover:
            z.write(kdp_cover, "OEBPS/cover.jpg", compress_type=zipfile.ZIP_DEFLATED)

    kdp_note = ("KDP par upload KARO (Hindi/English eBook)" if LANG != "urdu"
                else "KDP Urdu support nahi karta - sirf Google Play Books par daalo")
    info = (f"TITLE: {book['title']}\nSUBTITLE: {book['subtitle']}\nLANGUAGE: {LANG_NAME}\n"
            f"KEYWORDS: {', '.join(book.get('keywords', []))}\n\nDESCRIPTION:\n{book['description']}\n\n"
            f"FILES: {LANG}.epub (book), {LANG}-kdp-cover.jpg (cover 1600x2560)\n"
            f"KDP: {kdp_note}\nGoogle Play Books: upload KARO\n")
    open(f"covers/{LANG}-publish-info.txt", "w", encoding="utf-8").write(info)
    print("EPUB + publish-info ban gayi (chapter images:", len(imgs_used), ") covers/ folder dekho")

    # KDP aur Google Books ke alag-alag complete SEO files (fail ho to baaki kaam nahi rukta)
    try:
        make_seo_packs(book)
    except BaseException as ex:  # groq() sys.exit bhi kar sakta hai
        print("SEO files nahi bani (ePub aur PDF theek hain):", ex)


# ---------------------------------------------------------------------------
# KDP aur Google Play Books ke liye alag-alag complete SEO
# ---------------------------------------------------------------------------
def _clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n].rstrip()


def _html_to_text(h):
    h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = html.unescape(h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _ai_platform_meta(book):
    chap = "; ".join(c["title"] for c in book["chapters"])[:1500]
    body = " ".join(c["text"] for c in book["chapters"])[:3500]
    out = groq([
        {"role": "system",
         "content": "You are an expert Amazon KDP and Google Play Books SEO specialist. Reply only with JSON."},
        {"role": "user", "content":
            f"eBook language: {LANG_NAME}. Title: {book['title']}. Subtitle: {book.get('subtitle', '')}.\n"
            f"Chapters: {chap}\n"
            "Return JSON with these keys:\n"
            "kdp_keywords: exactly 7 search phrases a reader would type, each max 50 characters, "
            "in the eBook's language.\n"
            "categories: 3 Amazon KDP category paths in English, like 'Business & Money > Personal Finance'.\n"
            "bisac: 3 BISAC subject heading names in English, like "
            "'BUSINESS & ECONOMICS / Personal Finance / General'.\n"
            "short_description: one hook sentence, max 200 characters, in the eBook's language.\n"
            "description_html: sales description, max 3000 characters, in the eBook's language, "
            "using only <p>, <b>, <i>, <ul>, <li>, <br> tags. Start with a strong hook, list the key "
            "benefits as bullets, end with a call to action.\n"
            "Do not invent facts about the author.\n\nEBOOK TEXT:\n" + body}
    ], max_tokens=2500, json_mode=True)
    return parse_json(out)


def _patch_epub(path, keywords):
    """ePub ke andar SEO + accessibility metadata aur image alt text jodta hai."""
    if not os.path.exists(path):
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    subjects = "".join(f"<dc:subject>{html.escape(k)}</dc:subject>" for k in keywords)
    dc_add = subjects + "<dc:publisher>Dr. Sabir Hussain</dc:publisher>" + f"<dc:date>{today}</dc:date>"
    a11y = ('<meta property="schema:accessMode">textual</meta>'
            '<meta property="schema:accessModeSufficient">textual</meta>'
            '<meta property="schema:accessibilityFeature">tableOfContents</meta>'
            '<meta property="schema:accessibilityFeature">structuralNavigation</meta>'
            '<meta property="schema:accessibilityHazard">none</meta>'
            '<meta property="schema:accessibilitySummary">Text-based eBook with a navigable table of '
            'contents and chapter headings. Images are decorative illustrations.</meta>')
    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            name = item.filename
            if name == "OEBPS/content.opf":
                s = data.decode("utf-8")
                if "<dc:publisher>" not in s:
                    marker = '<meta property="dcterms:modified">'
                    if marker in s and "</metadata>" in s:
                        s = s.replace(marker, dc_add + marker, 1)
                        s = s.replace("</metadata>", a11y + "</metadata>", 1)
                data = s.encode("utf-8")
            elif re.match(r"^OEBPS/ch\d+\.xhtml$", name):
                s = data.decode("utf-8")
                m = re.search(r"<h2>(.*?)</h2>", s, re.S)
                if m and 'alt=""' in s:
                    s = s.replace('alt=""', f'alt="{m.group(1)}"')
                data = s.encode("utf-8")
            zout.writestr(item, data, compress_type=item.compress_type)
    os.replace(tmp, path)
    return True


def make_seo_packs(book):
    title = _clip(book["title"], 200)
    subtitle = _clip(book.get("subtitle", ""), 200)

    meta = {}
    try:
        meta = _ai_platform_meta(book)
    except BaseException as ex:
        print("SEO ka AI hissa nahi bana, book ke apne keywords use kiye:", ex)

    keywords = [_clip(k, 50) for k in (meta.get("kdp_keywords") or book.get("keywords") or [])
                if str(k).strip()][:7]
    cats = [_clip(c, 120) for c in (meta.get("categories") or [])][:3]
    bisac = [_clip(c, 120) for c in (meta.get("bisac") or [])][:3]
    short = _clip(meta.get("short_description") or book.get("description", ""), 200)
    desc_html = (meta.get("description_html") or "").strip()
    if not desc_html:
        desc_html = "<p>" + html.escape(book.get("description", "")) + "</p>"
    desc_html = desc_html[:3900]
    desc_text = _html_to_text(desc_html)

    try:
        ok = _patch_epub(f"covers/{LANG}.epub", keywords)
        print("ePub mein SEO metadata jodi:", ok)
    except BaseException as ex:
        print("ePub metadata nahi judi (ePub waisi hi rahi):", ex)

    def numbered(items):
        return "\n".join(f"  {i}. {x}" for i, x in enumerate(items, 1)) or "  (nahi bana)"

    kdp_lang_note = ("Urdu KDP par shayad na dikhe. Language dropdown mein Urdu na mile to KDP chhod do, "
                     "sirf Google Play Books par daalo." if LANG == "urdu"
                     else "Language dropdown mein apni language dikhe tabhi KDP par daalo; na dikhe to sirf Google Play Books.")

    kdp = f"""=== AMAZON KDP SEO: {title} ({LANG_NAME}) ===
Website: kdp.amazon.com > Create > Kindle eBook
Files (GitHub repo ke covers/ folder se):
  Manuscript : {LANG}.epub
  Cover      : {LANG}-kdp-cover.jpg  (1600x2560)
Har field mein neeche ka text seedha copy-paste karo.

{kdp_lang_note}

Title      : {title}
Subtitle   : {subtitle}
Author     : Dr. Sabir Hussain
Publishing rights: I own the copyright
AI-generated content: agar text ya images AI se bane hain to "Yes" chuno (KDP upload mein ye sawal aata hai, sach jawab do)

Keywords (7 alag-alag boxes mein, ek ek karke):
{numbered(keywords)}

Categories (3 tak, KDP ke dropdown mein sabse milta-julta chuno):
{numbered(cats)}

Description (HTML sahit paste karo):
{desc_html}

KDP Select : ENROLL MAT KARO (Select mein ebook sirf Amazon par rakhni padti hai, aapki Gumroad par bhi hai)
Price      : KDP 70% royalty tabhi deta hai jab price $2.99 se $9.99 ke beech ho. Sab jagah ek hi price rakhna safe hai.

Upload se pehle checks:
  1. ePub kholkar dekho: title, chapters aur contents list theek dikh rahi hai?
  2. Cover par title saaf padha ja raha hai?
  3. Gumroad, KDP aur Google Play par title aur description same hain.
"""

    google = f"""=== GOOGLE PLAY BOOKS SEO: {title} ({LANG_NAME}) ===
Website: play.google.com/books/publish > Book catalog
Files (GitHub repo ke covers/ folder se):
  Book  : {LANG}.epub  (ya {LANG}.pdf)
  Cover : {LANG}-kdp-cover.jpg  (ya {LANG}.jpg)
Har field mein neeche ka text seedha copy-paste karo.

Title      : {title}
Subtitle   : {subtitle}
Author     : Dr. Sabir Hussain
Publisher  : Dr. Sabir Hussain
Language   : {LANG_NAME}

Subjects (BISAC, dropdown mein sabse milta-julta chuno):
{numbered(bisac)}

Short hook (preview aur social share ke liye):
{short}

Description (plain text, seedha paste karo):
{desc_text}

Search ke liye keywords (Google Books mein alag keyword box nahi hota, inme se kuch words description mein rakhna behtar hai):
{numbered(keywords)}

Upload se pehle checks:
  1. ePub kholkar dekho: title, chapters aur contents list theek dikh rahi hai?
  2. Cover par title saaf padha ja raha hai?
  3. Gumroad, KDP aur Google Play par title aur description same hain.
"""
    open(f"covers/{LANG}-kdp-seo.txt", "w", encoding="utf-8").write(kdp)
    open(f"covers/{LANG}-google-books-seo.txt", "w", encoding="utf-8").write(google)
    print(f"SEO files ban gayi: covers/{LANG}-kdp-seo.txt aur covers/{LANG}-google-books-seo.txt")


def gum_file(tok, api, path, filename):
    """Ek file Gumroad par chadhata hai, uska file_url lautata hai."""
    size = os.path.getsize(path)
    r = requests.post(api + "/files/presign", data={
        "access_token": tok, "filename": filename, "file_size": size}, timeout=60).json()
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
    print("File complete:", filename, "->", c.get("success"), "" if c.get("success") else c)
    return c.get("file_url") or r["file_url"]


def upload():
    """Gumroad par POORA kaam karta hai. Kuch adhoora rah jaye to product hata deta hai aur run ko FAIL karta hai
    (taaki 'Ho gaya' ka jhootha sandesh na aaye aur adhoore drafts jama na hon)."""
    tok = os.environ["GUMROAD_TOKEN"]
    api = "https://api.gumroad.com/v2"
    book = json.load(open("work/book.json", encoding="utf-8"))
    title = book["title"][:200]

    def abort(msg, pid=None):
        if pid:
            try:
                d = requests.delete(f"{api}/products/{pid}", params={"access_token": tok}, timeout=60).json()
                print("Adhoora product hata diya:", pid, d.get("success"))
            except Exception as ex:
                print("WARNING: adhoora product hata nahi paya, Gumroad mein haath se delete karo:", pid, ex)
        sys.exit("ADHOORA, isliye run fail kiya: " + msg)

    def get_prod(pid):
        r = requests.get(f"{api}/products/{pid}", params={"access_token": tok}, timeout=60).json()
        return r.get("product") or {}

    def up(path, name):
        last = None
        for i in range(3):
            try:
                return gum_file(tok, api, path, name)
            except BaseException as ex:  # gum_file sys.exit bhi karta hai
                last = ex
                print(f"{name} upload fail, dobara koshish ({i + 1}/3):", ex)
                time.sleep(5)
        raise RuntimeError(f"{name} Gumroad par upload nahi ho payi: {last}")

    # 0) Isi naam ka purana adhoora draft ho to hata do (duplicate na bane)
    try:
        lst = requests.get(f"{api}/products", params={"access_token": tok}, timeout=60).json()
        for p in lst.get("products", []):
            if p.get("name") == title:
                if p.get("published"):
                    sys.exit("Is naam ka product Gumroad par pehle se PUBLISHED hai, dobara nahi banaya. "
                             "Naya version chahiye to Gumroad mein purana hata do.")
                d = requests.delete(f"{api}/products/{p['id']}", params={"access_token": tok}, timeout=60).json()
                print("Purana draft hataya:", p["id"], d.get("success"))
    except Exception as ex:
        print("Purane drafts check nahi hue:", ex)

    # 1) Files (PDF zaroori, ePub bhi agar bani hai)
    urls = []
    try:
        urls.append(up("work/ebook.pdf", f"{LANG}-ebook.pdf"))
        if os.path.exists(f"covers/{LANG}.epub"):
            urls.append(up(f"covers/{LANG}.epub", f"{LANG}-ebook.epub"))
    except RuntimeError as ex:
        abort(str(ex))
    print("Files ready:", len(urls))

    # 2) Product (SEO title, subtitle, description, chapters, keywords)
    chapters = "".join("<li>" + html.escape(c["title"]) + "</li>" for c in book["chapters"])
    desc = ("<p><strong>" + html.escape(book["subtitle"]) + "</strong></p>"
            "<p>" + html.escape(book["description"]) + "</p>"
            "<ul>" + chapters + "</ul>")
    if book.get("keywords"):
        desc += "<p>" + html.escape(", ".join(book["keywords"])) + "</p>"

    prod = None
    for ntype in ("ebook", "digital"):
        data = [("access_token", tok), ("native_type", ntype), ("name", title),
                ("description", desc), ("price", PRICE_CENTS), ("price_currency_type", "usd")]
        data += [("files[][url]", u) for u in urls]
        pr = requests.post(f"{api}/products", data=data, timeout=60).json()
        if pr.get("success"):
            prod = pr["product"]
            break
        print("Product create fail:", ntype, pr)
    if not prod:
        abort("Gumroad product nahi bana. Upar ka error dekho.")
    pid = prod["id"]
    print("Product bana:", pid, "| title:", title)

    # 3) Check: files sach mein judi? Na judi to dobara jodo, phir bhi na judi to product hata do
    ok_files = False
    for attempt in range(3):
        time.sleep(3)
        files = get_prod(pid).get("files") or []
        if len(files) >= len(urls):
            ok_files = True
            break
        print(f"Product mein files {len(files)}/{len(urls)} hain, dobara jod rahe hain...")
        ur = requests.put(f"{api}/products/{pid}",
                          data=[("access_token", tok)] + [("files[][url]", u) for u in urls], timeout=60).json()
        print("Files update:", ur.get("success"), "" if ur.get("success") else ur)
    if not ok_files:
        abort("Files Gumroad product mein nahi judi.", pid)
    print("Files judi:", len(urls))

    # 4) Cover (zaroori) aur Thumbnail (alag cheez hai)
    sq = f"covers/{LANG}.jpg"
    if not os.path.exists(sq):
        abort(f"{sq} nahi mili (Drive mein {LANG}.jpg / {LANG}_cover.jpg cover image check karo).", pid)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = f"https://raw.githubusercontent.com/{repo}/main/covers/{LANG}.jpg"
    cover_ok = False
    for attempt in range(4):
        time.sleep(8 if attempt == 0 else 20)  # GitHub par nayi file dikhne mein thoda time lagta hai
        cr = requests.post(f"{api}/products/{pid}/covers", data={"access_token": tok, "url": url}, timeout=60).json()
        print("Cover:", cr.get("success"), "" if cr.get("success") else cr)
        if cr.get("success"):
            cover_ok = True
            break
    if not cover_ok:
        abort("Cover Gumroad par nahi laga.", pid)
    thumb_ok = False
    for attempt in range(3):
        try:
            tr = requests.post(f"{api}/products/{pid}/thumbnail",
                               data={"access_token": tok, "url": url}, timeout=60).json()
            print("Thumbnail:", tr.get("success"), "" if tr.get("success") else tr)
            if tr.get("success"):
                thumb_ok = True
                break
        except Exception as ex:
            print("Thumbnail set nahi hua:", ex)
        time.sleep(15)

    if CTA:
        try:
            ur = requests.put(f"{api}/products/{pid}", data={
                "access_token": tok, "call_to_action": CTA}, timeout=60).json()
            print("Call to action:", ur.get("success"), "" if ur.get("success") else ur)
        except Exception as ex:
            print("Call to action set nahi hua:", ex)

    # 5) Publish ya draft
    if os.environ.get("EB_PUBLISH", "false").lower() == "true":
        er = requests.put(f"{api}/products/{pid}/enable", data={"access_token": tok}, timeout=60).json()
        print("Publish:", er.get("success"))
        state = "PUBLISHED" if er.get("success") else "publish NAHI hua, Gumroad mein jaake Publish dabao"
    else:
        dr = requests.put(f"{api}/products/{pid}/disable", data={"access_token": tok}, timeout=60).json()
        if dr.get("success"):
            state = "DRAFT (Gumroad mein jaake dekho aur Publish karo)"
        else:
            state = "WARNING: draft nahi ban paya, product LIVE ho sakta hai! Gumroad mein jaake Unpublish karo"

    print("=" * 44)
    print("GUMROAD REPORT:", title)
    print("  Files (PDF + ePub) :", "OK" if ok_files else "NAHI")
    print("  Cover              : OK")
    print("  Thumbnail          :", "OK" if thumb_ok else "NAHI LAGA (Gumroad mein haath se Upload karo)")
    print("  Status             :", state)
    print("  Product link       :", prod.get("short_url"))
    print("=" * 44)


{"fetch": fetch, "write": write, "pdf": pdf, "upload": upload}[sys.argv[1]]()
