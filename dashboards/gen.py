#!/usr/bin/env python3
"""Generates the Claude Code Grafana dashboards into ../grafana/dashboards/.

    python3 dashboards/gen.py

Data sources (provisioned uids): loki, prometheus, tempo.
Claude Code events live in Loki (service_name="claude-code"); every event field
(session_id, prompt_id, model, input_tokens, ...) is structured metadata, so it is
filtered with `| field="x"` and aggregated with `| unwrap field`.
"""
import json
import pathlib
import urllib.parse

OUT = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
LOKI = {"type": "loki", "uid": "loki"}
PROM = {"type": "prometheus", "uid": "prometheus"}

SEL = '{service_name="claude-code", host_name=~"$host"}'
SESSION_DASH = "claude-session"
TOKEN_FIELDS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"]


# ----------------------------------------------------------------- queries
def ev(event, sel=SEL, extra=""):
    return f'{sel} | event_name="{event}"{extra}'


def un(event, field, win, agg="sum", by="", sel=SEL, extra=""):
    """agg(by)(sum_over_time(<events> | unwrap field [win]))"""
    by = f" by ({by})" if by else ""
    return f"{agg}{by}(sum_over_time({ev(event, sel, extra)} | unwrap {field} [{win}]))"


def cnt(event, win, by="", sel=SEL, extra=""):
    by = f" by ({by})" if by else ""
    return f"sum{by}(count_over_time({ev(event, sel, extra)} [{win}]))"


def all_tokens(win, by="", sel=SEL):
    return " + ".join(un("api_request", f, win, by=by, sel=sel) for f in TOKEN_FIELDS)


def cache_hit(win, sel=SEL):
    rd = un("api_request", "cache_read_tokens", win, sel=sel)
    return (f"{rd} / ({rd} + {un('api_request', 'cache_creation_tokens', win, sel=sel)}"
            f" + {un('api_request', 'input_tokens', win, sel=sel)})")


def prom_total(metric, sel, win):
    """Growth of per-session counters over `win`. increase() loses the first value of every new
    series (each session is one), so use max_over_time minus the value at the window start."""
    m = metric + sel
    return (f"sum(max_over_time({m}[{win}])) - "
            f"(sum({m} offset {win} and ignoring(__name__) max_over_time({m}[{win}])) or vector(0))")


def loki(expr, legend="", ref="A", instant=False, max_lines=None):
    t = {"datasource": LOKI, "expr": expr, "legendFormat": legend, "refId": ref,
         "queryType": "instant" if instant else "range", "editorMode": "code"}
    if max_lines:
        t["maxLines"] = max_lines
    return t


def prom(expr, legend="", ref="A", instant=False):
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref,
            "instant": instant, "range": not instant, "editorMode": "code"}


