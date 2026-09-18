"""
ممیزیِ خودکارِ منوها — «هیچ دکمهٔ مرده‌ای» (Dead Button Audit).

چرا این تست وجود دارد؟
    گزارش‌های کاربر: بعضی دکمه‌ها هیچ کاری نمی‌کنند. یکی از ریشه‌های این کلاس
    از باگ‌ها این است که دکمه‌ای در منو (constants.py) تعریف شود اما هیچ
    هندلری آن را مسیردهی نکند. این تست به‌صورت ایستا (بدون اجرای ربات و بدون
    پکیج telegram) بررسی می‌کند که هر لیبل توسط حداقل یک مسیر پوشش داده شود.

روش:
    ۱) لیبل‌های منو از constants.py استخراج می‌شوند — با حلِ ارجاعات به
       ثابت‌های دیگر (BTN_BACK، BTN_LEAVE_ALL_CHATS) و نام‌مستعارها
       (MAIN_MENU = USER_MAIN_MENU).
    ۲) مسیرهای واقعی از main.py: هر Regexِ داخل ثبتِ یک هندلر — شامل الگوهایی
       که با اتصالِ چند رشته، f-string یا متغیّرِ فیلتر (FILTER_BACK) ساخته
       شده‌اند. نکتهٔ کلیدی: فیلترِ «طردی» (مثل ~FILTER_NAV_BUTTONS داخل
       STD_TEXT) مسیر حساب نمی‌شود، وگرنه دکمه‌های مرده «تحت‌پوشش» دیده
       می‌شدند.
    ۳) رشته‌های لفظیِ کد برای تطبیق‌های زیررشته‌ایِ رایج در هندلرها
       (مثل if "تغییر نام" in choice).

نتیجهٔ این ممیزی در نسخهٔ ۲.۲.۵: منوی SECURITY_SETTINGS_MENU کاملاً
بلااستفاده بود (منوی امنیتی واقعی اینلاین است، با کال‌بک‌های sec_toggle_*)،
۴ دکمهٔ مرده داشت و حذف شد.
"""

import ast
import glob
import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _read(rel_path):
    with io.open(os.path.join(ROOT, rel_path), "r", encoding="utf-8") as handle:
        return handle.read()


# ─────────────────────────── ابزارهای تحلیل ایستا ───────────────────────────

