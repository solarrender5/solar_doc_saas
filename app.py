import os, zipfile, io, base64, tempfile, subprocess, glob, uuid, requests, shutil, threading
from math import ceil
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for, flash, jsonify, send_file
from supabase import create_client
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Cm
from PIL import Image, ImageOps
from dotenv import load_dotenv
from functools import wraps

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

BASE_DIR       = os.path.abspath(os.path.dirname(__file__))
INPUT_DOCS_DIR = os.path.join(BASE_DIR, 'input_docs')
app.config['MAX_CONTENT_LENGTH']   = 50 * 1024 * 1024
app.config['MAX_FORM_MEMORY_SIZE'] = 50 * 1024 * 1024

PER_PAGE_ADMIN   = 10
PER_PAGE_HISTORY = 15

jobs, jobs_lock = {}, threading.Lock()

# ── Hardcoded admin contact & payment — set these in .env, never in DB ────────
# Required .env keys:
#   ADMIN_UPI_ID        e.g.  yourname@paytm
#   ADMIN_UPI_QR_URL    e.g.  https://... (hosted image URL, or leave blank)
#   ADMIN_PHONE         e.g.  9876543210
#   ADMIN_WHATSAPP      e.g.  9876543210  (digits only — used in wa.me link)
#   ADMIN_EMAIL         e.g.  admin@solardoc.in
ADMIN_INFO = {
    'upi_id':         os.getenv('ADMIN_UPI_ID',      ''),
    'upi_qr_url':     os.getenv('ADMIN_UPI_QR_URL',  ''),
    'admin_phone':    os.getenv('ADMIN_PHONE',        ''),
    'admin_whatsapp': os.getenv('ADMIN_WHATSAPP',     ''),
    'admin_email':    os.getenv('ADMIN_EMAIL',        ''),
}

# ── Image target sizes at 150 dpi (aspect-ratio locked) ──────────────────────
# Derived from docxtpl InlineImage dimensions:
#   signature  4.5 × 1.5 cm  → 266 × 89 px   ratio 3.00
#   aadhar    11.0 × 7.0 cm  → 650 × 413 px  ratio 1.57
#   stamp      6.5 × 2.5 cm  → 384 × 148 px  ratio 2.60
#   logo       4.5 × 4.5 cm  → 266 × 266 px  ratio 1.00
IMG_TARGETS = {
    'consumer_signature_image': (266,  89),
    'consumer_aadhar_image':    (650, 413),
    'agency_stamp_image':       (384, 148),
    'agency_logo_image':        (266, 266),
}

# ── Pre-loaded DocxTemplate cache (loaded once at startup) ───────────────────
_TEMPLATE_CACHE: dict[str, bytes] = {}   # fname → raw docx bytes

# ── Decorators ────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if session.get('user', {}).get('role') != 'admin':
            flash("Access denied.", "danger")
            return redirect(url_for('agency_dashboard'))
        return f(*a, **kw)
    return d

# ── Helpers ───────────────────────────────────────────────────────────────────
def days_left(exp_str):
    if not exp_str:
        return None
    try:
        return (datetime.strptime(str(exp_str)[:10], '%Y-%m-%d').date() - datetime.now().date()).days
    except Exception:
        return None

def job_log(jid, msg, error=False):
    with jobs_lock:
        if jid in jobs:
            jobs[jid]['logs'].append({'msg': msg, 'error': error})

def _resize_to_target(img: Image.Image, target_wh: tuple[int,int]) -> Image.Image:
    """
    Resize `img` to fit exactly within target (w, h) while preserving aspect
    ratio, then paste onto a white canvas of exactly target_wh size.
    This guarantees the InlineImage in Word never stretches.
    """
    tw, th = target_wh
    img = img.convert('RGB')
    img.thumbnail((tw, th), Image.LANCZOS)   # shrink in-place, keep ratio
    canvas = Image.new('RGB', (tw, th), (255, 255, 255))
    ox = (tw - img.width)  // 2
    oy = (th - img.height) // 2
    canvas.paste(img, (ox, oy))
    return canvas

def process_b64_image(b64: str, key: str = '') -> bytes | None:
    """
    Decode base64, resize to the locked aspect-ratio target for `key`,
    and return JPEG bytes.  Quality 90 for sharp document reproduction.
    """
    if not b64:
        return None
    try:
        if ',' in b64:
            b64 = b64.split(',')[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        target = IMG_TARGETS.get(key)
        if target:
            img = _resize_to_target(img, target)
        else:
            img = img.convert('RGB')
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=90, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"Image error [{key}]: {e}")
        return None

