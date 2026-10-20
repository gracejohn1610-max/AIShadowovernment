from flask import Flask, request, redirect, url_for, session, render_template_string, flash, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3
import os
import re
import secrets
import json
import time

from datetime import datetime, timezone
from functools import wraps

from google import genai
from google.genai import types


# ============================================================
# APP CONFIGURATION
# ============================================================

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'shadow_government.db')
UP = os.path.join(BASE, 'uploads')

os.makedirs(UP, exist_ok=True)

app = Flask(__name__)

app.secret_key = os.environ.get(
    'SHADOW_SECRET_KEY',
    'change-this-secret-key'
)

app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

ALLOWED = {'png', 'jpg', 'jpeg', 'webp'}

CATS = [
    'Roads & Infrastructure',
    'Waste Management',
    'Water Supply',
    'Electricity & Street Lights',
    'Public Safety',
    'Healthcare',
    'Education',
    'Public Transport',
    'Environment',
    'Other'
]

STATUS = [
    'Submitted',
    'Under Review',
    'In Progress',
    'Resolved',
    'Rejected'
]

DEPT = {
    'Roads & Infrastructure': 'Public Works Department',
    'Waste Management': 'Municipal Waste Management',
    'Water Supply': 'Water Supply Department',
    'Electricity & Street Lights': 'Electrical / Utilities Department',
    'Public Safety': 'Public Safety Department',
    'Healthcare': 'Health Department',
    'Education': 'Education Department',
    'Public Transport': 'Transport Department',
    'Environment': 'Environment Department',
    'Other': 'General Administration'
}

KEY = {
    'Roads & Infrastructure': [
        'pothole',
        'road',
        'street',
        'bridge',
        'drain',
        'footpath',
        'pavement',
        'traffic'
    ],
    'Waste Management': [
        'garbage',
        'waste',
        'trash',
        'dump',
        'rubbish',
        'litter',
        'bin',
        'sewage'
    ],
    'Water Supply': [
        'water',
        'pipeline',
        'tap',
        'leak',
        'drinking water',
        'supply'
    ],
    'Electricity & Street Lights': [
        'electricity',
        'power',
        'streetlight',
        'street light',
        'lamp',
        'light',
        'transformer'
    ],
    'Public Safety': [
        'unsafe',
        'crime',
        'accident',
        'danger',
        'police',
        'harassment',
        'fire',
        'emergency'
    ],
    'Healthcare': [
        'hospital',
        'doctor',
        'clinic',
        'medicine',
        'ambulance',
        'health',
        'patient'
    ],
    'Education': [
        'school',
        'college',
        'teacher',
        'classroom',
        'student',
        'education'
    ],
    'Public Transport': [
        'bus',
        'train',
        'metro',
        'transport',
        'station',
        'stop',
        'vehicle'
    ],
    'Environment': [
        'pollution',
        'smoke',
        'tree',
        'noise',
        'air',
        'environment',
        'flood',
        'waterlogging'
    ]
}


# ============================================================
# GEMINI CONFIGURATION
# ============================================================

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Primary model that already worked in your testing.
PRIMARY_GEMINI_MODEL = 'gemini-3.8-flash'
FALLBACK_GEMINI_MODEL = 'gemini-3.5-flash-lite'

# Number of attempts for each model.
MAX_RETRIES = 1

# Timeout in milliseconds.
GEMINI_TIMEOUT = 90000

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(
                timeout=GEMINI_TIMEOUT
            )
        )
    except Exception as e:
        print("Gemini client initialization error:", e)


# ============================================================
# BASIC HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


# ============================================================
# DATABASE
# ============================================================

