import os
import json
import logging
from datetime import date, timedelta

import requests
from flask import Flask, request
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)

WEBHOOK = os.environ["B24_WEBHOOK"].rstrip("/") + "/"
NORM = int(os.environ.get("VACATION_NORM", 28))
PORTAL = urlparse(WEBHOOK).netloc

ENTITY_TYPE_ID = 1062
F_START = "UF_CRM_22_1761003149"
F_END = "UF_CRM_22_1761003173"
F_TYPE = "UF_CRM_22_1761003205"
TYPE_VACATION = 480
STAGE_APPROVED = "DT1062_30:SUCCESS"
EMPLOYEE_FIELD = "UF_CRM_22_1761003127"  # «Кто будет отсутствовать»

# Нерабочие праздничные дни, ст. 112 ТК РФ. В срок отпуска не входят (ст. 120).
HOLIDAYS = {
    (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
    (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4),
}

app = Flask(__name__)


def b24(method, params=None):
    r = requests.post(WEBHOOK + method, json=params or {}, timeout=20)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']} {data.get('error_description')}")
    return data


def parse_date(value):
    return date.fromisoformat(str(value)[:10])


def count_days(start, end, year):
    """Календарные дни отрезка, попавшие в year, без праздничных дней."""
    lo = max(start, date(year, 1, 1))
    hi = min(end, date(year, 12, 31))
    days = 0
    d = lo
    while d <= hi:
        if (d.month, d.day) not in HOLIDAYS:
            days += 1
        d += timedelta(days=1)
    return days


def pick(item, name):
    """Портал может вернуть поле как id или как ID — берём любой вариант."""
    if name in item:
        return item[name]
    for key in (name.upper(), name.lower()):
        if key in item:
            return item[key]
    return None


def field_users(item):
    """Значение поля-сотрудника приводим к списку ID (поле может быть множественным)."""
    v = pick(item, EMPLOYEE_FIELD)
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)] if v else []


def used_days(user_id, year, exclude_id):
    select = ["id", F_START, F_END]
    flt = {
        "stageId": STAGE_APPROVED,
        F_TYPE: TYPE_VACATION,
        f">={F_END}": f"{year}-01-01",
        f"<={F_START}": f"{year}-12-31",
    }
    if EMPLOYEE_FIELD:
        select.append(EMPLOYEE_FIELD)
    else:
        flt["createdBy"] = user_id

    items = []
    start = 0
    while True:
        res = b24("crm.item.list", {
            "entityTypeId": ENTITY_TYPE_ID,
            "useOriginalUfNames": "Y",
            "select": select,
            "filter": flt,
            "start": start,
        })
        items += res["result"]["items"]
        if "next" not in res:
            break
        start = res["next"]

    total = 0
    for it in items:
        if str(pick(it, "id")) == str(exclude_id):
            continue
        if EMPLOYEE_FIELD and str(user_id) not in field_users(it):
            continue
        start_val, end_val = pick(it, F_START), pick(it, F_END)
        if not start_val or not end_val:
            continue
        total += count_days(parse_date(start_val), parse_date(end_val), year)
    return total


def user_name(uid):
    res = b24("user.get", {"ID": uid})["result"]
    u = res[0] if res else {}
    return " ".join(x for x in (u.get("LAST_NAME"), u.get("NAME")) if x) or f"ID {uid}"


def owner_id(domain, auth_id, item_id):
    if item_id and int(item_id) > 0:
        item = b24("crm.item.get", {"entityTypeId": ENTITY_TYPE_ID,
                                    "id": int(item_id), "useOriginalUfNames": "Y"})["result"]["item"]
        if not EMPLOYEE_FIELD:
            return pick(item, "createdBy")
        users = field_users(item)
        if users:
            return users[0]
    r = requests.post(f"https://{domain}/rest/user.current", data={"auth": auth_id}, timeout=20)
    data = r.json()
    if "result" not in data:
        raise RuntimeError(f"user.current: {data.get('error')} {data.get('error_description')}")
    return data["result"]["ID"]


PAGE = """<!doctype html><meta charset="utf-8">
<style>
 body{margin:0;font:13px/18px "Helvetica Neue",Arial,sans-serif;color:#333}
 .b{display:flex;align-items:center;gap:8px;min-height:22px}
 .n{font-size:17px;font-weight:600;color:%(color)s}
 .c{color:#828b95}
 .r{color:#2066b0;font-size:12px}
 .e{color:#c0392b;font-size:11px;line-height:14px}
</style>
<div class="b">
  <span class="n">%(left)s</span>
  <span class="c">из %(norm)s дней &middot; %(who)s</span>
  %(action)s
</div>
<div class="e">%(err)s</div>"""


