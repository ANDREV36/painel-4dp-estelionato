import os, re
from datetime import datetime, date
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE-ME")
RO_RE = re.compile(r"^\d{3}-\d{5}/\d{4}$")
TZ = ZoneInfo("America/Sao_Paulo")

def local_now():
    return datetime.now(TZ)

def local_date():
    return local_now().date()

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    def conn():
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return psycopg.connect(url, row_factory=dict_row)
    PH = "%s"
else:
    import sqlite3
    DB_PATH = os.path.join(os.path.dirname(__file__), "estelionato.db")
    def conn():
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        return c
    PH = "?"

def init_db():
    c = conn()
    if IS_POSTGRES:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS records(
            id SERIAL PRIMARY KEY, rio TEXT NOT NULL, telephony TEXT, bank TEXT,
            other_offices TEXT, telephony_returned INTEGER NOT NULL DEFAULT 0,
            bank_returned INTEGER NOT NULL DEFAULT 0, other_returned INTEGER NOT NULL DEFAULT 0,
            informed INTEGER NOT NULL DEFAULT 0, informed_at TIMESTAMP NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    else:
        c.executescript("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(
            id INTEGER PRIMARY KEY AUTOINCREMENT, rio TEXT NOT NULL,
            telephony TEXT, bank TEXT, other_offices TEXT,
            telephony_returned INTEGER NOT NULL DEFAULT 0,
            bank_returned INTEGER NOT NULL DEFAULT 0, other_returned INTEGER NOT NULL DEFAULT 0,
            informed INTEGER NOT NULL DEFAULT 0, informed_at TIMESTAMP NULL,
            user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id));""")
    if IS_POSTGRES:
        c.execute("""CREATE TABLE IF NOT EXISTS daily_goals(
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            work_date DATE NOT NULL, goal INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'trabalhando',
            UNIQUE(user_id, work_date))""")
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS daily_goals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            work_date TEXT NOT NULL, goal INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'trabalhando',
            UNIQUE(user_id, work_date), FOREIGN KEY(user_id) REFERENCES users(id))""")
    # Migração segura para bancos já existentes: adiciona os indicadores de retorno.
    if IS_POSTGRES:
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS telephony_returned INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS bank_returned INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS other_returned INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS informed INTEGER NOT NULL DEFAULT 0")
        c.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS informed_at TIMESTAMP NULL")
    else:
        existing_cols = {row[1] for row in c.execute("PRAGMA table_info(records)").fetchall()}
        if "telephony_returned" not in existing_cols:
            c.execute("ALTER TABLE records ADD COLUMN telephony_returned INTEGER NOT NULL DEFAULT 0")
        if "bank_returned" not in existing_cols:
            c.execute("ALTER TABLE records ADD COLUMN bank_returned INTEGER NOT NULL DEFAULT 0")
        if "other_returned" not in existing_cols:
            c.execute("ALTER TABLE records ADD COLUMN other_returned INTEGER NOT NULL DEFAULT 0")
        if "informed" not in existing_cols:
            c.execute("ALTER TABLE records ADD COLUMN informed INTEGER NOT NULL DEFAULT 0")
        if "informed_at" not in existing_cols:
            c.execute("ALTER TABLE records ADD COLUMN informed_at TEXT")
    # Migração única: nas versões anteriores "operacao" era o estado padrão.
    # Agora o padrão é "trabalhando" (nenhuma das duas caixas marcada).
    mig = c.execute("SELECT value FROM settings WHERE key='daily_status_v12_migrated'").fetchone()
    if not mig:
        c.execute("UPDATE daily_goals SET status='trabalhando' WHERE status='operacao'")
        c.execute("INSERT INTO settings(key,value) VALUES('daily_status_v12_migrated','1')")
    c.commit(); c.close()

def q(sql, params=(), one=False):
    c=conn(); cur=c.execute(sql, params)
    data=cur.fetchone() if one else cur.fetchall()
    c.close()
    return data

def setting(key, default="0"):
    r=q(f"SELECT value FROM settings WHERE key={PH}", (key,), True)
    return r["value"] if r else default

