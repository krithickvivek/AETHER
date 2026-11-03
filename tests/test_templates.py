"""Static checks for the Developer's project templates.

These tests read and compile the generated source but never run it: AETHER does not
execute generated code in this milestone.
"""

import ast
import re
import unittest

from aether.manager import ManagerAgent
from aether.models import TaskBrief
from aether.planner import RuleBasedPlanner
from aether.templates import TEMPLATE_ROOT, TEMPLATES, TST, choose_template, map_coverage

REQUIREMENTS = {
    "calculator": "A command-line calculator for basic arithmetic",
    "todo_web": "Build a to-do website where users can log in and save their tasks",
    "library_api": "Create a REST API for a library that stores books and members in a database",
}
DANGEROUS_BUILTINS = {"eval", "exec", "compile", "__import__"}   # called by bare name
DANGEROUS_METHODS = {"system", "popen", "spawnl", "startfile"}  # os.system(...) and similar
# Running programs, unsafe loading, or reaching other computers. (urllib.parse is fine: it only parses text.)
DANGEROUS_MODULES = {"subprocess", "pickle", "marshal", "ctypes", "socket", "urllib.request", "http.client",
                     "requests", "ftplib", "smtplib"}


def is_dangerous_module(name: str) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in DANGEROUS_MODULES)


def brief_for(key: str) -> TaskBrief:
    return ManagerAgent(RuleBasedPlanner(), log=lambda m: None).understand(REQUIREMENTS[key])


def rendered(key: str) -> dict[str, str]:
    template = next(t for t in TEMPLATES if t.key == key)
    return template.render(brief_for(key))


def top_level_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


class TemplateFileTests(unittest.TestCase):
    def test_every_listed_file_exists_and_nothing_is_left_over(self):
        for template in TEMPLATES:
            listed = {f.path for f in template.files}
            on_disk = {p.relative_to(TEMPLATE_ROOT / template.key).as_posix()[:-len(".tmpl")]
                       for p in (TEMPLATE_ROOT / template.key).rglob("*.tmpl")}
            with self.subTest(template=template.key):
                self.assertEqual(listed, on_disk)

    def test_all_python_files_compile(self):
        for key in REQUIREMENTS:
            for path, text in rendered(key).items():
                if path.endswith(".py"):
                    with self.subTest(file=f"{key}/{path}"):
                        compile(text, path, "exec")

    def test_tests_import_names_that_exist(self):
        for key in REQUIREMENTS:
            files = rendered(key)
            modules = {path[:-3].replace("/", "."): ast.parse(text) for path, text in files.items()
                       if path.endswith(".py")}
            modules.update({name[:-len(".__init__")]: tree for name, tree in list(modules.items())
                            if name.endswith(".__init__")})
            for path, text in files.items():
                if not (path.startswith("tests/test_") and path.endswith(".py")):
                    continue
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, ast.ImportFrom) and node.module in modules:
                        for alias in node.names:
                            submodule = f"{node.module}.{alias.name}"
                            with self.subTest(file=path, name=alias.name):
                                self.assertTrue(alias.name in top_level_names(modules[node.module])
                                                or submodule in modules)

    def test_every_template_has_tests_for_what_it_supports(self):
        for template in TEMPLATES:
            tested = set().union(*(f.features for f in template.files if TST in f.phases))
            with self.subTest(template=template.key):
                self.assertTrue(template.supports <= tested)
                self.assertIn("core", tested)

    def test_readme_contains_the_requirement(self):
        for key, requirement in REQUIREMENTS.items():
            readme = rendered(key)["README.md"]
            with self.subTest(template=key):
                self.assertIn(requirement, readme)
                self.assertNotIn("{{", readme)
                self.assertIn("python -m unittest discover", readme)


