import os, re
import calendar
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL=os.environ.get("DATABASE_URL","").strip()
IS_POSTGRES=DATABASE_URL.startswith(("postgres://","postgresql://"))
app=Flask(__name__); app.secret_key=os.environ.get("SECRET_KEY","CHANGE-ME")
RO_RE=re.compile(r"^\d{3}-\d{5}/\d{4}$"); TZ=ZoneInfo("America/Sao_Paulo")
def local_now(): return datetime.now(TZ)
def local_date(): return local_now().date()
if IS_POSTGRES:
 import psycopg
 from psycopg.rows import dict_row
 def conn():
  return psycopg.connect(DATABASE_URL.replace("postgres://","postgresql://",1),row_factory=dict_row)
 PH="%s"
else:
 import sqlite3
 DB_PATH=os.path.join(os.path.dirname(__file__),"estelionato.db")
 def conn():
  c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c
 PH="?"
def init_db():
 c=conn()
 if IS_POSTGRES:
  c.execute("CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY,name TEXT NOT NULL,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
  c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
  c.execute("CREATE TABLE IF NOT EXISTS records(id SERIAL PRIMARY KEY,rio TEXT NOT NULL,indicted_count INTEGER NOT NULL DEFAULT 0,telephony TEXT,bank TEXT,other_offices TEXT,telephony_returned INTEGER NOT NULL DEFAULT 0,bank_returned INTEGER NOT NULL DEFAULT 0,other_returned INTEGER NOT NULL DEFAULT 0,informed INTEGER NOT NULL DEFAULT 0,informed_at TIMESTAMP NULL,user_id INTEGER NOT NULL REFERENCES users(id),created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
  c.execute("CREATE TABLE IF NOT EXISTS daily_goals(id SERIAL PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),work_date DATE NOT NULL,goal INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'trabalhando',UNIQUE(user_id,work_date))")
  for col,typ in [("indicted_count","INTEGER NOT NULL DEFAULT 0"),("telephony_returned","INTEGER NOT NULL DEFAULT 0"),("bank_returned","INTEGER NOT NULL DEFAULT 0"),("other_returned","INTEGER NOT NULL DEFAULT 0"),("informed","INTEGER NOT NULL DEFAULT 0"),("informed_at","TIMESTAMP NULL")]: c.execute(f"ALTER TABLE records ADD COLUMN IF NOT EXISTS {col} {typ}")
 else:
  c.executescript("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY AUTOINCREMENT,rio TEXT NOT NULL,telephony TEXT,bank TEXT,other_offices TEXT,telephony_returned INTEGER NOT NULL DEFAULT 0,bank_returned INTEGER NOT NULL DEFAULT 0,other_returned INTEGER NOT NULL DEFAULT 0,informed INTEGER NOT NULL DEFAULT 0,informed_at TIMESTAMP NULL,user_id INTEGER NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));")
  c.execute("CREATE TABLE IF NOT EXISTS daily_goals(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,work_date TEXT NOT NULL,goal INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'trabalhando',UNIQUE(user_id,work_date),FOREIGN KEY(user_id) REFERENCES users(id))")
  cols={r[1] for r in c.execute("PRAGMA table_info(records)").fetchall()}
  for col,typ in [("indicted_count","INTEGER NOT NULL DEFAULT 0"),("telephony_returned","INTEGER NOT NULL DEFAULT 0"),("bank_returned","INTEGER NOT NULL DEFAULT 0"),("other_returned","INTEGER NOT NULL DEFAULT 0"),("informed","INTEGER NOT NULL DEFAULT 0"),("informed_at","TEXT")]:
   if col not in cols:c.execute(f"ALTER TABLE records ADD COLUMN {col} {typ}")
 mig2=c.execute("SELECT value FROM settings WHERE key='system_start_v2_migrated'").fetchone()
 if not mig2:
  fr=c.execute("SELECT MIN(created_at) AS first_date FROM records").fetchone(); fd=fr["first_date"] if fr and fr["first_date"] else local_date().isoformat(); fd=fd.date().isoformat() if hasattr(fd,"date") else str(fd)[:10]
  if IS_POSTGRES:
   c.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",("system_start_date",fd)); c.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",("system_start_v2_migrated","1"))
  else:
   c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",("system_start_date",fd)); c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",("system_start_v2_migrated","1"))
 mig=c.execute("SELECT value FROM settings WHERE key='daily_status_v12_migrated'").fetchone()
 if not mig:
  c.execute("UPDATE daily_goals SET status='trabalhando' WHERE status='operacao'")
  if IS_POSTGRES:c.execute("INSERT INTO settings(key,value) VALUES(%s,%s)",("daily_status_v12_migrated","1"))
  else:c.execute("INSERT INTO settings(key,value) VALUES(?,?)",("daily_status_v12_migrated","1"))
 c.commit();c.close()
