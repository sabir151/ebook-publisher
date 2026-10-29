"""KDP + Google Play Books ke liye complete SEO publish pack.

ebook.py ke epub() ke end se chalta hai. Ye 2 kaam karta hai:
1. covers/<lang>.epub ke andar SEO metadata (subjects, publisher, date, accessibility, image alt) jodta hai.
2. covers/<lang>-publish-pack.txt banata hai: KDP aur Google Play ke form mein seedha copy-paste karne layak sab kuch.

Kuch bhi fail ho to baaki setup (PDF, ePub, Gumroad) par koi asar nahi padta.
"""
import os, re, json, html, time, zipfile


def _clip(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n].rstrip()


def _parse_json(s):
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        return json.loads(m.group(0))


def _ai_meta(book, lang_name, groq):
    chap = "; ".join(c["title"] for c in book["chapters"])[:1500]
    body = " ".join(c["text"] for c in book["chapters"])[:3500]
    out = groq([
        {"role": "system",
         "content": "You are an expert Amazon KDP and Google Play Books SEO specialist. Reply only with JSON."},
        {"role": "user", "content":
            f"eBook language: {lang_name}. Title: {book['title']}. Subtitle: {book.get('subtitle', '')}.\n"
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
    return _parse_json(out)


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


def run(lang, lang_name, lang_code, groq):
    book = json.load(open("work/book.json", encoding="utf-8"))
    title = _clip(book["title"], 200)
    subtitle = _clip(book.get("subtitle", ""), 200)

    meta = {}
    try:
        meta = _ai_meta(book, lang_name, groq)
    except BaseException as ex:  # groq() sys.exit bhi kar sakta hai
        print("SEO pack ka AI hissa nahi bana, book ke apne keywords use kiye:", ex)

    keywords = [_clip(k, 50) for k in (meta.get("kdp_keywords") or book.get("keywords") or []) if str(k).strip()][:7]
    cats = [_clip(c, 120) for c in (meta.get("categories") or [])][:3]
    bisac = [_clip(c, 120) for c in (meta.get("bisac") or [])][:3]
    short = _clip(meta.get("short_description") or book.get("description", ""), 200)
    desc_html = (meta.get("description_html") or "").strip()
    if not desc_html:
        desc_html = "<p>" + html.escape(book.get("description", "")) + "</p>"
    desc_html = desc_html[:3900]

    epub_path = f"covers/{lang}.epub"
    try:
        ok = _patch_epub(epub_path, keywords)
        print("ePub mein SEO metadata jodi:", ok)
    except BaseException as ex:
        print("ePub metadata nahi judi (ePub waisi hi rahi):", ex)

    kdp_lang_note = ("Urdu KDP par shayad na dikhe. Language dropdown mein Urdu na mile to KDP chhod do, "
                     "sirf Google Play Books par daalo." if lang == "urdu"
                     else "Language dropdown mein apni language dikhe tabhi KDP par daalo; na dikhe to sirf Google Play Books.")

    def numbered(items):
        return "\n".join(f"  {i}. {x}" for i, x in enumerate(items, 1)) or "  (nahi bana)"

    pack = f"""=== PUBLISH PACK: {title} ({lang_name}) ===
Files (GitHub repo ke covers/ folder mein):
  Book  : {lang}.epub
  Cover : {lang}-kdp-cover.jpg  (1600x2560)
Har field mein neeche ka text seedha copy-paste karo.

------------------------------------------------------------
STEP 1: AMAZON KDP  (kdp.amazon.com > Create > Kindle eBook)
------------------------------------------------------------
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
Manuscript : {lang}.epub upload karo
Cover      : {lang}-kdp-cover.jpg upload karo
KDP Select : ENROLL MAT KARO (Select mein ebook sirf Amazon par rakhni padti hai, aapki Gumroad par bhi hai)
Price      : KDP 70% royalty tabhi deta hai jab price $2.99 se $9.99 ke beech ho. Sab jagah ek hi price rakhna safe hai.

------------------------------------------------------------
STEP 2: GOOGLE PLAY BOOKS  (play.google.com/books/publish)
------------------------------------------------------------
Title      : {title}
Subtitle   : {subtitle}
Author     : Dr. Sabir Hussain
Publisher  : Dr. Sabir Hussain
Language   : {lang_name}
Subjects (BISAC, dropdown mein sabse milta-julta chuno):
{numbered(bisac)}
Short hook (social/preview ke liye):
{short}
Description:
{desc_html}
Book file  : {lang}.epub upload karo
Cover      : {lang}-kdp-cover.jpg upload karo (ya {lang}.jpg)

------------------------------------------------------------
Upload se pehle 4 checks
------------------------------------------------------------
  1. ePub kholkar dekho: title, chapters aur contents list theek dikh rahi hai?
  2. Cover par title saaf padha ja raha hai?
  3. Description mein koi galat dava ya jhoothi baat to nahi?
  4. Gumroad, KDP aur Google Play par title aur description same hain.
"""
    open(f"covers/{lang}-publish-pack.txt", "w", encoding="utf-8").write(pack)
    print(f"Publish pack ban gaya: covers/{lang}-publish-pack.txt")


def safe_run(lang, lang_name, lang_code, groq):
    """ebook.py yahi chalata hai. Koi bhi error aaye to baaki setup nahi rukta."""
    try:
        run(lang, lang_name, lang_code, groq)
    except BaseException as ex:
        print("Publish pack nahi bana (baaki setup theek hai):", ex)
