"""Phase 0c: language-agnostic static signals.

* approximate reference graph (identifier references resolved by name, receiver type, locality
  and imports) -> fan-in, fan-out, reachability from entry points
* entry-point candidates from a pattern table covering common frameworks
* per-method tags (db / ext / tx / auth / money / val), complexity and a static priority

Everything here is a heuristic meant to *focus* the LLM, never to replace it.
"""
from __future__ import annotations

import bisect
import math
import os
import re
from collections import defaultdict, deque

from .langs import LINE_COMMENT
from .symbols import KEYWORDS, Symbol, is_declaration_only
from .util import one_line

IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
RECEIVER = re.compile(r"([A-Za-z_$][\w$]*)\s*(?:\??\.|->|::)\s*$")
AMBIG_MAX = 12
# Names that are overwhelmingly library calls; only resolve them with a receiver-type match or as a
# bare same-file call. Keeps `x.toString()` / `obj.get()` from inflating fan-in of local methods.
EXPLICIT_IMPORT_LANGS = {"python", "javascript", "typescript", "tsx", "vue", "svelte", "dart", "elixir", "haskell",
                         "ocaml", "elm", "gleam", "clojure", "lua", "julia", "r"}
UNIVERSAL = set("""toString equals hashCode get set put add remove size length isEmpty contains map filter forEach
then catch finally apply call bind push pop shift keys values items append extend join split format valueOf
compareTo clone close open read write run start stop init next iterator stream collect of from build builder
value name id type data error log info debug warn print println update delete create save find findAll findById
findOne list all count exists query execute handle process render setup test send emit on off parse stringify
reduce sort slice splice concat includes indexOf trim replace match search lower upper strip encode decode
copy merge reset clear load dump wrap unwrap resolve reject request response result status message equal
first last min max sum abs len str int float bool dict json text html url path key keys""".split())

# ----------------------------------------------------------------------------------------
# comments


def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def strip_comments(text: str, lang: str | None) -> str:
    lc = LINE_COMMENT.get(lang or "", "//")
    if lc == "//":
        text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)
        text = re.sub(r"(?<![:\\\"'])//[^\n]*", "", text)
        if lang in ("vue", "svelte", "php"):
            text = re.sub(r"<!--.*?-->", _blank, text, flags=re.S)
        if lang == "php":
            text = re.sub(r"(?m)(^|(?<=\s))#(?!\[)[^\n]*", "", text)
    elif lc == "#":
        if lang == "python":  # statement-level triple-quoted strings (docstrings), not `x = """SQL"""`
            text = re.sub(r'(?ms)^(\s*)[rRbBuU]?("""|\'\'\').*?\2', _blank, text)
        text = re.sub(r"(?m)(^|(?<=\s))#(?!\{)[^\n]*", "", text)
        if lang == "ruby":
            text = re.sub(r"(?ms)^=begin.*?^=end", _blank, text)
    else:
        text = re.sub(r"(?m)(^|(?<=\s))" + re.escape(lc) + r"[^\n]*", "", text)
        if lang == "lua":
            text = re.sub(r"--\[\[.*?\]\]", _blank, text, flags=re.S)
    return text