# ------------------------------------------------------------------ layout
class Dash:
    def __init__(self, uid, title, variables, time_from="now-7d", tags=None, time_to="now"):
        self.uid, self.title, self.vars, self.time_from, self.time_to = uid, title, variables, time_from, time_to
        self.tags = tags or []
        self.panels, self._id, self.y, self.x, self.rowh = [], 0, 0, 0, 0

    def _next(self):
        self._id += 1
        return self._id

    def row(self, title):
        self._wrap()
        self.panels.append({"type": "row", "id": self._next(), "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1

    def _wrap(self):
        if self.x:
            self.y += self.rowh
            self.x, self.rowh = 0, 0

    def add(self, panel, w, h):
        if self.x + w > 24:
            self._wrap()
        panel["id"] = self._next()
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.panels.append(panel)
        self.x += w
        self.rowh = max(self.rowh, h)

    def build(self):
        self._wrap()
        return {
            "uid": self.uid, "title": self.title, "tags": self.tags, "schemaVersion": 39,
            "version": 1, "editable": True, "graphTooltip": 1, "refresh": "1m",
            "timezone": "browser", "time": {"from": self.time_from, "to": self.time_to},
            "templating": {"list": self.vars}, "panels": self.panels,
            "links": [], "annotations": {"list": []},
        }


def host_var():
    return {"name": "host", "label": "Machine", "type": "query", "datasource": LOKI,
            "query": {"label": "host_name", "stream": '{service_name="claude-code"}', "type": 1,
                      "refId": "host"},
            "refresh": 2, "includeAll": True, "multi": True, "allValue": ".+",
            "current": {"text": "All", "value": "$__all"}, "sort": 1}


# Loki instant queries arrive as one frame per series; turn the labels into columns and stack the
# frames so a pie / bar gauge gets one row (slice, bar) per label value.
ROWS = [{"id": "labelsToFields", "options": {"mode": "columns"}}, {"id": "merge", "options": {}}]

# ------------------------------------------------------------------ panels
def _base(kind, title, targets, desc="", ds=LOKI):
    return {"type": kind, "title": title, "description": desc, "datasource": ds,
            "targets": targets, "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}


def stat(title, expr, unit="short", decimals=None, color="blue", desc="", prom_q=False, win=None):
    t = prom(expr, instant=True) if prom_q else loki(expr, instant=True)
    p = _base("stat", title, [t], desc, PROM if prom_q else LOKI)
    d = p["fieldConfig"]["defaults"]
    d.update({"unit": unit, "noValue": "0", "color": {"mode": "fixed", "fixedColor": color}})
    if decimals is not None:
        d["decimals"] = decimals
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto"}
    return p


def timeseries(title, targets, unit="short", bars=False, stack=False, interval=None, desc="",
               legend=("sum",), legend_mode="table", ds=LOKI, transformations=None, fill=None,
               overrides=None, max_val=None, min_val=None):
    p = _base("timeseries", title, targets, desc, ds)
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 1, "showPoints": "never",
              "fillOpacity": fill if fill is not None else (80 if bars else 15),
              "stacking": {"mode": "normal" if stack else "none", "group": "A"},
              "barAlignment": 0, "spanNulls": True}
    p["fieldConfig"]["defaults"] = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if max_val is not None:
        p["fieldConfig"]["defaults"]["max"] = max_val
    if min_val is not None:
        p["fieldConfig"]["defaults"]["min"] = min_val
    p["fieldConfig"]["overrides"] = overrides or []
    p["options"] = {"legend": {"displayMode": legend_mode, "placement": "bottom", "calcs": list(legend)},
                    "tooltip": {"mode": "multi", "sort": "desc"}}
    if interval:
        p["interval"] = interval
        if interval == "1d":
            custom["barAlignment"] = -1   # a daily bucket is plotted at its end; draw it over its day
    if transformations:
        p["transformations"] = transformations
    return p


def pie(title, targets, unit="short", desc="", label=None):
    p = _base("piechart", title, targets, desc)
    p["fieldConfig"]["defaults"] = {"unit": unit, "color": {"mode": "palette-classic"}}
    p["options"] = {"pieType": "donut", "displayLabels": [],
                    "legend": {"displayMode": "table", "placement": "bottom", "values": ["value", "percent"]},
                    "reduceOptions": {"calcs": [], "fields": "/^Value/", "values": True},
                    "tooltip": {"mode": "single"}}
    p["transformations"] = ROWS
    return p


def bargauge(title, targets, unit="short", desc="", label=None):
    p = _base("bargauge", title, targets, desc)
    p["fieldConfig"]["defaults"] = {"unit": unit, "color": {"mode": "palette-classic"}, "min": 0}
    p["options"] = {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                    "reduceOptions": {"calcs": [], "fields": "/^Value/", "values": True},
                    "namePlacement": "left", "valueMode": "color"}
    p["transformations"] = ROWS + [{"id": "sortBy", "options": {"fields": {}, "sort": [{"field": "Value", "desc": True}]}}]
    return p


def table(title, targets, transformations, overrides=None, desc="", ds=LOKI, sort=None, footer=False):
    p = _base("table", title, targets, desc, ds)
    p["transformations"] = transformations
    p["fieldConfig"]["overrides"] = overrides or []
    p["fieldConfig"]["defaults"] = {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True}}
    p["options"] = {"showHeader": True, "footer": {"show": footer, "reducer": ["sum"], "fields": ""}}
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort, "desc": True}]
    return p