def q(sql,params=(),one=False):
 c=conn();cur=c.execute(sql,params);data=cur.fetchone() if one else cur.fetchall();c.close();return data
def setting(key,default="0"):
 r=q(f"SELECT value FROM settings WHERE key={PH}",(key,),True);return r["value"] if r else default
def system_start_date():
 try:return date.fromisoformat(setting("system_start_date",local_date().isoformat()))
 except ValueError:return local_date()
def default_daily_goal(d):return 0 if d.weekday()>=5 else 3
def daily_goal_for(user_id,d=None):
 d=d or local_date();r=q(f"SELECT goal,status FROM daily_goals WHERE user_id={PH} AND work_date={PH}",(user_id,d),True)
 if not r:return {"goal":default_daily_goal(d),"base_goal":default_daily_goal(d),"status":"trabalhando","assigned":False}
 s=r["status"] or "trabalhando";g=0 if s in ("folga","operacao") else int(r["goal"] or 0);return {"goal":g,"base_goal":int(r["goal"] or 0),"status":s,"assigned":True}
def upsert_daily_goal(user_id,d,goal=3,status="trabalhando"):
 status=status if status in ("trabalhando","operacao","folga") else "trabalhando";goal=default_daily_goal(d);c=conn()
 if IS_POSTGRES:c.execute("INSERT INTO daily_goals(user_id,work_date,goal,status) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,work_date) DO UPDATE SET goal=EXCLUDED.goal,status=EXCLUDED.status",(user_id,d,goal,status))
 else:c.execute("INSERT INTO daily_goals(user_id,work_date,goal,status) VALUES(?,?,?,?) ON CONFLICT(user_id,work_date) DO UPDATE SET goal=excluded.goal,status=excluded.status",(user_id,str(d),goal,status))
 c.commit();c.close()
def master_required(f):
 @wraps(f)
 def w(*a,**k):return f(*a,**k) if session.get("master") else redirect(url_for("master_login"))
 return w
def user_required(f):
 @wraps(f)
 def w(*a,**k):return f(*a,**k) if session.get("user_id") else redirect(url_for("login"))
 return w
@app.route("/")
def index():return redirect(url_for("master_dashboard") if session.get("master") else url_for("user_dashboard") if session.get("user_id") else url_for("login"))
@app.route("/login",methods=["GET","POST"])
def login():
 if request.method=="POST":
  u=q(f"SELECT * FROM users WHERE username={PH} AND active=1",(request.form["username"].strip().lower(),),True)
  if u and check_password_hash(u["password_hash"],request.form["password"]):session.clear();session["user_id"]=u["id"];session["user_name"]=u["name"];return redirect(url_for("user_dashboard"))
  flash("Usuário ou senha inválidos.","danger")
 return render_template("login.html")
