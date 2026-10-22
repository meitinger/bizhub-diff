import argparse
import getpass
import http.cookiejar
import http.cookies
import jsrun
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree

from typing import Protocol, TypedDict


class Args(argparse.Namespace):
    host: str
    port: int | None
    path: str
    use_ssl: bool
    out_file: str | None
    insecure: bool
    password: str | None


class FetchResponse(TypedDict):
    ok: bool
    statusCode: int | None
    reason: str | None
    text: str | None


class SupportsRead(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class WcdUrlHandler(urllib.request.BaseHandler):
    PREFIX = "wcd://"

    def __init__(self, base_url: str):
        self.base_url = base_url

    def wcd_open(self, req: urllib.request.Request):
        assert req.full_url.startswith(WcdUrlHandler.PREFIX)
        req.full_url = urllib.parse.urljoin(
            self.base_url, req.full_url[len(WcdUrlHandler.PREFIX) :]
        )
        return self.parent.open(req)


def eval_script(url: str) -> None:
    with urllib.request.urlopen(url) as response:
        jsrun.eval(
            response.read().decode(response.headers.get_content_charset() or "utf-8")
        )


def fetch(url: str) -> FetchResponse:
    if "://" in url or url.startswith("/"):
        raise ValueError(f"absolute url {url} not allowed")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"wcd://{url}", headers={"Accept": "application/json"}
            )
        ) as response:
            return {
                "ok": True,
                "statusCode": response.status,
                "reason": response.reason,
                "text": response.read().decode(
                    response.headers.get_content_charset() or "utf-8"
                ),
            }
    except urllib.error.HTTPError as ex:
        return {
            "ok": False,
            "statusCode": ex.status,
            "reason": ex.reason,
            "text": None,
        }
    except urllib.error.URLError as ex:
        return {
            "ok": False,
            "statusCode": None,
            "reason": ex.strerror,
            "text": None,
        }


def get_error_message(response: SupportsRead) -> str | None:
    try:
        data = response.read()
        try:
            parsed_json = json.loads(data)
        except json.JSONDecodeError:
            try:
                parsed_xml = xml.etree.ElementTree.fromstring(data)
            except xml.etree.ElementTree.ParseError:
                return None
            else:
                if parsed_xml.tag != "MFP":
                    raise ValueError("expected <MFP> root")
                for segment in ("Message", "Item"):
                    parsed_xml = parsed_xml.find(segment)
                    if parsed_xml is None:
                        raise ValueError(f"<{segment}> not found")
                text = parsed_xml.text or ""
        else:
            path = "$"
            for segment in ("MFP", "Message", "Item", "#text"):
                if not isinstance(parsed_json, dict):
                    raise TypeError(f"{path} is not an object")
                path = f"{path}.{segment}"
                if segment not in parsed_json:
                    raise ValueError(f"{path} is missing")
                parsed_json = parsed_json[segment]
            if not isinstance(parsed_json, str):
                raise TypeError(f"{path} is not a string")
            text = parsed_json
        text = text.strip()
        if not text:
            raise ValueError("message is empty")
        return text
    except Exception as ex:
        return f"invalid error: {ex}"


def login(password: str) -> str:
    value = post_form_data_and_get_cookie(
        url="wcd://login.cgi",
        form_data={
            "func": "PSL_LP1_LOG",
            "password": password,
            "AuthType": "None",
            "R_ADM": "AdminAdmin",
        },
        cookie_name="ID",
    )
    if not value:
        raise RuntimeError("ID cookie is empty")
    return value


def logout() -> None:
    value = post_form_data_and_get_cookie(
        url="wcd://a_user.cgi", form_data={"func": "PSL_ACO_LGO"}, cookie_name="usr"
    )
    if value:
        raise RuntimeError(f"usr={value} cookie wasn't cleared")


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--path", default="/wcd/")
    parser.add_argument("--use-ssl", dest="use_ssl", action="store_true")
    parser.add_argument("--out-file", dest="out_file")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--password")
    return parser.parse_args(namespace=Args())


