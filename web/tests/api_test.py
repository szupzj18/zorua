"""API checks for the Zorua web dashboard. Run through tests/run.sh (isolated HOME, fake CLIs)."""
import json, time, urllib.request, urllib.error, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
ROOT = os.environ["ZW_ROOT"]
B = os.environ["ZW_BASE"]
H = {"Content-Type": "application/json", "Origin": B, "X-Zorua-Web": "1"}
def post(body, headers=None, host=None):
    h = dict(H if headers is None else headers)
    if host: h["Host"] = host
    r = urllib.request.Request(B + "/api/action", data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as f: return f.status, json.load(f)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
def prov(name, reveal=False, headers=None):
    h = dict(H if headers is None else headers)
    r = urllib.request.Request(B + "/api/provider", data=json.dumps({"name": name, "reveal": reveal}).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as f: return f.status, json.load(f)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
def run_check(name, headers=None):
    h = dict(H if headers is None else headers)
    r = urllib.request.Request(B + "/api/check", data=json.dumps({"name": name}).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=60) as f: return f.status, json.load(f)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
def get(path):
    with urllib.request.urlopen(B + path, timeout=60) as f: return json.load(f)
def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label, extra if not cond else ""); 
    if not cond: sys.exit(1)

# --- response headers: the page cannot be framed by another site
def headers_of(path):
    with urllib.request.urlopen(B + path, timeout=60) as f: return f.headers
for path in ("/", "/api/state"):
    h = headers_of(path)
    check(path + " cannot be framed", "frame-ancestors 'none'" in (h.get("Content-Security-Policy") or "") and h.get("X-Frame-Options") == "DENY", dict(h))
    check(path + " sends nosniff and no referrer", h.get("X-Content-Type-Options") == "nosniff" and h.get("Referrer-Policy") == "no-referrer")

# --- guards
check("no Origin -> 403", post({"action":"x"}, headers={"Content-Type":"application/json","X-Zorua-Web":"1"})[0] == 403)
check("foreign Origin -> 403", post({"action":"x"}, headers={**H,"Origin":"http://evil.example"})[0] == 403)
check("no custom header -> 403", post({"action":"x"}, headers={"Content-Type":"application/json","Origin":B})[0] == 403)
check("text/plain -> 415", post({"action":"x"}, headers={"Content-Type":"text/plain","Origin":B,"X-Zorua-Web":"1"})[0] == 415)
check("bad Host -> 403", post({"action":"x"}, host="evil.example")[0] == 403)
check("unknown action -> 400", post({"action":"nope"})[0] == 400)

# --- accounts
s, r = post({"action":"account.add","name":"w1","agent":"codex","login":False}); check("add codex", s == 200, r)
s, r = post({"action":"account.add","name":"c1","agent":"claude","login":True}); check("add claude + login job", s == 200 and r.get("job",{}).get("status") == "running", r)
check("dup name rejected", post({"action":"account.add","name":"w1","agent":"codex","login":False})[0] == 422)
check("bad name rejected", post({"action":"account.add","name":"a b;rm","agent":"codex"})[0] == 400)
time.sleep(1)
j = get("/api/login?name=c1")["job"]; check("login job captured URL", j and any("claude.ai" in u for u in j["urls"]), j)
time.sleep(4)
j = get("/api/login?name=c1")["job"]; check("login job finished", j["status"] == "done", j)
st = get("/api/state?refresh=1")["data"]
acc = {a["name"]: a for a in st["accounts"]}
check("state lists new accounts", "w1" in acc and acc["c1"]["state"] == "ok" and acc["c1"]["email"] == "fake@example.com", acc.get("c1"))
s, r = post({"action":"account.login","name":"w1"}); check("codex login job starts", s == 200 and r["job"]["status"] == "running", r)
time.sleep(3); j = get("/api/login?name=w1")["job"]; check("codex login failed cleanly", j["status"] == "failed", j)
check("login unknown account 400", post({"action":"account.login","name":"zzz"})[0] == 400)

# --- providers (key must never come back)
KEY = "sk-test-SECRET-1234567890"
s, r = post({"action":"provider.add","agent":"claude","name":"kimi2","baseUrl":"https://api.moonshot.cn/anthropic","key":KEY,"model":"kimi-k2"}); check("add claude provider", s == 200, r)
s, r = post({"action":"provider.add","agent":"codex","name":"ds2","baseUrl":"http://127.0.0.1:8080/v1","key":KEY,"model":"gpt-6.1-sol"}); check("add codex provider", s == 200, r)
check("codex provider needs model", post({"action":"provider.add","agent":"codex","name":"ds3","baseUrl":"https://x.example","key":KEY})[0] == 400)
check("bad scheme rejected", post({"action":"provider.add","agent":"claude","name":"p9","baseUrl":"file:///etc/passwd","key":KEY})[0] == 400)
check("credentials in URL rejected", post({"action":"provider.add","agent":"claude","name":"p9","baseUrl":"https://u:p@x.example","key":KEY})[0] == 400)
s, r = post({"action":"provider.add","agent":"claude","name":"kimi2","baseUrl":"https://x.example","key":KEY}); check("dup provider -> 422, key not echoed", s == 422 and KEY not in json.dumps(r), r)
raw = json.dumps(get("/api/state?refresh=1"))
check("state lists providers, no key", "kimi2" in raw and "ds2" in raw and KEY not in raw)
cfg = open(ROOT + "/cfg/providers.json").read(); check("key stored by zorua (0600)", KEY in cfg and oct(os.stat(ROOT + "/cfg/providers.json").st_mode & 0o777) == "0o600")
# --- provider editor: masked by default, revealed on request, saved through `provider put`
s, r = prov("kimi2"); doc = r.get("doc", {})
check("provider doc is masked", s == 200 and KEY not in json.dumps(r) and doc["env"]["ANTHROPIC_AUTH_TOKEN"] != KEY and doc["env"]["ANTHROPIC_BASE_URL"] == "https://api.moonshot.cn/anthropic", r)
check("provider doc needs Origin", prov("kimi2", headers={"Content-Type": "application/json", "X-Zorua-Web": "1"})[0] == 403)
check("provider doc needs custom header", prov("kimi2", headers={"Content-Type": "application/json", "Origin": B})[0] == 403)
check("provider doc unknown 400", prov("zzz")[0] == 400)
s, full = prov("kimi2", reveal=True); check("legacy reveal stays masked", s == 200 and KEY not in json.dumps(full))
before = open(ROOT + "/cfg/providers.json").read()
s, r = post({"action":"provider.save","name":"kimi2","doc":doc}); check("saving the masked doc keeps the key", s == 200 and json.load(open(ROOT + "/cfg/providers.json"))["providers"]["kimi2"]["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY, r)
edited = json.loads(json.dumps(doc)); edited["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "64000"; edited["models"]["big"] = "kimi-k2-big"
s, r = post({"action":"provider.save","name":"kimi2","doc":edited}); saved = json.load(open(ROOT + "/cfg/providers.json"))["providers"]["kimi2"]
check("edit saved", s == 200 and saved["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000" and saved["models"]["big"] == "kimi-k2-big" and saved["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY, r)
check("previous file kept as .bak", os.path.exists(ROOT + "/cfg/providers.json.bak") and oct(os.stat(ROOT + "/cfg/providers.json.bak").st_mode & 0o777) == "0o600")
NEWKEY = "sk-test-REPLACED-0987654321"
rev = json.loads(json.dumps(full["doc"])); rev["env"]["ANTHROPIC_AUTH_TOKEN"] = NEWKEY
s, r = post({"action":"provider.save","name":"kimi2","doc":rev}); check("new key replaces the old", s == 200 and json.load(open(ROOT + "/cfg/providers.json"))["providers"]["kimi2"]["env"]["ANTHROPIC_AUTH_TOKEN"] == NEWKEY, r)
bad = json.loads(json.dumps(doc)); bad["env"]["ANTHROPIC_BASE_URL"] = "ftp://x.example"
s, r = post({"action":"provider.save","name":"kimi2","doc":bad}); check("bad base URL rejected, no secret echoed", s == 422 and NEWKEY not in json.dumps(r) and KEY not in json.dumps(r), r)
check("lowercase env name rejected", post({"action":"provider.save","name":"kimi2","doc":{**doc,"env":{**doc["env"],"bad name":"x"}}})[0] == 422)
check("save for another agent rejected", post({"action":"provider.save","name":"kimi2","doc":{"agent":"codex","base_url":"https://x.example","key":"k","model":"m","wire_api":"responses"}})[0] == 422)
check("save unknown provider 400", post({"action":"provider.save","name":"zzz","doc":doc})[0] == 400)
check("save non-object doc 400", post({"action":"provider.save","name":"kimi2","doc":"x"})[0] == 400)
s, r = prov("ds2"); cd = r["doc"]; check("codex doc", s == 200 and cd["agent"] == "codex" and cd["model"] == "gpt-6.1-sol" and KEY not in json.dumps(r), r)
cd["model"] = "gpt-6.2"; s, r = post({"action":"provider.save","name":"ds2","doc":cd})
check("codex edit saved, key kept", s == 200 and json.load(open(ROOT + "/cfg/providers.json"))["providers"]["ds2"]["model"] == "gpt-6.2" and json.load(open(ROOT + "/cfg/providers.json"))["providers"]["ds2"]["key"] == KEY, r)
check("provider.remove unknown 400", post({"action":"provider.remove","name":"w1"})[0] == 400)
s, r = post({"action":"provider.remove","name":"ds2"}); check("remove provider", s == 200, r)

# --- provider check: a stand-in endpoint that accepts one key
GOOD = "sk-test-GOODKEY-1122334455"
class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        ok = self.headers.get("Authorization") == "Bearer " + GOOD
        body = json.dumps({"data": [{"id": "m"}]} if ok else {"error": {"message": "bad key"}}).encode()
        self.send_response(200 if ok else 401); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
srv = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d" % srv.server_address[1]
check("add provider with a good key", post({"action":"provider.add","agent":"claude","name":"chk-good","baseUrl":url,"key":GOOD,"model":"m"})[0] == 200)
check("add provider with a wrong key", post({"action":"provider.add","agent":"claude","name":"chk-bad","baseUrl":url,"key":"sk-test-WRONG-0000000000","model":"m"})[0] == 200)
s, r = run_check("chk-good"); c = r.get("check", {})
check("check: good key is ok", s == 200 and c.get("status") == "ok" and c.get("http") == 200 and c.get("checked_at"), r)
s, r = run_check("chk-bad"); c = r.get("check", {})
check("check: wrong key fails, key not echoed", s == 200 and c.get("status") == "fail" and c.get("http") == 401 and "WRONG" not in json.dumps(r) and GOOD not in json.dumps(r), r)
st = get("/api/state?refresh=1")
check("state carries the remembered checks", st["checks"].get("chk-good", {}).get("status") == "ok" and st["checks"].get("chk-bad", {}).get("status") == "fail", st.get("checks"))
check("check needs Origin", run_check("chk-good", headers={"Content-Type": "application/json", "X-Zorua-Web": "1"})[0] == 403)
check("check needs the custom header", run_check("chk-good", headers={"Content-Type": "application/json", "Origin": B})[0] == 403)
check("check of an unknown provider is 400", run_check("nobody")[0] == 400)
s, doc = prov("chk-good"); d = doc["doc"]; d["env"]["ANTHROPIC_BASE_URL"] = url + "/"
check("saving a provider forgets its check", post({"action":"provider.save","name":"chk-good","doc":d})[0] == 200 and "chk-good" not in get("/api/state?refresh=1")["checks"])
post({"action":"provider.remove","name":"chk-good"}); post({"action":"provider.remove","name":"chk-bad"})
check("removed providers drop out of the checks", not {"chk-good", "chk-bad"} & set(get("/api/state?refresh=1")["checks"]))
srv.shutdown()

# --- bindings
d = ROOT + "/proj"; os.makedirs(d, exist_ok=True)
s, r = post({"action":"binding.add","dir":d,"name":"w1"}); check("bind dir", s == 200, r)
bs = get("/api/state?refresh=1")["data"]["bindings"]; check("binding listed", any(b["dir"] == d and b["name"] == "w1" for b in bs), bs)
check("bind relative path 400", post({"action":"binding.add","dir":"rel/path","name":"w1"})[0] == 400)
check("bind missing dir 400", post({"action":"binding.add","dir":"/nonexistent/zz","name":"w1"})[0] == 400)
check("bind unknown target 400", post({"action":"binding.add","dir":d,"name":"nobody"})[0] == 400)
s, r = post({"action":"binding.remove","dir":d}); check("unbind", s == 200, r)
check("unbind again 400", post({"action":"binding.remove","dir":d})[0] == 400)

# --- remove accounts
check("remove default refused", post({"action":"account.remove","name":"default"})[0] == 400)
check("purge needs typed confirm", post({"action":"account.remove","name":"w1","purge":True})[0] == 400)
home = ROOT + "/home"
check("w1 dir exists", os.path.isdir(home + "/.codex-w1"))
s, r = post({"action":"account.remove","name":"w1"}); check("remove keeps data", s == 200 and os.path.isdir(home + "/.codex-w1"), r)
s, r = post({"action":"account.remove","name":"c1","purge":True,"confirm":"c1"}); check("purge deletes data", s == 200 and not os.path.exists(home + "/.claude-c1"), r)
print("ALL OK")