@app.route("/handler", methods=["POST", "GET"])
def handler():
    opts = json.loads(request.form.get("PLACEMENT_OPTIONS", "{}"))
    domain = request.form.get("DOMAIN") or PORTAL
    auth_id = request.form.get("AUTH_ID")
    item_id = opts.get("ENTITY_VALUE_ID") or request.args.get("item") or 0

    year = date.today().year
    err = ""
    who = ""
    try:
        uid = owner_id(domain, auth_id, item_id)
        who = user_name(uid)
        used = used_days(uid, year, item_id)
        left = NORM - used
        logging.info("balance: user=%s item=%s year=%s used=%s left=%s",
                     uid, item_id, year, used, left)
    except Exception as e:
        logging.exception("balance failed")
        left = "—"
        err = str(e)[:300]

    return PAGE % {
        "left": left,
        "norm": NORM,
        "color": "#c0392b" if isinstance(left, int) and left <= 0 else "#333",
        "err": err,
        "who": who,
        "action": (f'<a class="r" href="/handler?item={item_id}">обновить</a>'
                   if int(item_id) > 0 else
                   '<span class="c">(сохраните заявку, чтобы пересчитать)</span>'),
    }


def rest_oauth(domain, auth, method, params=None):
    r = requests.post(f"https://{domain}/rest/{method}",
                      json={**(params or {}), "auth": auth}, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{data['error']}: {data.get('error_description', '')}")
    return data["result"]


@app.route("/", methods=["GET", "POST"])
def install():
    domain = request.form.get("DOMAIN") or PORTAL
    auth = request.form.get("AUTH_ID", "")
    action = request.form.get("action", "")
    result = ""

    if action and auth and domain:
        try:
            if action == "type":
                params = {
                    "USER_TYPE_ID": "vacbal",
                    "HANDLER": f"https://{request.host}/handler",
                    "TITLE": "Остаток отпуска",
                    "DESCRIPTION": "Считает остаток дней отпуска за текущий год",
                    "OPTIONS": {"height": 36},
                }
                try:
                    rest_oauth(domain, auth, "userfieldtype.add", params)
                    result = "Готово: тип поля зарегистрирован."
                except RuntimeError as e:
                    if "already binded" not in str(e):
                        raise
                    rest_oauth(domain, auth, "userfieldtype.update", params)
                    result = "Готово: тип поля уже был зарегистрирован, адрес обработчика обновлён."
            else:
                info = rest_oauth(domain, auth, "app.info")
                type_code = f"rest_{info['ID']}_vacbal"
                rest_oauth(domain, auth, "userfieldconfig.add", {
                    "moduleId": "crm",
                    "field": {
                        "entityId": "CRM_22",
                        "fieldName": "UF_CRM_22_VACATION_BALANCE",
                        "userTypeId": type_code,
                        "editFormLabel": {"ru": "Остаток отпуска"},
                        "listColumnLabel": {"ru": "Остаток отпуска"},
                    },
                })
                result = (f"Готово: поле создано (тип {type_code}). "
                          "Добавьте его в карточку через «Выбрать поле».")
        except Exception as e:
            logging.exception("install action failed")
            result = f"Не получилось. {e}"

    diag = (f"Портал: {domain or 'НЕ ПЕРЕДАН'} | "
            f"Ключ доступа: {'есть, ' + str(len(auth)) + ' симв.' if auth else 'НЕ ПЕРЕДАН'} | "
            f"Нажата кнопка: {action or 'нет'}")
    result = (result + "\n\n" if result else "") + diag

    return f"""<!doctype html><meta charset="utf-8">
<script src="//api.bitrix24.com/api/v1/"></script>
<style>body{{font:14px Arial;padding:20px}}button{{padding:8px 14px;margin-right:8px}}
.log{{background:#f5f5f5;padding:10px;margin-top:14px;min-height:20px;white-space:pre-wrap}}</style>
<h3>Остаток отпуска</h3>
<form method="post" style="display:inline">
  <input type="hidden" name="DOMAIN" value="{domain}">
  <input type="hidden" name="AUTH_ID" value="{auth}">
  <input type="hidden" name="action" value="type">
  <button type="submit">1. Зарегистрировать тип поля</button>
</form>
<form method="post" style="display:inline">
  <input type="hidden" name="DOMAIN" value="{domain}">
  <input type="hidden" name="AUTH_ID" value="{auth}">
  <input type="hidden" name="action" value="field">
  <button type="submit">2. Создать поле в смарт-процессе</button>
</form>
<div class="log">{result}</div>
<script>try {{ BX24.init(function(){{ BX24.installFinish(); }}); }} catch(e) {{}}</script>"""