def prepare_jsrun() -> None:
    jsrun.bind_function("__print", lambda *args: print(*args, file=sys.stderr))
    jsrun.bind_function("__fetch", fetch)
    jsrun.eval(r"""
const __not_implemented = (what) => {
    const error = new Error(`not implemented: ${what}`);
    __print(error.stack);
    throw error;
};

const __assert = (fn) => {
    if (!fn()) {
        const condition = fn.toString().replace(/^\(\s*\)\s*=>\s*/,"");
        const error = new Error(`assertion failed: ${condition}`);
        __print(error.stack);
        throw error;
    }
};

const __ignore = (what) => __print(`ignoring: ${what}`);

const __make_proxy = (target, name, options) => new Proxy(target, {
    get(target, prop, receiver) {
        if (!Reflect.has(target, prop)) {
            __not_implemented(`get ${name}.${prop}`);
        }
        return Reflect.get(target, prop, receiver);
    },

    apply(target, thisArgument, argumentsList) {
        if (!options?.callable) {
            __not_implemented(`apply ${name}(${argumentsList})`);
        }
        return Reflect.apply(target, thisArgument, argumentsList);
    },

    getOwnPropertyDescriptor(target, prop) {
        const descriptor = Reflect.getOwnPropertyDescriptor(target, prop);
        if (!descriptor && !options?.writable) {
            __ignore(`hasOwn(${name}, "${prop}")`);
        }
        return descriptor;
    },

    set(target, prop, value, receiver) {
        const descriptor = Reflect.getOwnPropertyDescriptor(target, prop);
        if (!(descriptor ? descriptor.set : options?.writable)) {
            __not_implemented(`set ${name}.${prop} = ${value}`);
        }
        Reflect.set(target, prop, value, receiver);
    },
});

const $ = __make_proxy(Object.assign(
    (...args) => {
        if (args.length === 1) {
            const arg = args[0];
            if (arg === window) {
                return __make_proxy({
                    on(event) {__ignore(`$(window).on(${event})`)},
                    load() {__ignore("$(window).load()")},
                    resize() {__ignore("$(window).resize()")},
                }, "$(window)");
            }
            if (arg === document) {
                return __make_proxy({
                    ready() {__ignore("$(document).ready()")},
                    on(event) {__ignore(`$(document).on(${event})`)},
                }, "$(document)");
            }
            if (typeof arg === "string") {
                switch (arg) {
                    case "#chgFormat": return __make_proxy({
                        text() {__ignore('$("#chgFormat").text()')}
                    });
                }
                if (!["#ID_Menu_", "#ID_SubMenu_", "#ID_SpareSubMenu_"].some(prefix => arg.startsWith(prefix))) {
                    __ignore(`$("${arg}")`);
                }
                return [];
            }
            if (typeof arg === "function") {
                return __ignore("$(fn)");
            }
        }
        return __not_implemented(`apply $(${args.map(arg => {try{return String(arg)}catch{return "?"}}).join(",")})`);
    }, {
        fn: __make_proxy({
            get bootstrapTable() {return __ignore("$.fn.bootstrapTable")},
            extend() {__ignore("$.fn.extend()")},
        }, "$.fn", { writable: true }),
        isXMLDoc(elem) {return false},
        isArray(obj) {return Array.isArray(obj)},
        ajax(url, options) {
            if (typeof url === "object") {
                options = url
                url = options.url
            }
            if (options.dataType && !["json", "text"].includes(options.dataType)) {
                __not_implemented(`$.ajax("${url}", {dataType: "${options.dataType}"})`)
            }
            if (options.type && options.type !== "GET") {
                __not_implemented(`$.ajax("${url}", {type: "${options.type}"})`)
            }
            const {ok, statusCode, reason, text} = __fetch(options.url);
            let json = undefined;
            let parseError = null;
            let textStatus;
            if (!ok) {
                textStatus = "error";
            } else if (!text) {
                textStatus = "nocontent";
            } else {
                textStatus = "success";
                try {
                    json = JSON.parse(text);
                } catch(ex) {
                    if (options.dataType === "json") {
                        textStatus = "parsererror";
                    }
                    parseError = ex;
                }
            }
            const xhr = __make_proxy({
                status: statusCode,
                statusText: reason,
                responseText: text,
                get responseJSON() {
                    if (parseError) {
                        throw parseError;
                    }
                    return json;
                },
                success(cb) {
                    if (json !== undefined) {
                        cb(json, textStatus, xhr);
                    }
                    return xhr;
                },
                error(cb) {
                    if (json === undefined) {
                        cb(xhr, textStatus, reason);
                    }
                    return xhr;
                },
                complete(cb) {
                    cb(xhr, textStatus);
                    return xhr;
                },
            }, `$.ajax("${url}")`);
            return xhr;
        },
        each(obj, callback) {
            const length = obj?.length;
            if (Array.isArray(obj) || length === 0 || typeof length === "number" && length > 0 && (length - 1) in obj) {
                for (let i = 0 ; i < length; i++ ) {
                    if (callback.call(obj[i], i, obj[i]) === false) {
                        break;
                    }
                }
            } else {
                for (const i in obj) {
                    if (callback.call(obj[i], i, obj[i]) === false) {
                        break;
                    }
                }
            }
            return obj;
        }
    },
), "$", { callable: true });
const jQuery = $;

const _ = __make_proxy({
    throttle() {__ignore("_.throttle()")},
}, "_");

const document = __make_proxy({
    get cookie() {return ""},
    set cookie(value) {__ignore(`document.cookie = "${value}"`)},
}, "document");

const navigator = __make_proxy({
    userAgent: "unknown",
}, "navigator");

const location = __make_proxy({
    href: "http://localhost/wcd/spa_main.html",
}, "location");

const alert = (message) => __print(new Error(`alert("${message}")`).stack);
const setTimeout = () => __not_implemented("setTimeout()");
const clearTimeout = () => __ignore("clearTimeout()");
const requestIdleCallback = () => __ignore("requestIdleCallback()");

const window = __make_proxy({
    _,
    $,
    document,
    navigator,
    location,
    alert,
    setTimeout,
    clearTimeout,
    requestIdleCallback,
}, "window");
""")


