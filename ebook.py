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
    out =
