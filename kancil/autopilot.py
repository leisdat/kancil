"""Autopilot: jalankan rencana browsing multi-step secara otonom.

Plan (JSON):
{
  "name": "rutin-pagi",
  "vault": "fb-sesi",        // auto-restore sesi saat start (opsional)
  "stuck": "auto",           // auto | stop | off — penanganan stuck detector
  "on_error": "stop",        // stop | continue
  "retries": 2, "backoff_s": 2,
  "screenshot_on_fail": true,
  "variables": {"query": "kancil"},   // default {{var}}
  "steps": [
    {"name": "buka",
     "action": "open",
     "params": {"url": "https://m.facebook.com", "settle": true},
     "verify_text": "Facebook"},
    {"name": "cari",
     "action": "type",
     "params": {"selector": "[name=q]", "text": "{{query}}", "verify": ".hasil"}}
  ]
}

Tiap step: watchdog ensure -> stuck check -> tool call (+retry/backoff)
-> verify. Gagal: screenshot + checkpoint tersimpan, bisa --resume.
"""

import json
import os
import time


def substitute_variables(obj, variables):
    """Substitusi {{nama}} di semua string (rekursif)."""
    if isinstance(obj, str):
        for k, v in (variables or {}).items():
            obj = obj.replace("{{" + str(k) + "}}", str(v))
        return obj
    if isinstance(obj, dict):
        return {kk: substitute_variables(vv, variables)
                for kk, vv in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [substitute_variables(vv, variables) for vv in obj]
    return obj


def _ckpt_path(plan_path, override=None):
    if override:
        return override
    return plan_path + ".checkpoint.json"


def _save_ckpt(path, plan_name, next_step, url=""):
    try:
        with open(path, "w") as f:
            json.dump({"plan": plan_name, "next_step": next_step,
                       "url": url,
                       "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    except OSError:
        pass


def _load_ckpt(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _screenshot(kancil, plan_name, step_name):
    try:
        d = os.path.expanduser("~/.kancil/autopilot-shots")
        os.makedirs(d, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in "%s_%s" % (plan_name, step_name))[:60]
        path = os.path.join(
            d, "%s_%s.png" % (safe, time.strftime("%H%M%S")))
        r = kancil.screenshot(path=path)
        if r.get("success"):
            return path
    except Exception:
        pass
    return ""


def run(kancil, plan, checkpoint_path=None, resume=False, variables=None,
        on_step=None):
    """Jalankan plan. Returns laporan akhir."""
    from . import watchdog as _wd
    from . import stuck as _st

    name = plan.get("name", "autopilot")
    steps = plan.get("steps", [])
    stuck_mode = plan.get("stuck", "auto")
    on_error = plan.get("on_error", "stop")
    default_retries = int(plan.get("retries", 2))
    backoff_s = float(plan.get("backoff_s", 2))
    shot_on_fail = bool(plan.get("screenshot_on_fail", True))
    merged_vars = dict(plan.get("variables", {}) or {})
    merged_vars.update(variables or {})
    ckpt = _ckpt_path(plan.get("_path", name), checkpoint_path)

    # #6: auto-restore sesi dari vault saat start
    vault_name = plan.get("vault")
    restored = None
    if vault_name:
        try:
            restored = kancil.restore_vault(vault_name)
        except Exception as e:
            restored = {"success": False, "errors": [str(e)[:150]]}

    start = 0
    if resume:
        c = _load_ckpt(ckpt)
        if c.get("plan") == name:
            start = int(c.get("next_step", 0))
    if start >= len(steps):
        return {"success": True, "name": name, "steps_ran": 0,
                "note": "checkpoint sudah di step terakhir"}

    report_steps = []
    paused = None
    for i in range(start, len(steps)):
        st = steps[i]
        sname = st.get("name", "step-%d" % (i + 1))
        action = st.get("action", "")
        params = substitute_variables(st.get("params", {}) or {},
                                      merged_vars)
        retries = int(st.get("retries", default_retries))

        # 1) watchdog: pastikan tab responsif
        ens = _wd.ensure(kancil)
        recovered = ens.get("recovered", False)

        # 2) stuck detector
        if stuck_mode != "off":
            c = _st.check(kancil)
            if c.get("stuck"):
                r = _st.resolve(kancil)
                if not r.get("resolved"):
                    _save_ckpt(ckpt, name, i)
                    if stuck_mode == "stop" or r.get("needs_human"):
                        paused = {"step": i, "name": sname,
                                  "kind": r.get("kind"),
                                  "hint": r.get("hint", "")}
                        report_steps.append(
                            {"name": sname, "action": action,
                             "success": False, "paused": True,
                             "detail": paused})
                        break

        # 3) eksekusi + retry/backoff + verify
        payload = {"action": action}
        payload.update(params)
        # clearance: sebelum open, inject cf_clearance cache bila ada
        if plan.get("clearance") and action == "open" and params.get("url"):
            try:
                from . import clearance as _cl
                _cl.inject_to_kancil(
                    kancil, url=params["url"])
            except Exception:
                pass
        ok, last = False, None
        for attempt in range(retries + 1):
            last = kancil.tool(payload)
            if last.get("success") and _verify(kancil, st):
                ok = True
                break
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
        entry = {"name": sname, "action": action, "success": ok,
                 "attempts": (attempt + 1) if not ok else None,
                 "recovered_zombie": recovered or None}
        if not ok:
            entry["error"] = str(last.get("error") or
                                 last.get("errors"))[:300]
            if shot_on_fail:
                entry["screenshot"] = _screenshot(kancil, name, sname)
            _save_ckpt(ckpt, name, i)
        else:
            _save_ckpt(ckpt, name, i + 1)
        report_steps.append(entry)
        if on_step:
            on_step(entry)
        if not ok and on_error == "stop":
            break

    failed = [s for s in report_steps if not s.get("success")]
    done = len([s for s in report_steps if s.get("success")])
    # checkpoint selesai penuh -> hapus
    if not failed and not paused:
        try:
            os.remove(ckpt)
        except OSError:
            pass
    return {"success": not failed and not paused, "name": name,
            "steps_ran": len(report_steps), "steps_ok": done,
            "failed": failed, "paused": paused,
            "vault_restored": bool(restored and restored.get("success"))
            if restored is not None else None,
            "report": report_steps}


def _verify(kancil, step):
    """Verify pasca-aksi: verify selector atau verify_text."""
    sel = step.get("verify")
    txt = step.get("verify_text")
    if not sel and not txt:
        return True
    try:
        if sel and not kancil._page_has(selector=sel):
            return False
        if txt and not kancil._page_has(text=txt):
            return False
    except Exception:
        return False
    return True


def load_plan(path):
    with open(path) as f:
        plan = json.load(f)
    if not isinstance(plan, dict) or not plan.get("steps"):
        raise ValueError("plan harus JSON object dengan key 'steps'")
    plan["_path"] = path
    return plan