@app.route("/master/login",methods=["GET","POST"])
def master_login():
 if request.method=="POST":
  password=request.form["password"];h=os.environ.get("MASTER_PASSWORD_HASH")
  if not h:h=generate_password_hash("admin123")
  if check_password_hash(h,password):session.clear();session["master"]=True;return redirect(url_for("master_dashboard"))
  flash("Senha Master inválida.","danger")
 return render_template("master_login.html")
@app.route("/logout")
def logout():session.clear();return redirect(url_for("login"))
def is_overdue(record):
 if int(record["informed"] or 0) or int(record["telephony_returned"] or 0) or int(record["bank_returned"] or 0) or int(record["other_returned"] or 0):return False
 if not (record["telephony"] or record["bank"] or record["other_offices"]):return False
 created=record["created_at"]
 if isinstance(created,str):
  try:created=datetime.fromisoformat(created.replace("Z","+00:00"))
  except ValueError:
   try:created=datetime.strptime(created[:19],"%Y-%m-%d %H:%M:%S")
   except ValueError:return False
 if isinstance(created,datetime) and created.tzinfo:created=created.astimezone(TZ).replace(tzinfo=None)
 return datetime.now().replace(microsecond=0)-created>=timedelta(days=15)
def accumulated_for_user(user_id,through_date=None):
 end=through_date or local_date();start=system_start_date()
 if end<start:return {"goal":0,"real":0}
 gr=q(f"SELECT work_date,status FROM daily_goals WHERE user_id={PH} AND work_date>={PH} AND work_date<={PH}",(user_id,start,end));gm={str(x["work_date"]):(x["status"] or "trabalhando") for x in gr}
 pr=q(f"SELECT date(created_at) work_date,COUNT(DISTINCT rio) n FROM records WHERE user_id={PH} AND date(created_at)>={PH} AND date(created_at)<={PH} GROUP BY date(created_at)",(user_id,start.isoformat(),end.isoformat()));pm={str(x["work_date"]):int(x["n"] or 0) for x in pr}
 goal=real=0;d=start
 while d<=end:
  if d.weekday()<5 and gm.get(d.isoformat(),"trabalhando") not in ("folga","operacao"):goal+=3
  real+=pm.get(d.isoformat(),0);d+=timedelta(days=1)
 return {"goal":goal,"real":real}
@app.route("/dashboard")
@user_required
def user_dashboard():
 ro_filter=request.args.get("ro_filter","").strip()
 if ro_filter:
  raw=q(f"SELECT r.*,u.name FROM records r JOIN users u ON u.id=r.user_id WHERE r.user_id={PH} AND r.rio={PH} ORDER BY r.created_at DESC",(session["user_id"],ro_filter))
 else:
  raw=q(f"SELECT r.*,u.name FROM records r JOIN users u ON u.id=r.user_id WHERE r.user_id={PH} ORDER BY r.created_at DESC",(session["user_id"],))
 rows=[dict(r,overdue=is_overdue(r)) for r in raw];today=local_date();gi=daily_goal_for(session["user_id"],today);prod=q(f"SELECT COUNT(DISTINCT rio) n FROM records WHERE user_id={PH} AND date(created_at)={PH}",(session["user_id"],today.isoformat()),True)["n"];acc=accumulated_for_user(session["user_id"]);all_users=q("SELECT id,name,active FROM users ORDER BY name");team_daily=[]
 for u in all_users:
  ug=daily_goal_for(u["id"],today);uprod=q(f"SELECT COUNT(DISTINCT rio) n FROM records WHERE user_id={PH} AND date(created_at)={PH}",(u["id"],today.isoformat()),True)["n"];uacc=accumulated_for_user(u["id"]);team_daily.append({"id":u["id"],"name":u["name"],"active":u["active"],"goal":ug["goal"],"production":uprod,"status":ug["status"],"acc_goal":uacc["goal"],"acc_real":uacc["real"]})
 sd=request.args.get("status_date") or today.isoformat()
 try:so=date.fromisoformat(sd)
 except ValueError:so=today;sd=today.isoformat()
 ss=daily_goal_for(session["user_id"],so)
 return render_template("user_dashboard.html",rows=rows,daily_goal=gi,daily_production=prod,acc_goal=acc["goal"],acc_real=acc["real"],work_date=today.strftime("%d/%m/%Y"),status_date=sd,selected_status=ss,status_date_display=so.strftime("%d/%m/%Y"),team_daily=team_daily,ro_filter=ro_filter)