def init():
    c = conn()

    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        department TEXT,
        employee_id TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS complaints(
        id INTEGER PRIMARY KEY,
        citizen_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        category TEXT NOT NULL,
        location TEXT NOT NULL,
        image_filename TEXT,
        ai_category TEXT,
        ai_priority TEXT,
        ai_summary TEXT,
        ai_department TEXT,
        status TEXT NOT NULL DEFAULT 'Submitted',
        assigned_official_id INTEGER,
        official_response TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(citizen_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS policies(
        id INTEGER PRIMARY KEY,
        official_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        analysis TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(official_id) REFERENCES users(id)
    );

    CREATE INDEX IF NOT EXISTS ix_c
    ON complaints(status, ai_category, location);
    ''')

    if not c.execute(
        'SELECT 1 FROM users WHERE email=?',
        ('official@shadow.gov',)
    ).fetchone():

        c.execute(
            '''
            INSERT INTO users(
                name,
                email,
                password_hash,
                role,
                department,
                employee_id,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            ''',
            (
                'Demo Government Official',
                'official@shadow.gov',
                generate_password_hash('Official@123'),
                'official',
                'Municipal Administration',
                'DEMO001',
                now()
            )
        )

    c.commit()
    c.close()


# ============================================================
# CSRF
# ============================================================

def token():
    session.setdefault(
        'csrf',
        secrets.token_urlsafe(24)
    )
    return session['csrf']


def csrf():
    return request.form.get('csrf') == session.get('csrf')


@app.context_processor
def inject_template_helpers():
    return {
        'csrf': token
    }


# ============================================================
# AUTH DECORATOR
# ============================================================

def required(role=None):

    def dec(f):

        @wraps(f)
        def w(*a, **k):

            if 'uid' not in session:
                flash(
                    'Please log in first.',
                    'error'
                )
                return redirect(url_for('login'))

            if role and session.get('role') != role:
                return 'Forbidden', 403

            return f(*a, **k)

        return w

    return dec


# ============================================================
# COMPLAINT AI / RULE-BASED TRIAGE
# ============================================================

def analyze(title, desc, location):

    t = (title + ' ' + desc).lower()

    scores = {
        k: sum(t.count(x) for x in v)
        for k, v in KEY.items()
    }

    if max(scores.values()) > 0:
        cat = max(scores, key=scores.get)
    else:
        cat = 'Other'

    urgent = sum(
        t.count(x)
        for x in [
            'danger',
            'accident',
            'injury',
            'fire',
            'emergency',
            'collapse',
            'contamination'
        ]
    )

    high = sum(
        t.count(x)
        for x in [
            'urgent',
            'severe',
            'major',
            'overflow',
            'blocked',
            'broken',
            'not working'
        ]
    )

    if urgent > 0 or cat == 'Public Safety':
        priority = 'High'
    elif high > 0 or len(desc) > 450:
        priority = 'Medium'
    else:
        priority = 'Low'

    s = re.sub(
        r'\s+',
        ' ',
        desc
    ).strip()

    if len(s) <= 220:
        summary = s
    else:
        summary = s[:217] + '...'

    department = DEPT.get(
        cat,
        'General Administration'
    )

    return cat, priority, summary, department


# ============================================================
# GEMINI POLICY ANALYSIS
# ============================================================

def clean_gemini_json(text):
    """
    Cleans Gemini output so JSON can be parsed even if Gemini
    accidentally wraps it in markdown code fences.
    """

    if not text:
        return None

    text = text.strip()

    # Remove markdown JSON fences.
    if text.startswith('```json'):
        text = text[7:]

    elif text.startswith('```'):
        text = text[3:]

    if text.endswith('```'):
        text = text[:-3]

    text = text.strip()

    # Find JSON object if Gemini added extra text.
    start = text.find('{')
    end = text.rfind('}')

    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text


def valid_policy_result(data):
    """
    Ensures Gemini returned the structure our application expects.
    """

    if not isinstance(data, dict):
        return False

    required_fields = [
        'risk',
        'benefits',
        'risks',
        'groups',
        'mitigations'
    ]

    for field in required_fields:
        if field not in data:
            return False

    if not isinstance(data['benefits'], list):
        return False

    if not isinstance(data['risks'], list):
        return False

    if not isinstance(data['groups'], list):
        return False

    if not isinstance(data['mitigations'], list):
        return False

    return True


def fallback_policy_result():
    """
    Used only when every Gemini model attempt fails.
    This is deliberately an error state rather than pretending
    that a real AI analysis was generated.
    """

    return {
        'risk': 'Unavailable',
        'benefits': [
            'AI analysis could not be generated at this time.'
        ],
        'risks': [
            'The Gemini AI service may be temporarily unavailable.'
        ],
        'groups': [
            'Residents',
            'Local businesses',
            'Public-service users'
        ],
        'mitigations': [
            'Retry the analysis after a short period.'
        ]
    }


def gemini_policy_request(model, prompt):
    """
    Sends one request to Gemini.

    Returns:
        Parsed dictionary on success.
        None on failure.
    """

    if not gemini_client:
        return None

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            print(
                f"Gemini policy analysis: "
                f"model={model}, attempt={attempt}/{MAX_RETRIES}"
            )

            response = gemini_client.models.generate_content(
                model=model,
                contents=prompt
            )

            text = getattr(
                response,
                'text',
                None
            )

            if not text:
                print(
                    f"Gemini returned an empty response "
                    f"from {model}."
                )
                continue

            cleaned = clean_gemini_json(text)

            data = json.loads(cleaned)

            if valid_policy_result(data):

                # Normalize risk.
                risk = str(
                    data.get('risk', 'Medium')
                ).strip().title()

                if risk not in [
                    'Low',
                    'Medium',
                    'High'
                ]:
                    risk = 'Medium'

                data['risk'] = risk

                print(
                    f"Gemini policy analysis succeeded "
                    f"using {model}."
                )

                return data

            print(
                f"Gemini returned unexpected JSON "
                f"structure from {model}."
            )

        except Exception as e:

            error_text = str(e)

            print(
                f"Gemini error using {model}, "
                f"attempt {attempt}: {error_text}"
            )

            # 503 / temporary unavailable / high demand.
            is_temporary = (
                '503' in error_text
                or 'UNAVAILABLE' in error_text.upper()
                or 'high demand' in error_text.lower()
                or 'temporarily unavailable' in error_text.lower()
                or 'deadline' in error_text.lower()
                or 'timeout' in error_text.lower()
                or 'timed out' in error_text.lower()
            )

            if is_temporary and attempt < MAX_RETRIES:

                wait_seconds = 2 ** attempt

                print(
                    f"Temporary Gemini failure. "
                    f"Retrying in {wait_seconds} seconds..."
                )

                time.sleep(wait_seconds)

                continue

            break

    return None


def policy_ai(title, desc):

    # --------------------------------------------------------
    # If API key is missing, do not make a fake AI response.
    # --------------------------------------------------------

    if not GEMINI_API_KEY or not gemini_client:

        print(
            "Gemini policy analysis unavailable: "
            "GEMINI_API_KEY is missing or client failed."
        )

        return fallback_policy_result()

    # --------------------------------------------------------
    # Strong structured prompt.
    # --------------------------------------------------------

    prompt = f"""
You are an AI policy analysis assistant for a civic government
communication platform called AI Shadow Government.

Your job is to provide neutral, evidence-oriented decision support
for a proposed government policy.

IMPORTANT:
- You are advisory only.
- Do NOT make the final government decision.
- Do NOT claim certainty where evidence is unavailable.
- Do NOT invent statistics.
- Consider citizens, businesses, vulnerable groups, accessibility,
  fairness, implementation feasibility, unintended consequences,
  and public-service effects.
- Keep the analysis practical and understandable.
- Analyze the actual policy supplied below.

POLICY TITLE:
{title}

POLICY DESCRIPTION:
{desc}

Return ONLY valid JSON.

Use exactly this structure:

{{
  "risk": "Low",
  "benefits": [
    "benefit 1",
    "benefit 2",
    "benefit 3"
  ],
  "risks": [
    "risk 1",
    "risk 2",
    "risk 3"
  ],
  "groups": [
    "affected group 1",
    "affected group 2",
    "affected group 3"
  ],
  "mitigations": [
    "mitigation 1",
    "mitigation 2",
    "mitigation 3"
  ]
}}

Rules:
- risk must be exactly Low, Medium, or High.
- Provide 3 to 5 benefits.
- Provide 3 to 5 risks.
- Provide 3 to 6 affected groups.
- Provide 3 to 5 mitigation strategies.
- Return JSON only.
"""

    # --------------------------------------------------------
    # FIRST MODEL
    # --------------------------------------------------------

    result = gemini_policy_request(
        PRIMARY_GEMINI_MODEL,
        prompt
    )

    if result:
        return result

    # --------------------------------------------------------
    # FALLBACK MODEL
    # --------------------------------------------------------

    print(
        f"Primary Gemini model failed. "
        f"Trying fallback model: {FALLBACK_GEMINI_MODEL}"
    )

    result = gemini_policy_request(
        FALLBACK_GEMINI_MODEL,
        prompt
    )

    if result:
        return result

    # --------------------------------------------------------
    # ALL GEMINI ATTEMPTS FAILED
    # --------------------------------------------------------

    print(
        "All Gemini policy analysis attempts failed."
    )

    return fallback_policy_result()


# ============================================================
# CSS
# ============================================================

CSS = '''
<style>
body{
    margin:0;
    font-family:Arial,sans-serif;
    background:#f4f7fb;
    color:#172033
}

nav{
    background:#102a56;
    color:#fff;
    padding:15px 5%;
    display:flex;
    justify-content:space-between
}

nav a{
    color:#fff;
    text-decoration:none;
    margin-left:16px
}

.container{
    max-width:1150px;
    margin:28px auto;
    padding:0 18px
}

.card{
    background:#fff;
    border:1px solid #e4e7ec;
    border-radius:14px;
    padding:20px;
    margin-bottom:18px;
    box-shadow:0 2px 8px #0000000d
}

.grid{
    display:grid;
    grid-template-columns:
        repeat(auto-fit,minmax(210px,1fr));
    gap:15px
}

.stat{
    font-size:30px;
    font-weight:800
}

input,textarea,select{
    width:100%;
    padding:11px;
    border:1px solid #d0d5dd;
    border-radius:8px;
    margin:7px 0 15px;
    font:inherit
}

textarea{
    min-height:130px
}

button,.btn{
    background:#175cd3;
    color:#fff;
    border:0;
    border-radius:8px;
    padding:11px 15px;
    text-decoration:none;
    cursor:pointer;
    font-weight:700
}

.secondary{
    background:#475467
}

.success{
    background:#039855
}

.badge{
    padding:5px 9px;
    border-radius:99px;
    background:#eef4ff;
    color:#175cd3;
    font-size:12px;
    font-weight:bold
}

.high{
    background:#fee4e2;
    color:#b42318
}

.medium{
    background:#fff4e5;
    color:#b54708
}

.low{
    background:#ecfdf3;
    color:#027a48
}

table{
    width:100%;
    border-collapse:collapse
}

th,td{
    text-align:left;
    padding:11px;
    border-bottom:1px solid #e4e7ec
}

.muted{
    color:#667085
}

.alert{
    padding:12px;
    border-radius:8px;
    background:#ecfdf3;
    margin-bottom:12px
}

.error{
    background:#fee4e2
}

.preview{
    max-width:100%;
    max-height:420px;
    border-radius:10px
}
</style>
'''


# ============================================================
# BASE TEMPLATE
# ============================================================

BASE = '''
<!doctype html>
<html>
<head>
<meta charset=utf-8>
<meta name=viewport
content="width=device-width,initial-scale=1">

<title>
{{title}} · AI Shadow Government
</title>
''' + CSS + '''
</head>

<body>

<nav>

<b>AI Shadow Government</b>

<span>

{% if session.get('uid') %}

<a href="{{url_for('dashboard')}}">
Dashboard
</a>

{% if session.get('role')=='citizen' %}

<a href="{{url_for('new')}}">
Report
</a>

{% else %}

<a href="{{url_for('policy')}}">
Policy Analyzer
</a>

{% endif %}

<a href="{{url_for('logout')}}">
Logout
</a>

{% else %}

<a href="{{url_for('login')}}">
Login
</a>

<a href="{{url_for('register')}}">
Register
</a>

{% endif %}

</span>

</nav>

<main class=container>

{% with ms=get_flashed_messages(with_categories=true) %}

{% for cat,msg in ms %}

<div class="alert {% if cat=='error' %}error{% endif %}">
{{msg}}
</div>

{% endfor %}

{% endwith %}

{{body|safe}}

</main>

</body>
</html>
'''


def page(title, t, **k):

    return render_template_string(
        BASE,
        title=title,
        body=render_template_string(t, **k)
    )


# ============================================================
# HOME
# ============================================================

@app.route('/')
def home():

    if session.get('uid'):
        return redirect(
            url_for('dashboard')
        )

    return page(
        'Home',
        '''
        <div class=card>

        <h1>
        AI Shadow Government
        </h1>

        <p class=muted>
        A civic intelligence platform connecting citizen
        concerns with government officials and providing
        AI-assisted policy analysis.
        </p>

        <a class=btn href="{{url_for('register')}}">
        Create account
        </a>

        <a class="btn secondary"
        href="{{url_for('login')}}">
        Login
        </a>

        </div>
        '''
    )


# ============================================================
# REGISTER
# ============================================================

@app.route('/register', methods=['GET', 'POST'])
def register():

    if request.method == 'POST':

        if not csrf():
            flash(
                'Invalid form token.',
                'error'
            )
            return redirect(
                url_for('register')
            )

        n = request.form.get(
            'name',
            ''
        ).strip()

        e = request.form.get(
            'email',
            ''
        ).strip().lower()

        p = request.form.get(
            'password',
            ''
        )

        role = request.form.get(
            'role',
            'citizen'
        )

        d = request.form.get(
            'department',
            ''
        ).strip()

        emp = request.form.get(
            'employee_id',
            ''
        ).strip()

        if (
            not n
            or not e
            or len(p) < 8
            or role not in ('citizen', 'official')
            or (
                role == 'official'
                and (
                    not d
                    or not emp
                )
            )
        ):

            flash(
                'Please complete all required fields.',
                'error'
            )

            return redirect(
                url_for('register')
            )

        c = conn()

        try:

            c.execute(
                '''
                INSERT INTO users(
                    name,
                    email,
                    password_hash,
                    role,
                    department,
                    employee_id,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?)
                ''',
                (
                    n,
                    e,
                    generate_password_hash(p),
                    role,
                    d or None,
                    emp or None,
                    now()
                )
            )

            c.commit()

            flash(
                'Registration successful.',
                'success'
            )

            return redirect(
                url_for('login')
            )

        except sqlite3.IntegrityError:

            flash(
                'Email already registered.',
                'error'
            )

        finally:

            c.close()

    return page(
        'Register',
        '''
        <div class=card>

        <h2>Create account</h2>

        <form method=post>

        <input
        type=hidden
        name=csrf
        value="{{csrf()}}"
        >

        <label>Name</label>
        <input name=name required>

        <label>Email</label>
        <input
        type=email
        name=email
        required
        >

        <label>Password</label>
        <input
        type=password
        name=password
        minlength=8
        required
        >

        <label>Role</label>

        <select
        name=role
        id=role
        onchange="f()"
        >

        <option value=citizen>
        Citizen
        </option>

        <option value=official>
        Government Official
        </option>

        </select>

        <div id=o style="display:none">

        <label>Department</label>
        <input name=department>

        <label>Employee ID</label>
        <input name=employee_id>

        </div>

        <button>
        Register
        </button>

        </form>

        </div>

        <script>

        function f(){

            o.style.display =
                role.value === 'official'
                ? 'block'
                : 'none';

        }

        </script>
        '''
    )


# ============================================================
# LOGIN
# ============================================================

@app.route('/login', methods=['GET', 'POST'])
def login():

    if request.method == 'POST':

        if not csrf():

            flash(
                'Invalid form token.',
                'error'
            )

            return redirect(
                url_for('login')
            )

        e = request.form.get(
            'email',
            ''
        ).strip().lower()

        p = request.form.get(
            'password',
            ''
        )

        c = conn()

        u = c.execute(
            'SELECT * FROM users WHERE email=?',
            (e,)
        ).fetchone()

        c.close()

        if u and check_password_hash(
            u['password_hash'],
            p
        ):

            session.clear()

            session.update(
                uid=u['id'],
                role=u['role'],
                name=u['name'],
                csrf=secrets.token_urlsafe(24)
            )

            return redirect(
                url_for('dashboard')
            )

        flash(
            'Invalid email or password.',
            'error'
        )

    return page(
        'Login',
        '''
        <div class=card>

        <h2>Login</h2>

        <form method=post>

        <input
        type=hidden
        name=csrf
        value="{{csrf()}}"
        >

        <label>Email</label>

        <input
        type=email
        name=email
        required
        >

        <label>Password</label>

        <input
        type=password
        name=password
        required
        >

        <button>
        Login
        </button>

        </form>

        <p class=muted>
        Demo official:
        official@shadow.gov /
        Official@123
        </p>

        </div>
        '''
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route('/logout')
def logout():

    session.clear()

    return redirect('/')


# ============================================================
# DASHBOARD
# ============================================================

@app.route('/dashboard')
@required()
def dashboard():

    c = conn()

    # --------------------------------------------------------
    # CITIZEN DASHBOARD
    # --------------------------------------------------------

    if session['role'] == 'citizen':

        rows = c.execute(
            '''
            SELECT *
            FROM complaints
            WHERE citizen_id=?
            ORDER BY id DESC
            ''',
            (session['uid'],)
        ).fetchall()

        c.close()

        return page(
            'Citizen Dashboard',
            '''
            <div class="row">

            <h2>
            Welcome, {{session.name}}
            </h2>

            <a class=btn
            href="{{url_for('new')}}">
            + Report concern
            </a>

            </div>

            <div class=card>

            <h3>My concerns</h3>

            {% if rows %}

            <table>

            <tr>
            <th>Issue</th>
            <th>Category</th>
            <th>Priority</th>
            <th>Status</th>
            <th></th>
            </tr>

            {% for x in rows %}

            <tr>

            <td>
            {{x.title}}
            <br>
            <span class=muted>
            {{x.location}}
            </span>
            </td>

            <td>
            {{x.ai_category}}
            </td>

            <td>
            <span class="badge {{x.ai_priority|lower}}">
            {{x.ai_priority}}
            </span>
            </td>

            <td>
            {{x.status}}
            </td>

            <td>

            <a
            class="btn secondary"
            href="{{url_for('detail',cid=x.id)}}"
            >
            View
            </a>

            </td>

            </tr>

            {% endfor %}

            </table>

            {% else %}

            <p>
            No concerns yet.
            </p>

            {% endif %}

            </div>
            ''',
            rows=rows
        )

    # --------------------------------------------------------
    # OFFICIAL DASHBOARD
    # --------------------------------------------------------

    s = {
        k: c.execute(q).fetchone()['n']
        for k, q in {
            'total':
                'SELECT COUNT(*) n FROM complaints',

            'pending':
                '''
                SELECT COUNT(*) n
                FROM complaints
                WHERE status IN
                ('Submitted','Under Review')
                ''',

            'progress':
                '''
                SELECT COUNT(*) n
                FROM complaints
                WHERE status='In Progress'
                ''',

            'resolved':
                '''
                SELECT COUNT(*) n
                FROM complaints
                WHERE status='Resolved'
                ''',

            'high':
                '''
                SELECT COUNT(*) n
                FROM complaints
                WHERE ai_priority='High'
                AND status!='Resolved'
                '''
        }.items()
    }

    rows = c.execute(
        '''
        SELECT
            c.*,
            u.name citizen
        FROM complaints c
        JOIN users u
        ON u.id=c.citizen_id
        ORDER BY
            CASE c.ai_priority
                WHEN "High" THEN 1
                WHEN "Medium" THEN 2
                ELSE 3
            END,
            c.id DESC
        LIMIT 100
        '''
    ).fetchall()

    trends = c.execute(
        '''
        SELECT
            ai_category,
            COUNT(*) n
        FROM complaints
        GROUP BY ai_category
        ORDER BY n DESC
        '''
    ).fetchall()

    c.close()

    return page(
        'Official Dashboard',
        '''
        <h2>
        Government Official Dashboard
        </h2>

        <div class=grid>

        {% for k,v in s.items() %}

        <div class=card>

        <span class=muted>
        {{k|capitalize}}
        </span>

        <div class=stat>
        {{v}}
        </div>

        </div>

        {% endfor %}

        </div>

        <div class=card>

        <h3>
        AI issue trends
        </h3>

        {% for x in trends[:8] %}

        <span class=pill>
        {{x.ai_category}}: {{x.n}}
        </span>

        &nbsp;

        {% endfor %}

        </div>

        <div class=card>

        <h3>
        Citizen concerns
        </h3>

        <table>

        <tr>
        <th>Issue</th>
        <th>Citizen</th>
        <th>Category</th>
        <th>Priority</th>
        <th>Status</th>
        <th></th>
        </tr>

        {% for x in rows %}

        <tr>

        <td>
        {{x.title}}
        <br>
        <span class=muted>
        {{x.location}}
        </span>
        </td>

        <td>
        {{x.citizen}}
        </td>

        <td>
        {{x.ai_category}}
        </td>

        <td>

        <span
        class="badge {{x.ai_priority|lower}}"
        >
        {{x.ai_priority}}
        </span>

        </td>

        <td>
        {{x.status}}
        </td>

        <td>

        <a
        class=btn
        href="{{url_for('detail',cid=x.id)}}"
        >
        Review
        </a>

        </td>

        </tr>

        {% endfor %}

        </table>

        </div>
        ''',
        s=s,
        rows=rows,
        trends=trends
    )


# ============================================================
# NEW COMPLAINT
# ============================================================

@app.route('/complaint/new', methods=['GET', 'POST'])
@required('citizen')
def new():

    if request.method == 'POST':

        if not csrf():

            flash(
                'Invalid form token.',
                'error'
            )

            return redirect(
                url_for('new')
            )

        title = request.form.get(
            'title',
            ''
        ).strip()

        desc = request.form.get(
            'description',
            ''
        ).strip()

        cat = request.form.get(
            'category',
            'Other'
        )

        loc = request.form.get(
            'location',
            ''
        ).strip()

        f = request.files.get('image')

        if (
            not title
            or not desc
            or not loc
            or cat not in CATS
        ):

            flash(
                'Complete all required fields.',
                'error'
            )

            return redirect(
                url_for('new')
            )

        fn = None

        if f and f.filename:

            ext = (
                f.filename.rsplit('.', 1)[-1].lower()
                if '.' in f.filename
                else ''
            )

            if ext not in ALLOWED:

                flash(
                    'Only PNG/JPG/JPEG/WEBP images are allowed.',
                    'error'
                )

                return redirect(
                    url_for('new')
                )

            fn = (
                secrets.token_hex(16)
                + '.'
                + ext
            )

            f.save(
                os.path.join(
                    UP,
                    secure_filename(fn)
                )
            )

        ac, pr, summ, dep = analyze(
            title,
            desc,
            loc
        )

        c = conn()

        cur = c.execute(
            '''
            INSERT INTO complaints(
                citizen_id,
                title,
                description,
                category,
                location,
                image_filename,
                ai_category,
                ai_priority,
                ai_summary,
                ai_department,
                status,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''',
            (
                session['uid'],
                title,
                desc,
                cat,
                loc,
                fn,
                ac,
                pr,
                summ,
                dep,
                'Submitted',
                now(),
                now()
            )
        )

        c.commit()

        cid = cur.lastrowid

        c.close()

        flash(
            f'Concern #{cid} submitted and analyzed.',
            'success'
        )

        return redirect(
            url_for(
                'detail',
                cid=cid
            )
        )

    return page(
        'Report Concern',
        '''
        <div class=card>

        <h2>
        Report a civic concern
        </h2>

        <form
        method=post
        enctype=multipart/form-data
        >

        <input
        type=hidden
        name=csrf
        value="{{csrf()}}"
        >

        <label>Title</label>

        <input
        name=title
        required
        >

        <label>Category</label>

        <select name=category>

        {% for x in cats %}

        <option>
        {{x}}
        </option>

        {% endfor %}

        </select>

        <label>
        Location / Area
        </label>

        <input
        name=location
        required
        >

        <label>
        Description
        </label>

        <textarea
        name=description
        required
        ></textarea>

        <label>
        Photo (optional, max 8MB)
        </label>

        <input
        type=file
        name=image
        accept=".png,.jpg,.jpeg,.webp"
        >

        <button>
        Submit concern
        </button>

        </form>

        </div>
        ''',
        cats=CATS
    )


# ============================================================
# COMPLAINT DETAIL
# ============================================================

@app.route('/complaint/<int:cid>')
@required()
def detail(cid):

    c = conn()

    x = c.execute(
        '''
        SELECT
            c.*,
            u.name citizen
        FROM complaints c
        JOIN users u
        ON u.id=c.citizen_id
        WHERE c.id=?
        ''',
        (cid,)
    ).fetchone()

    c.close()

    if not x:
        return 'Not found', 404

    if (
        session['role'] == 'citizen'
        and x['citizen_id'] != session['uid']
    ):
        return 'Forbidden', 403

    return page(
        'Complaint #' + str(cid),
        '''
        <div class=grid>

        <div class=card>

        <h2>
        #{{x.id}} — {{x.title}}
        </h2>

        <p>

        <b>Location:</b>
        {{x.location}}

        |

        <b>Status:</b>
        {{x.status}}

        </p>

        <p>
        {{x.description}}
        </p>

        {% if x.image_filename %}

        <h3>
        Citizen evidence
        </h3>

        <img
        class=preview
        src="{{url_for(
            'upload',
            filename=x.image_filename
        )}}"
        >

        {% endif %}

        {% if x.official_response %}

        <hr>

        <h3>
        Official response
        </h3>

        <p>
        {{x.official_response}}
        </p>

        {% endif %}

        </div>

        <div class=card>

        <h3>
        AI triage
        </h3>

        <p>
        <b>Category:</b>
        {{x.ai_category}}
        </p>

        <p>

        <b>Priority:</b>

        <span
        class="badge {{x.ai_priority|lower}}"
        >
        {{x.ai_priority}}
        </span>

        </p>

        <p>
        <b>Department:</b>
        {{x.ai_department}}
        </p>

        <p>
        <b>Summary:</b>
        {{x.ai_summary}}
        </p>

        </div>

        </div>

        {% if session.role=='official' %}

        <div class=card>

        <h3>
        Official action
        </h3>

        <form
        method=post
        action="{{url_for(
            'update',
            cid=x.id
        )}}"
        >

        <input
        type=hidden
        name=csrf
        value="{{csrf()}}"
        >

        <label>
        Status
        </label>

        <select name=status>

        {% for s in statuses %}

        <option
        {% if s==x.status %}
        selected
        {% endif %}
        >
        {{s}}
        </option>

        {% endfor %}

        </select>

        <label>
        Response to citizen
        </label>

        <textarea
        name=response
        >{{x.official_response or ''}}</textarea>

        <button class=success>
        Save response
        </button>

        </form>

        </div>

        {% endif %}
        ''',
        x=x,
        statuses=STATUS
    )


# ============================================================
# UPDATE COMPLAINT
# ============================================================

@app.route(
    '/complaint/<int:cid>/update',
    methods=['POST']
)
@required('official')
def update(cid):

    if not csrf():
        return 'Invalid CSRF token', 400

    st = request.form.get('status')

    resp = request.form.get(
        'response',
        ''
    ).strip()

    if st not in STATUS:
        return 'Invalid status', 400

    c = conn()

    c.execute(
        '''
        UPDATE complaints
        SET
            status=?,
            official_response=?,
            assigned_official_id=?,
            updated_at=?
        WHERE id=?
        ''',
        (
            st,
            resp,
            session['uid'],
            now(),
            cid
        )
    )

    c.commit()
    c.close()

    flash(
        'Complaint updated.',
        'success'
    )

    return redirect(
        url_for(
            'detail',
            cid=cid
        )
    )


# ============================================================
# POLICY ANALYZER
# ============================================================

@app.route('/policy', methods=['GET', 'POST'])
@required('official')
def policy():

    result = None

    if request.method == 'POST':

        if not csrf():
            return 'Invalid CSRF token', 400

        title = request.form.get(
            'title',
            ''
        ).strip()

        desc = request.form.get(
            'description',
            ''
        ).strip()

        if title and desc:

            # Genuine Gemini-powered analysis.
            result = policy_ai(
                title,
                desc
            )

            text = (
                'Risk: '
                + result['risk']
                + ' | Benefits: '
                + '; '.join(result['benefits'])
                + ' | Risks: '
                + '; '.join(result['risks'])
                + ' | Groups: '
                + '; '.join(result['groups'])
                + ' | Mitigation: '
                + '; '.join(result['mitigations'])
            )

            c = conn()

            c.execute(
                '''
                INSERT INTO policies(
                    official_id,
                    title,
                    description,
                    analysis,
                    created_at
                )
                VALUES(?,?,?,?,?)
                ''',
                (
                    session['uid'],
                    title,
                    desc,
                    text,
                    now()
                )
            )

            c.commit()
            c.close()

    return page(
        'Policy Analyzer',
        '''
        <div class=card>

        <h2>
        AI Policy Analyzer
        </h2>

        <p class=muted>
        Decision support only — the official remains
        responsible for the decision.
        </p>

        <form method=post>

        <input
        type=hidden
        name=csrf
        value="{{csrf()}}"
        >

        <label>
        Policy title
        </label>

        <input
        name=title
        required
        >

        <label>
        Proposed policy
        </label>

        <textarea
        name=description
        required
        ></textarea>

        <button>
        Analyze policy with AI
        </button>

        </form>

        </div>

        {% if result %}

        <div class=card>

        <h2>
        AI Policy Analysis
        </h2>

        <p>

        <span
        class="badge {{result.risk|lower}}"
        >
        Overall Risk: {{result.risk}}
        </span>

        </p>

        <div class=grid>

        {% for k,label in [
            ('benefits','Potential Benefits'),
            ('risks','Potential Risks'),
            ('groups','Affected Groups'),
            ('mitigations','Mitigation Strategies')
        ] %}

        <div>

        <h3>
        {{label}}
        </h3>

        <ul>

        {% for x in result[k] %}

        <li>
        {{x}}
        </li>

        {% endfor %}

        </ul>

        </div>

        {% endfor %}

        </div>

        </div>

        {% endif %}
        ''',
        result=result
    )


# ============================================================
# UPLOADS
# ============================================================

@app.route('/uploads/<path:filename>')
@required()
def upload(filename):

    return send_from_directory(
        UP,
        filename
    )


# ============================================================
# FILE TOO LARGE
# ============================================================

@app.errorhandler(413)
def big(e):

    flash(
        'Image is larger than 8MB.',
        'error'
    )

    return redirect(
        url_for('new')
    )


# ============================================================
# START APP
# ============================================================

if __name__ == '__main__':

    init()

    print()
    print('==============================================')
    print('       AI SHADOW GOVERNMENT')
    print('==============================================')
    print()
    print(
        'Website: http://127.0.0.1:5000'
    )
    print(
        'Demo official: official@shadow.gov'
    )
    print(
        'Demo password: Official@123'
    )
    print()

    if GEMINI_API_KEY and gemini_client:

        print(
            'Gemini AI: ENABLED'
        )

        print(
            'Primary model:',
            PRIMARY_GEMINI_MODEL
        )

        print(
            'Fallback model:',
            FALLBACK_GEMINI_MODEL
        )

        print(
            'Retries per model:',
            MAX_RETRIES
        )

    else:

        print(
            'Gemini AI: NOT CONFIGURED'
        )

        print(
            'Set GEMINI_API_KEY before starting the app.'
        )

    print()
    print('==============================================')
    print()

    app.run(
        host='127.0.0.1',
        port=5000,
        debug=True
    )