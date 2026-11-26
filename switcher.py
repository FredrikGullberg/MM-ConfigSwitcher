#!/usr/bin/env python3
"""MM-ConfigSwitcher: Linux/systemd user service profile manager."""
import argparse
import base64
import fcntl
import hmac
import html
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

VERSION = "0.1.2"
STATE = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config"))) / "mm-configswitcher"
USER_UNITS = STATE.parent / "systemd/user"
CONFIG = STATE / "profiles.json"
ENV = STATE / "active.env"
PENDING = STATE / "pending.json"
TOKEN = STATE / "web-password"
WEB_UNIT = "mm-configswitcher.service"
PORT = 8765
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
REL = re.compile(r"[a-zA-Z0-9_./-]+")
SERVICE = re.compile(r"[a-zA-Z0-9_.@-]+\.service")

class SwitchError(Exception):
    pass

def atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def put_json(path, obj):
    atomic(path, json.dumps(obj, indent=2, ensure_ascii=False)+"\n")

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

@contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE/"lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=kwargs.pop("timeout", 45), **kwargs)

def ctl(*args):
    return run(["systemctl", "--user", *args], timeout=90)

def require(result, message):
    if result.returncode:
        # Do not echo config checker output: it may include API keys.
        raise SwitchError(message)
    return result

def settings():
    if not CONFIG.is_file():
        raise SwitchError("Not initialized. Run install.sh or switcher.py init.")
    return read_json(CONFIG)

def checked_path(root, name, allowed_roots=()):
    if not isinstance(name, str) or not REL.fullmatch(name) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise SwitchError("Use a safe relative path within MagicMirror.")
    target = (root/name).resolve()
    if not any(target.is_relative_to(Path(p).resolve()) for p in (root, *allowed_roots)) or not target.is_file():
        raise SwitchError("Missing file or path outside MagicMirror: "+name)
    return target

def profile_env(name, profile):
    if not ID.fullmatch(name):
        raise SwitchError("Invalid profile ID")
    lines = ["MM_SWITCHER_PROFILE="+name, "MM_CONFIG_FILE="+profile["config"]]
    if profile.get("css"):
        lines.append("MM_CUSTOMCSS_FILE="+profile["css"])
    return "\n".join(lines)+"\n"

def selected():
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if line.startswith("MM_SWITCHER_PROFILE="):
                return line.split("=",1)[1]
    return None

def validate(cfg, name):
    if name not in cfg["profiles"]:
        raise SwitchError("Unknown profile: "+name)
    root = Path(cfg["mm_root"])
    profile = cfg["profiles"][name]
    checked_path(root, profile["config"], cfg.get("allowed_roots", []))
    if profile.get("css"):
        checked_path(root, profile["css"], cfg.get("allowed_roots", []))
    env = dict(os.environ)
    env.pop("MM_CONFIG_FILE", None)
    env.pop("MM_CUSTOMCSS_FILE", None)
    env.update({"MM_CONFIG_FILE":profile["config"]})
    if profile.get("css"):
        env["MM_CUSTOMCSS_FILE"] = profile["css"]
    node = cfg["node"]
    require(run([node, "--check", str(root/profile["config"])], cwd=root, env=env),
            "JavaScript syntax check failed. Run node --check locally for details.")
    require(run([node, "--run", "config:check"], cwd=root, env=env),
            "MagicMirror config:check failed. Run it locally for details.")
    return profile

def service_state(cfg):
    r = ctl("show", cfg["service"], "--property=ActiveState,SubState,MainPID,NRestarts")
    if r.returncode:
        return {}
    return dict(line.split("=",1) for line in r.stdout.splitlines() if "=" in line)

def process_profile_env(pid):
    """Read only profile variables, not other environment secrets."""
    if not str(pid).isdigit() or int(pid) <= 0:
        return {}
    try:
        entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    keys = {b"MM_SWITCHER_PROFILE", b"MM_CONFIG_FILE", b"MM_CUSTOMCSS_FILE"}
    return {k.decode(): v.decode(errors="replace") for entry in entries
            for k, sep, v in [entry.partition(b"=")] if sep and k in keys}

def profile_environment_matches(pid, name, profile):
    if not name:
        return False
    expected = dict(line.split("=", 1) for line in profile_env(name, profile).splitlines())
    actual = process_profile_env(pid)
    return all(actual.get(k) == v for k, v in expected.items())

def http_ready(port):
    if port is None:
        return True
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()