def logs(title, expr, desc="", wrap=True, asc=False, max_lines=1000):
    p = _base("logs", title, [loki(expr, max_lines=max_lines)], desc)
    p["options"] = {"showTime": True, "wrapLogMessage": wrap, "enableLogDetails": True,
                    "sortOrder": "Ascending" if asc else "Descending", "prettifyLogMessage": False,
                    "dedupStrategy": "none", "showLabels": False, "showCommonLabels": False}
    return p


def text(title, md):
    p = _base("text", title, [], "")
    p.pop("datasource")
    p["options"] = {"mode": "markdown", "content": md}
    return p


def ov(field, **props):
    """Field override by name. props: unit=..., displayName=..., width=..., decimals=..."""
    mapping = {"unit": "unit", "displayName": "displayName", "width": "custom.width",
               "decimals": "decimals", "links": "links", "cell": "custom.cellOptions"}
    return {"matcher": {"id": "byName", "options": field},
            "properties": [{"id": mapping[k], "value": v} for k, v in props.items()]}


# --------------------------------------------------- log-frame transformations
def log_fields(numeric, strings=(), rename=None, extra=(), order=None):
    """Turn a Loki *logs* frame (labels JSON + Time) into typed columns."""
    keep = ["Time"] + list(strings) + list(numeric)
    t = [
        {"id": "extractFields", "options": {"source": "labels", "format": "json", "replace": False}},
        {"id": "convertFieldType", "options": {"conversions": [
            {"targetField": f, "destinationType": "number"} for f in numeric]}},
        {"id": "filterFieldsByName", "options": {"include": {"names": keep}}},
        {"id": "sortBy", "options": {"fields": {}, "sort": [{"field": "Time"}]}},
    ]
    t.extend(extra)
    if rename or order:
        opts = {"renameByName": rename or {}}
        if order:
            opts["indexByName"] = {n: i for i, n in enumerate(order)}
        t.append({"id": "organize", "options": opts})
    return t


def trace_link(range_from="now-7d"):
    pre = '{"a":{"datasource":"tempo","queries":[{"refId":"A","datasource":{"type":"tempo","uid":"tempo"},' \
          '"queryType":"traceql","query":"'
    post = '"}],"range":{"from":"' + range_from + '","to":"now"}}}'
    url = ("/explore?schemaVersion=1&orgId=1&panes=" + urllib.parse.quote(pre) + "${__value.raw}"
           + urllib.parse.quote(post))
    return [{"title": "Open trace in Tempo", "url": url, "targetBlank": True}]


