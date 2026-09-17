from pathlib import Path
import sys, tempfile, json
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from database import Database
from run_regression import run as run_regression

def check(cond,msg):
    if not cond:
        raise AssertionError(msg)

def stock(db,bid,mid):
    return next(x for x in db.get_menu_for_branch(bid) if x["id"]==mid)["quantity"]

def main():
    run_regression()

    # 50 parallel payment holds compete for exactly 3 units.
    with tempfile.TemporaryDirectory() as td:
        db=Database(str(Path(td)/"load.db"))
        with db.conn() as c:
            c.execute("UPDATE branch_schedule SET open_time='00:00',close_time='23:59',enabled=1")
            c.commit()
        bid=db.get_branches()[0]["id"]
        item=next(x for x in db.get_menu_for_branch(bid) if not x.get("is_special"))
        db.set_stock(item["id"],bid,3)
        orders=[]
        for i in range(50):
            uid=db.create_user(f"+79002{i:06d}")
            orders.append(db.create_order(uid,bid,[{"id":item["id"],"quantity":1}],None,None))

        def hold(oid):
            try:
                db.start_payment_hold(oid,10)
                return 1
            except Exception:
                return 0

        with ThreadPoolExecutor(max_workers=30) as ex:
            wins=sum(ex.map(hold,orders))
        check(wins==3,f"Expected 3 winners, got {wins}")
        check(stock(db,bid,item["id"])==0,"Stock must be exactly zero, never negative")

    client=(ROOT/"templates/client.html").read_text(encoding="utf-8")
    operator=(ROOT/"templates/operator.html").read_text(encoding="utf-8")
    admin=(ROOT/"templates/admin.html").read_text(encoding="utf-8")
    app=(ROOT/"app.py").read_text(encoding="utf-8")
    config=json.loads((ROOT/"local_payment_config.json").read_text(encoding="utf-8"))
    secret=config.get("yookassa_secret_key") or ""

    check((ROOT/"static/manifest.webmanifest").exists(),"PWA manifest missing")
    check((ROOT/"static/sw.js").exists(),"Service worker missing")
    check("serviceWorker" in client and "beforeinstallprompt" in client,"PWA client integration missing")
    check("offlineBanner" in client,"Offline state UI missing")
    check("@media(max-width:900px)" in client and "@media(min-width:901px)" in client,"Client must keep mobile and PC layouts")
    check("@media(max-width:1100px)" in operator,"Operator mobile layout missing")
    check("@media(max-width:900px)" in admin,"Admin responsive layout missing")
    check("/api/admin/health" in app and "/api/operator/menu" in app,"Critical V79/V77 routes missing")
    if secret:
        check(secret not in client and secret not in admin and secret not in app,"YooKassa secret leaked into public source")

    print("RELEASE GATE OK")
    print("Extra: 50 concurrent buyers / 3 units -> exactly 3 successful holds")
    print("Extra: PWA + offline + desktop/mobile contracts OK")
    print("Extra: payment secret not present in client/admin/app source")

if __name__=="__main__":
    main()