def process_url_image(url: str, key: str = '') -> bytes | None:
    """Download branding image, resize to target, return JPEG bytes."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        target = IMG_TARGETS.get(key)
        if target:
            img = _resize_to_target(img, target)
        else:
            img = img.convert('RGB')
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=90, optimize=True)
        return out.getvalue()
    except Exception:
        return None

def fetch_url_bytes(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception:
        return None

def preload_templates():
    """
    Read each input .docx into memory once at startup.
    render_doc clones from these bytes, never touching disk per-job.
    """
    doc_names = [
        "commissioning_report.docx",
        "meter_testing.docx",
        "model_agreement.docx",
        "net_metering.docx",
        "work_completion.docx",
    ]
    loaded = 0
    for name in doc_names:
        path = os.path.join(INPUT_DOCS_DIR, name)
        if os.path.exists(path):
            with open(path, 'rb') as f:
                _TEMPLATE_CACHE[name] = f.read()
            loaded += 1
    print(f"[SolarDoc] Preloaded {loaded}/{len(doc_names)} templates into memory.")

def upload_image(b64, bucket):
    if not b64:
        return None
    try:
        raw  = base64.b64decode(b64.split(',')[1] if ',' in b64 else b64)
        path = f"branding/{uuid.uuid4()}.png"
        supabase.storage.from_(bucket).upload(path, raw, {"content-type": "image/png"})
        return supabase.storage.from_(bucket).get_public_url(path)
    except Exception as e:
        print(f"Upload error: {e}")
        return None

# ── Document generation ───────────────────────────────────────────────────────
def convert_pdf(docx_path: str, out_dir: str, jid: str):
    """Convert one DOCX → PDF using an isolated LibreOffice profile."""
    p = tempfile.mkdtemp()
    try:
        subprocess.run(
            ['libreoffice', f'-env:UserInstallation=file://{p}',
             '--headless', '--convert-to', 'pdf', '--outdir', out_dir, docx_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        job_log(jid, f"PDF: {os.path.basename(docx_path).replace('.docx','')}")
    except subprocess.TimeoutExpired:
        job_log(jid, f"PDF timeout: {os.path.basename(docx_path)}", error=True)
    finally:
        shutil.rmtree(p, ignore_errors=True)

def render_doc(doc_info, tmp: str, ctx: dict, imap: dict, jid: str,
               fmt: str, pdf_threads: list, pdf_lock: threading.Lock):
    """
    Fill one template from in-memory cache.
    Immediately spawns PDF conversion as soon as DOCX is saved —
    so DOCX rendering and PDF conversion overlap (pipeline parallelism).
    """
    fname, oname = doc_info
    try:
        job_log(jid, f"Filling {oname} ...")
        raw = _TEMPLATE_CACHE.get(fname)
        if raw is None:                          # cold-cache fallback
            src = os.path.join(INPUT_DOCS_DIR, fname)
            if not os.path.exists(src):
                job_log(jid, f"Missing template: {fname}", error=True)
                return
            with open(src, 'rb') as f:
                raw = f.read()

        doc = DocxTemplate(io.BytesIO(raw))      # clone from cached bytes — no disk I/O
        c   = ctx.copy()
        for k, v in imap.items():
            if v:
                s = io.BytesIO(v)
                if   'signature' in k: c[k] = InlineImage(doc, s, width=Cm(4.5), height=Cm(1.5))
                elif 'aadhar'    in k: c[k] = InlineImage(doc, s, width=Cm(11),  height=Cm(7))
                elif 'stamp'     in k: c[k] = InlineImage(doc, s, width=Cm(6.5), height=Cm(2.5))
                elif 'logo'      in k: c[k] = InlineImage(doc, s, width=Cm(4.5), height=Cm(4.5))
        doc.render(c)
        docx_out = os.path.join(tmp, f"{oname}.docx")
        doc.save(docx_out)
        job_log(jid, f"DOCX done: {oname}")

        # ── Immediately start PDF conversion for this doc ──────────
        if fmt in ('pdf', 'both'):
            t = threading.Thread(target=convert_pdf, args=(docx_out, tmp, jid), daemon=True)
            with pdf_lock:
                pdf_threads.append(t)
            t.start()

    except Exception as e:
        job_log(jid, f"Failed {oname}: {e}", error=True)

def run_job(jid, form_data, agency_id):
    def log(m, e=False): job_log(jid, m, e)
    try:
        log("Fetching profile ...")
        profile    = supabase.table('agencies').select('*').eq('id', agency_id).single().execute().data
        sig_b64    = form_data.pop('sig_b64', None)
        aadhar_b64 = form_data.pop('aadhar_b64', None)
        fmt        = form_data.pop('format', 'both')

        # ── 4 images in parallel, each pre-resized to locked aspect ratio ──
        log("Processing & resizing images ...")
        res = [None] * 4
        def fetch(i, fn): res[i] = fn()
        ts = [threading.Thread(target=fetch, args=(i, fn)) for i, fn in enumerate([
            lambda: process_b64_image(sig_b64,    'consumer_signature_image'),
            lambda: process_b64_image(aadhar_b64, 'consumer_aadhar_image'),
            lambda: process_url_image(profile.get('logo_url'),  'agency_logo_image'),
            lambda: process_url_image(profile.get('stamp_url'), 'agency_stamp_image'),
        ])]
        for t in ts: t.start()
        for t in ts: t.join()

        imap = {
            'consumer_signature_image': res[0], 'consumer_aadhar_image': res[1],
            'agency_logo_image':        res[2], 'agency_stamp_image':    res[3],
        }
        docs = [
            ("commissioning_report.docx", "1_Commissioning_Report"),
            ("meter_testing.docx",         "2_Meter_Testing"),
            ("model_agreement.docx",        "3_Model_Agreement"),
            ("net_metering.docx",           "4_Net_Metering"),
            ("work_completion.docx",         "5_Work_Completion"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base_ctx = {
                'agency_name':    profile.get('agency_name', ''),
                'agency_address': profile.get('agency_address', ''),
                'director_name':  profile.get('director_name', ''),
                'agency_contact': profile.get('contact_number', ''),
                'today_date':     datetime.now().strftime("%d-%m-%Y"),
                **form_data
            }

            # ── Pipeline: each render_doc fires its own PDF thread immediately ──
            pdf_threads: list = []
            pdf_lock = threading.Lock()

            log("Filling all 5 templates (parallel) ...")
            dts = [
                threading.Thread(
                    target=render_doc,
                    args=(d, tmp, base_ctx, imap, jid, fmt, pdf_threads, pdf_lock),
                    daemon=True)
                for d in docs
            ]
            for t in dts: t.start()
            for t in dts: t.join()           # wait for all DOCX done

            # wait for any still-running PDF conversions (already mostly done)
            if fmt in ('pdf', 'both'):
                log("Finishing PDF conversions ...")
                with pdf_lock:
                    running = list(pdf_threads)
                for t in running:
                    t.join()

            # ── ZIP built entirely in memory — never touches disk ──────────────
            log("Building ZIP ...")
            cn    = form_data.get('consumer_name', 'Client').replace(' ', '_')
            cno   = form_data.get('consumer_number', '0000')
            zname = f"{cn}_{cno}_{datetime.now().strftime('%d-%m-%Y_%H%M%S')}.zip"
            zbuf  = io.BytesIO()
            with zipfile.ZipFile(zbuf, 'w') as zf:
                for f in os.listdir(tmp):
                    full = os.path.join(tmp, f)
                    if fmt == 'docx' and f.endswith('.docx'):
                        zf.write(full, f, compress_type=zipfile.ZIP_DEFLATED)
                    elif fmt == 'pdf' and f.endswith('.pdf'):
                        zf.write(full, f, compress_type=zipfile.ZIP_STORED)
                    elif fmt == 'both' and (f.endswith('.docx') or f.endswith('.pdf')):
                        ct = zipfile.ZIP_DEFLATED if f.endswith('.docx') else zipfile.ZIP_STORED
                        zf.write(full, f, compress_type=ct)
            zbuf.seek(0)

            with jobs_lock:
                jobs[jid]['status']    = 'done'
                jobs[jid]['zip_name']  = zname           # filename for Content-Disposition
                jobs[jid]['zip_bytes'] = zbuf.getvalue() # raw bytes — served once, then deleted
            log("ZIP ready — downloading.")
    except Exception as e:
        job_log(jid, f"Fatal: {e}", error=True)
        with jobs_lock: jobs[jid]['status'] = 'error'

# ── Generation API ────────────────────────────────────────────────────────────
@app.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    aid = session['user']['id']
    fd  = request.get_json()

    # DB insert is fire-and-forget — never blocks the response
    def _db_insert():
        try:
            db = {k: v for k, v in fd.items() if k not in ['sig_b64', 'aadhar_b64', 'format']}
            supabase.table('generation_history').insert({**db, 'agency_id': aid}).execute()
        except Exception as e:
            print(f"History insert: {e}")
    threading.Thread(target=_db_insert, daemon=True).start()

    jid = str(uuid.uuid4())
    with jobs_lock:
        jobs[jid] = {'logs': [], 'status': 'running', 'download_url': None}
    def go():
        with app.app_context(): run_job(jid, dict(fd), aid)
    threading.Thread(target=go, daemon=True).start()
    return jsonify({'job_id': jid})

@app.route('/api/job/<jid>/status')
@login_required
def api_job_status(jid):
    since = int(request.args.get('since', 0))
    with jobs_lock: job = jobs.get(jid)
    if not job:
        return jsonify({'error': 'not found'}), 404
    # Expose a download URL only when ready — points to the one-time stream route
    dl = f'/api/job/{jid}/download' if job.get('status') == 'done' else None
    return jsonify({'logs': job['logs'][since:], 'total': len(job['logs']),
                    'status': job['status'], 'download_url': dl})

@app.route('/api/job/<jid>/download')
@login_required
def api_job_download(jid):
    """
    Stream the in-memory ZIP to the browser, then immediately purge it
    from the job store.  The file never existed on disk and never will.
    Agency can regenerate any time from generation history.
    """
    with jobs_lock:
        job = jobs.get(jid)
        if not job or job.get('status') != 'done' or not job.get('zip_bytes'):
            return jsonify({'error': 'Not ready or already downloaded'}), 404
        # Grab bytes and filename, then wipe from memory
        raw   = job.pop('zip_bytes')
        zname = job.pop('zip_name', 'documents.zip')
        # Keep the job record itself (status/logs) but bytes are gone
    return send_file(
        io.BytesIO(raw),
        mimetype='application/zip',
        as_attachment=True,
        download_name=zname,
    )

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        lid = request.form.get('login_id', '').strip()
        pw  = request.form.get('password', '')
        res = supabase.table('agencies').select('*').or_(
            f"username.eq.{lid},email.eq.{lid}").execute()
        if res.data and res.data[0]['password'] == pw:
            u = res.data[0]
            if u['role'] != 'admin' and u.get('expires_at'):
                if datetime.now().date() > datetime.strptime(u['expires_at'], '%Y-%m-%d').date():
                    flash("Subscription expired. Contact administrator.", "danger")
                    return redirect(url_for('login'))
            u['days_left'] = days_left(u.get('expires_at')) or 9999
            session['user'] = u
            flash(f"Welcome back, {u['agency_name']}!", "success")
            return redirect(url_for('index'))
        flash("Invalid credentials.", "danger")

    # Active agencies with logos for login-page carousel
    carousel_agencies = []
    try:
        all_ag = supabase.table('agencies').select('agency_name,logo_url,expires_at') \
                         .neq('role', 'admin').execute().data or []
        carousel_agencies = [
            a for a in all_ag
            if a.get('logo_url') and (days_left(a.get('expires_at') or '') or 0) > 0
        ]
    except Exception:
        pass

    return render_template('login.html', contact_info=ADMIN_INFO,
                           carousel_agencies=carousel_agencies)

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return redirect(url_for('admin_dashboard' if session['user']['role'] == 'admin' else 'agency_dashboard'))

# ── Agency portal — branded login page at /<username> ────────────────────────
# Each agency gets their own shareable URL: mydomain.com/sunpower_nagpur
# Uses their existing username as the slug — no new DB column needed.
# Reserved slugs (existing routes) are blocked below.
_RESERVED_SLUGS = {
    'login', 'logout', 'dashboard', 'admin', 'generate', 'history',
    'static', 'api', 'favicon.ico',
}

@app.route('/<slug>')
def agency_portal(slug):
    slug = slug.strip().lower()
    if slug in _RESERVED_SLUGS:
        return redirect(url_for('login'))
    try:
        res = supabase.table('agencies').select(
            'id,username,agency_name,logo_url,expires_at,role'
        ).eq('username', slug).neq('role', 'admin').execute()
    except Exception:
        return redirect(url_for('login'))
    if not res.data:
        # Unknown slug — fall through to normal login
        return redirect(url_for('login'))
    agency = res.data[0]
    # Expired agencies still get their portal but a warning is shown
    dl = days_left(agency.get('expires_at') or '')
    return render_template('portal.html',
        agency=agency,
        days_left=dl,
        contact_info=ADMIN_INFO)

# ── Agency dashboard ──────────────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def agency_dashboard():
    aid = session['user']['id']
    try:
        total_docs = supabase.table('generation_history').select('id', count='exact') \
                              .eq('agency_id', aid).execute().count or 0
    except Exception:
        total_docs = 0

    payment_info = ADMIN_INFO

    return render_template('agency_dashboard.html',
        total_docs=total_docs,
        payment_info=payment_info)

# ── Admin dashboard — search + status filter + pagination ─────────────────────
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    search = request.args.get('search', '').strip()
    status = request.args.get('status', 'all')
    page   = max(1, int(request.args.get('page', 1)))

    raw = supabase.table('agencies').select('*').neq('role', 'admin').execute().data or []
    for a in raw:
        a['days_left'] = days_left(a.get('expires_at')) or 0

    stats = {
        'total':   len(raw),
        'active':  sum(1 for a in raw if a['days_left'] > 0),
        'expired': sum(1 for a in raw if a['days_left'] <= 0),
    }

    filtered = raw
    if search:
        sl = search.lower()
        filtered = [a for a in raw if
                    sl in (a.get('agency_name') or '').lower() or
                    sl in (a.get('username') or '').lower() or
                    sl in (a.get('director_name') or '').lower() or
                    sl in (a.get('contact_number') or '').lower()]
    if status == 'active':
        filtered = [a for a in filtered if a['days_left'] > 0]
    elif status == 'expired':
        filtered = [a for a in filtered if a['days_left'] <= 0]

    total       = len(filtered)
    total_pages = max(1, ceil(total / PER_PAGE_ADMIN))
    page        = min(page, total_pages)
    agencies_pg = filtered[(page - 1) * PER_PAGE_ADMIN: page * PER_PAGE_ADMIN]

    # Payment & contact info — read from .env, no DB query needed
    payment_info = ADMIN_INFO

    return render_template('admin_dashboard.html',
        agencies=agencies_pg, stats=stats,
        page=page, total_pages=total_pages, total=total,
        search=search, status=status,
        payment_info=payment_info)

# ── Admin: create ─────────────────────────────────────────────────────────────
@app.route('/admin/agency/new', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_create_agency():
    if request.method == 'POST':
        months = int(request.form.get('subscription_months', 12))
        data = {
            "username":       request.form.get('username'),
            "email":          request.form.get('email'),
            "password":       request.form.get('password'),
            "agency_name":    request.form.get('agency_name'),
            "director_name":  request.form.get('director_name'),
            "contact_number": request.form.get('contact_number'),
            "agency_address": request.form.get('agency_address'),
            "role":           "agency",
            "expires_at":     (datetime.now() + timedelta(days=months * 30)).strftime('%Y-%m-%d'),
        }
        logo_url  = upload_image(request.form.get('logo_base64'),  'agency-logos')
        stamp_url = upload_image(request.form.get('stamp_base64'), 'agency-stamps')
        if logo_url:  data['logo_url']  = logo_url
        if stamp_url: data['stamp_url'] = stamp_url
        try:
            supabase.table('agencies').insert(data).execute()
            flash(f"Agency '{data['agency_name']}' created — active for {months} months.", "success")
            return redirect(url_for('admin_dashboard'))
        except Exception as e:
            flash(f"Error: {e}", "danger")
    return render_template('admin_agency_form.html', agency=None, edit=False)

# ── Admin: edit ───────────────────────────────────────────────────────────────
@app.route('/admin/agency/edit/<agency_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_agency(agency_id):
    agency = supabase.table('agencies').select('*').eq('id', agency_id).single().execute().data
    if not agency:
        flash("Agency not found.", "danger")
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        data = {
            "agency_name":    request.form.get('agency_name'),
            "director_name":  request.form.get('director_name'),
            "contact_number": request.form.get('contact_number'),
            "agency_address": request.form.get('agency_address'),
            "email":          request.form.get('email'),
            "username":       request.form.get('username'),
        }
        if request.form.get('password', '').strip():
            data['password'] = request.form['password'].strip()
        if request.form.get('expires_at', '').strip():
            data['expires_at'] = request.form['expires_at'].strip()
        new_logo  = upload_image(request.form.get('logo_base64'),  'agency-logos')
        new_stamp = upload_image(request.form.get('stamp_base64'), 'agency-stamps')
        if new_logo:  data['logo_url']  = new_logo
        if new_stamp: data['stamp_url'] = new_stamp
        try:
            supabase.table('agencies').update(data).eq('id', agency_id).execute()
            flash("Agency updated successfully.", "success")
            return redirect(url_for('admin_dashboard'))
        except Exception as e:
            flash(f"Update error: {e}", "danger")
    return render_template('admin_agency_form.html', agency=agency, edit=True)

# ── Admin: renew / delete ─────────────────────────────────────────────────────
@app.route('/admin/renew/<id>', methods=['POST'])
@login_required
@admin_required
def renew_agency(id):
    months  = int(request.form.get('renewal_months', 12))
    row     = supabase.table('agencies').select('expires_at').eq('id', id).single().execute().data
    base    = datetime.strptime(row['expires_at'], '%Y-%m-%d') if row.get('expires_at') else datetime.now()
    if base < datetime.now(): base = datetime.now()
    new_exp = (base + timedelta(days=months * 30)).strftime('%Y-%m-%d')
    supabase.table('agencies').update({'expires_at': new_exp}).eq('id', id).execute()
    flash(f"Renewed. New expiry: {new_exp}", "success")
    return redirect(url_for('admin_dashboard',
        search=request.form.get('_search', ''), page=request.form.get('_page', 1)))

@app.route('/admin/delete/<id>', methods=['POST'])
@login_required
@admin_required
def delete_agency(id):
    supabase.table('agencies').delete().eq('id', id).execute()
    flash("Agency deleted.", "info")
    return redirect(url_for('admin_dashboard'))

# ── Generate ──────────────────────────────────────────────────────────────────
@app.route('/generate')
@login_required
def generate():
    prefill = {}
    from_history = request.args.get('from_history', '').strip()
    if from_history:
        try:
            aid = session['user']['id']
            rec = (supabase.table('generation_history').select('*')
                   .eq('id', from_history).eq('agency_id', aid).single().execute().data)
            if rec:
                # Strip keys not relevant to the form
                skip = {'id', 'agency_id', 'created_at'}
                prefill = {k: (v or '') for k, v in rec.items() if k not in skip}
        except Exception:
            pass
    return render_template('generate_form.html', prefill=prefill)

# ── History: list with search + pagination ────────────────────────────────────
@app.route('/history')
@login_required
def history():
    aid  = session['user']['id']
    q    = request.args.get('q', '').strip()
    page = max(1, int(request.args.get('page', 1)))
    all_r = (supabase.table('generation_history').select('*')
             .eq('agency_id', aid).order('created_at', desc=True).execute().data or [])
    if q:
        ql    = q.lower()
        all_r = [r for r in all_r if
                 ql in (r.get('consumer_name') or '').lower() or
                 ql in (r.get('consumer_number') or '').lower() or
                 ql in (r.get('city') or '').lower()]
    total       = len(all_r)
    total_pages = max(1, ceil(total / PER_PAGE_HISTORY))
    page        = min(page, total_pages)
    records     = all_r[(page - 1) * PER_PAGE_HISTORY: page * PER_PAGE_HISTORY]
    return render_template('history.html',
        history=records, page=page, total_pages=total_pages, total=total, q=q)

# ── History: single record detail ─────────────────────────────────────────────
@app.route('/history/<record_id>')
@login_required
def history_detail(record_id):
    aid = session['user']['id']
    rec = (supabase.table('generation_history').select('*')
           .eq('id', record_id).eq('agency_id', aid).single().execute().data)
    if not rec:
        flash("Record not found.", "danger")
        return redirect(url_for('history'))
    return render_template('history_detail.html', record=rec)

# ── History: delete ───────────────────────────────────────────────────────────
@app.route('/history/delete/<record_id>', methods=['POST'])
@login_required
def history_delete(record_id):
    aid = session['user']['id']
    supabase.table('generation_history').delete().eq('id', record_id).eq('agency_id', aid).execute()
    flash("Record deleted.", "info")
    return redirect(url_for('history',
        q=request.form.get('_q', ''), page=request.form.get('_page', 1)))

# ── Error handler ─────────────────────────────────────────────────────────────
@app.errorhandler(Exception)
def handle_exc(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException): return e
    return render_template('error.html', error_message=str(e)), 500

# Pre-load templates into RAM so first job has zero disk cold-start penalty
preload_templates()

if __name__ == '__main__':
    app.run(debug=True, threaded=True)