# ================================================================== OVERVIEW
def overview():
    # Loki floors the end of a 1d-step query to UTC midnight, which would hide today's bucket;
    # ending the range at now+1d includes it. The two fixed-window tiles are shifted back to compensate.
    d = Dash("claude-overview", "Claude Code - Overview", [host_var()], "now-6d", ["claude-code"], time_to="now+1d")
    R = "$__range"

    # --- stats
    d.add(stat("Sessions (24h)", f'count({cnt("user_prompt", "24h", by="session_id")})', color="blue",
               desc="Distinct sessions with at least one prompt in the last 24h (ignores the time picker)."), 4, 4)
    d.add(stat("Sessions (7d)", f'count({cnt("user_prompt", "7d", by="session_id")})', color="blue",
               desc="Distinct sessions with at least one prompt in the last 7 days (ignores the time picker)."), 4, 4)
    d.add(stat("User Prompts", cnt("user_prompt", R), color="purple"), 4, 4)
    d.add(stat("API Requests", cnt("api_request", R), color="green"), 4, 4)
    d.add(stat("API Errors", cnt("api_error", R), color="red"), 4, 4)
    d.add(stat("Active Time", prom_total("claude_code_active_time_seconds_total", '{host_name=~"$host"}', R),
               unit="s", color="orange", prom_q=True), 4, 4)
    d.add(stat("Total Cost", un("api_request", "cost_usd", R), unit="currencyUSD", decimals=2, color="red"), 4, 4)
    d.add(stat("Total Tokens", all_tokens(R), color="blue", desc="input + output + cache read + cache write"), 4, 4)
    d.add(stat("Cache Hit Rate", cache_hit(R), unit="percentunit", decimals=1, color="green",
               desc="cache_read / (cache_read + cache_write + uncached input)"), 4, 4)
    d.add(stat("p95 Time to First Token",
               f'max(quantile_over_time(0.95, {ev("api_request")} | unwrap ttft_ms [{R}]))', unit="ms", color="orange"), 4, 4)
    d.add(stat("Tool Calls", cnt("tool_result", R), color="purple"), 4, 4)
    d.add(stat("Lines Changed", prom_total("claude_code_lines_of_code_count_total", '{host_name=~"$host"}', R),
               color="blue", prom_q=True), 4, 4)

    for pn in d.panels:
        if pn.get("title") in ("Sessions (24h)", "Sessions (7d)"):
            pn["timeShift"], pn["hideTimeOverride"] = "1d", True

    # --- cost & token trends
    d.row("Cost & Token Trends")
    d.add(timeseries("Daily Cost by Model",
                     [loki(un("api_request", "cost_usd", "$__interval", by="model"), "{{model}}")],
                     unit="currencyUSD", bars=True, stack=True, interval="1d"), 12, 9)
    d.add(timeseries("Daily Token Usage by Type",
                     [loki(un("api_request", f, "$__interval"), label, ref)
                      for f, label, ref in zip(TOKEN_FIELDS, ["input (uncached)", "output", "cache read", "cache write"], "ABCD")],
                     bars=True, stack=True, interval="1d"), 12, 9)

    # --- model breakdown
    d.row("Model Usage Breakdown")
    d.add(pie("Token Usage by Model", [loki(all_tokens(R, by="model"), "{{model}}", instant=True)], label="model"), 8, 8)
    d.add(pie("Cost by Model", [loki(un("api_request", "cost_usd", R, by="model"), "{{model}}", instant=True)],
              unit="currencyUSD", label="model"), 8, 8)
    d.add(pie("API Requests by Model", [loki(cnt("api_request", R, by="model"), "{{model}}", instant=True)], label="model"), 8, 8)
    d.add(timeseries("p95 Time to First Token by Model",
                     [loki(f'quantile_over_time(0.95, {ev("api_request")} | unwrap ttft_ms [$__interval]) by (model)', "{{model}}")],
                     unit="ms", legend=("mean", "max")), 8, 8)
    d.add(pie("Requests by Effort", [loki(cnt("api_request", R, by="effort"), "{{effort}}", instant=True)], label="effort"), 8, 8)
    d.add(pie("Requests by Source", [loki(cnt("api_request", R, by="query_source"), "{{query_source}}", instant=True)],
              desc="query_source: main thread vs subagents / SDK / background queries", label="query_source"), 8, 8)

    # --- tools
    d.row("Tool Usage Analytics")
    d.add(bargauge("Top Tools", [loki(f'topk(15, {cnt("tool_result", R, by="tool_name")})', "{{tool_name}}", instant=True)], label="tool_name"), 12, 9)
    d.add(timeseries("Daily Tool Usage",
                     [loki(cnt("tool_result", "$__interval", by="tool_name"), "{{tool_name}}")],
                     bars=True, stack=True, interval="1d", legend=("sum",)), 12, 9)
    d.add(bargauge("Failed Tool Calls", [loki(cnt("tool_result", R, by="tool_name", extra=' | success="false"'),
                                              "{{tool_name}}", instant=True)], label="tool_name"), 8, 8)
    d.add(bargauge("Avg Tool Duration",
                   [loki(f'avg by (tool_name)(avg_over_time({ev("tool_result")} | unwrap duration_ms [{R}]))',
                         "{{tool_name}}", instant=True)], unit="ms", label="tool_name"), 8, 8)
    d.add(bargauge("Top Bash Commands",
                   [loki(f'topk(12, sum by (bash_command)(count_over_time({ev("tool_result", extra=" | tool_name=\"Bash\"")}'
                         f' | line_format "{{{{.tool_parameters}}}}" | json bash_command [{R}])))',
                         "{{bash_command}}", instant=True)],
                   desc="First word of Bash commands (tool_parameters.bash_command)", label="bash_command"), 8, 8)
    d.add(pie("Permission Decisions", [loki(cnt("tool_decision", R, by="decision"), "{{decision}}", instant=True)], label="decision"), 8, 8)
    d.add(pie("Tool Source", [loki(cnt("tool_decision", R, by="tool_source"), "{{tool_source}}", instant=True)],
              desc="builtin vs MCP / skill / plugin tools", label="tool_source"), 8, 8)
    d.add(timeseries("Lines of Code Changed per Day",
                     [prom(prom_total("claude_code_lines_of_code_count_total", '{host_name=~"$host",type="added"}', "$__interval"), "added", "A"),
                      prom(prom_total("claude_code_lines_of_code_count_total", '{host_name=~"$host",type="removed"}', "$__interval"), "removed", "B")],
                     bars=True, stack=True, interval="1d", ds=PROM), 8, 8)

    # --- errors
    d.row("Errors")
    d.add(timeseries("API Errors over Time", [loki(cnt("api_error", "$__interval"), "errors")],
                     bars=True, legend=("sum",)), 8, 8)
    d.add(logs("Recent API Errors", ev("api_error") + ' | line_format "{{.model}} {{.status_code}} {{.error}}"',
               max_lines=200), 16, 8)

    # --- sessions table
    d.row("Sessions")
    by = "host_name, session_id"
    qs = [
        ("A", "prompts", f'sum by ({by})(count_over_time({ev("user_prompt")} [{R}]))'),
        ("B", "api_calls", f'sum by ({by})(count_over_time({ev("api_request")} [{R}]))'),
        ("C", "tool_calls", f'sum by ({by})(count_over_time({ev("tool_result")} [{R}]))'),
        ("D", "cost", f'sum by ({by})(sum_over_time({ev("api_request")} | unwrap cost_usd [{R}]))'),
        ("E", "output", f'sum by ({by})(sum_over_time({ev("api_request")} | unwrap output_tokens [{R}]))'),
        ("F", "last", f'max by ({by})(max_over_time({SEL} | unwrap observed_timestamp [{R}])) / 1e6'),
        ("G", "first", f'min by ({by})(min_over_time({SEL} | unwrap observed_timestamp [{R}])) / 1e6'),
    ]
    cols = {"Value #A": "Prompts", "Value #B": "API calls", "Value #C": "Tool calls", "Value #D": "Cost",
            "Value #E": "Output tokens", "Value #F": "Last activity", "Value #G": "Started",
            "host_name": "Machine", "session_id": "Session"}
    session_link = [{"title": "Open session", "targetBlank": False,
                     "url": f"/d/{SESSION_DASH}?var-host=${{__data.fields.Machine}}&var-session=${{__value.raw}}"
                            "&${__url_time_range}"}]
    d.add(table(
        "Sessions (click a session id to drill in)",
        [loki(e, legend=lg, ref=r, instant=True) for r, lg, e in qs],
        [{"id": "labelsToFields", "options": {"mode": "columns"}},
         {"id": "merge", "options": {}},
         {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": cols,
                                        "indexByName": {"Session": 0, "Machine": 1, "Started": 2, "Last activity": 3,
                                                        "Prompts": 4, "API calls": 5, "Tool calls": 6,
                                                        "Output tokens": 7, "Cost": 8}}},
         {"id": "sortBy", "options": {"fields": {}, "sort": [{"field": "Last activity", "desc": True}]}}],
        overrides=[ov("Session", links=session_link, width=330), ov("Cost", unit="currencyUSD", decimals=2),
                   ov("Started", unit="dateTimeAsIso"), ov("Last activity", unit="dateTimeAsIso"),
                   ov("Output tokens", unit="short")]), 24, 12)
    return d.build()