def wait_ready(cfg, profile):
    deadline = time.monotonic()+cfg.get("startup_timeout",45)
    stable_since = None
    last = None
    while time.monotonic() < deadline:
        state = service_state(cfg)
        identity = (state.get("MainPID"),state.get("NRestarts"))
        if state.get("ActiveState")=="active" and state.get("SubState")=="running" and identity[0] not in (None,"0") and profile_environment_matches(identity[0], selected(), profile) and http_ready(profile.get("health_port")):
            if last != identity or stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic()-stable_since >= cfg.get("stable_seconds",8):
                return True
        else:
            stable_since = None
        last = identity
        time.sleep(1)
    return False

def restart(cfg, profile):
    require(ctl("restart", cfg["service"]), "systemd restart failed")
    if not wait_ready(cfg, profile):
        raise SwitchError("MagicMirror did not pass startup health checks")

def dropin_path(cfg):
    return USER_UNITS / (cfg["service"]+".d") / "90-mm-configswitcher.conf"

def quote_unit(value):
    return '"' + str(value).replace("\\","\\\\").replace('"','\\"').replace("%","%%") + '"'

def dropin_text():
    path = str(ENV)
    if not ENV.is_absolute() or any(c in path for c in "\n\r*?[") or path != path.rstrip():
        raise SwitchError("Unsupported EnvironmentFile path")
    return "[Service]\nEnvironmentFile="+path.replace("%","%%")+"\n"

def legacy_dropin_text():
    return "[Service]\nEnvironmentFile="+quote_unit(ENV)+"\n"

def assert_installed(cfg):
    p = dropin_path(cfg)
    if not p.is_file() or p.read_text() != dropin_text():
        raise SwitchError("Expected managed systemd drop-in not found. Run install first.")

def recover(cfg):
    if not PENDING.exists():
        return False
    previous = read_json(PENDING)["previous"]
    profile = validate(cfg, previous)
    atomic(ENV, profile_env(previous, profile))
    restart(cfg, profile)
    PENDING.unlink()
    return True

def activate(name):
    with locked():
        cfg = settings()
        assert_installed(cfg)
        if PENDING.exists():
            raise SwitchError("Interrupted switch detected. Run recover first.")
        profile = validate(cfg, name)
        previous = selected()
        if previous not in cfg["profiles"]:
            raise SwitchError("Unknown previous profile; refusing unsafe switch")
        validate(cfg, previous)
        put_json(PENDING, {"previous":previous,"target":name})
        try:
            atomic(ENV, profile_env(name, profile))
            restart(cfg, profile)
        except Exception as exc:
            try:
                recover(cfg)
            except Exception as rollback:
                raise SwitchError("Switch failed; rollback also failed. Pending record retained. Run recover after fixing service.") from rollback
            raise SwitchError("Switch failed; previous profile restored.") from exc
        PENDING.unlink()
        return name

def initialize(args):
    with locked():
        if CONFIG.exists():
            raise SwitchError("Already initialized. Existing settings were not changed.")
        root = Path(args.mm_root).expanduser().resolve()
        if not SERVICE.fullmatch(args.service):
            raise SwitchError("Invalid systemd service name")
        node = shutil.which(args.node)
        if not node:
            raise SwitchError("Node.js not found; specify --node /absolute/path/to/node")
        if not (root/"package.json").is_file():
            raise SwitchError("MagicMirror package.json not found")
        require(ctl("cat",args.service),"Existing systemd user service not found")
        profile = {"label":args.label,"config":args.config,"css":args.css,"health_port":args.health_port}
        allowed_roots = [str(Path(p).expanduser().resolve(strict=True)) for p in args.allow_root]
        if any(not Path(p).is_dir() for p in allowed_roots):
            raise SwitchError("Allowed roots must be directories")
        cfg = {"mm_root":str(root),"allowed_roots":allowed_roots,"node":str(Path(node).resolve()),"service":args.service,
               "startup_timeout":45,"stable_seconds":8,"profiles":{args.id:profile}}
        if not ID.fullmatch(args.id):
            raise SwitchError("Invalid profile ID")
        # Require explicit CSS for legacy installations to prevent core migration.
        if not args.css and (root/"css/custom.css").exists() and not (root/"config/custom.css").exists():
            raise SwitchError("Legacy css/custom.css detected. Inspect your existing config and migrate or explicitly select CSS before setup.")
        validate(cfg,args.id)
        backup = STATE/"backup"
        backup.mkdir(mode=0o700, exist_ok=True)
        for rel in {args.config,args.css,"config/custom.css","css/custom.css"}-{None}:
            src = root/rel
            if src.is_file():
                target = backup/rel
                if not target.exists():
                    atomic(target,src.read_text(encoding="utf-8"))
        put_json(CONFIG,cfg)
        atomic(ENV,profile_env(args.id,profile))
        atomic(TOKEN,secrets.token_urlsafe(24)+"\n")
        print("Initialized. No MagicMirror restart performed.")

