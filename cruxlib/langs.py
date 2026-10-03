"""Language detection and file classification. No third-party dependencies."""
from __future__ import annotations

import fnmatch
import os
import re

# extension -> tree-sitter language name (also used by the heuristic extractor)
EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".sc": "scala",
    ".groovy": "groovy", ".go": "go", ".rs": "rust", ".rb": "ruby", ".rake": "ruby",
    ".php": "php", ".cs": "csharp", ".fs": "fsharp", ".fsx": "fsharp", ".vb": "vb",
    ".swift": "swift", ".m": "objc", ".mm": "objc", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".dart": "dart", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hrl": "erlang",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure", ".hs": "haskell",
    ".ml": "ocaml", ".mli": "ocaml", ".lua": "lua", ".pl": "perl", ".pm": "perl",
    ".r": "r", ".jl": "julia", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".ps1": "powershell", ".sql": "sql", ".vue": "vue", ".svelte": "svelte",
    ".sol": "solidity", ".zig": "zig", ".nim": "nim", ".cr": "crystal", ".elm": "elm",
    ".gleam": "gleam", ".cls": "apex", ".trigger": "apex", ".cbl": "cobol", ".cob": "cobol",
    ".f90": "fortran", ".f95": "fortran", ".pas": "pascal", ".erb": None, ".ejs": None,
}

# Heuristic-extractor families (used only when tree-sitter is unavailable for a file)
FAMILY = {
    "python": "indent",
    "ruby": "end", "crystal": "end", "lua": "end", "julia": "end", "elixir": "end",
    "javascript": "brace", "typescript": "brace", "tsx": "brace", "java": "brace",
    "kotlin": "brace", "scala": "brace", "groovy": "brace", "go": "brace", "rust": "brace",
    "php": "brace", "csharp": "brace", "swift": "brace", "objc": "brace", "c": "brace",
    "cpp": "brace", "dart": "brace", "solidity": "brace", "zig": "brace", "apex": "brace",
    "vue": "brace", "svelte": "brace",
}

LINE_COMMENT = {
    "python": "#", "ruby": "#", "crystal": "#", "elixir": "#", "julia": "#", "perl": "#",
    "r": "#", "bash": "#", "powershell": "#", "nim": "#",
    "lua": "--", "sql": "--", "haskell": "--", "elm": "--",
    "erlang": "%", "clojure": ";",
}  # everything else: "//" plus /* */ blocks

MANIFEST_NAMES = {
    "package.json", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "mix.exs",
    "pubspec.yaml", "Package.swift", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "compose.yaml", "Procfile", "serverless.yml", "serverless.yaml",
    "app.yaml", "Makefile", "angular.json", "next.config.js", "next.config.mjs",
    "nest-cli.json", "manage.py", "routes.rb", "web.php", "api.php", "urls.py",
    "template.yaml", "template.yml", "netlify.toml", "vercel.json", "fly.toml",
}
MANIFEST_SUFFIXES = (".csproj", ".fsproj", ".sln")
OPENAPI_HINT = re.compile(r"(openapi|swagger)[^/]*\.(ya?ml|json)$", re.I)

VENDOR_ANYWHERE = {"node_modules", "bower_components", "jspm_packages", "site-packages", ".yarn",
                   "elm-stuff", "pods", "carthage", ".bundle"}
VENDOR_TOP = {"vendor", "third_party", "third-party", "thirdparty", "external", "extern", "deps", "_deps"}
VENDOR_DIRS = VENDOR_ANYWHERE | VENDOR_TOP
BUILD_ANYWHERE = {
    ".next", ".nuxt", ".svelte-kit", ".output", "__pycache__", ".gradle", ".mvn", ".idea", ".vscode",
    "venv", ".venv", ".tox", ".cache", ".pytest_cache", ".mypy_cache", ".git", ".hg", ".svn",
    ".terraform", ".serverless", ".angular", "storybook-static", ".docusaurus", ".crux", "coverage",
}
BUILD_TOP = {"dist", "build", "out", "target", "tmp", "log", "logs"}
BUILD_DIRS = BUILD_ANYWHERE | BUILD_TOP
WRAPPER_FILES = {"gradlew", "mvnw", "gradlew.bat", "mvnw.cmd", "gradle-wrapper.jar"}
TEST_DIRS = {
    "test", "tests", "__tests__", "__test__", "spec", "specs", "e2e", "cypress",
    "playwright", "androidtest", "testfixtures", "test-utils", "testutils", "testing",
    "integration-tests", "integration_tests", "unittests", "unit_tests", "__mocks__",
    "mocks", "fixtures", "testdata", "test_data",
}
DOC_DIRS = {"docs", "doc", "examples", "example", "samples", "sample", "demo", "demos",
            "benchmarks", "benchmark", "bench", "tutorial", "tutorials", "artwork"}

TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$|_tests?\.py$|^conftest\.py$|_test\.(go|rb|exs?|dart|rs|cpp|cc|c)$"
    r"|\.(test|spec|e2e|cy)\.[cm]?[jt]sx?$|(Test|Tests|IT|ITCase|Spec)\.(java|kt|scala|groovy|cs|swift)$"
    r"|_spec\.rb$|Tests?\.cs$|\.stories\.[jt]sx?$|_steps?\.[a-z]+$|Steps\.(java|kt|cs)$)"
)
GENERATED_RE = re.compile(
    r"(\.pb(\.gw)?\.go$|_pb2(_grpc)?\.pyi?$|\.g\.dart$|\.freezed\.dart$|\.designer\.cs$"
    r"|\.generated\.|(^|/)generated/|(^|/)__generated__/|\.min\.(js|css)$|\.d\.ts$|\.bundle\.js$"
    r"|(^|/)gen/.*\.go$|_generated\.go$|\.pyi$|(^|/)schema\.rb$|\.snap$|-lock\.json$|\.lock$)"
)
BUILD_CONFIG_RE = re.compile(
    r"(^|/)([\w.-]+\.config|\.?eslintrc|\.?prettierrc|\.?babelrc|karma\.conf|protractor\.conf|gulpfile|gruntfile|"
    r"webpack(\.[\w-]+)?|rollup\.config|commitlint\.config|lint-staged\.config|setup|noxfile|fabfile|conanfile)"
    r"\.(c|m)?[jt]s$|(^|/)(setup|noxfile|fabfile|conanfile)\.py$", re.I)
MIGRATION_RE = re.compile(r"(^|/)(migrations?|migrate|db/migrate|alembic/versions|flyway|liquibase)/", re.I)
GENERATED_MARKER = re.compile(r"(@generated|auto-?generated|do not edit|generated by|code generated)", re.I)


def language_for(path: str, first_line: str = "") -> str | None:
    base = os.path.basename(path)
    ext = os.path.splitext(base)[1].lower()
    if ext in EXT_LANG:
        return EXT_LANG[ext]
    if base in ("Rakefile", "Gemfile", "Guardfile", "Capfile"):
        return None  # build DSLs: context only
    if first_line.startswith("#!"):
        if "python" in first_line:
            return "python"
        if "node" in first_line:
            return "javascript"
        if "ruby" in first_line:
            return "ruby"
        if re.search(r"\b(ba|z)?sh\b", first_line):
            return "bash"
    return None


def classify(path: str, include_tests: bool = False, include_migrations: bool = False) -> str:
    """Return 'source' or a skip reason. Ambiguous directory names (build, external, samples...)
    only count near the top of the tree so Java packages like org/x/samples/ are not dropped."""
    p = path.replace("\\", "/")
    parts = [s.lower() for s in p.split("/")]
    dirs, base = parts[:-1], os.path.basename(p)
    if any(d in VENDOR_ANYWHERE for d in dirs) or any(d in VENDOR_TOP for d in dirs[:2]):
        return "vendor"
    if any(d in BUILD_ANYWHERE for d in dirs) or any(d in BUILD_TOP for d in dirs[:3]):
        return "build-output"
    if base.lower() in WRAPPER_FILES:
        return "build-wrapper"
    if GENERATED_RE.search(p):
        return "generated"
    if BUILD_CONFIG_RE.search(p):
        return "build-config"
    if not include_migrations and MIGRATION_RE.search(p):
        return "migration"
    if any(d in TEST_DIRS for d in dirs) or TEST_FILE_RE.search(base):
        return "source" if include_tests else "test"
    if any(d in DOC_DIRS for d in dirs[:2]):
        return "docs-example"
    return "source"


def matches_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.rstrip("/") + "/*") for g in globs)


def is_manifest(path: str) -> bool:
    base = os.path.basename(path)
    return base in MANIFEST_NAMES or base.endswith(MANIFEST_SUFFIXES) or bool(OPENAPI_HINT.search(base))