# ================================================================== SESSION
def session():
    sv = {"name": "session", "label": "Session", "type": "query", "datasource": PROM,
          "query": {"query": 'label_values(claude_code_cost_usage_USD_total{host_name=~"$host"}, session_id)',
                    "refId": "session"},
          "definition": 'label_values(claude_code_cost_usage_USD_total{host_name=~"$host"}, session_id)',
          "refresh": 2, "sort": 0, "includeAll": False, "multi": False}
    qv = {"name": "qsrc", "label": "Query source (regex)", "type": "textbox", "query": ".*",
          "current": {"text": ".*", "value": ".*"}, "options": []}
    hv = host_var()
    hv["multi"] = False
    hv["includeAll"] = False
    d = Dash(SESSION_DASH, "Claude Code - Session", [hv, sv, qv], "now-24h", ["claude-code"])
    S = SEL + ' | session_id="$session"'
    R = "$__range"
    SELS = "{service_name=\"claude-code\", host_name=~\"$host\"} | session_id=\"$session\""

    def sev(event, extra=""):
        return f'{SELS} | event_name="{event}"{extra}'

    def sun(event, field, win=R):
        return f'sum(sum_over_time({sev(event)} | unwrap {field} [{win}]))'

    def scnt(event, win=R):
        return f'sum(count_over_time({sev(event)} [{win}]))'

    rd = sun("api_request", "cache_read_tokens")
    hit = (f'{rd} / ({rd} + {sun("api_request", "cache_creation_tokens")} + {sun("api_request", "input_tokens")})')

    d.add(text("Resume", "```\nclaude --resume $session\n```\nRun this on **$host** to continue the session."), 6, 4)
    d.add(stat("Messages", scnt("user_prompt"), color="purple"), 3, 4)
    d.add(stat("API Calls", scnt("api_request"), color="green"), 3, 4)
    d.add(stat("Tool Calls", scnt("tool_result"), color="blue"), 3, 4)
    d.add(stat("Errors", scnt("api_error"), color="red"), 3, 4)
    d.add(stat("Cost", sun("api_request", "cost_usd"), unit="currencyUSD", decimals=3, color="red"), 3, 4)
    d.add(stat("Duration", f'(max(max_over_time({SELS} | unwrap observed_timestamp [{R}]))'
                           f' - min(min_over_time({SELS} | unwrap observed_timestamp [{R}]))) / 1e9',
               unit="s", color="orange", desc="first to last event of the session"), 3, 4)
    d.add(stat("Output Tokens", sun("api_request", "output_tokens"), color="blue"), 4, 4)
    d.add(stat("Uncached Input", sun("api_request", "input_tokens"), color="blue"), 4, 4)
    d.add(stat("Cache Read", sun("api_request", "cache_read_tokens"), color="green"), 4, 4)
    d.add(stat("Cache Write", sun("api_request", "cache_creation_tokens"), color="orange"), 4, 4)
    d.add(stat("Cache Hit Rate", hit, unit="percentunit", decimals=1, color="green"), 4, 4)
    d.add(stat("Tool Failures", f'sum(count_over_time({sev("tool_result", " | success=\"false\"")} [{R}]))',
               color="red"), 4, 4)

    # --- per API call series (from raw events, no bucketing)
    api = loki(f'{sev("api_request")} | query_source=~"$qsrc"', max_lines=5000)
    ren = {"input_tokens": "input (uncached)", "cache_read_tokens": "cache read",
           "cache_creation_tokens": "cache write", "output_tokens": "output"}
    d.row("Context & Tokens (one point per API call)")
    d.add(timeseries(
        "Context Size", [api], bars=False, stack=True, fill=40, legend=("max", "last"),
        desc="Prompt size per API call = uncached input + cache read + cache write. A sharp drop is a /compact. "
             "Use the Query source filter to isolate the main thread from subagents.",
        transformations=log_fields(["input_tokens", "cache_read_tokens", "cache_creation_tokens"], rename=ren)), 12, 9)
    d.add(timeseries(
        "Tokens by Type per Call", [api], bars=True, stack=True, legend=("sum",),
        transformations=log_fields(TOKEN_FIELDS, rename=ren)), 12, 9)
    d.add(timeseries(
        "Cache Hit Rate per Call", [api], unit="percentunit", legend=("mean", "min"), max_val=1, min_val=0,
        transformations=log_fields(
            ["input_tokens", "cache_read_tokens", "cache_creation_tokens"],
            extra=[{"id": "calculateField", "options": {"mode": "reduceRow", "alias": "context",
                                                        "reduce": {"reducer": "sum"}, "replaceFields": False}},
                   {"id": "calculateField", "options": {"mode": "binary", "alias": "cache hit rate",
                                                        "binary": {"left": "cache_read_tokens", "operator": "/",
                                                                   "right": "context"}, "replaceFields": False}},
                   {"id": "filterFieldsByName", "options": {"include": {"names": ["Time", "cache hit rate"]}}}])), 8, 8)
    d.add(timeseries(
        "Latency per Call", [api], unit="ms", legend=("mean", "max"),
        transformations=log_fields(["ttft_ms", "duration_ms"], rename={"ttft_ms": "time to first token", "duration_ms": "total duration"})), 8, 8)
    d.add(timeseries(
        "Cost (per call & cumulative)", [api], unit="currencyUSD", legend=("sum", "last"),
        overrides=[{"matcher": {"id": "byName", "options": "cumulative"},
                    "properties": [{"id": "custom.axisPlacement", "value": "right"},
                                   {"id": "custom.drawStyle", "value": "line"}]},
                   {"matcher": {"id": "byName", "options": "per call"},
                    "properties": [{"id": "custom.drawStyle", "value": "bars"}]}],
        transformations=log_fields(
            ["cost_usd"],
            extra=[{"id": "calculateField", "options": {"mode": "cumulativeFunctions", "alias": "cumulative",
                                                        "cumulative": {"field": "cost_usd", "reducer": "sum"},
                                                        "replaceFields": False}}],
            rename={"cost_usd": "per call"})), 8, 8)

    # --- per message
    d.row("Messages")
    by = "prompt_id"
    mq = [
        ("B", "calls", f'sum by ({by})(count_over_time({sev("api_request")} [{R}]))'),
        ("C", "tools", f'sum by ({by})(count_over_time({sev("tool_result")} [{R}]))'),
        ("D", "cost", f'sum by ({by})(sum_over_time({sev("api_request")} | unwrap cost_usd [{R}]))'),
        ("E", "output", f'sum by ({by})(sum_over_time({sev("api_request")} | unwrap output_tokens [{R}]))'),
        ("F", "cache_read", f'sum by ({by})(sum_over_time({sev("api_request")} | unwrap cache_read_tokens [{R}]))'),
        ("G", "cache_write", f'sum by ({by})(sum_over_time({sev("api_request")} | unwrap cache_creation_tokens [{R}]))'),
        ("H", "input", f'sum by ({by})(sum_over_time({sev("api_request")} | unwrap input_tokens [{R}]))'),
    ]
    prompts = loki(sev("user_prompt"), ref="A", max_lines=2000)
    d.add(table(
        "Per-message Breakdown (one row per prompt)",
        [prompts] + [loki(e, legend=lg, ref=r, instant=True) for r, lg, e in mq],
        [{"id": "extractFields", "options": {"source": "labels", "format": "json", "replace": False}},
         {"id": "labelsToFields", "options": {"mode": "columns"}},
         {"id": "joinByField", "options": {"byField": "prompt_id", "mode": "outer"}},
         {"id": "filterFieldsByName", "options": {"include": {"pattern":
             "^(Time.*|prompt|prompt_id|trace_id|Value.*)$"}}},
         {"id": "organize", "options": {
             "excludeByName": {f"Time {i}": True for i in range(2, 9)},
             "indexByName": {n: i for i, n in enumerate(["Sent", "Prompt", "API calls", "Tools", "Cost", "Output",
                                                         "Input", "Cache read", "Cache write", "Trace", "Prompt id"])},
             "renameByName": {"Time 1": "Sent", "Time": "Sent", "prompt": "Prompt", "prompt_id": "Prompt id", "trace_id": "Trace",
                              "Value #B": "API calls", "Value #C": "Tools", "Value #D": "Cost",
                              "Value #E": "Output", "Value #F": "Cache read", "Value #G": "Cache write",
                              "Value #H": "Input"}}},
         {"id": "organize", "options": {"indexByName": {n: i for i, n in enumerate(
             ["Sent", "Prompt", "API calls", "Tools", "Cost", "Output", "Input", "Cache read", "Cache write",
              "Trace", "Prompt id"])}}},
         {"id": "sortBy", "options": {"fields": {}, "sort": [{"field": "Sent"}]}}],
        overrides=[ov("Sent", width=170), ov("Prompt", width=480), ov("Cost", unit="currencyUSD", decimals=3),
                   ov("Trace", links=trace_link("now-30d"), width=110)]), 24, 12)

    d.row("Conversation & Tools")
    d.add(logs("Conversation (prompts & replies)",
               f'{SELS} | event_name=~"user_prompt|assistant_response"'
               ' | line_format "{{ if eq .event_name \\"user_prompt\\" }}YOU: {{ .prompt }}{{ else }}CLAUDE: {{ .response }}{{ end }}"',
               asc=True, max_lines=2000), 12, 14)
    d.add(table(
        "Tool Invocations",
        [loki(sev("tool_result"), max_lines=5000)],
        log_fields(["duration_ms", "tool_result_size_bytes", "tool_input_size_bytes"],
                   strings=["tool_name", "success", "tool_input"],
                   rename={"Time": "Time", "tool_name": "Tool", "success": "OK", "duration_ms": "Duration (ms)",
                           "tool_input_size_bytes": "Input bytes", "tool_result_size_bytes": "Result bytes",
                           "tool_input": "Input"},
                   order=["Time", "tool_name", "success", "duration_ms", "tool_input_size_bytes",
                          "tool_result_size_bytes", "tool_input"]),
        overrides=[ov("Input", width=300)]), 12, 14)
    d.add(table(
        "API Calls",
        [api],
        log_fields(["input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd", "ttft_ms", "duration_ms"],
                   strings=["model", "query_source", "effort"],
                   rename={"model": "Model", "query_source": "Source", "effort": "Effort", "input_tokens": "Input",
                           "output_tokens": "Output", "cache_read_tokens": "Cache read",
                           "cache_creation_tokens": "Cache write", "cost_usd": "Cost", "ttft_ms": "TTFT (ms)",
                           "duration_ms": "Duration (ms)"},
                   order=["Time", "model", "query_source", "effort", "input_tokens", "output_tokens",
                          "cache_read_tokens", "cache_creation_tokens", "cost_usd", "ttft_ms", "duration_ms"]),
        overrides=[ov("Cost", unit="currencyUSD", decimals=4)]), 24, 10)
    d.add(logs("Full Event Timeline",
               f'{SELS} | event_name!~"api_(request|response)_body" | line_format "{{{{.event_name}}}} {{{{.tool_name}}}} {{{{.model}}}} {{{{.prompt}}}}{{{{.response}}}}"',
               asc=True, wrap=False, max_lines=3000), 24, 10)
    return d.build()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, dash in (("claude-overview", overview()), ("claude-session", session())):
        (OUT / f"{name}.json").write_text(json.dumps(dash, indent=1))
        print("wrote", OUT / f"{name}.json")