def _string_constants(tree):
    """ثابت‌های رشته‌ایِ سطح‌ماژول: name -> value."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                value = ast.literal_eval(node.value)
            except Exception:
                continue
            if isinstance(value, str):
                out[node.targets[0].id] = value
    return out


def _make_resolver(names):
    """حل‌گر رشته‌ها: رشته، نامِ ثابت، لیست/تاپل، اتصال و f-string."""

    def resolve(node, depth=0):
        if depth > 8 or node is None:
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for element in node.elts:
                value = resolve(element, depth + 1)
                if value is None:
                    return None
                out.append(value)
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = resolve(node.left, depth + 1)
            right = resolve(node.right, depth + 1)
            if left is None and right is None:
                return None
            return (left or "") + (right or "")
        if isinstance(node, ast.JoinedStr):  # f-string
            out = ""
            for part in node.values:
                if isinstance(part, ast.Constant):
                    out += str(part.value)
                elif isinstance(part, ast.FormattedValue):
                    inner = resolve(part.value, depth + 1)
                    if inner is None:
                        return None
                    out += inner if isinstance(inner, str) else str(inner)
                else:
                    return None
            return out
        return None

    return resolve


def _filter_variables(main_tree, resolve):
    """متغیّرهای فیلتر در main.py: name -> [regex‌های غیرطردی]."""
    variables = {}
    for node in ast.walk(main_tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        target = node.targets[0].id
        if not target.startswith(("FILTER_", "STD_", "SECURITY_")):
            continue
        found = []

        def scan(current, negated=False):
            if isinstance(current, ast.Call):
                called = getattr(current.func, "attr", None) or getattr(current.func, "id", None)
                if called == "Regex" and current.args:
                    value = resolve(current.args[0])
                    if isinstance(value, str) and not negated:
                        found.append(value)
                for arg in current.args:
                    scan(arg, negated)
                for keyword in current.keywords:
                    scan(keyword.value, negated)
            elif isinstance(current, ast.UnaryOp):
                scan(current.operand, (not negated) if isinstance(current.op, ast.Invert) else negated)
            elif isinstance(current, ast.BinOp):
                scan(current.left, negated)
                scan(current.right, negated)

        scan(node.value)
        if found:
            variables[target] = found
    return variables


def collect_route_patterns():
    """الگوهای Regex که واقعاً یک هندلر را مسیردهی می‌کنند."""
    names = _string_constants(ast.parse(_read("constants.py")))
    main_tree = ast.parse(_read("main.py"))
    names.update(_string_constants(main_tree))
    resolve = _make_resolver(names)
    filter_vars = _filter_variables(main_tree, resolve)

    routes = []
    handler_names = {"MessageHandler", "CallbackQueryHandler", "CommandHandler"}

    def scan_arg(node, negated=False):
        found = []
        if isinstance(node, ast.Name):
            if not negated and node.id in filter_vars:
                found += filter_vars[node.id]
            value = names.get(node.id)
            if isinstance(value, str) and not negated and node.id.startswith("REGEX_"):
                found.append(value)
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if called == "Regex" and node.args and not negated:
                value = resolve(node.args[0])
                if isinstance(value, str):
                    found.append(value)
            for arg in node.args:
                found += scan_arg(arg, negated)
            for keyword in node.keywords:
                found += scan_arg(keyword.value, negated)
        elif isinstance(node, ast.UnaryOp):
            found += scan_arg(node.operand, (not negated) if isinstance(node.op, ast.Invert) else negated)
        elif isinstance(node, ast.BinOp):
            found += scan_arg(node.left, negated) + scan_arg(node.right, negated)
        return found

    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call):
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if called in handler_names and node.args:
                for arg in node.args:
                    routes += scan_arg(arg)
                for keyword in node.keywords:
                    routes += scan_arg(keyword.value)
        if isinstance(node, ast.keyword) and node.arg == "pattern":
            value = resolve(node.value)
            if isinstance(value, str):
                routes.append(value)

    compiled = []
    for raw in routes:
        try:
            compiled.append(re.compile(raw))
        except re.error:
            continue
    return compiled


def collect_code_strings():
    """همهٔ رشته‌های لفظیِ هندلرها + main.py (برای تطبیق‌های دستی/زیررشته‌ای)."""
    strings = set()
    files = sorted(glob.glob(os.path.join(ROOT, "handlers", "*.py"))) + [os.path.join(ROOT, "main.py")]
    for path in files:
        try:
            tree = ast.parse(_read(os.path.relpath(path, ROOT)))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
                strings.add(node.value.strip())
    return strings


def collect_labels():
    """{label: set(نام ثابت‌های منو)} از constants.py."""
    names = _string_constants(ast.parse(_read("constants.py")))
    resolve = _make_resolver(names)
    labels = {}
    for node in ast.parse(_read("constants.py")).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if name.startswith("REGEX_") or name == "BOT_VERSION":
            continue
        value = resolve(node.value)
        flat = []

        def walk(item):
            if isinstance(item, (list, tuple)):
                for sub in item:
                    walk(sub)
            elif isinstance(item, str):
                flat.append(item)

        walk(value)
        for label in flat:
            if label and not label.startswith(".*"):
                labels.setdefault(label, set()).add(name)
    return labels


def coverage_reason(label, patterns, strings):
    """چرا این لیبل تحت‌پوشش است؟ None یعنی هیچ هندلری آن را نمی‌شناسد."""
    for regex in patterns:
        if regex.search(label):
            return "regex: %s" % regex.pattern[:45]
    if label in strings:
        return "exact-literal"
    for text in strings:
        # تطبیقِ زیررشته‌ایِ رایج: if "تغییر نام" in choice
        if len(text) >= 3 and text in label:
            return "substring: %r" % text[:28]
    return None


# ─────────────────────────── تست‌ها ───────────────────────────

class TestNoDeadButtons(unittest.TestCase):
    def test_every_menu_label_has_a_route(self):
        labels = collect_labels()
        self.assertGreater(len(labels), 40, "استخراج لیبل‌ها ناموفق بود")
        patterns = collect_route_patterns()
        self.assertGreater(len(patterns), 50, "استخراج مسیرها ناموفق بود")
        strings = collect_code_strings()

        dead = {
            label: sorted(owners)
            for label, owners in sorted(labels.items())
            if coverage_reason(label, patterns, strings) is None
        }
        self.assertEqual(
            dead, {},
            "این دکمه‌ها در منو هستند اما هیچ هندلری آن‌ها را مسیردهی نمی‌کند:\n"
            + "\n".join("  • %r (%s)" % (label, ", ".join(owners)) for label, owners in dead.items()),
        )

    def test_critical_buttons_are_routed(self):
        """دکمه‌های حساسی که کاربر در گزارش‌هایش نام برده بود."""
        patterns = collect_route_patterns()
        strings = collect_code_strings()
        critical = [
            "🛍 خرید سرویس",
            "📦 سفارشات من",
            "📉 آمار کل ربات",
            "🛠 حالت تعمیرات",
            "📦 مدیریت سفارشات کاربران",
            "🚑 گزارش سلامت اکانت‌ها",
            "💰 کیف پول من",
            "🆘 پشتیبانی",
            "🔙 انصراف",
        ]
        for label in critical:
            self.assertIsNotNone(
                coverage_reason(label, patterns, strings),
                "دکمهٔ حساسِ %r هندلر ندارد" % label,
            )

    def test_every_menu_constant_is_used(self):
        """هر ثابتِ *_MENU در constants.py باید جایی استفاده شود (نه منوی فراموش‌شده)."""
        tree = ast.parse(_read("constants.py"))
        menu_names = [
            node.targets[0].id
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_MENU")
            and not node.targets[0].id.startswith("AWAITING_")  # کدِ state است، نه منو
        ]
        sources = [_read("main.py")] + [
            _read(os.path.relpath(path, ROOT))
            for path in sorted(glob.glob(os.path.join(ROOT, "handlers", "*.py")))
        ]
        unused = [name for name in menu_names if not any(name in src for src in sources)]
        self.assertEqual(unused, [], "ثابت‌های منوی بلااستفاده: %s" % unused)

    def test_maintenance_guard_covers_every_update_kind(self):
        """حالت تعمیرات باید دستور، متن و کال‌بک را ببندد (گزارش: کاربر در
        تعمیرات هم می‌توانست سفارش بزند)."""
        src = _read("main.py")
        self.assertIn("_maintenance_guard", src)
        self.assertIn("filters.TEXT & ~filters.COMMAND", src)
        self.assertIn("CallbackQueryHandler(_maintenance_guard)", src)
        self.assertIn("MessageHandler(filters.COMMAND, _maintenance_guard)", src)
        self.assertIn("raise ApplicationHandlerStop", src)


if __name__ == "__main__":
    unittest.main()