def add_profile(args):
    with locked():
        cfg = settings()
        if not ID.fullmatch(args.id) or args.id in cfg["profiles"]:
            raise SwitchError("Invalid or existing profile ID")
        profile = {"label":args.label or args.id,"config":args.config,"css":args.css,"health_port":args.health_port}
        cfg["profiles"][args.id]=profile
        if not args.css and (Path(cfg["mm_root"])/"css/custom.css").exists() and not (Path(cfg["mm_root"])/"config/custom.css").exists():
            raise SwitchError("Specify a CSS file for this legacy installation.")
        validate(cfg,args.id)
        put_json(CONFIG,cfg)
        print("Added "+args.id)

def install():
    with locked():
        cfg = settings()
        drop = dropin_path(cfg)
        unit = USER_UNITS/WEB_UNIT
        script = Path(__file__).resolve()
        service = ("[Unit]\nDescription=MagicMirror configuration switcher (loopback only)\n"
                   "[Service]\nType=simple\nExecStart="+quote_unit(sys.executable)+" "+quote_unit(script)+" serve\n"
                   "Restart=on-failure\nRestartSec=3\nUMask=0077\n"
                   "[Install]\nWantedBy=default.target\n")
        for path, expected in ((drop,dropin_text()),(unit,service)):
            if path.exists() and path.read_text()!=expected:
                if path != drop or path.read_text() != legacy_dropin_text():
                    raise SwitchError("Refusing to overwrite existing unit: "+str(path))
        atomic(drop,dropin_text(),0o644)
        atomic(unit,service,0o644)
        require(ctl("daemon-reload"),"systemd daemon-reload failed")
        require(ctl("enable","--now",WEB_UNIT),"Could not start switcher web service")
        require(ctl("restart",WEB_UNIT),"Could not reload switcher web service")
        print("Installed. Existing MagicMirror start command unchanged; no mirror restart performed.")

def uninstall():
    with locked():
        cfg=settings()
        if PENDING.exists():
            raise SwitchError("Recover interrupted switch before uninstalling")
        drop=dropin_path(cfg)
        if drop.exists() and drop.read_text()!=dropin_text():
            raise SwitchError("Unexpected drop-in content; refusing removal")
        require(ctl("disable","--now",WEB_UNIT),"Could not disable switcher web service")
        drop.unlink(missing_ok=True)
        (USER_UNITS/WEB_UNIT).unlink(missing_ok=True)
        require(ctl("daemon-reload"),"systemd daemon-reload failed")
        print("Removed switcher units; state and backup retained. Restart MagicMirror manually to use its original service configuration.")

def status():
    cfg=settings()
    state=service_state(cfg)
    actual=process_profile_env(state.get("MainPID")) if state.get("ActiveState")=="active" else {}
    name=selected()
    return {"version":VERSION,"selected":name,"service":cfg["service"],
            "running_profile":actual.get("MM_SWITCHER_PROFILE"),
            "environment_matches":name in cfg["profiles"] and profile_environment_matches(state.get("MainPID"),name,cfg["profiles"][name]),
            "systemd":state,"pending":PENDING.exists(),
            "profiles":{k:v["label"] for k,v in cfg["profiles"].items()}}

def page(message="", error=False):
    info=status()
    esc=html.escape
    cards=[]
    for name,label in info["profiles"].items():
        active=name==info["selected"]
        cards.append('<article><div><h2>'+esc(label)+'</h2><small>'+esc(name)+'</small></div>'
                     '<form method="post" action="/switch"><input type="hidden" name="profile" value="'+esc(name)+'">'
                     '<input type="hidden" name="csrf" value="'+CSRF+'">'
                     '<button type="submit"'+(' class="active"' if active else '')+'>'+('Restart' if active else 'Activate')+'</button></form></article>')
    notice='<p role="status" class="'+("error" if error else "notice")+'">'+esc(message)+'</p>' if message else ""
    if info["pending"]:
        notice+='<p class="error">Interrupted switch. <code>switcher.py recover</code> is required via SSH.</p>'
    state=esc(info["systemd"].get("ActiveState","unknown"))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MM-ConfigSwitcher</title><style>
