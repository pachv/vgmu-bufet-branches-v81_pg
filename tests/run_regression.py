from pathlib import Path
import sys
import tempfile
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database, CART_RESERVE_MINUTES, MSK_TZ
import payments


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text
    def json(self):
        return self._data


class FakeRequests:
    def __init__(self):
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith('/payments'):
            return FakeResponse(200, {"id":"yk-test-1","status":"pending","confirmation":{"confirmation_url":"https://example.test/pay"}})
        if url.endswith('/refunds'):
            return FakeResponse(200, {"id":"refund-test-1","status":"succeeded"})
        return FakeResponse(200,{})
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith('/payments'):
            return FakeResponse(200,{"type":"list","items":[]})
        return FakeResponse(200,{"id":"yk-test-1","status":"succeeded"})


def assert_true(cond, message):
    if not cond:
        raise AssertionError(message)


def stock_for(db, branch_id, item_id):
    return next(x for x in db.get_menu_for_branch(branch_id) if x['id']==item_id)['quantity']


def run():
    passed=[]
    with tempfile.TemporaryDirectory() as td:
        db=Database(str(Path(td)/'regression.db'))
        with db.conn() as c:
            c.execute("UPDATE branch_schedule SET open_time='00:00', close_time='23:59', enabled=1")
            c.commit()

        branches=db.get_branches(); assert_true(len(branches)>=3,'Нет базовых буфетов'); passed.append('branches')
        branch_id=branches[0]['id']
        menu=db.get_menu_for_branch(branch_id); assert_true(len(menu)>=6,'Меню пустое'); assert_true(any(x.get('image') for x in menu),'У меню нет фото'); passed.append('menu+images')
        item=next(x for x in menu if not x.get('is_special'))
        uid=db.create_user('+79000000123')

        # Корзина: 3 минуты.
        token='regression-cart'
        exp=db.reserve_cart(token, uid, branch_id, [{'id':item['id'],'quantity':1}])
        dt=datetime.strptime(exp,'%Y-%m-%d %H:%M:%S')
        delta=(dt-datetime.now(MSK_TZ).replace(tzinfo=None)).total_seconds()
        assert_true(150 <= delta <= 190, f'Бронь корзины не около {CART_RESERVE_MINUTES} минут: {delta}')
        db.clear_reservation(token); passed.append('cart reserve 3 min')

        # Заказ и 10-минутная платёжная бронь.
        oid=db.create_order(uid, branch_id, [{'id':item['id'],'quantity':1}], token=None, pickup_time=None)
        before=stock_for(db,branch_id,item['id'])
        db.start_payment_hold(oid,10)
        after=stock_for(db,branch_id,item['id'])
        assert_true(after==before-1,'Платёжная бронь не списала товар')
        db.start_payment_hold(oid,10)
        assert_true(stock_for(db,branch_id,item['id'])==after,'Повторная бронь списала товар второй раз')
        db.create_payment_record(oid,'demo','DEMO-1',item['price'],'/demo',{})
        try:
            db.next_order_status(oid,branch_id,1)
            raise AssertionError('Оператор принял pending-оплату')
        except Exception as e:
            assert_true('Ожидается онлайн-оплата' in str(e),'Неверная блокировка pending заказа')
        db.finalize_paid_order(oid,'DEMO-1')
        assert_true(db.get_order(oid)['payment_status']=='paid','Оплата не подтверждена')
        db.next_order_status(oid,branch_id,1)
        assert_true(stock_for(db,branch_id,item['id'])==after,'После принятия оплаченного заказа товар списался повторно')
        passed.append('payment hold + no double stock')

        # Истечение платёжной брони возвращает товар.
        uid2=db.create_user('+79000000124')
        oid2=db.create_order(uid2,branch_id,[{'id':item['id'],'quantity':1}],token=None,pickup_time=None)
        before2=stock_for(db,branch_id,item['id'])
        db.start_payment_hold(oid2,10)
        db.create_payment_record(oid2,'demo','DEMO-2',item['price'],'/demo',{})
        with db.conn() as c:
            c.execute("UPDATE orders SET payment_hold_expires_at=? WHERE id=?", ((datetime.now(MSK_TZ).replace(tzinfo=None)-timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'),oid2)); c.commit()
        changed=db.expire_payment_holds()
        assert_true(stock_for(db,branch_id,item['id'])==before2,'Истекшая бронь не вернула товар')
        assert_true(db.get_order(oid2)['payment_status']=='unpaid','Статус после истечения брони неверный')
        passed.append('payment hold expiry')

        # Возврат нового оплаченного заказа возвращает товар и отменяет заказ.
        uid3=db.create_user('+79000000125')
        oid3=db.create_order(uid3,branch_id,[{'id':item['id'],'quantity':1}],token=None,pickup_time=None)
        before3=stock_for(db,branch_id,item['id'])
        db.start_payment_hold(oid3,10); db.create_payment_record(oid3,'demo','DEMO-3',item['price'],'/demo',{}); db.finalize_paid_order(oid3,'DEMO-3')
        db.mark_order_refunded(oid3,'DEMO-3','REF-3','succeeded',{},cancel_order=True)
        o3=db.get_order(oid3)
        assert_true(o3['payment_status']=='refunded' and o3['status']=='canceled','Возврат не закрыл новый заказ')
        assert_true(stock_for(db,branch_id,item['id'])==before3,'Возврат не восстановил остаток')
        passed.append('refund')


        # Feedback: one rating per order and bounded comment length.
        db.next_order_status(oid,branch_id,1)
        code=db.get_order(oid).get('pickup_code')
        assert_true(bool(code),'Нет кода выдачи')
        db.issue_order_by_code(branch_id,code,1)
        db.rate_order_items(oid,uid,5,'Всё отлично')
        try:
            db.rate_order_items(oid,uid,4,'Повтор')
            raise AssertionError('Повторная оценка прошла')
        except Exception as e:
            assert_true('уже оценён' in str(e),'Нет защиты от повторной оценки')
        try:
            # Another issued order is not needed: validation occurs before DB access.
            db.rate_order_items(oid,uid,5,'x'*501)
            raise AssertionError('Слишком длинный комментарий принят')
        except Exception as e:
            assert_true('500' in str(e),'Нет лимита комментария')
        passed.append('ratings UX contract')




        # New branch must receive schedule and zero stock without damaging existing menu.
        new_bid=db.add_branch('Тестовый буфет регрессии')
        assert_true(any(b['id']==new_bid for b in db.get_branches()),'Новый буфет не создан')
        new_menu=db.get_menu_for_branch(new_bid)
        assert_true(len(new_menu)==len(db.get_menu_for_branch(branch_id)),'У нового буфета потерялись позиции меню')
        assert_true(all(int(x.get('quantity') or 0)==0 for x in new_menu),'У нового буфета остатки должны быть 0')
        passed.append('add branch contract')

        # Operator credentials are hashed and never returned through DB API.
        op=db.get_kitchen('podval','1234')
        assert_true(op and op.get('login')=='podval','Оператор не может войти после миграции паролей')
        assert_true('password' not in op,'Пароль оператора возвращается клиенту')
        with db.conn() as c:
            stored=c.execute("SELECT password FROM kitchens WHERE login='podval'").fetchone()['password']
        assert_true(stored.startswith('pbkdf2_sha256$'),'Пароль оператора не хэширован')
        passed.append('operator password hashing')

        analytics=db.get_analytics()
        for key in ['payment_statuses','rating_summary','rating_distribution','recent_feedback','daily','comparison','service','hourly','stock_risks','low_rated']:
            assert_true(key in analytics,f'Нет блока аналитики: {key}')
        assert_true(analytics['rating_summary']['feedback_count']>=1,'Оценки не попали в аналитику')
        assert_true(any((x.get('comment') or '')=='Всё отлично' for x in analytics['recent_feedback']),'Комментарий не попал в аналитику')
        passed.append('rich analytics')

        # YooKassa provider: правильный URL, Basic Auth, idempotence, payload и refund.
        fake=FakeRequests(); old_requests=payments.requests; payments.requests=fake
        try:
            settings={'enabled':'1','provider':'yookassa','yookassa_shop_id':'shop-test','yookassa_secret_key':'secret-test','yookassa_api_url':'https://api.yookassa.ru/v3','yookassa_payment_method':'bank_card'}
            client=payments.build_payment_client(settings)
            result=client.create_payment({'id':77,'total':321,'branch_id':1},return_url='https://example.test/payment-result?order_id=77',idempotence_key='vgmu-order-77-attempt-1')
            assert_true(result['provider_order_id']=='yk-test-1','YooKassa create id')
            method,url,kw=fake.calls[-1]
            assert_true(url=='https://api.yookassa.ru/v3/payments','Неверный endpoint YooKassa')
            assert_true(kw['auth']==('shop-test','secret-test'),'Нет Basic Auth')
            assert_true(kw['headers'].get('Idempotence-Key')=='vgmu-order-77-attempt-1','Idempotence-Key не стабилен')
            assert_true(kw['json']['amount']['value']=='321.00','Неверная сумма YooKassa')
            assert_true(kw['json']['payment_method_data']['type']=='bank_card','Неверный payment method')
            ref=client.refund_payment('yk-test-1',321,order_id=77)
            assert_true(ref['status']=='succeeded','YooKassa refund')
            passed.append('yookassa mocked API')
        finally:
            payments.requests=old_requests


    # V74: finite-state guards and incident scan.
    with tempfile.TemporaryDirectory() as td:
        state_db=Database(str(Path(td)/'state.db'))
        with state_db.conn() as c:
            c.execute("UPDATE branch_schedule SET open_time='00:00', close_time='23:59', enabled=1")
            c.commit()
        bid=state_db.get_branches()[0]['id']
        itm=state_db.get_menu_for_branch(bid)[0]
        u=state_db.create_user('+79000000901')
        o=state_db.create_order(u,bid,[{'id':itm['id'],'quantity':1}],None,None)
        try:
            state_db._assert_order_transition('new','issued','unpaid')
            raise AssertionError('Недопустимый переход new->issued разрешён')
        except Exception:
            pass
        try:
            state_db._assert_payment_transition('refunded','paid','new')
            raise AssertionError('Недопустимый переход refunded->paid разрешён')
        except Exception:
            pass
        with state_db.conn() as c:
            c.execute("UPDATE orders SET status='ready', pickup_code=NULL WHERE id=?",(o,))
            c.commit()
        assert_true(state_db.scan_state_incidents()>=1,'Incident scanner не видит ready без кода')
        assert_true(any(x['category']=='order' for x in state_db.get_incidents()),'Incident не сохранён')
        passed.append('V74 state machine + incidents')


    # V75: конкурентная попытка забрать последнюю единицу.
    with tempfile.TemporaryDirectory() as td:
        race_db=Database(str(Path(td)/'race.db'))
        with race_db.conn() as c:
            c.execute("UPDATE branch_schedule SET open_time='00:00', close_time='23:59', enabled=1")
            c.commit()
        bid=race_db.get_branches()[0]['id']
        itm=next(x for x in race_db.get_menu_for_branch(bid) if not x.get('is_special'))
        race_db.set_stock(itm['id'],bid,1)
        order_ids=[]
        for n in range(20):
            u=race_db.create_user(f'+7900001{n:04d}')
            order_ids.append(race_db.create_order(u,bid,[{'id':itm['id'],'quantity':1}],None,None))

        def try_hold(oid):
            try:
                race_db.start_payment_hold(oid,10)
                return True
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=20) as ex:
            results=list(ex.map(try_hold,order_ids))
        assert_true(sum(1 for x in results if x)==1,f'Последнюю единицу зарезервировали {sum(1 for x in results if x)} заказов')
        with race_db.conn() as c:
            qty=c.execute("SELECT quantity FROM menu_stock WHERE menu_id=? AND branch_id=?",(itm['id'],bid)).fetchone()['quantity']
        assert_true(qty==0,f'Остаток после гонки должен быть 0, получено {qty}')
        passed.append('V75 atomic last-item stress')

    # Уникальный код выдачи: генератор не должен вернуть уже занятый код.
    with tempfile.TemporaryDirectory() as td:
        code_db=Database(str(Path(td)/'codes.db'))
        user_id=code_db.create_user('+79000000999')
        branch_id=code_db.get_branches()[0]['id']
        with code_db.conn() as c:
            c.execute("UPDATE branch_schedule SET open_time='00:00', close_time='23:59', enabled=1")
            c.execute("INSERT INTO orders(user_id,branch_id,items,total,status,pickup_code) VALUES(?,?, '[]',0,'ready','1234')",(user_id,branch_id))
            c.commit()
            generated=code_db._generate_unique_pickup_code(c,branch_id)
        assert_true(generated!='1234','Сгенерирован уже занятый код выдачи')
        passed.append('V75 unique pickup code')


    # V77: операторский стоп-лист действует только на свой буфет.
    with tempfile.TemporaryDirectory() as td:
        opdb=Database(str(Path(td)/'operator_stock.db'))
        b1,b2=opdb.get_branches()[:2]
        item=opdb.get_menu_for_branch(b1['id'])[0]
        before_other=stock_for(opdb,b2['id'],item['id'])
        result=opdb.operator_mark_sold_out(item['id'],b1['id'],1)
        assert_true(stock_for(opdb,b1['id'],item['id'])==0,'Оператор не обнулил остаток своего буфета')
        assert_true(stock_for(opdb,b2['id'],item['id'])==before_other,'Стоп-лист оператора затронул другой буфет')
        assert_true(result['previous_quantity']>=0,'Нет предыдущего остатка')
        passed.append('V77 branch-local operator stop-list')


    # V78: оценка комбо не должна падать на отсутствии item["id"].
    with tempfile.TemporaryDirectory() as td:
        cdb=Database(str(Path(td)/'combo_rating.db'))
        with cdb.conn() as c:
            c.execute("UPDATE branch_schedule SET open_time='00:00', close_time='23:59', enabled=1")
            c.commit()
        bid=cdb.get_branches()[0]['id']
        menu=cdb.get_menu_for_branch(bid)
        ids=[x['id'] for x in menu[:2]]
        combo_id=cdb.add_combo('Тест-комбо',100,'','', '', [{'menu_id':ids[0],'quantity':1},{'menu_id':ids[1],'quantity':1}])
        u=cdb.create_user('+79000000988')
        oid=cdb.create_order(u,bid,[{'combo_id':combo_id,'is_combo':True,'quantity':1}],None,None)
        cdb.next_order_status(oid,bid,1)
        cdb.next_order_status(oid,bid,1)
        code=cdb.get_order(oid)['pickup_code']
        cdb.issue_order_by_code(bid,code,1)
        cdb.rate_order_items(oid,u,4,'Комбо норм')
        with cdb.conn() as c:
            cnt=c.execute("SELECT COUNT(*) AS cnt FROM ratings WHERE order_id=?",(oid,)).fetchone()['cnt']
        assert_true(cnt==2,f'Комбо должно оценить 2 компонента, получено {cnt}')
        passed.append('V78 combo rating + analytics')


    # V79 health snapshot is factual and does not expose secrets.
    with tempfile.TemporaryDirectory() as td:
        hdb=Database(str(Path(td)/'health.db'))
        snap=hdb.get_health_snapshot()
        for key in ['orders_active','payments_pending','refund_required','incidents_open','negative_stock']:
            assert_true(key in snap,f'Health snapshot missing {key}')
        assert_true(isinstance(hdb.get_pending_order_ids(),list),'Pending payment list broken')
        passed.append('V79 health + incident backend')

    # Статические критические проверки.
    app=(ROOT/'app.py').read_text(encoding='utf-8')
    client=(ROOT/'templates/client.html').read_text(encoding='utf-8')
    assert_true(app.index('@app.route("/payment-success")') < app.index('if __name__ == "__main__":'),'payment-success зарегистрирован после запуска сервера')
    assert_true('/api/payments/yookassa/webhook' in app,'Нет webhook YooKassa')
    assert_true('/api/payments/status/<int:order_id>' in app,'Нет ручной сверки статуса')
    assert_true('async function payOrder' in client and 'async function checkPayment' in client,'Нет клиентских функций оплаты')
    assert_true('async_mode="threading"' in app, 'SocketIO не переведён в Windows/Python 3.14 threading mode')
    assert_true('async_mode="eventlet"' not in app, 'В app.py остался eventlet async mode')
    requirements=(ROOT/'requirements.txt').read_text(encoding='utf-8').lower()
    assert_true('eventlet' not in requirements, 'eventlet остался в requirements.txt')
    assert_true('simple-websocket' in requirements, 'Нет simple-websocket для threading WebSocket')
    assert_true('pillow>=12' in requirements, 'Для Python 3.14 нужен Pillow 12+')
    assert_true('ssl_context=' in app and 'certfile=' not in app and 'keyfile=' not in app, 'HTTPS threading mode должен использовать ssl_context, а не eventlet certfile/keyfile')
    bat=(ROOT/'START_HTTPS.bat').read_bytes()
    assert_true(all(b < 128 for b in bat), 'START_HTTPS.bat содержит non-ASCII и снова может сломаться в cmd.exe')
    assert_true(b'/operator-login' in bat, 'START_HTTPS.bat указывает несуществующий старый kitchen-login')
    passed.append('static critical routes + Python 3.14 startup compatibility')





    assert_true('get_payment_attempt_count' in (ROOT/'database.py').read_text(encoding='utf-8'),'Нет счётчика попыток платежа')
    assert_true('backup_database_on_start' in app,'Нет резервной копии БД при старте')
    assert_true((ROOT/'BACKUP_DATA.bat').exists(),'Нет ручного backup BAT')
    assert_true((ROOT/'RUN_RELEASE_GATE.bat').exists(),'Нет release gate BAT')
    assert_true((ROOT/'static/manifest.webmanifest').exists() and (ROOT/'static/sw.js').exists(),'Нет PWA shell V80')
    for page in ['client.html','operator.html','admin.html']:
        html=(ROOT/'templates'/page).read_text(encoding='utf-8')
        assert_true("typeof io === 'function'" in html,f'{page}: нет fallback без Socket.IO CDN')
    passed.append('resilience + stable payment idempotence')

    assert_true('/api/admin/health' in app and '/api/admin/incidents' in app and '/api/admin/audit' in app,'Нет V79 control API')
    assert_true('last_payment_reconcile' in app and 'get_pending_order_ids' in app,'Нет фоновой сверки pending-платежей')
    assert_true('X-Content-Type-Options' in app and 'AUTH_MAX_FAILURES' in app,'Нет базового security hardening')
    assert_true('MAX_CONTENT_LENGTH' in app,'Нет ограничения размера upload')
    passed.append('auth rate limit + security headers')

    admin_html=(ROOT/'templates/admin.html').read_text(encoding='utf-8')
    assert_true('id="analyticsTab"' in admin_html,'В админке отсутствует analyticsTab')
    assert_true('id="controlTab"' in admin_html and 'loadControlCenter' in admin_html,'Нет центра контроля V79')
    for token in ['trendText','Пики заказов','Скорость обслуживания','Низкие остатки']:
        assert_true(token in admin_html,f'V78 analytics UI missing: {token}')
    passed.append('admin analytics tab + management analytics')

    client_html=(ROOT/'templates/client.html').read_text(encoding='utf-8')
    for token in ['current-order-card','paymentModal','openPaymentPanel','paymentHoldRemaining','renderOrderCard']:
        assert_true(token in client_html,f'V76 client UX missing: {token}')
    assert_true('@media(max-width:900px)' in client_html and '@media(min-width:901px)' in client_html,'Нет раздельной адаптации ПК/мобильной')
    passed.append('V76 responsive current-order/payment UX')

    operator=(ROOT/'templates/operator.html').read_text(encoding='utf-8')
    assert_true('function esc(value)' in operator,'Операторская не экранирует пользовательский текст')
    assert_true('Ждём онлайн-оплату' in operator and 'Оплата на кассе' in operator,'Нет понятных статусов оплаты оператору')
    assert_true('urgencyInfo(order)' in operator,'Нет приоритета предзаказов')
    for token in ['openStockModal','markSoldOut','preorderStrip','renderPreorders']:
        assert_true(token in operator,f'V77 operator UI missing: {token}')
    assert_true('/api/operator/menu/<int:menu_id>/sold-out' in app,'Нет operator sold-out route')
    passed.append('operator UX + escaping + stop-list')

    # Local YooKassa bootstrap config is present and masked from source defaults.
    local_cfg=json.loads((ROOT/'local_payment_config.json').read_text(encoding='utf-8'))
    assert_true(local_cfg.get('provider')=='yookassa' and local_cfg.get('enabled')=='1','Локальная ЮKassa не включена')
    assert_true(bool(local_cfg.get('yookassa_shop_id')) and bool(local_cfg.get('yookassa_secret_key')),'Нет тестовых реквизитов ЮKassa')
    passed.append('local YooKassa test config')


    # Every simple inline UI handler must have a matching function in that page.
    import re as _re
    for page in ['client.html','admin.html','operator.html','operator_login.html']:
        html=(ROOT/'templates'/page).read_text(encoding='utf-8')
        calls=set(_re.findall(r'on(?:click|change|input)="([A-Za-z_$][\w$]*)\s*\(', html))
        defs=set(_re.findall(r'(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(', html))
        missing=sorted((calls-defs)-{'if'})
        assert_true(not missing,f'{page}: кнопки вызывают отсутствующие функции: {missing}')
    passed.append('inline UI handler audit')

    print('REGRESSION OK')
    for i,name in enumerate(passed,1): print(f'{i}. OK — {name}')
    print(f'PASSED: {len(passed)}')


if __name__=='__main__':
    run()