def daily_goal_for(user_id, work_date=None):
    work_date = work_date or local_date()
    r=q(f"SELECT goal,status FROM daily_goals WHERE user_id={PH} AND work_date={PH}",
        (user_id, work_date), True)
    if not r:
        return {"goal":0, "status":"trabalhando", "assigned":False}
    status = r["status"] or "trabalhando"
    effective_goal = 0 if status in ("folga", "operacao") else int(r["goal"] or 0)
    return {"goal": effective_goal,
            "base_goal": int(r["goal"] or 0), "status": status, "assigned":True}

def upsert_daily_goal(user_id, work_date, goal, status="trabalhando"):
    status = status if status in ("trabalhando", "operacao", "folga") else "trabalhando"
    goal = max(0, int(goal))
    if IS_POSTGRES:
        c=conn(); c.execute("""INSERT INTO daily_goals(user_id,work_date,goal,status) VALUES(%s,%s,%s,%s)
            ON CONFLICT(user_id,work_date) DO UPDATE SET goal=EXCLUDED.goal,status=EXCLUDED.status""",
            (user_id,work_date,goal,status))
    else:
        c=conn(); c.execute("""INSERT INTO daily_goals(user_id,work_date,goal,status) VALUES(?,?,?,?)
            ON CONFLICT(user_id,work_date) DO UPDATE SET goal=excluded.goal,status=excluded.status""",
            (user_id,str(work_date),goal,status))
    c.commit(); c.close()