class TemplateSafetyTests(unittest.TestCase):
    def python_files(self):
        for key in REQUIREMENTS:
            for path, text in rendered(key).items():
                if path.endswith(".py"):
                    yield f"{key}/{path}", text

    def test_no_dangerous_calls_or_modules(self):
        for name, text in self.python_files():
            for node in ast.walk(ast.parse(text)):
                with self.subTest(file=name, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, DANGEROUS_BUILTINS)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, DANGEROUS_METHODS)
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(is_dangerous_module(a.name) for a in node.names))
                    if isinstance(node, ast.ImportFrom) and node.module:
                        self.assertFalse(is_dangerous_module(node.module))
                        self.assertFalse(any(is_dangerous_module(f"{node.module}.{a.name}") for a in node.names))

    def test_sql_is_never_built_with_string_formatting(self):
        for name, text in self.python_files():
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("execute", "executemany"):
                    query = node.args[0] if node.args else None
                    with self.subTest(file=name, line=node.lineno):
                        self.assertNotIsInstance(query, ast.JoinedStr)  # f-string
                        self.assertFalse(isinstance(query, ast.BinOp) and isinstance(query.op, ast.Mod))
                        self.assertFalse(isinstance(query, ast.Call) and getattr(query.func, "attr", "") == "format")

    def test_servers_listen_on_this_computer_only(self):
        for key in ("todo_web", "library_api"):
            server = rendered(key)["server.py"]
            with self.subTest(template=key):
                self.assertIn('HOST, PORT = "127.0.0.1", 8000', server)
                self.assertNotIn("0.0.0.0", server)

    def test_passwords_are_salted_hashes(self):
        files = rendered("todo_web")
        auth, schema = files["todo_app/auth.py"], files["todo_app/schema.sql"]
        self.assertIn("hashlib.pbkdf2_hmac(", auth)
        self.assertIn("secrets.token_bytes(16)", auth)
        self.assertIn("hmac.compare_digest(", auth)
        self.assertIn("password_hash TEXT", schema)
        self.assertIn("salt          TEXT", schema)
        self.assertIsNone(re.search(r"^\s*password\s+TEXT", schema, re.MULTILINE))

    def test_database_schemas_have_the_expected_tables_and_links(self):
        todo = rendered("todo_web")["todo_app/schema.sql"]
        library = rendered("library_api")["library_api/schema.sql"]
        for table in ("users", "sessions", "tasks"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", todo)
        for table in ("books", "members", "loans"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", library)
        self.assertIn("REFERENCES users(id) ON DELETE CASCADE", todo)
        self.assertIn("REFERENCES books(id)", library)
        self.assertIn("REFERENCES members(id)", library)


class TemplateChoiceTests(unittest.TestCase):
    def choose(self, requirement):
        return choose_template(ManagerAgent(RuleBasedPlanner(), log=lambda m: None).understand(requirement))

    def test_each_example_picks_its_template(self):
        for key, requirement in REQUIREMENTS.items():
            choice = self.choose(requirement)
            with self.subTest(template=key):
                self.assertEqual(choice.template.key, key)
                self.assertEqual(choice.unsupported, [])

    def test_unsupported_requirement_is_identified(self):
        choice = self.choose("Build a multiplayer chess game with a leaderboard")
        self.assertIsNone(choice.template)
        self.assertIn("No supported project type", choice.reason)
        self.assertIn("To-do web app", choice.reason)

    def test_features_a_template_lacks_are_listed(self):
        choice = self.choose("A to-do website with login and a public API")
        self.assertEqual(choice.template.key, "todo_web")
        self.assertEqual(choice.unsupported, ["api"])
        self.assertIn("Not covered by this template: api", choice.reason)

    def test_rule_based_plans_are_fully_covered(self):
        for key, requirement in REQUIREMENTS.items():
            plan = ManagerAgent(RuleBasedPlanner(), log=lambda m: None).run(requirement)
            template = next(t for t in TEMPLATES if t.key == key)
            covers, uncovered = map_coverage(plan, template)
            with self.subTest(template=key):
                self.assertEqual(uncovered, [])
                covered = set().union(*map(set, covers.values()))
                self.assertEqual(covered, set(range(1, len(plan.steps) + 1)))

    def test_steps_for_unsupported_features_are_reported_as_uncovered(self):
        requirement = "A to-do website with login and a public API"
        plan = ManagerAgent(RuleBasedPlanner(), log=lambda m: None).run(requirement)
        _, uncovered = map_coverage(plan, next(t for t in TEMPLATES if t.key == "todo_web"))
        api_steps = [n for n, step in enumerate(plan.steps, 1) if "API" in step.description]
        self.assertTrue(api_steps)
        self.assertEqual(uncovered, api_steps)


if __name__ == "__main__":
    unittest.main()
