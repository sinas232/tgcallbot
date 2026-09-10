# wire_ml.py - connect the self-healing/learning layer to the app
# idempotent + safe:  run it inside /root/callmanager with  python3 tools/wire_ml.py
import io
import py_compile

E = "utf-8"
T = "\t"


def rd(p):
    return io.open(p, encoding=E).read()


def wr(p, s):
    io.open(p, "w", encoding=E, newline="").write(s)


def ins(path, anchor, add, tag):
    s = rd(path)
    if tag in s:
        print("SKIP (already):", path)
        return
    if anchor not in s:
        print("MISS:", path, "->", anchor.strip()[:50])
        return
    wr(path, s.replace(anchor, anchor + add, 1))
    print("OK:", path)


BOOT = '''
# self_healing bootstrap - must be the FIRST import of the app
try:
    from services.self_healing import install as _sh_install
    _sh_install()
except Exception:
    pass
'''

# 1) main.py : install the layer before any other import
s = rd("main.py")
if "self_healing" in s:
    print("SKIP (already): main.py")
else:
    e = s.index('"""', 3) + 3          # end of the module docstring
    wr("main.py", s[:e] + BOOT + s[e:])
    print("OK: main.py")

# 2) order_executor.py : import + feed outcomes + use the learned delay
P = "services/order_executor.py"
ins(P, "from services.join_brain import join_brain, OUTCOME_OK, OUTCOME_DEAD\n",
    "from services import self_healing\n", "from services import self_healing")

ins(P, T + "                join_brain.report_result(order_id, OUTCOME_OK)\n",
    T + "                try:\n"
    + T + '                    self_healing.report("", True, key=f"{order_id}:{aid}")\n'
    + T + "                except Exception:\n"
    + T + "                    pass\n",
    'self_healing.report("", True')

ins(P, T + '                msg = str(res.get("msg") or "")\n',
    T + "                try:\n"
    + T + '                    self_healing.report(msg, False, key=f"{order_id}:{aid}")\n'
    + T + "                except Exception:\n"
    + T + "                    pass\n",
    "self_healing.report(msg, False")

ins(P, T + "                    delay = min(backoff_base * (2 ** (n_att - 1)), 60.0)\n",
    T + "                    try:\n"
    + T + '                        _b, _fac = self_healing.pick(msg, key=f"{order_id}:{aid}")\n'
    + T + "                        delay = min(max(delay * _fac, 1.0), 300.0)\n"
    + T + "                    except Exception:\n"
    + T + "                        pass\n",
    "self_healing.pick(msg")

ins(P, T + "        candidates = self._voice_candidates(order_id, window, joined_ids, set(), now)\n",
    T + "        try:\n"
    + T + "            candidates = self_healing.rank(candidates)\n"
    + T + "        except Exception:\n"
    + T + "            pass\n",
    "self_healing.rank(candidates)")

print("\n--- verify ---")
for f in ("main.py", "services/self_healing.py", "services/order_executor.py"):
    py_compile.compile(f, doraise=True)
print("COMPILE_OK")
se = rd(P)
print("executor hooks:", se.count("self_healing."))
print("main.py bootstrap:",
      "yes" if "self_healing" in rd("main.py").split("import logging")[0] else "NO")