@app.route("/daily-status",methods=["POST"])
@user_required
def daily_status():
 status=request.form.get("status","trabalhando")
 if status not in ("trabalhando","operacao","folga"):status="trabalhando"
 try:d=date.fromisoformat(request.form.get("work_date") or local_date().isoformat())
 except ValueError:d=local_date()
 if d>local_date():flash("Não é permitido alterar um dia futuro.","danger");return redirect(url_for("user_dashboard"))
 upsert_daily_goal(session["user_id"],d,default_daily_goal(d),status);flash("Status do dia atualizado.","success");return redirect(url_for("user_dashboard",status_date=d.isoformat()))
@app.route("/records",methods=["POST"])
@user_required
def create_record():
 rio=request.form["rio"].strip();indicted_raw=request.form.get("indicted_count","").strip();tel=request.form.get("telephony","").strip();bank=request.form.get("bank","").strip();other=request.form.get("other_offices","").strip();tr=1 if request.form.get("telephony_returned")=="1" and tel else 0;br=1 if request.form.get("bank_returned")=="1" and bank else 0;orr=1 if request.form.get("other_returned")=="1" and other else 0
 try:indicted_count=int(indicted_raw)
 except ValueError:indicted_count=-1
 if not RO_RE.fullmatch(rio):flash("RO inválido. Use 000-00000/AAAA.","danger")
 elif indicted_count < 0:flash("Informe a quantidade de indiciados.","warning")
 elif tel not in ("Vivo","TIM","Claro"):flash("FAÇA CONTATO COM A VÍTIMA E IDENTIFIQUE O TELEFONE QUE FEZ CONTATO.","warning")
 elif not bank or not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]",bank):flash("IDENTIFIQUE COM A VITIMA, SOLICITE O COMPROVANTE PARA SABER PARA QUE BANCO O $ FOI TRASFERIDO","warning")
 else:
  existing=q(f"SELECT r.created_at,u.name FROM records r JOIN users u ON u.id=r.user_id WHERE r.rio={PH} ORDER BY r.created_at ASC LIMIT 1",(rio,),True)
  if existing:flash(f"Este procedimento já está sendo trabalhado por {existing['name']}.","warning")
  else:
   c=conn();now=datetime.now()
   if IS_POSTGRES:c.execute("INSERT INTO records(rio,indicted_count,telephony,bank,other_offices,telephony_returned,bank_returned,other_returned,informed,informed_at,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,NULL,%s)",(rio,indicted_count,tel or None,bank or None,other or None,tr,br,orr,session["user_id"]))
   else:c.execute("INSERT INTO records(rio,indicted_count,telephony,bank,other_offices,telephony_returned,bank_returned,other_returned,informed,informed_at,user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(rio,indicted_count,tel or None,bank or None,other or None,tr,br,orr,0,None,session["user_id"],now.isoformat(timespec="seconds")))
   c.commit();c.close();flash("Procedimento salvo.","success")
 return redirect(url_for("user_dashboard"))
@app.route("/records/<int:record_id>/delete",methods=["POST"])
@user_required
def delete_record(record_id):
 r=q(f"SELECT id,user_id FROM records WHERE id={PH}",(record_id,),True)
 if not r or int(r["user_id"])!=int(session["user_id"]):
  flash("Você não pode excluir este procedimento.","danger");return redirect(url_for("user_dashboard"))
 c=conn()
 c.execute(f"DELETE FROM records WHERE id={PH}",(record_id,))
 c.commit();c.close();flash("Lançamento excluído.","success");return redirect(url_for("user_dashboard"))

@app.route("/master/records/<int:record_id>/delete",methods=["POST"])
@master_required
def master_delete_record(record_id):
 r=q(f"SELECT id FROM records WHERE id={PH}",(record_id,),True)
 if not r:
  flash("Procedimento não encontrado.","danger");return redirect(url_for("master_dashboard"))
 c=conn()
 c.execute(f"DELETE FROM records WHERE id={PH}",(record_id,))
 c.commit();c.close();flash("Lançamento excluído pelo Master.","success");return redirect(url_for("master_dashboard"))

@app.route("/records/<int:record_id>/toggle-return/<kind>",methods=["POST"])
@user_required
def toggle_return(record_id,kind):
 column={"telephony":"telephony_returned","bank":"bank_returned","other":"other_returned"}.get(kind)
 if not column:flash("Tipo de retorno inválido.","danger");return redirect(url_for("user_dashboard"))
 r=q(f"SELECT id,user_id FROM records WHERE id={PH}",(record_id,),True)
 if not r or int(r["user_id"])!=int(session["user_id"]):flash("Você não pode alterar este procedimento.","danger");return redirect(url_for("user_dashboard"))
 c=conn()
 if IS_POSTGRES:c.execute(f"UPDATE records SET {column}=CASE WHEN {column}=1 THEN 0 ELSE 1 END WHERE id=%s",(record_id,))
 else:c.execute(f"UPDATE records SET {column}=CASE {column} WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(record_id,))
 c.commit();c.close();return redirect(url_for("user_dashboard"))
@app.route("/records/<int:record_id>/toggle-informed",methods=["POST"])
@user_required
def toggle_informed(record_id):
 r=q(f"SELECT id,user_id,informed,indicted_count FROM records WHERE id={PH}",(record_id,),True)
 if not r or int(r["user_id"])!=int(session["user_id"]):flash("Você não pode alterar este procedimento.","danger");return redirect(url_for("user_dashboard"))
 if not int(r["informed"] or 0) and int(r["indicted_count"] or 0) <= 0:flash("Informe a quantidade de indiciados antes de marcar como informado.","warning");return redirect(url_for("user_dashboard"))
 new=0 if int(r["informed"] or 0) else 1;stamp=local_now().strftime("%Y-%m-%d %H:%M:%S") if new else None;c=conn()
 if IS_POSTGRES:c.execute("UPDATE records SET informed=%s,informed_at=%s WHERE id=%s",(new,stamp,record_id))
 else:c.execute("UPDATE records SET informed=?,informed_at=? WHERE id=?",(new,stamp,record_id))
 c.commit();c.close();return redirect(url_for("user_dashboard"))
@app.route("/master/records/<int:record_id>/toggle-informed",methods=["POST"])
@master_required
def master_toggle_informed(record_id):
 r=q(f"SELECT informed,indicted_count FROM records WHERE id={PH}",(record_id,),True)
 if not r:flash("Procedimento não encontrado.","danger");return redirect(url_for("master_dashboard"))
 if not int(r["informed"] or 0) and int(r["indicted_count"] or 0) <= 0:flash("Informe a quantidade de indiciados antes de marcar como informado.","warning");return redirect(url_for("master_dashboard"))
 new=0 if int(r["informed"] or 0) else 1;stamp=local_now().strftime("%Y-%m-%d %H:%M:%S") if new else None;c=conn()
 if IS_POSTGRES:c.execute("UPDATE records SET informed=%s,informed_at=%s WHERE id=%s",(new,stamp,record_id))
 else:c.execute("UPDATE records SET informed=?,informed_at=? WHERE id=?",(new,stamp,record_id))
 c.commit();c.close();return redirect(url_for("master_dashboard"))
@app.route("/master/records/<int:record_id>/toggle-return/<kind>",methods=["POST"])
@master_required
def master_toggle_return(record_id,kind):
 column={"telephony":"telephony_returned","bank":"bank_returned","other":"other_returned"}.get(kind)
 if not column:flash("Tipo de retorno inválido.","danger");return redirect(url_for("master_dashboard"))
 c=conn()
 if IS_POSTGRES:c.execute(f"UPDATE records SET {column}=CASE WHEN {column}=1 THEN 0 ELSE 1 END WHERE id=%s",(record_id,))
 else:c.execute(f"UPDATE records SET {column}=CASE {column} WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(record_id,))
 c.commit();c.close();return redirect(url_for("master_dashboard"))
@app.route("/master")
@master_required
def master_dashboard():
 ro_filter=request.args.get("ro_filter","").strip()
 users=q("SELECT id,name,username,active,created_at FROM users ORDER BY name")
 if ro_filter:
  raw=q(f"SELECT r.*,u.name FROM records r JOIN users u ON u.id=r.user_id WHERE r.rio={PH} ORDER BY r.created_at DESC",(ro_filter,))
 else:
  raw=q("SELECT r.*,u.name FROM records r JOIN users u ON u.id=r.user_id ORDER BY r.created_at DESC")
 rows=[dict(r,overdue=is_overdue(r)) for r in raw];start=q("SELECT COUNT(*) n FROM records",one=True)["n"];today=q("SELECT COUNT(*) n FROM records WHERE DATE(created_at)=CURRENT_DATE" if IS_POSTGRES else "SELECT COUNT(*) n FROM records WHERE date(created_at)=date('now','localtime')",one=True)["n"];tel=q("SELECT COUNT(*) n FROM records WHERE telephony IS NOT NULL AND telephony<>''",one=True)["n"];bank=q("SELECT COUNT(*) n FROM records WHERE bank IS NOT NULL AND bank<>''",one=True)["n"];other=q("SELECT COUNT(*) n FROM records WHERE other_offices IS NOT NULL AND other_offices<>''",one=True)["n"];informed=q("SELECT COUNT(*) n FROM records WHERE informed=1",one=True)["n"];pending=start-informed
 month_start=local_date().replace(day=1)
 month_end=local_date()
 points_by_user=q(f"SELECT u.name,COALESCE(SUM(CASE WHEN date(r.created_at)>={PH} AND date(r.created_at)<={PH} THEN r.indicted_count ELSE 0 END),0) indicted FROM users u LEFT JOIN records r ON r.user_id=u.id GROUP BY u.id,u.name ORDER BY u.name",(month_start.isoformat(),month_end.isoformat()))
 byuser=q("SELECT u.name,COUNT(DISTINCT r.rio) procedures,COUNT(DISTINCT CASE WHEN r.informed=1 THEN r.rio END) informed,SUM(CASE WHEN r.telephony IS NOT NULL AND r.telephony<>'' THEN 1 ELSE 0 END) telephony,SUM(CASE WHEN r.bank IS NOT NULL AND r.bank<>'' THEN 1 ELSE 0 END) bank,SUM(CASE WHEN r.other_offices IS NOT NULL AND r.other_offices<>'' THEN 1 ELSE 0 END) other,SUM(CASE WHEN r.telephony_returned=1 THEN 1 ELSE 0 END) telephony_returned,SUM(CASE WHEN r.bank_returned=1 THEN 1 ELSE 0 END) bank_returned,SUM(CASE WHEN r.other_returned=1 THEN 1 ELSE 0 END) other_returned FROM users u LEFT JOIN records r ON r.user_id=u.id GROUP BY u.id,u.name ORDER BY procedures DESC")
 selected_date=request.args.get("date") or local_date().isoformat()
 try:date.fromisoformat(selected_date)
 except ValueError:selected_date=local_date().isoformat()
 daily=[]
 for u in users:
  g=daily_goal_for(u["id"],date.fromisoformat(selected_date));prod=q(f"SELECT COUNT(DISTINCT rio) n FROM records WHERE user_id={PH} AND date(created_at)={PH}",(u["id"],selected_date),True)["n"];acc=accumulated_for_user(u["id"])
  daily.append({"id":u["id"],"name":u["name"],"username":u["username"],"active":u["active"],"goal":g.get("goal",0),"effective_goal":g.get("goal",0),"status":g.get("status","trabalhando"),"assigned":True,"production":prod,"acc_goal":acc["goal"],"acc_real":acc["real"]})
 # Calendário mensal: cada dia é dividido em um bloco por usuário.
 calendar_month=request.args.get("month") or local_date().strftime("%Y-%m")
 try:
  cal_year,cal_mon=map(int,calendar_month.split("-"))
  if cal_mon<1 or cal_mon>12: raise ValueError
 except ValueError:
  cal_year,cal_mon=local_date().year,local_date().month
  calendar_month=f"{cal_year:04d}-{cal_mon:02d}"
 first_day=date(cal_year,cal_mon,1)
 last_day=date(cal_year,cal_mon,calendar.monthrange(cal_year,cal_mon)[1])
 prev_month=(first_day-timedelta(days=1)).strftime("%Y-%m")
 next_month=(last_day+timedelta(days=1)).strftime("%Y-%m")
 month_prod={}
 month_status={}
 for u in users[:3]:
  prod_rows=q(f"SELECT date(created_at) work_date,COUNT(DISTINCT rio) n FROM records WHERE user_id={PH} AND date(created_at)>={PH} AND date(created_at)<={PH} GROUP BY date(created_at)",(u["id"],first_day.isoformat(),last_day.isoformat()))
  month_prod[u["id"]]={str(x["work_date"]):int(x["n"] or 0) for x in prod_rows}
  st_rows=q(f"SELECT work_date,status FROM daily_goals WHERE user_id={PH} AND work_date>={PH} AND work_date<={PH}",(u["id"],first_day,last_day))
  month_status[u["id"]]={str(x["work_date"]):(x["status"] or "trabalhando") for x in st_rows}
 calendar_days=[]
 d=first_day
 while d<=last_day:
  segments=[]
  for u in users[:3]:
   key=d.isoformat()
   if d>local_date():
    st="future"; count=None
   elif d.weekday()>=5:
    st="weekend"; count=month_prod.get(u["id"],{}).get(key,0)
   else:
    st=month_status.get(u["id"],{}).get(key,"trabalhando"); count=month_prod.get(u["id"],{}).get(key,0)
   segments.append({"name":u["name"],"count":count,"status":st})
  calendar_days.append({"date":d,"day":d.day,"weekday":d.weekday(),"segments":segments})
  d+=timedelta(days=1)
 leading_days=first_day.weekday()
 calendar_days=[{"date":None,"day":None,"weekday":None,"segments":[]} for _ in range(leading_days)]+calendar_days
 return render_template("master_dashboard.html",users=users,rows=rows,meta=int(setting("monthly_goal","100") or 0),total=start,today=today,tel=tel,bank=bank,other=other,byuser=byuser,daily=daily,selected_date=selected_date,informed=informed,pending=pending,points_by_user=points_by_user,points_factor=5.25,ro_filter=ro_filter,calendar_days=calendar_days,calendar_month=calendar_month,calendar_month_label=first_day.strftime("%B/%Y").capitalize(),calendar_prev=prev_month,calendar_next=next_month,calendar_users=users[:3])
@app.route("/master/daily-goal",methods=["POST"])
@master_required
def master_daily_goal():
 user_id=int(request.form["user_id"]);wd=request.form.get("work_date") or local_date().isoformat()
 try:d=date.fromisoformat(wd)
 except ValueError:d=local_date()
 goal=max(0,int(request.form.get("goal","0")));status=daily_goal_for(user_id,d).get("status","trabalhando");upsert_daily_goal(user_id,d,goal,status);flash("Meta diária atualizada.","success");return redirect(url_for("master_dashboard",date=wd))
@app.route("/master/users/create",methods=["POST"])
@master_required
def create_user():
 name=request.form["name"].strip();username=request.form["username"].strip().lower();password=request.form["password"]
 if len(password)<6:flash("Senha mínima de 6 caracteres.","danger")
 else:
  try:
   c=conn()
   if IS_POSTGRES:c.execute("INSERT INTO users(name,username,password_hash) VALUES(%s,%s,%s)",(name,username,generate_password_hash(password)))
   else:c.execute("INSERT INTO users(name,username,password_hash,created_at) VALUES(?,?,?,?)",(name,username,generate_password_hash(password),datetime.now().isoformat(timespec="seconds")))
   c.commit();c.close();flash("Usuário criado.","success")
  except Exception:flash("Não foi possível criar. Usuário pode já existir.","danger")
 return redirect(url_for("master_dashboard"))
@app.route("/master/users/<int:user_id>/edit",methods=["GET","POST"])
@master_required
def edit_user(user_id):
 u=q(f"SELECT id,name,username,active FROM users WHERE id={PH}",(user_id,),True)
 if not u:flash("Usuário não encontrado.","danger");return redirect(url_for("master_dashboard"))
 if request.method=="POST":
  name=request.form.get("name","").strip();username=request.form.get("username","").strip().lower();password=request.form.get("password","")
  if not name or not username:flash("Nome e usuário são obrigatórios.","danger");return render_template("edit_user.html",user=u)
  if password and len(password)<6:flash("A nova senha deve ter pelo menos 6 caracteres.","danger");return render_template("edit_user.html",user=u)
  try:
   c=conn()
   if password:
    if IS_POSTGRES:c.execute("UPDATE users SET name=%s,username=%s,password_hash=%s WHERE id=%s",(name,username,generate_password_hash(password),user_id))
    else:c.execute("UPDATE users SET name=?,username=?,password_hash=? WHERE id=?",(name,username,generate_password_hash(password),user_id))
   else:
    if IS_POSTGRES:c.execute("UPDATE users SET name=%s,username=%s WHERE id=%s",(name,username,user_id))
    else:c.execute("UPDATE users SET name=?,username=? WHERE id=?",(name,username,user_id))
   c.commit();c.close();flash("Dados do usuário atualizados.","success");return redirect(url_for("master_dashboard"))
  except Exception:
   try:c.rollback();c.close()
   except Exception:pass
   flash("Não foi possível atualizar. O nome de usuário pode já existir.","danger")
 return render_template("edit_user.html",user=u)
@app.route("/master/users/<int:user_id>/delete",methods=["POST"])
@master_required
def delete_user(user_id):
 c=conn();c.execute(f"UPDATE users SET active=0 WHERE id={PH}",(user_id,));c.commit();c.close();flash("Usuário removido do acesso.","success");return redirect(url_for("master_dashboard"))
@app.route("/master/users/<int:user_id>/toggle",methods=["POST"])
@master_required
def toggle_user(user_id):
 c=conn()
 if IS_POSTGRES:c.execute("UPDATE users SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=%s",(user_id,))
 else:c.execute("UPDATE users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(user_id,))
 c.commit();c.close();return redirect(url_for("master_dashboard"))
@app.route("/master/goal",methods=["POST"])
@master_required
def goal():
 value=max(0,int(request.form.get("monthly_goal","0")));c=conn()
 if IS_POSTGRES:c.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",("monthly_goal",str(value)))
 else:c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",("monthly_goal",str(value)))
 c.commit();c.close();return redirect(url_for("master_dashboard"))
@app.route("/api/rio/<path:rio>")
@master_required
def api_rio(rio):
 rows=q(f"SELECT r.rio,r.telephony,r.bank,r.other_offices,r.created_at,u.name FROM records r JOIN users u ON u.id=r.user_id WHERE r.rio={PH} ORDER BY r.created_at DESC",(rio,));return jsonify([dict(x) for x in rows])
init_db()
if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")))