# ----------------------------------------------------------------------------------------
# entry-point patterns: (label, regex, kind, attach, languages or None, path regex or None[, requires])
# `requires`: regex that must appear somewhere in the file (framework import), cuts false positives
# attach: next | self | enclosing | handler | class | file
J = ("java", "kotlin", "groovy", "scala")
JS = ("javascript", "typescript", "tsx", "vue", "svelte")
PY = ("python",)
ENTRY_PATTERNS = [
    # --- HTTP / RPC handlers
    ("spring-mapping", r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b", "http", "next", J, None),
    ("jaxrs", r"@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b", "http", "next", J, None),
    ("micronaut/nest-verb", r"@(Get|Post|Put|Delete|Patch|All|Options|Head)\(", "http", "next", J + JS, None),
    ("nest-gql", r"^\s*@(Query|Mutation|Subscription|ResolveField)\(", "graphql", "next", JS + J, None, r"@Resolver|graphql"),
    ("nest-ms", r"@(MessagePattern|EventPattern|GrpcMethod|SubscribeMessage)\(", "consumer", "next", JS, None),
    ("spring-gql", r"@(QueryMapping|MutationMapping|SchemaMapping|SubscriptionMapping)\b", "graphql", "next", J, None),
    ("dgs", r"@Dgs(Query|Mutation|Data|Subscription|EntityFetcher)\b", "graphql", "next", J, None),
    ("graphene/strawberry", r"(@strawberry\.(mutation|field)|def\s+(resolve|mutate)_?\w*\s*\()", "graphql", "self", PY, None, r"strawberry|graphene|ariadne"),
    # --- middleware / filters / guards: run before every (or many) requests; auth cruxes live here
    ("servlet-filter", r"(extends\s+(OncePerRequestFilter|GenericFilterBean|AbstractAuthenticationProcessingFilter)|implements\s+(jakarta\.servlet\.|javax\.servlet\.)?Filter\b|implements\s+HandlerInterceptor)", "middleware", "class", J, None),
    ("nest-guard", r"implements\s+(CanActivate|NestMiddleware|NestInterceptor)\b", "middleware", "class", JS, None),
    ("express-mw", r"\b(app|router|server)\s*\.\s*use\s*\(\s*(?![\'\"`])", "middleware", "handler", JS, None),
    ("aspnet-mw", r"public\s+(async\s+)?Task\s+(Invoke|InvokeAsync)\s*\(\s*HttpContext", "middleware", "self", ("csharp",), None),
    ("go-mw", r"func\s+\w+\([^)]*\)\s*(gin\.HandlerFunc|func\s*\(\s*http\.Handler\s*\)\s*http\.Handler|echo\.MiddlewareFunc)", "middleware", "self", ("go",), None),
    ("rails-before", r"^\s*(before_action|before_filter|around_action)\s+:\w+", "middleware", "handler", ("ruby",), None),
    ("django-mw", r"def\s+(process_request|process_view)\s*\(", "middleware", "self", PY, None, r"django"),
    ("py-route", r"@[\w.]+\.(route|get|post|put|delete|patch|api_route|websocket|head|options)\(", "http", "next", PY, None),
    ("drf", r"@(api_view|action)\(", "http", "next", PY, None),
    ("django-path", r"\b(re_)?path\(\s*r?['\"]", "http", "handler", PY, r"urls\.py$", r"django"),
    ("django-view", r"class\s+\w+\(([\w.]+,\s*)*(\w+\.)?(APIView|ViewSet|ModelViewSet|GenericAPIView|GenericViewSet|View|TemplateView|FormView|ListView|DetailView|CreateView|UpdateView|DeleteView)\b", "http", "class", PY, None, r"django|rest_framework"),
    ("js-route", r"\b(app|router|server|api|routes?|r|fastify|instance|v1|v2|apiRouter|[a-z]\w*Router)\s*\.\s*(get|post|put|patch|delete|del|all|options|head|route)\s*\(\s*['\"`]", "http", "handler", JS, None),
    ("js-route-ml", r"\b(app|router|server|api|routes?|fastify|[a-z]\w*Router)\s*\.\s*(get|post|put|patch|delete|del|all)\s*\(\s*$", "http", "handler", JS, None),
    ("go-route", r"\.\s*(GET|POST|PUT|PATCH|DELETE|Any|Handle|HandleFunc|Get|Post|Put|Patch|Delete|Method)\s*\(\s*\"", "http", "handler", ("go",), None),
    ("go-http", r"http\.(HandleFunc|Handle)\(", "http", "handler", ("go",), None),
    ("rust-attr-route", r"#\[(get|post|put|delete|patch|route|head)\s*\(", "http", "next", ("rust",), None),
    ("axum-route", r"\.route\(\s*\"", "http", "handler", ("rust",), None),
    ("rails-routes", r"^\s*(get|post|put|patch|delete|match|resources?|root)\b", "http", "enclosing", ("ruby",), r"config/routes\.rb$"),
    ("rails-controller", r"class\s+\w+\s*<\s*(ApplicationController|ActionController::(Base|API)|Api::\w*Controller|\w+Controller)\b", "http", "class", ("ruby",), None),
    ("sinatra", r"^\s*(get|post|put|patch|delete)\s+['\"]/", "http", "enclosing", ("ruby",), None),
    ("laravel-route", r"Route::(get|post|put|patch|delete|any|match|resource|apiResource)\(", "http", "handler", ("php",), None),
    ("symfony-route", r"(#\[Route\(|@Route\()", "http", "next", ("php",), None),
    ("php-controller", r"class\s+\w+\s+extends\s+(\w*Controller|AbstractController)\b", "http", "class", ("php",), None),
    ("aspnet-attr", r"\[(Http(Get|Post|Put|Delete|Patch)|Route)\b", "http", "next", ("csharp",), None),
    ("aspnet-minimal", r"\.Map(Get|Post|Put|Delete|Patch|Methods)?\(\s*\"", "http", "handler", ("csharp",), None),
    ("aspnet-controller", r"class\s+\w+(\([^)]*\))?\s*:\s*(Controller|ControllerBase|ApiController)\b", "http", "class", ("csharp",), None),
    ("ktor", r"^\s*(get|post|put|delete|patch)\s*\(\s*\"/", "http", "enclosing", ("kotlin",), None),
    ("phoenix", r"^\s*(get|post|put|patch|delete|resources|live)\s+\"/", "http", "enclosing", ("elixir",), r"router\.ex$"),
    ("graphql-resolvers", r"\b(Query|Mutation|Subscription)\s*:\s*\{", "graphql", "enclosing", JS, None),
    ("grpc-py", r"class\s+\w+\(\s*[\w.]*Servicer\s*\)", "grpc", "class", PY, None),
    ("grpc-java", r"extends\s+\w+ImplBase\b", "grpc", "class", J, None),
    ("grpc-go", r"Unimplemented\w+Server\b", "grpc", "class", ("go",), None),
    ("ws", r"@(WebSocketGateway|ServerEndpoint|MessageMapping)\b", "websocket", "next", JS + J, None),
    ("mediatr", r"(IRequestHandler|INotificationHandler|ICommandHandler|IQueryHandler)<", "handler", "class", ("csharp",), None),
    # --- file-convention routes (Next.js, Remix, SvelteKit, Nuxt)
    ("next-api", r"export\s+(default\s+)?(async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|handler)?\b|export\s+default\b", "http", "file", JS, r"(^|/)pages/api/"),
    ("next-route", r"export\s+(async\s+)?(function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", "http", "self", JS, r"(^|/)app/(.+/)?route\.[jt]sx?$"),
    ("next-page", r"export\s+default\b", "ui-route", "file", JS, r"(^|/)(app/(.+/)?page|pages/(?!api/).+)\.[jt]sx?$"),
    ("server-actions", r"^['\"]use server['\"]", "http", "file", JS, None),
    ("remix", r"export\s+(async\s+)?(function|const)\s+(loader|action)\b", "http", "self", JS, r"(^|/)routes/"),
    ("sveltekit", r"export\s+(async\s+)?(function|const)\s+(GET|POST|PUT|PATCH|DELETE|load|actions)\b", "http", "self", JS, r"\+(page|layout)?(\.server)?\.[jt]s$|\+server\.[jt]s$"),
    # --- UI routing / bootstrap
    ("ui-route", r"<Route\b[^>]*\bpath=", "ui-route", "enclosing", JS, None),
    ("ui-router", r"\b(createBrowserRouter|createHashRouter|RouterModule\.for(Root|Child)|createRouter|new VueRouter|provideRouter)\s*\(", "ui-route", "enclosing", JS, None),
    ("ui-bootstrap", r"\b(createRoot|ReactDOM\.render|hydrateRoot|createApp|bootstrapApplication|platformBrowserDynamic\(\)|AppRegistry\.registerComponent)\s*\(", "ui-bootstrap", "enclosing", JS, None),
    # --- main / server bootstrap
    ("py-main", r"if\s+__name__\s*==\s*['\"]__main__['\"]", "main", "enclosing", PY, None),
    ("main-fn", r"(^\s*(async\s+)?def\s+main\s*\(|public\s+static\s+void\s+main\s*\(|^\s*fun\s+main\s*\(|^func\s+main\s*\(\)|^\s*(pub\s+)?(async\s+)?fn\s+main\s*\(|static\s+(async\s+)?(void|int|Task(<int>)?)\s+Main\s*\(|^\s*int\s+main\s*\()", "main", "self", None, None),
    ("server-start", r"\b(SpringApplication\.run|app\.listen|server\.listen|http\.ListenAndServe|uvicorn\.run|app\.run|WebApplication\.CreateBuilder|NestFactory\.create|serve\(|Rack::Builder|createServer)\s*\(", "bootstrap", "enclosing", None, None),
    # --- CLI
    ("py-cli", r"(argparse\.ArgumentParser\(|@click\.(command|group)|typer\.Typer\(|@\w+\.command\(|fire\.Fire\()", "cli", "next", PY, None),
    ("js-cli", r"(new\s+Command\(|\bprogram\s*\.\s*command\(|yargs\(|\.command\(\s*['\"`]|cac\(|meow\()", "cli", "enclosing", JS, None),
    ("go-cli", r"&?cobra\.Command\s*\{", "cli", "enclosing", ("go",), None),
    ("rust-cli", r"#\[derive\([^)]*\b(Parser|Subcommand)\b", "cli", "next", ("rust",), None),
    ("java-cli", r"@(Command|ShellMethod|ShellComponent)\b", "cli", "next", J, None),
    ("thor", r"<\s*Thor\b", "cli", "class", ("ruby",), None),
    ("rake", r"^\s*task\s+[:\w]", "cli", "enclosing", ("ruby",), r"\.rake$"),
    # --- async consumers, jobs, schedules
    ("java-listener", r"@(KafkaListener|RabbitListener|JmsListener|SqsListener|StreamListener|EventListener|TransactionalEventListener|ServiceActivator|Incoming|KafkaHandler|RabbitHandler)\b", "consumer", "next", J, None),
    ("scheduled", r"(@Scheduled\b|@Cron\(|@Schedule\(|\bcron\.schedule\(|schedule\.every\(|new\s+CronJob\(|@periodic_task|\.add_job\(|@repeat_every)", "scheduled", "next", None, None),
    ("py-task", r"@(shared_task|app\.task|celery\.task|celery_app\.task|dramatiq\.actor|job|huey\.task|rq_job)\b", "job", "next", PY, None),
    ("js-queue", r"(\bconsumer\.(run|subscribe)\(|\bchannel\.consume\(|\bnew\s+Worker\(|\.process\(\s*['\"`]|\bsubscribe\(\s*\{\s*topic)", "consumer", "enclosing", JS, None),
    ("ruby-job", r"(include\s+Sidekiq::(Worker|Job)|<\s*(ApplicationJob|ActiveJob::Base)\b)", "job", "class", ("ruby",), None),
    ("dotnet-hosted", r":\s*(BackgroundService|IHostedService|IConsumer<)", "consumer", "class", ("csharp",), None),
    ("go-consumer", r"\.(Subscribe|Consume|QueueSubscribe)\(", "consumer", "enclosing", ("go",), None),
    # --- serverless
    ("js-lambda", r"(exports\.handler\s*=|export\s+(const|async\s+function|function)\s+handler\b|module\.exports\.handler)", "serverless", "self", JS, None),
    ("py-lambda", r"def\s+(lambda_handler|handler)\s*\(\s*event\s*,\s*context", "serverless", "self", PY, None),
    ("gcf", r"(@functions_framework\.(http|cloud_event)|functions\.(https|pubsub|firestore|storage|auth)\.)", "serverless", "next", PY + JS, None),
    ("azure-fn", r"(\bapp\.(http|timer|storageBlob|serviceBusQueue|eventHub|cosmosDB)\(|\[Function(Name)?\()", "serverless", "next", JS + ("csharp",), None),
    # --- mobile
    ("android", r"(extends\s+(AppCompat|Fragment|Component)?Activity\b|:\s*(AppCompat|Component)?Activity\(\))", "ui-bootstrap", "class", J, None),
    ("ios", r"(@main\b|UIApplicationMain|@UIApplicationMain)", "ui-bootstrap", "next", ("swift", "objc"), None),
]
_COMPILED = [(p[0], re.compile(p[1], re.M), p[2], p[3], p[4], re.compile(p[5]) if p[5] else None,
              re.compile(p[6]) if len(p) > 6 and p[6] else None) for p in ENTRY_PATTERNS]
VERB_RE = re.compile(r"(?:@|\.|\b)(?:(Get|Post|Put|Delete|Patch)(?:Mapping)?\b|(GET|POST|PUT|DELETE|PATCH|HEAD)\b|"
                     r"(get|post|put|patch|delete|del|all)\s*\(|Http(Get|Post|Put|Delete|Patch)\b|Map(Get|Post|Put|Delete|Patch)\b)")
STRING_LIT = re.compile(r"([\"'`])((?:\\.|(?!\1)[^\\\n])*)\1")
STRING_RE = STRING_LIT


def call_args(lines: list[str], ln: int, col: int, max_lines: int = 8) -> str:
    """Text of the call starting at (ln, col) up to its closing parenthesis."""
    text = " ".join(l.strip() if i else l[col:] for i, l in enumerate(lines[ln - 1: ln - 1 + max_lines]))
    start = text.find("(")
    if start < 0:
        return text[:300]
    depth, i, n = 0, start, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[: i + 1]
        i += 1
    return text[:400]

# ----------------------------------------------------------------------------------------
# tags
TAG_PATTERNS = {
    "db": re.compile(
        r"(\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|SELECT\s+[\w*,\s.]+\s+FROM)\b"
        r"|\b(repository|repo|Repository|Repo|dao|Dao|DAO|mapper|Mapper|session|Session|db|DB|em|entityManager|EntityManager|"
        r"jdbcTemplate|prisma|knex|sequelize|mongoose|collection|cursor|conn|connection|tx|context|dbContext|DbContext|"
        r"objects|Model|queryRunner|manager|orm|sql|pool|client)\s*(\.|->|::)\s*\w+\s*\("
        r"|\.(save|saveAndFlush|saveAll|persist|merge|flush|commit|rollback|insert_one|insert_many|update_one|update_many|"
        r"delete_one|delete_many|bulk_write|executemany|SaveChanges|SaveChangesAsync|ExecuteSqlRaw|create!|save!|update!|destroy!|"
        r"upsert|createMany|updateMany|deleteMany|findUnique|findFirst|findMany|insertInto|deleteFrom|returning)\s*\("
        r"|@(Insert|Update|Delete|Select|Query|Modifying)\b|\$transaction|db\.session|\.execute\s*\()", re.I),
    "ext": re.compile(
        r"(\bfetch\s*\(|\baxios\b|\brequests\.(get|post|put|delete|patch)\b|\bhttpx\.|\bhttp\.(Get|Post|NewRequest|Do)\b|"
        r"\bHttpClient\b|\bRestTemplate\b|\bWebClient\b|\bOkHttp|\burllib\b|Net::HTTP|Faraday|Guzzle|\bcurl_|"
        r"\bgrpc\.|\bkafka|\brabbit|\bamqp|\bsqs\b|\bsns\b|\bpubsub|\bpublish\s*\(|\bproduce\s*\(|\bsendMessage|"
        r"\bsmtp|\bsendmail|\bsend_mail|\bmailer|\bstripe\b|\bpaypal|\btwilio|\bs3\.|\bboto3\.|\bFeignClient|\bRetrofit|\bredis\.)", re.I),
    "tx": re.compile(r"(@Transactional|transaction\.atomic|@atomic|\bwith\s+transaction|BEGIN\s+TRANSACTION|\.transaction\s*\(|"
                     r"BeginTransaction|ActiveRecord::Base\.transaction|\bdb\.Begin\(|\$transaction|TransactionScope|"
                     r"\.begin_nested\(|startTransaction|withTransaction)", re.I),
    "auth": re.compile(
        r"(@PreAuthorize|@PostAuthorize|@Secured|@RolesAllowed|@login_required|@permission_required|permission_classes|"
        r"IsAuthenticated|\[Authorize|\bauthorize\b|\bauthenticat|\bjwt\b|\bJwt|\bbearer\b|\bpassword\b|bcrypt|argon2|"
        r"\bhash_password|verify_password|check_password|\bcurrent_user\b|\bcurrentUser\b|AuthenticationPrincipal|"
        r"\bpassport\.|\bUseGuards|AuthGuard|\bhasRole\b|\bhasAuthority\b|\bisAdmin\b|\bcan\?|\bpolicy\b|\bpermission|"
        r"\bcsrf\b|\bsession\[|\bsign(ed)?_?in\b|\blog_?in\b|\boauth|\bopenid|\bsaml\b|\bapi_?key\b|\bsecret\b|\btoken\b)", re.I),
    "money": re.compile(r"\b(prices?|pricing|amounts?|payments?|payouts?|charges?|refunds?|invoices?|billing|currenc(y|ies)|"
                        r"balances?|ledgers?|credits?|debits?|wallets?|checkout|subtotal|tax(es)?|discounts?|coupons?|fees?|"
                        r"stripe|paypal|braintree|adyen|withdraw\w*|deposits?|payable|receivable|money|cents)\b", re.I),
    "val": re.compile(r"(\bvalidat\w*|@Valid\b|@NotNull|@NotBlank|@Size\(|\bassert\b|\bschema\.parse|\bjoi\.|\byup\.|\bzod\b|"
                      r"\bpydantic|\bclean_\w+|throw\s+new\s+\w*(Validation|Invalid|IllegalArgument|BadRequest)\w*|"
                      r"raise\s+\w*(Validation|Invalid|Value)Error)", re.I),
}
DOMAIN_WORDS = re.compile(
    r"(auth|login|logout|signin|signup|register|password|token|session|permission|role|acl|payment|pay|charge|refund|"
    r"order|checkout|cart|invoice|billing|subscription|account|transfer|balance|transaction|ledger|security|crypt|sign|"
    r"verify|migrat|sync|import|export|publish|webhook|notif|schedul|admin|user|tenant|price|inventory|stock|booking|"
    r"reserv|ship|deliver|approve|submit|settle|reconcil)", re.I)
DECO_OR_COMMENT = re.compile(r"^(@|#\[|\[[A-Z]|//|/\*|\*|#)")
BRANCH = re.compile(r"\b(if|elif|elsif|else\s+if|for|foreach|while|case|catch|except|rescue|when|unless|until|guard)\b|&&|\|\||\?\?")


# ----------------------------------------------------------------------------------------
class RepoGraph:
    def __init__(self, files: list, texts: dict, symbols_by_file: dict, imports: dict):
        self.files = {f.path: f for f in files}
        self.texts = texts
        self.syms_by_file = symbols_by_file           # path -> [Symbol]
        self.imports = imports                        # path -> (list[str], set[str])
        self.symbols: list[Symbol] = [s for p in sorted(symbols_by_file) for s in symbols_by_file[p]]
        self.by_id = {s.id: s for s in self.symbols}
        self.module_ids: dict[str, str] = {}          # path -> pseudo symbol id for top-level code
        self.edges: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.entries: list[dict] = []

    # -- helpers --------------------------------------------------------------------------
    def _line_owner_index(self, path: str, nlines: int) -> list[str]:
        owner = [self.module_ids[path]] * (nlines + 2)
        # paint bigger ranges first so the innermost symbol wins
        for s in sorted(self.syms_by_file.get(path, []), key=lambda x: -(x.end - x.deco_start)):
            for ln in range(max(1, s.deco_start), min(nlines, s.end) + 1):
                owner[ln] = s.id
        return owner

    @staticmethod
    def _guess_type(receiver: str) -> str:
        r = receiver.lstrip("_$@")
        if r.startswith("m_"):
            r = r[2:]
        if not r:
            return ""
        if "_" in r:
            r = "".join(p[:1].upper() + p[1:] for p in r.split("_") if p)
        return r[:1].upper() + r[1:]

    @staticmethod
    def _type_map(text: str, lang: str) -> dict:
        tm = {}
        pats = [
            (r"(?:^|[\s(,<])([A-Z][\w]*)(?:<[^<>()]*>)?(?:\[\])?\??\s+([a-z_$][\w$]*)\s*[;=,)]", 1, 2),
            (r"([a-z_$][\w$]*)\s*[?!]?\s*:\s*([A-Z][\w]*)", 2, 1),
            (r"(?:this|self)\.(\w+)\s*=\s*(?:new\s+)?([A-Z]\w*)\s*\(", 2, 1),
            (r"\b([a-z_]\w*)\s*:?=\s*(?:new\s+|&)?([A-Z]\w*)\s*[({]", 2, 1),
            (r"@?(\w+)\s*=\s*([A-Z]\w*)\.new\b", 2, 1),
            (r"\$(\w+)\s*=\s*new\s+([A-Z]\w*)", 2, 1),
            (r"([A-Z]\w*)\s+\$(\w+)", 1, 2),
        ]
        if lang == "go":
            pats.append((r"\b([a-z]\w*)\s+\*?(?:[a-z]\w*\.)?([A-Z]\w*)\b", 2, 1))
        for rx, ti, vi in pats:
            for m in re.finditer(rx, text):
                t, v = m.group(ti), m.group(vi)
                if v not in KEYWORDS and len(v) > 1:
                    tm[v] = t
        return tm

    @staticmethod
    def _local_names(text: str, tmap: dict) -> set:
        """Identifiers declared as variables/parameters in this file (approximate)."""
        names = set(tmap)
        for rx in (r"\b(?:let|const|var|val|auto|my|local)\s+([A-Za-z_$][\w$]*)",
                   r"(?m)^\s*([A-Za-z_$][\w$]*)\s*(?::=|=)(?!=)",
                   r"\bfor\s*\(?\s*(?:\w+\s+)?([A-Za-z_$][\w$]*)\s+(?:in|of|:)\s",
                   r"([A-Za-z_$][\w$]*)\s*(?:->|=>)",
                   r"\|\s*([a-z_]\w*)\s*(?:,\s*[a-z_]\w*\s*)*\|",
                   r"\b(?:catch|except)\s*\(?\s*(?:\w+\s+)?(?:\w+\s+as\s+)?([a-z_]\w*)"):
            names.update(re.findall(rx, text))
        for m in re.finditer(r"\(([^()]{1,200})\)\s*(?:->|=>|\{|:)", text):
            for part in m.group(1).split(","):
                ids = re.findall(r"[A-Za-z_$][\w$]*", part.split("=")[0].split(":")[0])
                if ids:
                    names.add(ids[-1])
        return names

    # -- build ----------------------------------------------------------------------------
    def build(self) -> None:
        for path, f in self.files.items():
            mid = "M" + path  # module pseudo node, not an S-id
            self.module_ids[path] = mid
        name_index: dict[str, list[Symbol]] = defaultdict(list)
        ctors = defaultdict(list)   # class qualname -> constructors
        for s in self.symbols:
            if s.kind == "constructor":
                ctors[s.container].append(s)
                if s.name == s.container.split(".")[-1]:
                    continue        # reached through the class name instead (new X(...))
            if s.name and not s.name.startswith("<") and "(" not in s.name:
                name_index[s.name].append(s)
        self._ctors = ctors
        self._name_index = name_index
        stems = {}
        for p in self.files:
            stem = os.path.splitext(os.path.basename(p))[0]
            stems[p] = (stem.lower(), os.path.basename(os.path.dirname(p)).lower())
        self._stems = stems
        self._stripped = {}
        for path, text in self.texts.items():
            f = self.files[path]
            stripped = strip_comments(text, f.lang)
            self._stripped[path] = stripped
            line_starts = [0]
            for m in re.finditer(r"\n", stripped):
                line_starts.append(m.end())
            nlines = len(line_starts)
            owner = self._line_owner_index(path, nlines)
            own_syms = {s.id: s for s in self.syms_by_file.get(path, [])}
            tmap = self._type_map(stripped, f.lang)
            local_names = self._local_names(stripped, tmap)
            imp_src, imp_names = self.imports.get(path, ([], set()))
            imp_blob = " ".join(imp_src).lower()
            decl_lines = {(s.start, s.name) for s in own_syms.values()}
            for m in IDENT.finditer(stripped):
                tok = m.group(0)
                cands = name_index.get(tok)
                if not cands or tok in KEYWORDS:
                    continue
                ln = bisect.bisect_right(line_starts, m.start())
                if (ln, tok) in decl_lines:
                    continue
                src = owner[ln] if ln < len(owner) else self.module_ids[path]
                before = stripped[max(0, m.start() - 60): m.start()]
                rm = RECEIVER.search(before)
                receiver = rm.group(1) if rm else None
                after = stripped[m.end(): m.end() + 3].lstrip()
                is_call = after.startswith("(") or after.startswith("{") and f.lang == "go"
                if not is_call and tok in local_names and not (receiver and receiver[:1].isupper()):
                    # `article` the variable, not `article()` the method
                    if not any(not c.callable for c in cands):
                        continue
                    cands = [c for c in cands if not c.callable]
                targets = self._resolve(tok, receiver, path, cands, tmap, imp_names, imp_blob, stems, is_call)
                if not targets:
                    continue
                w = (1.0 if is_call else 0.5) / len(targets)
                for t in targets:
                    if t.id != src:
                        self.edges[src][t.id] += w
                    if is_call and not t.callable and t.qualname in ctors:   # new X(...) / X(...)
                        cs = ctors[t.qualname]
                        for c in cs:
                            if c.id != src:
                                self.edges[src][c.id] += 1.0 / len(cs)
        self._link_implementations()
        self._metrics()

    def _link_implementations(self) -> None:
        """Interface/abstract/base method -> same-named method in implementing classes, so that
        reachability crosses DI boundaries (OrderService.place -> OrderServiceImpl.place)."""
        containers = [s for s in self.symbols if not s.callable and s.kind in
                      ("class", "interface", "struct", "trait", "impl", "object", "record", "protocol")]
        by_name = defaultdict(list)
        for c in containers:
            by_name[c.name].append(c)
        members = defaultdict(dict)
        for s in self.symbols:
            if s.callable and s.container:
                members[s.container].setdefault(s.name, s)
        for c in containers:
            sig = c.signature or ""
            supers = set()
            m = re.search(r"\b(?:extends|implements)\b(.*)", sig) or re.search(r"(?:\)|\w)\s*:\s*([\w<>, .]+)", sig) \
                or re.search(r"\bclass\s+\w+\s*\(([^)]*)\)", sig) or re.search(r"\bimpl\b(?:<[^>]*>)?\s+(\w+)\s+for\b", sig)
            if m:
                supers.update(re.findall(r"[A-Z]\w*", m.group(1)))
            for sup_name in supers:
                for sup in by_name.get(sup_name, []):
                    if sup is c:
                        continue
                    for mname, ms in members.get(sup.qualname, {}).items():
                        impl = members.get(c.qualname, {}).get(mname)
                        if impl is not None:
                            self.edges[ms.id][impl.id] += 1.0

    def _resolve(self, tok, receiver, path, cands, tmap, imp_names, imp_blob, stems, is_call=True):
        universal = tok in UNIVERSAL
        if len(cands) == 1 and not universal:
            c = cands[0]
            if c.file == path or not c.callable:
                return [c]
            if len(tok) <= 2:
                return []
            # cross-file function: in explicit-import languages, require import evidence so that a
            # library call that merely shares a name (validate(), parse()) does not become an edge
            lang = self.files[path].lang
            if lang in EXPLICIT_IMPORT_LANGS and (imp_blob or imp_names):
                stem, d = stems[c.file]
                imported = tok in imp_names or stem in imp_blob or (d and d in imp_blob and c.container == "")
                typed = bool(receiver) and (tmap.get(receiver) or "").lower() == (c.container.split(".")[-1].lower() if c.container else "-")
                return [c] if (imported or typed) else []
            return [c] if (is_call or receiver) else []
        rtype = ""
        if receiver:
            if receiver in ("this", "self", "me", "super", "base", "$this", "static", "cls"):
                rtype = "__self__"
            else:
                rtype = tmap.get(receiver) or (receiver if receiver[:1].isupper() else self._guess_type(receiver))
        scored = []
        for c in cands:
            sc = 0.0
            cont = (c.container.split(".")[-1] if c.container else "").lower()
            if c.file == path:
                sc += 3
            elif os.path.dirname(c.file) == os.path.dirname(path):
                sc += 1
            if rtype == "__self__":
                sc += 3 if c.file == path else 0
            elif rtype and cont:
                rt = rtype.lower()
                base_rt = rt[1:] if rt.startswith("i") and len(rt) > 2 and rtype[1:2].isupper() else rt
                if cont in (rt, base_rt) or cont.startswith(base_rt) or base_rt.startswith(cont):
                    sc += 6
            if tok in imp_names or (c.file != path and stems[c.file][0] in imp_blob):
                stem, d = stems[c.file]
                if stem in imp_blob or d in imp_blob:
                    sc += 2.5
            scored.append((sc, c))
        best = max(sc for sc, _ in scored)
        chosen = [c for sc, c in scored if sc == best]
        if universal:
            typed = rtype and rtype != "__self__" and best >= 6
            local = (not receiver or rtype == "__self__") and all(c.file == path for c in chosen)
            if not (typed or local):
                return []
        if len(cands) > AMBIG_MAX and best < 5:
            return []
        if len(chosen) > 4 and best < 3:
            return []
        return chosen

    def _metrics(self) -> None:
        callers = defaultdict(set)
        caller_files = defaultdict(set)
        for src, outs in self.edges.items():
            sfile = src[1:] if src.startswith("M") else self.by_id[src].file
            for dst in outs:
                callers[dst].add(src)
                caller_files[dst].add(sfile)
        for s in self.symbols:
            s.fan_in = len(callers.get(s.id, ()))
            s.fan_in_files = len(caller_files.get(s.id, set()) - {s.file})
            s.fan_out = len(self.edges.get(s.id, {}))

    # -- per-symbol signals -----------------------------------------------------------------
    def compute_signals(self, trivial_fn) -> None:
        for path, syms in self.syms_by_file.items():
            raw_lines = self.texts[path].split("\n")
            stripped_lines = self._stripped[path].split("\n")
            f = self.files[path]
            for s in syms:
                s.loc = max(1, s.end - s.deco_start + 1)
                if not s.callable:
                    continue
                body = "\n".join(stripped_lines[s.deco_start - 1: s.end])
                s.tags = [t for t, rx in TAG_PATTERNS.items() if rx.search(body)]
                if is_declaration_only(raw_lines, s, f.lang):
                    s.tags.append("decl-only")
                s.complexity = len(BRANCH.findall(body))
                s.trivial = trivial_fn(raw_lines, s, f.lang)

    # -- entry points -----------------------------------------------------------------------
    def detect_entries(self, library_fallback: bool = True) -> list[dict]:
        found = []
        seen = set()
        for path, f in self.files.items():
            text = self._stripped[path]
            syms = self.syms_by_file.get(path, [])
            callables = [s for s in syms if s.callable]
            lines = text.split("\n")
            for lab, rx, kind, att, langs_, prx, req in _COMPILED:
                if langs_ and f.lang not in langs_:
                    continue
                if prx and not prx.search(path):
                    continue
                if req and not req.search(text):
                    continue
                for m in rx.finditer(text):
                    ln = text.count("\n", 0, m.start()) + 1
                    col = m.start() - (text.rfind("\n", 0, m.start()) + 1)
                    evidence = one_line(lines[ln - 1], 160)
                    targets = self._attach(att, ln, path, callables, syms, lines, col)
                    trig = self._trigger(kind, lines, ln, col)
                    for t in targets:
                        key = (t, kind)
                        if key in seen:
                            continue
                        seen.add(key)
                        tt = trig
                        if kind == "http" and len(targets) > 1 and not t.startswith("M"):
                            # per-handler verbs: .route("/x", get(list).post(create))
                            nm = re.escape(self.by_id[t].name)
                            vm = re.search(r"\b(get|post|put|patch|delete)\s*\(\s*(?:[\w:]+::)?" + nm + r"\b",
                                           call_args(lines, ln, col), re.I)
                            if vm:
                                tt = f"{vm.group(1).upper()} {trig.split(' ', 1)[-1] if ' ' in trig else trig}"
                        found.append({"kind": kind, "file": path, "line": ln, "symbol": t if not t.startswith("M") else None,
                                      "module": t.startswith("M"), "evidence": evidence, "pattern": lab, "trigger": tt})
                    if lab in ("next-api", "next-page", "server-actions"):
                        break  # file-level conventions: one match is enough
        app_kinds = {"http", "graphql", "grpc", "websocket", "consumer", "job", "scheduled",
                     "serverless", "ui-route", "handler", "middleware"}   # CLIs/mains alone don't make an app: libraries ship CLIs too
        if library_fallback and not any(e["kind"] in app_kinds for e in found):
            found.extend(self._public_api_candidates())
        self._apply_class_prefixes(found)
        found.sort(key=lambda e: (e["file"], e["line"]))
        for i, e in enumerate(found, 1):
            e["id"] = f"E{i}"
            if e["symbol"]:
                self.by_id[e["symbol"]].entries.append(e["id"])
        self.entries = found
        return found

    def _attach(self, att, ln, path, callables, syms, lines, col=0) -> list[str]:
        if att in ("next", "self"):
            for s in callables:
                if s.deco_start <= ln <= s.start:
                    return [s.id]
            nxt = [s for s in callables if 0 < s.deco_start - ln <= 3]
            if nxt:
                s = min(nxt, key=lambda x: x.deco_start)
                between = lines[ln: s.deco_start - 1]
                if all(not b.strip() or DECO_OR_COMMENT.match(b.strip()) for b in between):
                    return [s.id]
            if att == "self":
                return self._attach("enclosing", ln, path, callables, syms, lines)
            return []
        if att == "enclosing":
            inner = [s for s in callables if s.deco_start <= ln <= s.end]
            if inner:
                return [min(inner, key=lambda s: s.end - s.deco_start).id]
            return [self.module_ids[path]]
        if att == "handler":
            out = []
            inline = [s for s in callables if s.anonymous and s.deco_start <= ln + 6 and s.start >= ln and s.start - ln <= 6]
            if inline:
                out.append(min(inline, key=lambda s: s.start).id)
            seg = call_args(lines, ln, col)
            rb = re.match(r"\s*(?:before_action|before_filter|around_action)\s+:(\w+[!?]?)", lines[ln - 1])
            if rb:  # rails callback: the first symbol is the handler
                cs = [c for c in self._name_index.get(rb.group(1), []) if c.callable]
                cs.sort(key=lambda c: (c.file != path, "application" not in c.file.lower()))
                return [cs[0].id] if cs else self._attach("enclosing", ln, path, callables, syms, lines)
            names = re.findall(r"['\"](\w+)@(\w+)['\"]", seg)                       # 'Controller@method'
            names += re.findall(r"\[\s*(\w+)::class\s*,\s*['\"](\w+)['\"]\s*\]", seg)  # [Controller::class, 'm']
            names += re.findall(r"['\"](\w+)#(\w+)['\"]", seg)                        # rails 'ctrl#action'
            for ctrl, meth in names:
                for c in self._name_index.get(meth, []):
                    if c.callable and ctrl.lower().replace("controller", "") in (c.container or "").lower():
                        out.append(c.id)
            if not out:
                args = STRING_LIT.sub('""', seg[seg.find("(") + 1:] if "(" in seg else seg)
                f = self.files[path]
                tmap = self._type_map(self._stripped[path], f.lang)
                imp_src, imp_names = self.imports.get(path, ([], set()))
                found_ids = []
                for m in reversed(list(re.finditer(r"(?:([A-Za-z_$][\w$]*)\s*(?:\.|::|->)\s*)?([A-Za-z_$][\w$]*)", args))):
                    recv, tok = m.group(1), m.group(2)
                    cs = [c for c in self._name_index.get(tok, []) if c.callable]
                    if not cs or tok in KEYWORDS:
                        continue
                    res = self._resolve(tok, recv, path, cs, tmap, imp_names, " ".join(imp_src).lower(), self._stems)
                    if res and len(res) <= 2:
                        found_ids.extend(c.id for c in res if c.id not in found_ids)
                    if len(found_ids) >= 4:
                        break
                out.extend(found_ids)
            return out or self._attach("enclosing", ln, path, callables, syms, lines)
        if att == "class":
            cls = [s for s in syms if not s.callable and s.deco_start <= ln <= s.end]
            if not cls:
                cls = [s for s in syms if not s.callable and 0 <= s.start - ln <= 3]
            if not cls:
                return []
            c = min(cls, key=lambda s: s.end - s.start)
            members = [s for s in callables if c.start <= s.start <= c.end and not s.trivial
                       and not s.name.startswith("_") and s.kind != "constructor"]
            members = self._public_only(members, lines, c)
            return [s.id for s in members[:40]]
        if att == "file":
            tops = [s for s in callables if not s.container]
            exported = [s for s in tops if re.match(r"\s*export\b", lines[s.start - 1] if s.start - 1 < len(lines) else "")]
            return [s.id for s in (exported or tops)[:10]] or [self.module_ids[path]]
        return []

    def _apply_class_prefixes(self, found: list[dict]) -> None:
        """Prepend class-level route prefixes (@RequestMapping / @Path / @Controller('x') / [Route])."""
        prefix_rx = re.compile(r"(@RequestMapping|@Path|@Controller|\[Route|@Route|#\[Route)\s*\(\s*(?:(?:path|value)\s*=\s*)?['\"]([^'\"]*)['\"]")
        prefixes = {}
        for path, syms in self.syms_by_file.items():
            raw = self.texts[path].split("\n")
            for c in syms:
                if c.callable or c.deco_start >= c.start:
                    continue
                block = " ".join(raw[c.deco_start - 1: c.start])
                m = prefix_rx.search(block)
                if m:
                    prefixes[c.qualname] = (path, m.group(2))
        if not prefixes:
            return
        for e in found:
            if e["kind"] != "http" or not e["symbol"]:
                continue
            s = self.by_id[e["symbol"]]
            pref = prefixes.get(s.container)
            if not pref or pref[0] != s.file:
                continue
            verb, _, rest = e["trigger"].partition(" ")
            if verb and verb.isupper():
                e["trigger"] = f"{verb} /{pref[1].strip('/')}/{rest.strip('/')}".rstrip("/")
            else:
                e["trigger"] = f"/{pref[1].strip('/')}/{e['trigger'].strip('/')}".rstrip("/")

    @staticmethod
    def _public_only(members, lines, cls):
        # Ruby/Python-style "private" sections and explicit private/protected modifiers
        priv_from = None
        for i in range(cls.start, min(cls.end, len(lines))):
            if re.match(r"^\s*(private|protected)\s*$", lines[i]):
                priv_from = i + 1
                break
        out = []
        for s in members:
            decl = lines[s.start - 1] if s.start - 1 < len(lines) else ""
            if re.search(r"\b(private|protected)\b", decl):
                continue
            if priv_from and s.start > priv_from:
                continue
            out.append(s)
        return out

    @staticmethod
    def _trigger(kind, lines, ln, col=0) -> str:
        if kind not in ("http", "graphql", "websocket", "consumer", "job", "scheduled", "cli"):
            return ""
        line = lines[ln - 1]
        seg = call_args(lines, ln, col) if "(" in line[col:] else line[col:]
        head = line[col:col + 60]
        verb = ""
        vm = VERB_RE.search(head)
        if vm:
            v = next(g for g in vm.groups() if g).upper()
            verb = {"DEL": "DELETE", "ALL": "ANY"}.get(v, v)
        if re.search(r"RequestMapping|@Route|\[Route|#\[Route|\.route\(|\.Handle", head):
            mm = re.search(r"(?:method\s*=\s*|methods\s*=\s*\[?\s*|RequestMethod\.|Method\(\s*)['\"]?(GET|POST|PUT|DELETE|PATCH)", seg, re.I)
            verb = mm.group(1).upper() if mm else (verb or "")
        sm = STRING_LIT.search(seg)
        target = sm.group(2) if sm else None
        if kind == "http":
            if target is not None and not target.startswith("/") and not re.match(r"^[a-z]+://", target):
                target = "/" + target
            return f"{verb} {target or ''}".strip()
        return target or ""

    def _public_api_candidates(self, limit: int = 40) -> list[dict]:
        cands = [s for s in self.symbols if s.callable and not s.anonymous and not s.name.startswith("_")
                 and not s.trivial and "internal" not in s.file.lower() and "/_" not in s.file]
        cands.sort(key=lambda s: (-(s.fan_in_files * 2 + s.fan_in), s.file))
        out = []
        for s in cands[:limit]:
            out.append({"kind": "public-api", "file": s.file, "line": s.start, "symbol": s.id, "module": False,
                        "evidence": s.signature, "pattern": "library-fallback", "trigger": s.qualname})
        return out

    # -- reachability -------------------------------------------------------------------------
    def reach_from(self, seeds: list[str], max_depth: int = 6, min_w: float = 0.25) -> dict[str, int]:
        dist = {}
        dq = deque()
        for s in seeds:
            if s and s not in dist:
                dist[s] = 0
                dq.append(s)
        while dq:
            u = dq.popleft()
            if dist[u] >= max_depth:
                continue
            for v, w in self.edges.get(u, {}).items():
                if w >= min_w and v not in dist:
                    dist[v] = dist[u] + 1
                    dq.append(v)
        return dist

    def entry_reach(self) -> None:
        for e in self.entries:
            seed = e["symbol"] or self.module_ids.get(e["file"])
            for sid, d in self.reach_from([seed]).items():
                s = self.by_id.get(sid)
                if s is not None:
                    s.reach[e["id"]] = d

    def priorities(self) -> None:
        for s in self.symbols:
            f = self.files[s.file]
            eps = len(s.reach)
            tags = set(s.tags)
            kw = len(set(m.group(0).lower() for m in DOMAIN_WORDS.finditer(f"{s.qualname} {s.file}")))
            s.prio = round(
                1.5 * math.log1p(s.fan_in) + 1.0 * math.log1p(s.fan_in_files) + 1.2 * math.log1p(eps)
                + 0.8 * len(tags & {"db", "tx", "auth", "money"}) + 0.4 * len(tags & {"ext", "val"})
                + 0.6 * math.log1p(f.churn) + 0.8 * math.log1p(f.fix_churn) + 0.3 * math.log1p(s.complexity)
                + 2.0 * bool(s.entries) + 0.5 * min(kw, 3) - (2.5 if s.trivial else 0.0), 3)
