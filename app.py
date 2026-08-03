import os
import json
import logging
from datetime import date, timedelta

import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO)

WEBHOOK = os.environ["B24_WEBHOOK"].rstrip("/") + "/"
NORM = int(os.environ.get("VACATION_NORM", 28))

ENTITY_TYPE_ID = 1062
F_START = "UF_CRM_22_1761003149"
F_END = "UF_CRM_22_1761003173"
F_TYPE = "UF_CRM_22_1761003205"
TYPE_VACATION = 480
STAGE_APPROVED = "DT1062_30:SUCCESS"

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
    return data["result"]


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


def used_days(user_id, year, exclude_id):
    items = []
    start = 0
    while True:
        res = b24("crm.item.list", {
            "entityTypeId": ENTITY_TYPE_ID,
            "useOriginalUfNames": "Y",
            "select": ["id", F_START, F_END],
            "filter": {
                "createdBy": user_id,
                "stageId": STAGE_APPROVED,
                F_TYPE: TYPE_VACATION,
                f">={F_END}": f"{year}-01-01",
                f"<={F_START}": f"{year}-12-31",
            },
            "start": start,
        })
        items += res["items"]
        if "next" not in res:
            break
        start = res["next"]

    total = 0
    for it in items:
        if str(it["id"]) == str(exclude_id):
            continue
        if not it.get(F_START) or not it.get(F_END):
            continue
        total += count_days(parse_date(it[F_START]), parse_date(it[F_END]), year)
    return total


def owner_id(domain, auth_id, item_id):
    if item_id and int(item_id) > 0:
        item = b24("crm.item.get", {"entityTypeId": ENTITY_TYPE_ID, "id": int(item_id)})
        return item["item"]["createdBy"]
    r = requests.post(f"https://{domain}/rest/user.current", data={"auth": auth_id}, timeout=20)
    return r.json()["result"]["ID"]


PAGE = """<!doctype html><meta charset="utf-8">
<style>
 body{margin:0;font:13px/18px "Helvetica Neue",Arial,sans-serif;color:#333}
 .b{display:flex;align-items:center;gap:8px;height:32px}
 .n{font-size:17px;font-weight:600;color:%(color)s}
 .c{color:#828b95}
 .r{cursor:pointer;color:#2066b0;text-decoration:none;font-size:12px}
</style>
<div class="b">
  <span class="n" id="n">%(left)s</span>
  <span class="c">из %(norm)s дней</span>
  <a class="r" id="r" href="#">обновить</a>
</div>
<script>
var P = %(params)s;
document.getElementById('r').onclick = function(e){
  e.preventDefault();
  var fd = new FormData();
  for (var k in P) fd.append(k, P[k]);
  fetch('/handler?json=1', {method:'POST', body: fd})
    .then(function(r){return r.json()})
    .then(function(d){ document.getElementById('n').textContent = d.left; });
};
</script>"""


@app.route("/handler", methods=["POST"])
def handler():
    opts = json.loads(request.form.get("PLACEMENT_OPTIONS", "{}"))
    domain = request.form.get("DOMAIN")
    auth_id = request.form.get("AUTH_ID")
    item_id = opts.get("ENTITY_VALUE_ID") or 0

    year = date.today().year
    try:
        uid = owner_id(domain, auth_id, item_id)
        left = NORM - used_days(uid, year, item_id)
    except Exception as e:
        logging.exception("balance failed")
        left = "—"

    if request.args.get("json"):
        return {"left": left}

    return PAGE % {
        "left": left,
        "norm": NORM,
        "color": "#c0392b" if isinstance(left, int) and left <= 0 else "#333",
        "params": json.dumps({
            "DOMAIN": domain,
            "AUTH_ID": auth_id,
            "PLACEMENT_OPTIONS": request.form.get("PLACEMENT_OPTIONS", "{}"),
        }),
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
    domain = request.form.get("DOMAIN", "")
    auth = request.form.get("AUTH_ID", "")
    action = request.form.get("action", "")
    result = ""

    if action and auth and domain:
        try:
            if action == "type":
                rest_oauth(domain, auth, "userfieldtype.add", {
                    "USER_TYPE_ID": "vacbal",
                    "HANDLER": f"https://{request.host}/handler",
                    "TITLE": "Остаток отпуска",
                    "DESCRIPTION": "Считает остаток дней отпуска за текущий год",
                    "OPTIONS": {"height": 36},
                })
                result = "Готово: тип поля зарегистрирован."
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