def master_required(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if session.get("master") else redirect(url_for("master_login"))
    return w

def user_required(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if session.get("user_id") else redirect(url_for("login"))
    return w

@app.route("/")
def index():
    return redirect(url_for("master_dashboard") if session.get("master")
                    else url_for("user_dashboard") if session.get("user_id")
                    else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        u=q(f"SELECT * FROM users WHERE username={PH} AND active=1",
            (request.form["username"].strip().lower(),), True)
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear(); session["user_id"]=u["id"]; session["user_name"]=u["name"]
            return redirect(url_for("user_dashboard"))
        flash("Usuário ou senha inválidos.","danger")
    return render_template("login.html")

@app.route("/master/login", methods=["GET","POST"])
def master_login():
    if request.method=="POST":
        password=request.form["password"]
        h=os.environ.get("MASTER_PASSWORD_HASH")
        if not h:
            # Somente para teste local. Em produção, configure MASTER_PASSWORD_HASH.
            h=generate_password_hash("admin123")
        if check_password_hash(h,password):
            session.clear(); session["master"]=True
            return redirect(url_for("master_dashboard"))
        flash("Senha Master inválida.","danger")
    return render_template("master_login.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/dashboard")
@user_required
def user_dashboard():
    rows=q(f"""SELECT r.*, u.name FROM records r JOIN users u ON u.id=r.user_id
               WHERE r.user_id={PH} ORDER BY r.created_at DESC""",(session["user_id"],))
    today=local_date()
    goal_info=daily_goal_for(session["user_id"], today)
    production=q(f"""SELECT COUNT(DISTINCT rio) n FROM records
                      WHERE user_id={PH} AND date(created_at)={PH}""",
                 (session["user_id"], today.isoformat()), True)["n"]
    return render_template("user_dashboard.html", rows=rows, daily_goal=goal_info,
                           daily_production=production, work_date=today.strftime("%d/%m/%Y"))

@app.route("/daily-status", methods=["POST"])
@user_required
def daily_status():
    status=request.form.get("status","trabalhando")
    if status not in ("trabalhando", "operacao", "folga"):
        status="trabalhando"
    today=local_date()
    current=daily_goal_for(session["user_id"], today)
    if not current.get("assigned"):
        flash("A meta de hoje ainda não foi definida pelo Master.", "warning")
    else:
        upsert_daily_goal(session["user_id"], today, current.get("base_goal", current.get("goal",0)), status)
        flash("Status do dia atualizado.", "success")
    return redirect(url_for("user_dashboard"))

@app.route("/records", methods=["POST"])
@user_required
def create_record():
    rio=request.form["rio"].strip()
    tel=request.form.get("telephony","").strip()
    bank=request.form.get("bank","").strip()
    other=request.form.get("other_offices","").strip()
    tel_returned = 1 if request.form.get("telephony_returned") == "1" and tel else 0
    bank_returned = 1 if request.form.get("bank_returned") == "1" and bank else 0
    other_returned = 1 if request.form.get("other_returned") == "1" and other else 0
    if not RO_RE.fullmatch(rio):
        flash("RO inválido. Use 000-00000/AAAA.","danger")
    elif not any([tel,bank,other]):
        flash("Informe pelo menos um ofício.","danger")
    else:
        # Impede que o mesmo RIO/procedimento seja lançado novamente.
        existing=q(f"""SELECT r.created_at,u.name
                      FROM records r JOIN users u ON u.id=r.user_id
                      WHERE r.rio={PH}
                      ORDER BY r.created_at ASC LIMIT 1""", (rio,), True)
        if existing:
            flash(f"Este procedimento já está sendo trabalhado por {existing['name']}.","warning")
        else:
            c=conn()
            now=datetime.now()
            if IS_POSTGRES:
                c.execute("INSERT INTO records(rio,telephony,bank,other_offices,telephony_returned,bank_returned,other_returned,informed,informed_at,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,0,NULL,%s)",
                          (rio,tel or None,bank or None,other or None,tel_returned,bank_returned,other_returned,session["user_id"]))
            else:
                c.execute("INSERT INTO records(rio,telephony,bank,other_offices,telephony_returned,bank_returned,other_returned,informed,informed_at,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (rio,tel or None,bank or None,other or None,tel_returned,bank_returned,other_returned,0,None,session["user_id"],now.isoformat(timespec="seconds")))
            c.commit(); c.close()
            flash("Procedimento salvo.","success")
    return redirect(url_for("user_dashboard"))

@app.route("/records/<int:record_id>/toggle-return/<kind>", methods=["POST"])
@user_required
def toggle_return(record_id, kind):
    columns = {
        "telephony": "telephony_returned",
        "bank": "bank_returned",
        "other": "other_returned",
    }
    column = columns.get(kind)
    if not column:
        flash("Tipo de retorno inválido.", "danger")
        return redirect(url_for("user_dashboard"))
    record = q(f"SELECT id, user_id FROM records WHERE id={PH}", (record_id,), True)
    if not record or int(record["user_id"]) != int(session["user_id"]):
        flash("Você não pode alterar este procedimento.", "danger")
        return redirect(url_for("user_dashboard"))
    c = conn()
    if IS_POSTGRES:
        c.execute(f"UPDATE records SET {column}=CASE WHEN {column}=1 THEN 0 ELSE 1 END WHERE id=%s", (record_id,))
    else:
        c.execute(f"UPDATE records SET {column}=CASE {column} WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (record_id,))
    c.commit(); c.close()
    return redirect(url_for("user_dashboard"))

@app.route("/records/<int:record_id>/toggle-informed", methods=["POST"])
@user_required
def toggle_informed(record_id):
    record=q(f"SELECT id,user_id,informed FROM records WHERE id={PH}",(record_id,),True)
    if not record or int(record["user_id"]) != int(session["user_id"]):
        flash("Você não pode alterar este procedimento.","danger")
        return redirect(url_for("user_dashboard"))
    new_value=0 if int(record["informed"] or 0) else 1
    c=conn()
    stamp=local_now().strftime("%Y-%m-%d %H:%M:%S") if new_value else None
    if IS_POSTGRES:
        c.execute("UPDATE records SET informed=%s,informed_at=%s WHERE id=%s",(new_value,stamp,record_id))
    else:
        c.execute("UPDATE records SET informed=?,informed_at=? WHERE id=?",(new_value,stamp,record_id))
    c.commit(); c.close()
    return redirect(url_for("user_dashboard"))

@app.route("/master/records/<int:record_id>/toggle-informed", methods=["POST"])
@master_required
def master_toggle_informed(record_id):
    record=q(f"SELECT informed FROM records WHERE id={PH}",(record_id,),True)
    if not record:
        flash("Procedimento não encontrado.","danger")
        return redirect(url_for("master_dashboard"))
    new_value=0 if int(record["informed"] or 0) else 1
    c=conn()
    stamp=local_now().strftime("%Y-%m-%d %H:%M:%S") if new_value else None
    if IS_POSTGRES:
        c.execute("UPDATE records SET informed=%s,informed_at=%s WHERE id=%s",(new_value,stamp,record_id))
    else:
        c.execute("UPDATE records SET informed=?,informed_at=? WHERE id=?",(new_value,stamp,record_id))
    c.commit(); c.close()
    return redirect(url_for("master_dashboard"))

@app.route("/master/records/<int:record_id>/toggle-return/<kind>", methods=["POST"])
@master_required
def master_toggle_return(record_id, kind):
    columns = {"telephony": "telephony_returned", "bank": "bank_returned", "other": "other_returned"}
    column = columns.get(kind)
    if not column:
        flash("Tipo de retorno inválido.", "danger")
        return redirect(url_for("master_dashboard"))
    c = conn()
    if IS_POSTGRES:
        c.execute(f"UPDATE records SET {column}=CASE WHEN {column}=1 THEN 0 ELSE 1 END WHERE id=%s", (record_id,))
    else:
        c.execute(f"UPDATE records SET {column}=CASE {column} WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (record_id,))
    c.commit(); c.close()
    return redirect(url_for("master_dashboard"))

@app.route("/master")
@master_required
def master_dashboard():
    users=q("SELECT id,name,username,active,created_at FROM users ORDER BY name")
    rows=q("""SELECT r.*,u.name FROM records r JOIN users u ON u.id=r.user_id
              ORDER BY r.created_at DESC""")
    start=q("SELECT COUNT(*) n FROM records",one=True)["n"]
    today=q("SELECT COUNT(*) n FROM records WHERE DATE(created_at)=CURRENT_DATE" if IS_POSTGRES
            else "SELECT COUNT(*) n FROM records WHERE date(created_at)=date('now','localtime')",one=True)["n"]
    tel=q("SELECT COUNT(*) n FROM records WHERE telephony IS NOT NULL AND telephony<>''",one=True)["n"]
    bank=q("SELECT COUNT(*) n FROM records WHERE bank IS NOT NULL AND bank<>''",one=True)["n"]
    other=q("SELECT COUNT(*) n FROM records WHERE other_offices IS NOT NULL AND other_offices<>''",one=True)["n"]
    informed=q("SELECT COUNT(*) n FROM records WHERE informed=1",one=True)["n"]
    pending=start-informed
    byuser=q("""SELECT u.name,
        COUNT(DISTINCT r.rio) procedures,
        COUNT(DISTINCT CASE WHEN r.informed=1 THEN r.rio END) informed,
        SUM(CASE WHEN r.telephony IS NOT NULL AND r.telephony<>'' THEN 1 ELSE 0 END) telephony,
        SUM(CASE WHEN r.bank IS NOT NULL AND r.bank<>'' THEN 1 ELSE 0 END) bank,
        SUM(CASE WHEN r.other_offices IS NOT NULL AND r.other_offices<>'' THEN 1 ELSE 0 END) other,
        SUM(CASE WHEN r.telephony_returned=1 THEN 1 ELSE 0 END) telephony_returned,
        SUM(CASE WHEN r.bank_returned=1 THEN 1 ELSE 0 END) bank_returned,
        SUM(CASE WHEN r.other_returned=1 THEN 1 ELSE 0 END) other_returned
        FROM users u LEFT JOIN records r ON r.user_id=u.id
        GROUP BY u.id,u.name ORDER BY procedures DESC""")
    selected_date=request.args.get("date") or local_date().isoformat()
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date=local_date().isoformat()
    daily=[]
    for u in users:
        g=daily_goal_for(u["id"], date.fromisoformat(selected_date))
        prod=q(f"""SELECT COUNT(DISTINCT rio) n FROM records WHERE user_id={PH} AND date(created_at)={PH}""",
               (u["id"],selected_date),True)["n"]
        daily.append({"id":u["id"],"name":u["name"],"username":u["username"],"active":u["active"],
                      "goal":g.get("base_goal",0),"effective_goal":g.get("goal",0),
                      "status":g.get("status","operacao"),"assigned":g.get("assigned",False),"production":prod})
    return render_template("master_dashboard.html",users=users,rows=rows,meta=int(setting("monthly_goal","100") or 0),
        total=start,today=today,tel=tel,bank=bank,other=other,byuser=byuser,
        daily=daily,selected_date=selected_date,informed=informed,pending=pending)

@app.route("/master/daily-goal", methods=["POST"])
@master_required
def master_daily_goal():
    user_id=int(request.form["user_id"])
    work_date=request.form.get("work_date") or local_date().isoformat()
    try: date.fromisoformat(work_date)
    except ValueError: work_date=local_date().isoformat()
    goal=max(0,int(request.form.get("goal","0")))
    current=daily_goal_for(user_id,date.fromisoformat(work_date))
    status=current.get("status","trabalhando") if current.get("assigned") else "trabalhando"
    upsert_daily_goal(user_id,date.fromisoformat(work_date),goal,status)
    flash("Meta diária atualizada.","success")
    return redirect(url_for("master_dashboard",date=work_date))

@app.route("/master/users/create",methods=["POST"])
@master_required
def create_user():
    name=request.form["name"].strip(); username=request.form["username"].strip().lower()
    password=request.form["password"]
    if len(password)<6: flash("Senha mínima de 6 caracteres.","danger")
    else:
        try:
            c=conn()
            if IS_POSTGRES:
                c.execute("INSERT INTO users(name,username,password_hash) VALUES(%s,%s,%s)",
                          (name,username,generate_password_hash(password)))
            else:
                c.execute("INSERT INTO users(name,username,password_hash,created_at) VALUES(?,?,?,?)",
                          (name,username,generate_password_hash(password),datetime.now().isoformat(timespec="seconds")))
            c.commit(); c.close(); flash("Usuário criado.","success")
        except Exception: flash("Não foi possível criar. Usuário pode já existir.","danger")
    return redirect(url_for("master_dashboard"))

@app.route("/master/users/<int:user_id>/edit", methods=["GET", "POST"])
@master_required
def edit_user(user_id):
    user=q(f"SELECT id,name,username,active FROM users WHERE id={PH}",(user_id,),True)
    if not user:
        flash("Usuário não encontrado.","danger")
        return redirect(url_for("master_dashboard"))
    if request.method == "POST":
        name=request.form.get("name","").strip()
        username=request.form.get("username","").strip().lower()
        password=request.form.get("password","")
        if not name or not username:
            flash("Nome e usuário são obrigatórios.","danger")
            return render_template("edit_user.html", user=user)
        if password and len(password)<6:
            flash("A nova senha deve ter pelo menos 6 caracteres.","danger")
            return render_template("edit_user.html", user=user)
        try:
            c=conn()
            if password:
                if IS_POSTGRES:
                    c.execute("UPDATE users SET name=%s, username=%s, password_hash=%s WHERE id=%s",
                              (name,username,generate_password_hash(password),user_id))
                else:
                    c.execute("UPDATE users SET name=?, username=?, password_hash=? WHERE id=?",
                              (name,username,generate_password_hash(password),user_id))
            else:
                if IS_POSTGRES:
                    c.execute("UPDATE users SET name=%s, username=%s WHERE id=%s",(name,username,user_id))
                else:
                    c.execute("UPDATE users SET name=?, username=? WHERE id=?",(name,username,user_id))
            c.commit(); c.close()
            flash("Dados do usuário atualizados.","success")
            return redirect(url_for("master_dashboard"))
        except Exception:
            try:
                c.rollback(); c.close()
            except Exception:
                pass
            flash("Não foi possível atualizar. O nome de usuário pode já existir.","danger")
    return render_template("edit_user.html", user=user)

@app.route("/master/users/<int:user_id>/delete",methods=["POST"])
@master_required
def delete_user(user_id):
    c=conn(); c.execute(f"UPDATE users SET active=0 WHERE id={PH}",(user_id,)); c.commit(); c.close()
    flash("Usuário removido do acesso.","success"); return redirect(url_for("master_dashboard"))

@app.route("/master/users/<int:user_id>/toggle",methods=["POST"])
@master_required
def toggle_user(user_id):
    c=conn()
    if IS_POSTGRES: c.execute("UPDATE users SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=%s",(user_id,))
    else: c.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(user_id,))
    c.commit(); c.close(); return redirect(url_for("master_dashboard"))

@app.route("/master/goal",methods=["POST"])
@master_required
def goal():
    value=max(0,int(request.form.get("monthly_goal","0")))
    c=conn()
    if IS_POSTGRES:
        c.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",("monthly_goal",str(value)))
    else:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",("monthly_goal",str(value)))
    c.commit(); c.close(); return redirect(url_for("master_dashboard"))

@app.route("/api/rio/<path:rio>")
@master_required
def api_rio(rio):
    rows=q(f"""SELECT r.rio,r.telephony,r.bank,r.other_offices,r.created_at,u.name
               FROM records r JOIN users u ON u.id=r.user_id WHERE r.rio={PH}
               ORDER BY r.created_at DESC""",(rio,))
    return jsonify([dict(x) for x in rows])

init_db()
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")))