def prepare_urllib(args: Args) -> None:
    cookie_jar = http.cookiejar.CookieJar()
    urllib.request.install_opener(
        urllib.request.build_opener(
            WcdUrlHandler(
                urllib.parse.urlunsplit(
                    (
                        "https" if args.use_ssl else "http",
                        args.host if args.port is None else f"{args.host}:{args.port}",
                        args.path,
                        None,
                        None,
                    )
                )
            ),
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(
                context=(
                    ssl._create_unverified_context()
                    if args.insecure
                    else ssl.create_default_context()
                )
            ),
        )
    )
    for name, value in (
        ("logoutIF", "a_user.cgi"),
        ("menuType", "Admin"),
        ("webUI", "new"),
    ):
        cookie_jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None if args.port is None else str(args.port),
                port_specified=args.port is not None,
                domain=args.host,
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=False,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )


def post_form_data_and_get_cookie(
    *, url: str, form_data: dict[str, str], cookie_name: str
) -> str:
    with urllib.request.urlopen(
        urllib.request.Request(
            url,
            data=urllib.parse.urlencode(form_data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    ) as response:
        for header in response.headers.get_all("Set-Cookie", []):
            cookie = http.cookies.SimpleCookie(header).get(cookie_name)
            if cookie is not None:
                return cookie.value
        raise RuntimeError(
            get_error_message(response) or f"no {cookie_name} cookie present"
        )


def collect_settings_json() -> str:
    return jsrun.eval(r"""
commonData.setPreLoadState(commonData.PRELOAD_STATE_READY);
commonData.update();

searchMenu = function() {
    const menuById = new Map();
    let scope;
    let currentMenu;
    let currentSubMenu;
    return __make_proxy({
        __byId(id) {
            return menuById.get(id);
        },
        __forEach(cb) {
            return menuById.values().forEach(cb);
        },
        init() {
            menuById.clear();
            scope = null;
            currentMenu = null;
            currentSubMenu = null;
        },
        pushMenu(entry) {
            __assert(() => !menuById.has(entry.Id));
            const menu = Object.seal({
                url: null, // xxx_xxx_xxxxxx
                connected: false,
                entry,
                parent: null,
                scope: null,
            });
            switch (entry.Category) {
                case "Menu":
                    const scopePrefix = "#ID_Menu_Common_";
                    if (entry.Id.startsWith(scopePrefix)) {
                        scope = entry.Id.substring(scopePrefix.length);
                        return;
                    }
                    currentMenu = menu;
                    currentSubMenu = null;
                    break;
                case "SubMenu":
                    __assert(() => currentMenu);
                    currentSubMenu = menu;
                    menu.parent = currentMenu;
                    break;
                case "SpareMenu":
                    __assert(() => currentSubMenu);
                    menu.parent = currentSubMenu;
                    break;
                default:
                    return __not_implemented(`searchMenu.pushMenu({Category: "${entry.Category}"})`);
            }
            __assert(() => scope);
            menu.scope = scope;
            menuById.set(entry.Id, menu);
        },
    }, "searchMenu");
}();;
setupMenuListAdmin();

const __disabledUrls = [];
connect = (id, url) => {
    const menu = searchMenu.__byId(id);
    if (url === "000_000_TOP000") {
        url = null;
    }
    if (menu) {
        menu.connected = true;
        menu.url = url;
    } else if (url) {
        __disabledUrls.push(url);
    }
};
attatchPageJumpAdmin();

(function(){
    const getSettingsAndError = (url) => {
        const {ok, statusCode, reason, text} = __fetch(`api/AppReqGetCustomData/_${url}?_=${Date.now()}`);
        if (!ok) {return [null, reason || `Http${statusCode ?? "Error"}`]}
        let settings;
        try {settings = JSON.parse(text)}
        catch {return [null, "ResultNotJson"]}
        if (!settings) {return [null, "ResultEmpty"]}
        const mfp = settings.MFP;
        if (mfp) {
            delete mfp.Token;
            return [mfp, null];
        }
        if (settings.Result?.ResultInfo === "Nack") {
            return [null, settings.ErrorDetails || "ResultNack"]
        }
        return [null, "ResultUnknown"];
    };
    const entries = new Map();
    searchMenu.__forEach(menu => {
        let level;
        __assert(() => menu.connected);
        switch (menu.entry.Category) {
            case "Menu":
                level = 1;
                break;
            case "SubMenu":
                level = 2;
                break;
            case "SpareMenu":
                __assert(() => menu.url);
                level = 3;
                break;
            default:
                return __assert(false);
        }
        if (menu.url && !entries.has(menu.url)) {
            __print(`retrieve: ${menu.url}${menu.entry.Id}`);
            const [settings, error] = getSettingsAndError(menu.url);
            const entry = {
                Scope: menu.scope,
                NW2: menu.entry.NW2 === "TRUE",
                Level: level,
                Settings: settings,
                Error: error,
            };
            for (let current = menu; current; current = current.parent) {
                entry[current.entry.Category] = current.entry.Id;
            }
            entries.set(menu.url, entry);
        }
    })
    return JSON.stringify(Object.fromEntries(entries.entries()));
})();
""")


if __name__ == "__main__":
    args = parse_args()
    password = getpass.getpass("Password: ") if args.password is None else args.password
    if args.out_file is not None:
        with open(args.out_file, "wb") as out_file:
            out_file.truncate()
    prepare_urllib(args)
    prepare_jsrun()
    login(password)
    try:
        eval_script("wcd://Integrated_main.js")
        result = collect_settings_json()
    finally:
        logout()
    if args.out_file is None:
        print(result)
    else:
        with open(args.out_file, "w", encoding="utf8") as out_file:
            out_file.write(result)