body{margin:0;background:#111820;color:#eff4fa;font:16px system-ui,sans-serif}main{max-width:720px;margin:auto;padding:32px 20px}h1{font-size:28px;margin:0}header{margin-bottom:24px}small,p{color:#b1bdca}article{background:#1c2835;border:1px solid #334355;border-radius:12px;padding:18px;margin:12px 0;display:flex;align-items:center;justify-content:space-between;gap:12px}h2{margin:0 0 4px;font-size:18px}button{background:#d8e7ff;color:#132030;border:0;border-radius:8px;padding:12px 18px;font-weight:650;cursor:pointer}.active{background:#263e45;color:#a5e7bf}.notice,.error{padding:12px;border:1px solid #466052;border-radius:8px}.error{border-color:#9f6464;color:#ffbbbb}footer{margin-top:28px;font-size:13px;color:#a7b3c1}code{overflow-wrap:anywhere}</style></head><body><main><header><h1>MM-ConfigSwitcher</h1><p>Selected: """+esc(str(info["selected"]))+" · MagicMirror: "+state+"</p></header>"+notice+"".join(cards)+"<footer>Version "+VERSION+" · Local administration</footer></main></body></html>"

CSRF = secrets.token_hex(32)

class Handler(BaseHTTPRequestHandler):
    def send_html(self, content, code=200, auth=False):
        data=content.encode("utf-8")
        self.send_response(code)
        if auth:
            self.send_header("WWW-Authenticate",'Basic realm="MM-ConfigSwitcher"')
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        self.send_header("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}",f"localhost:{PORT}"):
            self.send_html("Invalid host",400)
            return False
        expected="Basic "+base64.b64encode(("admin:"+TOKEN.read_text().strip()).encode()).decode()
        supplied=self.headers.get("Authorization","")
        if not hmac.compare_digest(supplied.encode(),expected.encode()):
            self.send_html("Authentication required",401,auth=True)
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path!="/":
            self.send_html("Not found",404)
            return
        self.send_html(page())

    def do_POST(self):
        if not self.authorized():
            return
        if self.path!="/switch":
            self.send_html("Not found",404)
            return
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=4096 or self.headers.get_content_type()!="application/x-www-form-urlencoded":
                raise ValueError()
            form=parse_qs(self.rfile.read(size).decode(),max_num_fields=4)
            if not hmac.compare_digest(form.get("csrf",[""])[0].encode(),CSRF.encode()):
                self.send_html("Invalid request token",403)
                return
            name=form.get("profile",[""])[0]
            activate(name)
            self.send_html(page("Profile activated: "+name))
        except (ValueError,UnicodeError):
            self.send_html("Invalid request",400)
        except SwitchError as exc:
            self.send_html(page(str(exc),True),409)
        except Exception:
            self.send_html(page("Operation failed. Check the user service journal.",True),500)

def serve():
    settings()
    if not TOKEN.is_file():
        raise SwitchError("Missing web password")
    server=HTTPServer(("127.0.0.1",PORT),Handler)
    print("MM-ConfigSwitcher listening on 127.0.0.1:"+str(PORT),flush=True)
    server.serve_forever()

def port(value):
    p=int(value)
    if not 1<=p<=65535:
        raise argparse.ArgumentTypeError("Invalid port")
    return p

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version",action="version",version=VERSION)
    commands=parser.add_subparsers(dest="command",required=True)
    init=commands.add_parser("init")
    init.add_argument("--mm-root",default=str(Path.home()/"MagicMirror"))
    init.add_argument("--node",default="node")
    init.add_argument("--service",default="magicmirror.service")
    init.add_argument("--id",default="dojo")
    init.add_argument("--label",default="Dojo")
    init.add_argument("--config",default="config/config.js")
    init.add_argument("--css")
    init.add_argument("--allow-root", action="append", default=[], help="Additional trusted directory for symlink targets; repeatable")
    init.add_argument("--health-port",type=port)
    add=commands.add_parser("add")
    add.add_argument("id")
    add.add_argument("--label")
    add.add_argument("--config",required=True)
    add.add_argument("--css")
    add.add_argument("--health-port",type=port)
    switch=commands.add_parser("switch")
    switch.add_argument("id")
    check=commands.add_parser("check")
    check.add_argument("id")
    for cmd in ("install","uninstall","status","recover","serve","password"):
        commands.add_parser(cmd)
    args=parser.parse_args()
    try:
        cmd=args.command
        if cmd=="init": initialize(args)
        elif cmd=="add": add_profile(args)
        elif cmd=="switch": print("Selected: "+activate(args.id))
        elif cmd=="check": validate(settings(),args.id);print("Configuration checks passed")
        elif cmd=="install": install()
        elif cmd=="uninstall": uninstall()
        elif cmd=="status": print(json.dumps(status(),indent=2,ensure_ascii=False))
        elif cmd=="recover":
            with locked():
                cfg=settings();assert_installed(cfg)
                print("Previous profile restored" if recover(cfg) else "No pending switch")
        elif cmd=="serve": serve()
        elif cmd=="password": print(TOKEN.read_text().strip())
        return 0
    except (SwitchError,OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        print("Error: "+str(exc),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
